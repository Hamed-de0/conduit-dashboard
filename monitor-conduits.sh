#!/usr/bin/env bash
# monitor-conduits.sh
# Monitors live docker logs -f conduit on multiple VPS in one tmux session

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="${SCRIPT_DIR}/conduit-vps.conf"
TMUX_SESSION="conduit-logs"
TMUX_SOCKET_DIR="/tmp/tmux-$(id -u)"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

# ─────────────────────────────────────────────────────────────
# 1. Check and install dependencies (tmux, sshpass)
# ─────────────────────────────────────────────────────────────
install_if_missing() {
    local cmd="$1"
    if ! command -v "$cmd" >/dev/null 2>&1; then
        warn "$cmd not found. Installing via Homebrew..."
        if ! command -v brew >/dev/null 2>&1; then
            error "Homebrew not installed. Please install Homebrew first."
        fi
        brew install "$cmd" || error "Failed to install $cmd"
        info "$cmd installed successfully."
    fi
}

install_if_missing tmux
install_if_missing sshpass

# ─────────────────────────────────────────────────────────────
# 2. Clean stale tmux sockets (fixes "no server running" error)
# ─────────────────────────────────────────────────────────────
cleanup_tmux() {
    info "Cleaning stale tmux sockets..."
    
    # Remove stale socket directory if it exists but is broken
    if [[ -d "$TMUX_SOCKET_DIR" ]]; then
        # Kill any existing session with our name
        tmux -L default kill-session -t "$TMUX_SESSION" 2>/dev/null || true
        
        # Remove stale sockets (files that are not actually sockets)
        find "$TMUX_SOCKET_DIR" -type s -name "default" 2>/dev/null | while read -r sock; do
            if ! tmux -S "$sock" list-sessions 2>/dev/null; then
                rm -f "$sock" 2>/dev/null || true
            fi
        done
    fi
    
    # Ensure socket directory exists with correct permissions
    mkdir -p "$TMUX_SOCKET_DIR" 2>/dev/null || true
    chmod 700 "$TMUX_SOCKET_DIR" 2>/dev/null || true
    
    # Kill server completely to ensure fresh start
    tmux kill-server 2>/dev/null || true
    sleep 0.5
}

# ─────────────────────────────────────────────────────────────
# 3. Validate config file
# ─────────────────────────────────────────────────────────────
if [[ ! -f "$CONFIG_FILE" ]]; then
    error "Config file not found: $CONFIG_FILE"
fi

# Count active VPS entries
VPS_COUNT=0
while IFS='|' read -r alias user ip port password comment || [[ -n "$alias" ]]; do
    [[ -z "$alias" || "$alias" =~ ^[[:space:]]*# ]] && continue
    ((VPS_COUNT++))
done < "$CONFIG_FILE"

if [[ $VPS_COUNT -eq 0 ]]; then
    error "No active VPS entries found in $CONFIG_FILE"
fi

info "Found $VPS_COUNT VPS entries in config."

# ─────────────────────────────────────────────────────────────
# 4. Cleanup and start fresh tmux
# ─────────────────────────────────────────────────────────────
cleanup_tmux

info "Starting tmux session '$TMUX_SESSION'..."

# Force-start tmux server explicitly
tmux start-server

# Create new detached session
tmux new-session -d -s "$TMUX_SESSION" -x 200 -y 50

# ─────────────────────────────────────────────────────────────
# 5. Create panes for each VPS
# ─────────────────────────────────────────────────────────────
first=1
while IFS='|' read -r alias user ip port password comment || [[ -n "$alias" ]]; do
    # Skip empty lines and comments
    [[ -z "$alias" || "$alias" =~ ^[[:space:]]*# ]] && continue
    
    # Trim whitespace
    alias="${alias//[[:space:]]/}"
    user="${user//[[:space:]]/}"
    ip="${ip//[[:space:]]/}"
    port="${port//[[:space:]]/}"
    password="${password//[[:space:]]/}"
    
    # Default port if empty
    port="${port:-22}"
    
    info "Adding monitor for $alias ($user@$ip:$port)"
    
    # Build SSH command with keep-alive options
    SSH_OPTS="-o StrictHostKeyChecking=no -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ConnectTimeout=10 -p $port"
    DOCKER_CMD="docker logs -f --tail 200 conduit"
    
    # Use sshpass if password is provided, otherwise use key-based auth
    if [[ -n "$password" && "$password" != "-" ]]; then
        SSH_CMD="sshpass -p '$password' ssh $SSH_OPTS ${user}@${ip} '$DOCKER_CMD'"
    else
        SSH_CMD="ssh $SSH_OPTS ${user}@${ip} '$DOCKER_CMD'"
    fi
    
    if [[ $first -eq 1 ]]; then
        # First VPS uses main pane - set pane title
        tmux send-keys -t "$TMUX_SESSION":0.0 "printf '\\033]2;${alias}\\033\\\\' && $SSH_CMD" C-m
        first=0
    else
        # Split and add new pane
        tmux split-window -t "$TMUX_SESSION" -h
        tmux send-keys -t "$TMUX_SESSION" "printf '\\033]2;${alias}\\033\\\\' && $SSH_CMD" C-m
        # Rebalance layout after each split
        tmux select-layout -t "$TMUX_SESSION" tiled
    fi
    
    sleep 0.3
done < "$CONFIG_FILE"

# ─────────────────────────────────────────────────────────────
# 6. Final layout and attach
# ─────────────────────────────────────────────────────────────
tmux select-layout -t "$TMUX_SESSION" tiled
tmux select-pane -t "$TMUX_SESSION":0.0

info "All monitors started. Attaching to tmux session..."
echo ""
echo -e "${YELLOW}Tmux shortcuts:${NC}"
echo "  Ctrl+b then arrow keys  - Navigate panes"
echo "  Ctrl+b then z           - Zoom/unzoom current pane"
echo "  Ctrl+b then d           - Detach (logs keep running)"
echo "  Ctrl+b then [           - Scroll mode (q to exit)"
echo "  Ctrl+c                  - Stop current log stream"
echo ""

# Attach to session
exec tmux attach-session -t "$TMUX_SESSION"