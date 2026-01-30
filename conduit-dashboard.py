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
HISTORY_DAYS = 2  # Keep 2 days of history


# Service names we track
SERVICES = ["conduit", "snowflake", "tor-bridge"]

# Global stats storage
current_stats = {"vps": [], "timestamp": ""}
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


def ssh_command(vps, cmd):
    """Execute SSH command on VPS."""
    ssh_opts = "-o StrictHostKeyChecking=no -o ConnectTimeout=10 -o ServerAliveInterval=30"
    
    if vps["password"] and vps["password"] != "-":
        full_cmd = f"sshpass -p '{vps['password']}' ssh {ssh_opts} -p {vps['port']} {vps['user']}@{vps['ip']} \"{cmd}\""
    else:
        full_cmd = f"ssh {ssh_opts} -p {vps['port']} {vps['user']}@{vps['ip']} \"{cmd}\""
    
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
        # Conduit stats
        "conduit_running": False,
        "conduit_uptime": "N/A",
        "connections": 0,
        "connecting": 0,
        "conduit_up": "N/A",
        "conduit_down": "N/A",
        "conduit_up_gb": 0,
        "conduit_down_gb": 0,
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
    
    with stats_lock:
        current_stats["vps"] = all_stats
        current_stats["timestamp"] = timestamp
    
    # Update history file
    history = load_history()
    history = cleanup_old_history(history)
    
    # Add new data point
    connections_data = {s["alias"]: s["connections"] for s in all_stats}
    history["data"].append({
        "time": full_timestamp,
        "connections": connections_data
    })
    history["vps_names"] = [s["alias"] for s in all_stats]
    
    save_history(history)
    
    total_conn = sum(s["connections"] for s in all_stats)
    print(f"[{timestamp}] Stats updated: {len(all_stats)} VPS, {total_conn} total connections")


def stats_collector_loop():
    """Background thread to collect stats periodically."""
    while True:
        try:
            collect_stats()
        except Exception as e:
            print(f"Error collecting stats: {e}")
        time.sleep(REFRESH_INTERVAL)


