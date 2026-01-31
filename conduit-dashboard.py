#!/usr/bin/env python3
"""
Anti-Censorship VPS Live Dashboard
Real-time stats for Conduit, Snowflake, and Tor Bridge services.
Run this script and open http://localhost:5050 in your browser.
"""

import subprocess
import re
import json
import threading
import time
import base64
from datetime import datetime, timedelta
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import HTTPServer, BaseHTTPRequestHandler

SCRIPT_DIR = Path(__file__).parent
CONFIG_FILE = SCRIPT_DIR / "conduit-vps.conf"
HISTORY_FILE = SCRIPT_DIR / "conduit-history.json"
PORT = 5050
REFRESH_INTERVAL = 15  # seconds
SSH_TIMEOUT = 15 # seconds
SSH_CONNECT_TIMEOUT = 10
SSH_Server_Alive_Interval = 30
HISTORY_DAYS = 2  # Keep 2 days of history

# Service names we track
SERVICES = ["conduit", "conduit2", "snowflake", "tor-bridge"]

# Global stats storage
current_stats = {"vps": [], "timestamp": "", "conduits": []}
stats_lock = threading.Lock()

# Docker execution strategy cache per VPS:
# "" (plain docker) or "sudo -n " or "echo 'pass' | sudo -S -p '' "
docker_prefix_cache = {}
docker_prefix_lock = threading.Lock()

# VPS static hardware cache per VPS (cores, total RAM in MB)
vps_hw_cache = {}
vps_hw_lock = threading.Lock()

def load_history():
    """Load connection history from JSON file."""
    if HISTORY_FILE.exists():
        try:
            with open(HISTORY_FILE, "r") as f:
                return json.load(f)
        except:
            pass
    return {"data": [], "vps_names": []}


def save_history(history):
    """Save connection history to JSON file."""
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2)


def cleanup_old_history(history):
    """Remove data points older than HISTORY_DAYS."""
    cutoff = datetime.now() - timedelta(days=HISTORY_DAYS)
    cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M:%S")
    history["data"] = [d for d in history["data"] if d["time"] >= cutoff_str]
    return history


def parse_config():
    """Parse VPS config file."""
    vps_list = []
    with open(CONFIG_FILE, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("|")
            if len(parts) >= 5:
                vps_list.append({
                    "alias": parts[0].strip(),
                    "user": parts[1].strip(),
                    "ip": parts[2].strip(),
                    "port": parts[3].strip() or "22",
                    "password": parts[4].strip(),
                    "comment": parts[5].strip() if len(parts) > 5 else "",
                })
    return vps_list


def mask_ip(ip):
    """Mask IP address for public display (e.g., 82.165.40.61 -> 82.165.***.***)"""
    parts = ip.split('.')
    if len(parts) == 4:
        return f"{parts[0]}.{parts[1]}.***.***"
    return "***.***.***.***"


def ssh_command(vps, cmd):
    """Execute SSH command on VPS."""
    ssh_opts = f"-o StrictHostKeyChecking=no -o ConnectTimeout={SSH_CONNECT_TIMEOUT} -o ServerAliveInterval={SSH_Server_Alive_Interval}"
    
    if vps["password"] and vps["password"] != "-":
        full_cmd = f"sshpass -p '{vps['password']}' ssh {ssh_opts} -p {vps['port']} {vps['user']}@{vps['ip']} \"{cmd}\""
    else:
        full_cmd = f"ssh {ssh_opts} -p {vps['port']} {vps['user']}@{vps['ip']} \"{cmd}\""

    if vps["ip"] in ("127.0.0.1", "LOCAL", "Local", "local"):
        full_cmd = cmd
    
    try:
        result = subprocess.run(full_cmd, shell=True, capture_output=True, text=True, timeout=SSH_TIMEOUT)
        return result.stdout.strip()
    except:
        return None

def _sh_single_quote(s: str) -> str:
    """Safely single-quote a string for /bin/sh."""
    return "'" + s.replace("'", "'\"'\"'") + "'"


def get_docker_prefix(vps):
    """
    Decide how to run docker on this VPS:
    1) docker ...
    2) sudo -n docker ...
    3) echo <pass> | sudo -S -p '' docker ...
    Cached per VPS so we probe only once.
    """
    key = f"{vps.get('user')}@{vps.get('ip')}:{vps.get('port')}"
    with docker_prefix_lock:
        if key in docker_prefix_cache:
            return docker_prefix_cache[key]

    # 1) plain docker
    probe = ssh_command(vps, "docker info >/dev/null 2>&1 && echo OK || echo FAIL")
    if probe and probe.strip() == "OK":
        prefix = ""
    else:
        # 2) passwordless sudo (non-interactive)
        probe2 = ssh_command(vps, "sudo -n docker info >/dev/null 2>&1 && echo OK || echo FAIL")
        if probe2 and probe2.strip() == "OK":
            prefix = "sudo -n "
        else:
            # 3) sudo with password over stdin (only if we have a password in config)
            prefix = ""
            if vps.get("password") and vps["password"] != "-":
                pw = _sh_single_quote(vps["password"])
                probe3 = ssh_command(
                    vps,
                    f"echo {pw} | sudo -S -p '' docker info >/dev/null 2>&1 && echo OK || echo FAIL"
                )
                if probe3 and probe3.strip() == "OK":
                    prefix = f"echo {pw} | sudo -S -p '' "

    with docker_prefix_lock:
        docker_prefix_cache[key] = prefix
    return prefix


def docker_command(vps, docker_args):
    """Run a docker command on VPS using the discovered strategy."""
    prefix = get_docker_prefix(vps)
    return ssh_command(vps, f"{prefix}docker {docker_args}")

def _vps_key(vps) -> str:
    """Stable cache key for a VPS."""
    return f"{vps.get('user')}@{vps.get('ip')}:{vps.get('port')}"

def get_vps_hardware(vps):
    """
    Get VPS static hardware info once (per VPS) and cache it:
      - cpu_cores (int)
      - mem_total_mb (float)
    """
    key = _vps_key(vps)
    with vps_hw_lock:
        if key in vps_hw_cache:
            return vps_hw_cache[key]["cpu_cores"], vps_hw_cache[key]["mem_total_mb"]

    # CPU cores
    cores_out = ssh_command(
        vps,
        "nproc 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null || grep -c '^processor' /proc/cpuinfo 2>/dev/null"
    )
    try:
        cpu_cores = int((cores_out or "").strip())
        if cpu_cores <= 0:
            cpu_cores = 1
    except:
        cpu_cores = 1

    # Total RAM (kB from /proc/meminfo)
    mem_kb_out = ssh_command(vps, "grep -i '^MemTotal:' /proc/meminfo 2>/dev/null | awk '{print $2}'")
    try:
        s = (mem_kb_out or "").strip()
        m = re.search(r'(\d+(?:\.\d+)?)', s)
        mem_total_kb = float(m.group(1)) if m else 0.0
        mem_total_mb = mem_total_kb / 1024.0
        if mem_total_mb <= 0:
            mem_total_mb = 0.0
    except:
        mem_total_mb = 0.0

    with vps_hw_lock:
        vps_hw_cache[key] = {"cpu_cores": cpu_cores, "mem_total_mb": mem_total_mb}

    return cpu_cores, mem_total_mb

def get_vps_stats(vps):
    """Collect all stats from a single VPS."""
    stats = {
        "alias": vps["alias"],
        "ip": vps["ip"],
        "comment": vps["comment"],
        "online": False,
        # Conduit 1 stats
        "conduit_running": False,
        "conduit_uptime": "N/A",
        "connections": 0,
        "connecting": 0,
        "conduit_up": "N/A",
        "conduit_down": "N/A",
        "conduit_up_gb": 0,
        "conduit_down_gb": 0,
        # Conduit 2 stats
        "conduit2_running": False,
        "conduit2_uptime": "N/A",
        "connections2": 0,
        "connecting2": 0,
        "conduit2_up": "N/A",
        "conduit2_down": "N/A",
        "conduit2_up_gb": 0,
        "conduit2_down_gb": 0,
        # Snowflake stats
        "snowflake_running": False,
        "snowflake_uptime": "N/A",
        "snowflake_clients": 0,
        # Tor Bridge stats
        "torbridge_running": False,
        "torbridge_uptime": "N/A",
        "torbridge_bootstrap": 0,
        # System stats
        "cpu_percent": 0,
        "memory_mb": 0,
        "memory_percent": 0,
        "uptime": "N/A",
    }
    
    # Check if online
    uptime = ssh_command(vps, "uptime -p 2>/dev/null || uptime | awk '{print $3,$4}'")
    if uptime is None:
        return stats
    
    stats["online"] = True
    stats["uptime"] = uptime.replace("up ", "")

    # Static hardware info (cached once per VPS)
    cpu_cores, mem_total_mb = get_vps_hardware(vps)
    # Optional: keep these in stats for debugging/visibility (UI can ignore them safely)
    stats["cpu_cores"] = cpu_cores
    stats["memory_total_mb"] = mem_total_mb

    # Check all container statuses in one command
    container_info = docker_command(
        vps,
        "ps -a --format '{{.Names}}|{{.Status}}' 2>/dev/null"
    )
    
    if container_info:
        for line in container_info.split('\n'):
            if '|' not in line:
                continue
            name, status = line.split('|', 1)
            is_up = status.startswith('Up')
            
            # Parse uptime from status like "Up 3 hours" or "Up 2 days"
            uptime_str = "N/A"
            if is_up:
                uptime_match = re.search(r'Up\s+(.+?)(?:\s+\(|$)', status)
                if uptime_match:
                    uptime_str = uptime_match.group(1).strip()
            
            if name == "conduit":
                stats["conduit_running"] = is_up
                stats["conduit_uptime"] = uptime_str
            elif name == "conduit2":
                stats["conduit2_running"] = is_up
                stats["conduit2_uptime"] = uptime_str
            elif name == "snowflake":
                stats["snowflake_running"] = is_up
                stats["snowflake_uptime"] = uptime_str
            elif name == "tor-bridge":
                stats["torbridge_running"] = is_up
                stats["torbridge_uptime"] = uptime_str
    
    # Get Conduit connection count from [STATS] log line
    if stats["conduit_running"]:
        stats_line = docker_command(vps, "logs conduit 2>&1 | grep '\\[STATS\\]' | tail -1")
        if stats_line:
            # Parse: [STATS] Connecting: 17 | Connected: 226 | Up: 7.1 GB | Down: 74.1 GB | Uptime: 3h47m8s
            
            connecting_match = re.search(r'Connecting:\s*(\d+)', stats_line)
            if connecting_match:
                stats["connecting"] = int(connecting_match.group(1))
            
            connected_match = re.search(r'Connected:\s*(\d+)', stats_line)
            if connected_match:
                stats["connections"] = int(connected_match.group(1))
            
            up_match = re.search(r'Up:\s*([\d.]+)\s*(GB|MB|KB)', stats_line)
            if up_match:
                val = float(up_match.group(1))
                unit = up_match.group(2)
                # Convert to GB for totals
                if unit == "KB":
                    stats["conduit_up_gb"] = val / 1024 / 1024
                    stats["conduit_up"] = f"{val:.1f} KB"
                elif unit == "MB":
                    stats["conduit_up_gb"] = val / 1024
                    stats["conduit_up"] = f"{val:.1f} MB"
                else:
                    stats["conduit_up_gb"] = val
                    stats["conduit_up"] = f"{val:.1f} GB"
            
            down_match = re.search(r'Down:\s*([\d.]+)\s*(GB|MB|KB)', stats_line)
            if down_match:
                val = float(down_match.group(1))
                unit = down_match.group(2)
                # Convert to GB for totals
                if unit == "KB":
                    stats["conduit_down_gb"] = val / 1024 / 1024
                    stats["conduit_down"] = f"{val:.1f} KB"
                elif unit == "MB":
                    stats["conduit_down_gb"] = val / 1024
                    stats["conduit_down"] = f"{val:.1f} MB"
                else:
                    stats["conduit_down_gb"] = val
                    stats["conduit_down"] = f"{val:.1f} GB"
    
    # Get Conduit2 connection count from [STATS] log line
    if stats["conduit2_running"]:
        stats_line2 = docker_command(vps, "logs conduit2 2>&1 | grep '\\[STATS\\]' | tail -1")
        if stats_line2:
            connecting_match2 = re.search(r'Connecting:\s*(\d+)', stats_line2)
            if connecting_match2:
                stats["connecting2"] = int(connecting_match2.group(1))
            
            connected_match2 = re.search(r'Connected:\s*(\d+)', stats_line2)
            if connected_match2:
                stats["connections2"] = int(connected_match2.group(1))
            
            up_match2 = re.search(r'Up:\s*([\d.]+)\s*(GB|MB|KB)', stats_line2)
            if up_match2:
                val = float(up_match2.group(1))
                unit = up_match2.group(2)
                if unit == "KB":
                    stats["conduit2_up_gb"] = val / 1024 / 1024
                    stats["conduit2_up"] = f"{val:.1f} KB"
                elif unit == "MB":
                    stats["conduit2_up_gb"] = val / 1024
                    stats["conduit2_up"] = f"{val:.1f} MB"
                else:
                    stats["conduit2_up_gb"] = val
                    stats["conduit2_up"] = f"{val:.1f} GB"
            
            down_match2 = re.search(r'Down:\s*([\d.]+)\s*(GB|MB|KB)', stats_line2)
            if down_match2:
                val = float(down_match2.group(1))
                unit = down_match2.group(2)
                if unit == "KB":
                    stats["conduit2_down_gb"] = val / 1024 / 1024
                    stats["conduit2_down"] = f"{val:.1f} KB"
                elif unit == "MB":
                    stats["conduit2_down_gb"] = val / 1024
                    stats["conduit2_down"] = f"{val:.1f} MB"
                else:
                    stats["conduit2_down_gb"] = val
                    stats["conduit2_down"] = f"{val:.1f} GB"
    
    # Get Snowflake client count from logs
    if stats["snowflake_running"]:
        snowflake_log = docker_command(
            vps,
            "logs snowflake 2>&1 | grep -c 'client connected' 2>/dev/null || echo 0"
        )
        if snowflake_log:
            try:
                stats["snowflake_clients"] = int(snowflake_log.strip())
            except:
                pass
    
    # Get Tor Bridge bootstrap status
    if stats["torbridge_running"]:
        tor_log = docker_command(vps, "logs tor-bridge 2>&1 | grep -i 'bootstrap' | tail -1")

        if tor_log:
            bootstrap_match = re.search(r'Bootstrapped (\d+)%', tor_log)
            if bootstrap_match:
                stats["torbridge_bootstrap"] = int(bootstrap_match.group(1))
    
    # Get docker stats for CPU/Memory
    docker_stats = docker_command(
        vps,
        "stats conduit --no-stream --format '{{.CPUPerc}}|{{.MemUsage}}' 2>/dev/null"
    )

    if docker_stats:
        parts = docker_stats.split("|")
        if len(parts) >= 2:
            # CPU: normalize Docker CPU% to 0-100 by dividing by VPS core count
            try:
                raw_cpu = float(parts[0].replace("%", "").strip())
                cores = stats.get("cpu_cores") or 1
                stats["cpu_percent"] = raw_cpu / float(cores) if cores else raw_cpu
                if stats["cpu_percent"] < 0:
                    stats["cpu_percent"] = 0
            except:
                pass

            # Memory: take container used memory, compute % of total VPS RAM
            # docker MemUsage usually looks like: "238MiB / 7.57GiB"
            mem_match = re.search(r'([\d.]+)\s*(KiB|MiB|GiB|KB|MB|GB)', parts[1])
            if mem_match:
                mem_val = float(mem_match.group(1))
                unit = mem_match.group(2)

                # Convert to MB
                if unit in ("KiB", "KB"):
                    used_mb = mem_val / 1024.0
                elif unit in ("GiB", "GB"):
                    used_mb = mem_val * 1024.0
                else:
                    used_mb = mem_val  # MiB or MB

                stats["memory_mb"] = round(used_mb, 1)

                total_mb = float(stats.get("memory_total_mb") or 0.0)
                if total_mb > 0:
                    pct = (used_mb / total_mb) * 100.0
                    if pct < 0:
                        pct = 0.0
                    if pct > 100:
                        pct = 100.0
                    stats["memory_percent"] = pct
    
    return stats


def collect_stats():
    """Collect stats from all VPS."""
    global current_stats
    vps_list = parse_config()
    all_stats = []
    
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(get_vps_stats, vps): vps for vps in vps_list}
        for future in as_completed(futures):
            all_stats.append(future.result())
    
    all_stats.sort(key=lambda x: x["alias"])
    
    now = datetime.now()
    timestamp = now.strftime("%H:%M:%S")
    full_timestamp = now.strftime("%Y-%m-%d %H:%M:%S")
    
    # Build conduits list (each conduit instance tracked separately)
    conduits_list = []
    for s in all_stats:
        if s["conduit_running"]:
            conduits_list.append({
                "name": f"{s['alias']}-c1",
                "vps": s["alias"],
                "instance": 1,
                "connections": s["connections"],
                "connecting": s["connecting"],
                "up_gb": s["conduit_up_gb"],
                "down_gb": s["conduit_down_gb"],
            })
        if s["conduit2_running"]:
            conduits_list.append({
                "name": f"{s['alias']}-c2",
                "vps": s["alias"],
                "instance": 2,
                "connections": s["connections2"],
                "connecting": s["connecting2"],
                "up_gb": s["conduit2_up_gb"],
                "down_gb": s["conduit2_down_gb"],
            })
    
    with stats_lock:
        current_stats["vps"] = all_stats
        current_stats["timestamp"] = timestamp
        current_stats["conduits"] = conduits_list
    
    # Update history file
    history = load_history()
    history = cleanup_old_history(history)
    
    # Add new data point - track all conduit instances separately
    connections_data = {}
    for s in all_stats:
        connections_data[f"{s['alias']}-c1"] = s["connections"]
        connections_data[f"{s['alias']}-c2"] = s["connections2"]
    
    history["data"].append({
        "time": full_timestamp,
        "connections": connections_data
    })
    # Track conduit names (34 instances: 17 VPS x 2 conduits)
    history["vps_names"] = [f"{s['alias']}-c1" for s in all_stats] + [f"{s['alias']}-c2" for s in all_stats]
    
    save_history(history)
    
    total_conn = sum(s["connections"] + s["connections2"] for s in all_stats)
    total_conduits = len(conduits_list)
    print(f"[{timestamp}] Stats updated: {len(all_stats)} VPS, {total_conduits} conduits, {total_conn} total connections")