HTML_TEMPLATE = '''<!DOCTYPE html>ش
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
            <div class="chart-title">📈 Current Connections by VPS</div>
            <div class="chart-container" style="height: 200px;"><canvas id="currentConnChart"></canvas></div>
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
                        legend: { labels: { color: '#888' } },
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
                        y: { grid: { display: false }, ticks: { color: '#888' } }
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
            const now = new Date();
            let hoursBack = 24;
            if (currentTimeRange === '1h') hoursBack = 1;
            else if (currentTimeRange === '6h') hoursBack = 6;
            else if (currentTimeRange === '24h') hoursBack = 24;
            else if (currentTimeRange === '48h') hoursBack = 48;
            
            const cutoff = new Date(now.getTime() - hoursBack * 60 * 60 * 1000);
            return historyData.data.filter(d => new Date(d.time) >= cutoff);
        }
        
        function updateConnectionsChart() {
            const filtered = getFilteredHistory();
            if (filtered.length === 0) return;
            
            const vpsNames = historyData.vps_names;
            
            connectionsChart.data.datasets = vpsNames.map((name, i) => ({
                label: name,
                data: filtered.map(d => ({
                    x: new Date(d.time),
                    y: d.connections[name] || 0
                })),
                borderColor: colors[i % colors.length],
                backgroundColor: colors[i % colors.length] + '33',
                fill: true,
                tension: 0.3,
                pointRadius: 0,
                borderWidth: 2
            }));
            
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
            const online = vps.filter(v => v.online).length;
            const running = vps.filter(v => v.container_running).length;
            const totalConn = vps.reduce((a, v) => a + v.connections, 0);
            const totalConnecting = vps.reduce((a, v) => a + (v.connecting || 0), 0);
            const totalUp = vps.reduce((a, v) => a + (v.conduit_up_gb || 0), 0);
            const totalDown = vps.reduce((a, v) => a + (v.conduit_down_gb || 0), 0);
            const avgCpu = vps.length ? (vps.reduce((a, v) => a + v.cpu_percent, 0) / vps.length) : 0;
            
            // Count services
            const conduitUp = vps.filter(v => v.conduit_running).length;
            const snowflakeUp = vps.filter(v => v.snowflake_running).length;
            const torbridgeUp = vps.filter(v => v.torbridge_running).length;
            const totalSnowflakeClients = vps.reduce((a, v) => a + (v.snowflake_clients || 0), 0);
            
            document.getElementById('summary').innerHTML = `
                <div class="summary-card"><div class="summary-icon">🖥️</div><div class="summary-value">${online}/${vps.length}</div><div class="summary-label">VPS Online</div></div>
                <div class="summary-card"><div class="summary-icon">🚀</div><div class="summary-value" style="color:#00d9ff">${conduitUp}</div><div class="summary-label">Conduit</div></div>
                <div class="summary-card"><div class="summary-icon">❄️</div><div class="summary-value" style="color:#a55eea">${snowflakeUp}</div><div class="summary-label">Snowflake</div></div>
                <div class="summary-card"><div class="summary-icon">🌉</div><div class="summary-value" style="color:#ff6b6b">${torbridgeUp}</div><div class="summary-label">Tor Bridge</div></div>
                <div class="summary-card"><div class="summary-icon">🔗</div><div class="summary-value">${totalConn}</div><div class="summary-label">Conduit Users</div></div>
                <div class="summary-card"><div class="summary-icon">📤</div><div class="summary-value">${totalUp.toFixed(1)}</div><div class="summary-label">Upload (GB)</div></div>
                <div class="summary-card"><div class="summary-icon">📥</div><div class="summary-value">${totalDown.toFixed(1)}</div><div class="summary-label">Download (GB)</div></div>
            `;
            
            // Update current connections bar chart
            currentConnChart.data.labels = vps.map(v => v.alias);
            currentConnChart.data.datasets[0].data = vps.map(v => v.connections);
            currentConnChart.update('none');
            
            // VPS cards
            document.getElementById('vpsGrid').innerHTML = vps.map(v => `
                <div class="vps-card ${v.online ? 'online' : 'offline'}">
                    <div class="vps-header">
                        <span class="vps-name">${v.alias}</span>
                        <span class="vps-status">${v.online ? '🟢 Online' : '🔴 Offline'}</span>
                    </div>
                    <div class="vps-ip">${v.ip}</div>
                    
                    <div class="services-row">
                        <span class="service-badge ${v.conduit_running ? 'running' : 'stopped'}">
                            <span class="dot"></span>Conduit
                        </span>
                        <span class="service-badge ${v.snowflake_running ? 'running' : 'stopped'}">
                            <span class="dot"></span>Snowflake
                        </span>
                        <span class="service-badge ${v.torbridge_running ? 'running' : 'stopped'}">
                            <span class="dot"></span>Tor Bridge
                        </span>
                    </div>
                    
                    ${v.conduit_running ? `
                    <div class="service-section">
                        <div class="service-title">🚀 Conduit (Psiphon)</div>
                        <div class="stat-row">
                            <span class="stat-label">Connected</span>
                            <span class="stat-value highlight">${v.connections}</span>
                        </div>
                        <div class="stat-row">
                            <span class="stat-label">Connecting</span>
                            <span class="stat-value">${v.connecting || 0}</span>
                        </div>
                        <div class="stat-row">
                            <span class="stat-label">↑ Upload</span>
                            <span class="stat-value">${v.conduit_up || 'N/A'}</span>
                        </div>
                        <div class="stat-row">
                            <span class="stat-label">↓ Download</span>
                            <span class="stat-value">${v.conduit_down || 'N/A'}</span>
                        </div>
                        <div class="stat-row">
                            <span class="stat-label">Uptime</span>
                            <span class="stat-value">${v.conduit_uptime || 'N/A'}</span>
                        </div>
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


class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Suppress HTTP logs
    
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
    
    # Initial stats collection
    print("📊 Collecting initial stats...")
    collect_stats()
    
    # Start background collector
    collector_thread = threading.Thread(target=stats_collector_loop, daemon=True)
    collector_thread.start()
    
    # Start HTTP server
    server = HTTPServer(('0.0.0.0', PORT), DashboardHandler)
    print()
    print(f"✅ Dashboard running at: http://localhost:{PORT}")
    print(f"   Auto-refresh every {REFRESH_INTERVAL} seconds")
    print()
    print("Press Ctrl+C to stop")
    print()
    
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 Shutting down...")
        server.shutdown()


if __name__ == "__main__":
    main()