def stats_collector_loop():
    """Background thread to collect stats periodically."""
    while True:
        try:
            collect_stats()
        except Exception as e:
            print(f"Error collecting stats: {e}")
        time.sleep(REFRESH_INTERVAL)


HTML_TEMPLATE = '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Anti-Censorship Dashboard</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns"></script>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 50%, #0f3460 100%);
            min-height: 100vh;
            color: #e0e0e0;
            padding: 20px;
        }
        .dashboard { max-width: 1600px; margin: 0 auto; }
        .top-bar {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 20px;
            padding: 10px 0;
        }
        .logo-container {
            display: flex;
            align-items: center;
        }
        .logo-img {
            height: 70px;
            width: auto;
            border: 2px solid gold;
            box-shadow: 0 0 20px rgba(255, 215, 0, 0.5);
        }
        .brand-text {
            font-family: 'Georgia', serif;
            font-size: 2rem;
            font-weight: bold;
            background: linear-gradient(135deg, #ffd700 0%, #ff6b35 50%, #00ff88 100%);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
            text-shadow: 0 0 30px rgba(255, 215, 0, 0.3);
            letter-spacing: 2px;
        }
        header { text-align: center; margin-bottom: 30px; }
        h1 {
            font-size: 2.5rem;
            background: linear-gradient(90deg, #00d9ff, #ff6b6b, #00ff88);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
        }
        .subtitle { color: #888; margin-top: 5px; font-size: 1rem; }
        .live-indicator {
            display: inline-flex;
            align-items: center;
            gap: 8px;
            background: rgba(0, 255, 136, 0.2);
            padding: 6px 16px;
            border-radius: 20px;
            margin-top: 10px;
            font-size: 0.9rem;
        }
        .live-dot {
            width: 10px;
            height: 10px;
            background: #00ff88;
            border-radius: 50%;
            animation: pulse 1.5s infinite;
        }
        @keyframes pulse {
            0%, 100% { opacity: 1; transform: scale(1); }
            50% { opacity: 0.5; transform: scale(1.2); }
        }
        .summary-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
            gap: 16px;
            margin-bottom: 30px;
        }
        .summary-card {
            background: rgba(255, 255, 255, 0.05);
            border-radius: 16px;
            padding: 20px;
            text-align: center;
            backdrop-filter: blur(10px);
            border: 1px solid rgba(255, 255, 255, 0.1);
        }
        .summary-icon { font-size: 2rem; margin-bottom: 8px; }
        .summary-value { font-size: 1.8rem; font-weight: 700; color: #00d9ff; }
        .summary-label { color: #888; font-size: 0.85rem; margin-top: 4px; }
        
        .chart-section {
            background: rgba(255, 255, 255, 0.03);
            border-radius: 16px;
            padding: 24px;
            margin-bottom: 30px;
            border: 1px solid rgba(255, 255, 255, 0.1);
        }
        .chart-title {
            font-size: 1.2rem;
            margin-bottom: 20px;
            color: #fff;
            display: flex;
            align-items: center;
            gap: 10px;
        }
        .chart-controls {
            display: flex;
            gap: 10px;
            margin-bottom: 16px;
        }
        .chart-controls button {
            background: rgba(255,255,255,0.1);
            border: 1px solid rgba(255,255,255,0.2);
            color: #fff;
            padding: 8px 16px;
            border-radius: 8px;
            cursor: pointer;
            transition: all 0.2s;
        }
        .chart-controls button:hover, .chart-controls button.active {
            background: #00d9ff;
            color: #1a1a2e;
        }
        .chart-container { position: relative; height: 350px; }
        
        .vps-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
            gap: 20px;
        }
        .vps-card {
            background: rgba(255, 255, 255, 0.03);
            border-radius: 16px;
            padding: 20px;
            border: 1px solid rgba(255, 255, 255, 0.1);
            transition: all 0.3s;
        }
        .vps-card:hover { background: rgba(255, 255, 255, 0.06); }
        .vps-card.online { border-left: 4px solid #00ff88; }
        .vps-card.offline { border-left: 4px solid #ff4444; }
        .vps-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
        .vps-name { font-size: 1.3rem; font-weight: 600; color: #fff; }
        .vps-status { font-size: 0.8rem; padding: 4px 10px; border-radius: 12px; background: rgba(255,255,255,0.1); }
        .vps-ip { font-family: monospace; color: #00d9ff; font-size: 0.9rem; margin-bottom: 16px; }
        
        .stat-row { display: flex; justify-content: space-between; margin-bottom: 12px; }
        .stat-label { color: #888; font-size: 0.85rem; }
        .stat-value { font-weight: 600; }
        .stat-value.highlight { color: #00ff88; font-size: 1.1rem; }
        .progress-bar { height: 6px; background: rgba(255,255,255,0.1); border-radius: 3px; margin-top: 4px; }
        .progress-fill { height: 100%; border-radius: 3px; transition: width 0.5s; }
        .progress-fill.cpu { background: linear-gradient(90deg, #00ff88, #00d9ff); }
        .progress-fill.mem { background: linear-gradient(90deg, #ff6b6b, #ffa502); }
        
        .services-row { display: flex; gap: 8px; margin-bottom: 16px; flex-wrap: wrap; }
        .service-badge {
            display: inline-flex;
            align-items: center;
            gap: 6px;
            padding: 6px 12px;
            border-radius: 20px;
            font-size: 0.8rem;
            font-weight: 500;
        }
        .service-badge.running { background: rgba(0, 255, 136, 0.2); color: #00ff88; }
        .service-badge.stopped { background: rgba(255, 68, 68, 0.2); color: #ff4444; }
        .service-badge .dot { width: 6px; height: 6px; border-radius: 50%; }
        .service-badge.running .dot { background: #00ff88; }
        .service-badge.stopped .dot { background: #ff4444; }
        
        .service-section { margin-bottom: 12px; padding: 12px; background: rgba(255,255,255,0.02); border-radius: 10px; }
        .service-title { font-size: 0.85rem; color: #00d9ff; margin-bottom: 8px; font-weight: 600; }
        
        .vps-footer { font-size: 0.8rem; color: #666; padding-top: 12px; border-top: 1px solid rgba(255,255,255,0.05); }
    </style>
</head>
<body>
    <div class="dashboard">
        <div class="top-bar">
            <div class="logo-container">
                <img src="/logo.jpeg" alt="Lion and Sun" class="logo-img">
            </div>
            <div class="brand-text">☀️ JavidShah ☀️</div>
        </div>
        <header>
            <h1>🌐 Anti-Censorship Network</h1>
            <div class="subtitle">Conduit • Snowflake • Tor Bridge</div>
            <div class="live-indicator">
                <div class="live-dot"></div>
                <span>Live • Updated: <span id="timestamp">--:--:--</span></span>
            </div>
        </header>
        
        <div class="summary-grid" id="summary"></div>
        
        <div class="chart-section">
            <div class="chart-title">📊 Conduit Connections Over Time (Last 2 Days)</div>
            <div class="chart-controls">
                <button onclick="setTimeRange('1h')" id="btn-1h">1 Hour</button>
                <button onclick="setTimeRange('6h')" id="btn-6h">6 Hours</button>
                <button onclick="setTimeRange('24h')" id="btn-24h" class="active">24 Hours</button>
                <button onclick="setTimeRange('48h')" id="btn-48h">48 Hours</button>
            </div>
            <div class="chart-container"><canvas id="connectionsChart"></canvas></div>
        </div>
        
        <div class="chart-section">
            <div class="chart-title">📈 Current Connections by Conduit (<span id="conduitCount">0</span> instances)</div>
            <div class="chart-container" style="height: 500px;"><canvas id="currentConnChart"></canvas></div>
        </div>
        
        <div class="vps-grid" id="vpsGrid"></div>
    </div>
    
    <script>
        const colors = ['#00d9ff', '#00ff88', '#ff6b6b', '#ffa502', '#a55eea', '#26de81', '#fd79a8', '#74b9ff'];
        let connectionsChart, currentConnChart;
        let historyData = { data: [], vps_names: [] };
        let currentTimeRange = '24h';
        
        function initCharts() {
            connectionsChart = new Chart(document.getElementById('connectionsChart'), {
                type: 'line',
                data: { datasets: [] },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    interaction: { mode: 'index', intersect: false },
                    plugins: {
                        legend: { display: false },
                        tooltip: { mode: 'index', intersect: false }
                    },
                    scales: {
                        x: {
                            type: 'time',
                            time: { unit: 'hour', displayFormats: { hour: 'HH:mm', day: 'MMM d' } },
                            grid: { color: 'rgba(255,255,255,0.1)' },
                            ticks: { color: '#888' }
                        },
                        y: {
                            beginAtZero: true,
                            stacked: true,
                            grid: { color: 'rgba(255,255,255,0.1)' },
                            ticks: { color: '#888' }
                        }
                    }
                }
            });
            
            currentConnChart = new Chart(document.getElementById('currentConnChart'), {
                type: 'bar',
                data: { labels: [], datasets: [{ data: [], backgroundColor: colors, borderRadius: 8 }] },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    indexAxis: 'y',
                    plugins: { legend: { display: false } },
                    scales: {
                        x: { beginAtZero: true, grid: { color: 'rgba(255,255,255,0.1)' }, ticks: { color: '#888' } },
                        y: { grid: { display: false }, ticks: { color: '#888', font: { size: 10 } } }
                    }
                }
            });
        }
        
        function setTimeRange(range) {
            currentTimeRange = range;
            document.querySelectorAll('.chart-controls button').forEach(b => b.classList.remove('active'));
            document.getElementById('btn-' + range).classList.add('active');
            updateConnectionsChart();
        }
        
        function getFilteredHistory() {
            if (!historyData.data || historyData.data.length === 0) return [];
            
            const now = new Date();
            let hoursBack = 24;
            if (currentTimeRange === '1h') hoursBack = 1;
            else if (currentTimeRange === '6h') hoursBack = 6;
            else if (currentTimeRange === '24h') hoursBack = 24;
            else if (currentTimeRange === '48h') hoursBack = 48;
            
            const cutoffMs = now.getTime() - (hoursBack * 60 * 60 * 1000);
            return historyData.data.filter(d => {
                // Parse time string like "2026-01-30 18:31:22"
                const timeStr = d.time.replace(' ', 'T');
                const dataTime = new Date(timeStr);
                return dataTime.getTime() >= cutoffMs;
            });
        }
        
        function updateConnectionsChart() {
            const filtered = getFilteredHistory();
            if (filtered.length === 0) {
                connectionsChart.data.datasets = [];
                connectionsChart.update('none');
                return;
            }
            
            // Show total connections as single stacked area instead of separate lines
            const totalData = filtered.map(d => {
                const total = Object.values(d.connections || {}).reduce((a, b) => a + (b || 0), 0);
                const timeStr = d.time.replace(' ', 'T');
                return { x: new Date(timeStr), y: total };
            });
            
            connectionsChart.data.datasets = [{
                label: 'Total Connections',
                data: totalData,
                borderColor: '#00d9ff',
                backgroundColor: 'rgba(0, 217, 255, 0.3)',
                fill: true,
                tension: 0.3,
                pointRadius: 0,
                borderWidth: 2
            }];
            
            // Adjust time unit based on range
            let unit = 'hour';
            if (currentTimeRange === '1h') unit = 'minute';
            else if (currentTimeRange === '48h') unit = 'hour';
            connectionsChart.options.scales.x.time.unit = unit;
            
            connectionsChart.update('none');
        }
        
        function updateDashboard(data) {
            document.getElementById('timestamp').textContent = data.timestamp;
            
            const vps = data.vps;
            const conduits = data.conduits || [];
            const online = vps.filter(v => v.online).length;
            const totalConn = vps.reduce((a, v) => a + v.connections + (v.connections2 || 0), 0);
            const totalConnecting = vps.reduce((a, v) => a + (v.connecting || 0) + (v.connecting2 || 0), 0);
            const totalUp = vps.reduce((a, v) => a + (v.conduit_up_gb || 0) + (v.conduit2_up_gb || 0), 0);
            const totalDown = vps.reduce((a, v) => a + (v.conduit_down_gb || 0) + (v.conduit2_down_gb || 0), 0);
            const avgCpu = vps.length ? (vps.reduce((a, v) => a + v.cpu_percent, 0) / vps.length) : 0;
            
            // Count services
            const conduit1Up = vps.filter(v => v.conduit_running).length;
            const conduit2Up = vps.filter(v => v.conduit2_running).length;
            const totalConduits = conduit1Up + conduit2Up;
            const snowflakeUp = vps.filter(v => v.snowflake_running).length;
            const torbridgeUp = vps.filter(v => v.torbridge_running).length;
            const totalSnowflakeClients = vps.reduce((a, v) => a + (v.snowflake_clients || 0), 0);
            
            document.getElementById('summary').innerHTML = `
                <div class="summary-card"><div class="summary-icon">🖥️</div><div class="summary-value">${online}/${vps.length}</div><div class="summary-label">VPS Online</div></div>
                <div class="summary-card"><div class="summary-icon">🚀</div><div class="summary-value" style="color:#00d9ff">${totalConduits}</div><div class="summary-label">Conduits (${conduit1Up}+${conduit2Up})</div></div>
                <div class="summary-card"><div class="summary-icon">❄️</div><div class="summary-value" style="color:#a55eea">${snowflakeUp}</div><div class="summary-label">Snowflake</div></div>
                <div class="summary-card"><div class="summary-icon">🌉</div><div class="summary-value" style="color:#ff6b6b">${torbridgeUp}</div><div class="summary-label">Tor Bridge</div></div>
                <div class="summary-card"><div class="summary-icon">🔗</div><div class="summary-value">${totalConn}</div><div class="summary-label">Conduit Users</div></div>
                <div class="summary-card"><div class="summary-icon">⏳</div><div class="summary-value" style="color:#ffa502">${totalConnecting}</div><div class="summary-label">Connecting</div></div>
                <div class="summary-card"><div class="summary-icon">📤</div><div class="summary-value">${totalUp.toFixed(1)}</div><div class="summary-label">Upload (GB)</div></div>
                <div class="summary-card"><div class="summary-icon">📥</div><div class="summary-value">${totalDown.toFixed(1)}</div><div class="summary-label">Download (GB)</div></div>
            `;
            
            // Update current connections bar chart - show all conduits
            const conduitLabels = conduits.map(c => c.name);
            const conduitConns = conduits.map(c => c.connections);
            currentConnChart.data.labels = conduitLabels;
            currentConnChart.data.datasets[0].data = conduitConns;
            currentConnChart.data.datasets[0].backgroundColor = conduits.map((c, i) => colors[i % colors.length]);
            currentConnChart.update('none');
            
            // Update conduit count in chart title
            document.getElementById('conduitCount').textContent = conduits.length;
            
            // VPS cards - mask IPs for security
            function maskIP(ip) {
                const parts = ip.split('.');
                if (parts.length === 4) return parts[0] + '.' + parts[1] + '.***.***';
                return '***.***.***.***';
            }
            document.getElementById('vpsGrid').innerHTML = vps.map(v => `
                <div class="vps-card ${v.online ? 'online' : 'offline'}">
                    <div class="vps-header">
                        <span class="vps-name">${v.alias}</span>
                        <span class="vps-status">${v.online ? '🟢 Online' : '🔴 Offline'}</span>
                    </div>
                    <div class="vps-ip">${maskIP(v.ip)}</div>
                    
                    <div class="services-row">
                        <span class="service-badge ${v.conduit_running ? 'running' : 'stopped'}">
                            <span class="dot"></span>C1
                        </span>
                        <span class="service-badge ${v.conduit2_running ? 'running' : 'stopped'}">
                            <span class="dot"></span>C2
                        </span>
                        <span class="service-badge ${v.snowflake_running ? 'running' : 'stopped'}">
                            <span class="dot"></span>SF
                        </span>
                        <span class="service-badge ${v.torbridge_running ? 'running' : 'stopped'}">
                            <span class="dot"></span>Tor
                        </span>
                    </div>
                    
                    ${v.conduit_running || v.conduit2_running ? `
                    <div class="service-section">
                        <div class="service-title">🚀 Conduit Instances</div>
                        ${v.conduit_running ? `
                        <div style="margin-bottom:8px;padding:8px;background:rgba(0,217,255,0.1);border-radius:6px;">
                            <div style="font-size:0.75rem;color:#00d9ff;margin-bottom:4px;">C1 (${v.conduit_uptime})</div>
                            <div class="stat-row">
                                <span class="stat-label">Connected / Connecting</span>
                                <span class="stat-value highlight">${v.connections} <span style="color:#ffa502;font-size:0.9rem">/ ${v.connecting || 0}</span></span>
                            </div>
                            <div class="stat-row">
                                <span class="stat-label">↑↓ Traffic</span>
                                <span class="stat-value">${v.conduit_up || 'N/A'} / ${v.conduit_down || 'N/A'}</span>
                            </div>
                        </div>
                        ` : ''}
                        ${v.conduit2_running ? `
                        <div style="padding:8px;background:rgba(0,255,136,0.1);border-radius:6px;">
                            <div style="font-size:0.75rem;color:#00ff88;margin-bottom:4px;">C2 (${v.conduit2_uptime})</div>
                            <div class="stat-row">
                                <span class="stat-label">Connected / Connecting</span>
                                <span class="stat-value highlight">${v.connections2 || 0} <span style="color:#ffa502;font-size:0.9rem">/ ${v.connecting2 || 0}</span></span>
                            </div>
                            <div class="stat-row">
                                <span class="stat-label">↑↓ Traffic</span>
                                <span class="stat-value">${v.conduit2_up || 'N/A'} / ${v.conduit2_down || 'N/A'}</span>
                            </div>
                        </div>
                        ` : ''}
                    </div>
                    ` : ''}
                    
                    ${v.snowflake_running ? `
                    <div class="service-section">
                        <div class="service-title">❄️ Snowflake (WebRTC)</div>
                        <div class="stat-row">
                            <span class="stat-label">Clients Served</span>
                            <span class="stat-value" style="color:#a55eea">${v.snowflake_clients || 0}</span>
                        </div>
                        <div class="stat-row">
                            <span class="stat-label">Uptime</span>
                            <span class="stat-value">${v.snowflake_uptime || 'N/A'}</span>
                        </div>
                    </div>
                    ` : ''}
                    
                    ${v.torbridge_running ? `
                    <div class="service-section">
                        <div class="service-title">🌉 Tor Bridge (obfs4)</div>
                        <div class="stat-row">
                            <span class="stat-label">Bootstrap</span>
                            <span class="stat-value" style="color:${v.torbridge_bootstrap >= 100 ? '#00ff88' : '#ffa502'}">${v.torbridge_bootstrap}%</span>
                        </div>
                        <div class="progress-bar"><div class="progress-fill cpu" style="width:${v.torbridge_bootstrap}%"></div></div>
                        <div class="stat-row" style="margin-top:8px">
                            <span class="stat-label">Uptime</span>
                            <span class="stat-value">${v.torbridge_uptime || 'N/A'}</span>
                        </div>
                    </div>
                    ` : ''}
                    
                    <div class="stat-row" style="margin-top:12px">
                        <span class="stat-label">CPU</span>
                        <span class="stat-value">${v.cpu_percent.toFixed(1)}%</span>
                    </div>
                    <div class="progress-bar"><div class="progress-fill cpu" style="width:${Math.min(v.cpu_percent,100)}%"></div></div>
                    <div class="stat-row" style="margin-top:12px">
                        <span class="stat-label">Memory</span>
                        <span class="stat-value">${v.memory_mb.toFixed(0)} MB</span>
                    </div>
                    <div class="progress-bar"><div class="progress-fill mem" style="width:${Math.min(v.memory_percent,100)}%"></div></div>
                    <div class="vps-footer">Server Uptime: ${v.uptime}</div>
                </div>
            `).join('');
        }
        
        async function fetchStats() {
            try {
                const [statsRes, historyRes] = await Promise.all([
                    fetch('/api/stats'),
                    fetch('/api/history')
                ]);
                const statsData = await statsRes.json();
                historyData = await historyRes.json();
                
                updateDashboard(statsData);
                updateConnectionsChart();
            } catch (e) {
                console.error('Failed to fetch stats:', e);
            }
        }
        
        initCharts();
        fetchStats();
        setInterval(fetchStats, 5000);
    </script>
</body>
</html>'''


# Load logo image at startup
LOGO_FILE = SCRIPT_DIR / "lionandsun.jpeg"
LOGO_DATA = None
if LOGO_FILE.exists():
    with open(LOGO_FILE, "rb") as f:
        LOGO_DATA = f.read()


class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Suppress HTTP logs
    
    def do_HEAD(self):
        """Handle HEAD requests (used by health checks)."""
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.end_headers()
    
    def do_GET(self):
        if self.path == '/api/stats':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            with stats_lock:
                self.wfile.write(json.dumps(current_stats).encode())
        elif self.path == '/api/history':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            history = load_history()
            self.wfile.write(json.dumps(history).encode())
        elif self.path == '/logo.jpeg':
            if LOGO_DATA:
                self.send_response(200)
                self.send_header('Content-Type', 'image/jpeg')
                self.send_header('Cache-Control', 'max-age=86400')
                self.end_headers()
                self.wfile.write(LOGO_DATA)
            else:
                self.send_response(404)
                self.end_headers()
        else:
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(HTML_TEMPLATE.encode())


def main():
    print("=" * 60)
    print("🌐 Anti-Censorship Network Dashboard")
    print("   Conduit • Snowflake • Tor Bridge")
    print("=" * 60)
    print()
    print(f"📁 History file: {HISTORY_FILE}")
    print(f"📅 Keeping {HISTORY_DAYS} days of connection history")
    print()
    
    # Start HTTP server first (non-blocking)
    server = HTTPServer(('0.0.0.0', PORT), DashboardHandler)
    print(f"✅ Dashboard running at: http://localhost:{PORT}")
    print(f"   Auto-refresh every {REFRESH_INTERVAL} seconds")
    print()
    print("📊 Collecting initial stats in background...")
    print("Press Ctrl+C to stop")
    print()
    
    # Start background collector (will do initial collection immediately)
    collector_thread = threading.Thread(target=stats_collector_loop, daemon=True)
    collector_thread.start()
    
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 Shutting down...")
        server.shutdown()


if __name__ == "__main__":
    main()
