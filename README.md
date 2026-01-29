# Anti-Censorship VPS Dashboard

A real-time monitoring dashboard for managing multiple VPS servers running anti-censorship tools:
- **Psiphon Conduit** - Proxy service for circumventing internet censorship
- **Snowflake** - Tor pluggable transport proxy
- **Tor Bridge** - obfs4 bridge relay

![Dashboard Preview](docs/dashboard-preview.png)

## Features

- 🔴 **Live Dashboard** - Real-time stats updated every 10 seconds
- 📊 **Connection Tracking** - Monitor active connections across all VPS
- 📈 **Historical Charts** - 48-hour connection history with interactive graphs
- 🌐 **Multi-Service Support** - Conduit, Snowflake, and Tor Bridge status
- 🖥️ **Tmux Monitor** - Live log streaming from all VPS in split panes
- ⚡ **Parallel SSH** - Fast concurrent status checks

## Quick Start

### 1. Clone and Configure

```bash
git clone https://github.com/hamed-de0/conduit-dashboard.git
cd conduit-dashboard

# Copy example config and add your VPS details
cp conduit-vps.conf.example conduit-vps.conf
nano conduit-vps.conf
```

### 2. Install Dependencies

```bash
# macOS
brew install sshpass tmux

# Ubuntu/Debian
sudo apt install sshpass tmux

# Python (for dashboard)
pip install -r requirements.txt  # or just use standard library
```

### 3. Run the Dashboard

```bash
# Start the web dashboard
python3 conduit-dashboard.py

# Open in browser
open http://localhost:5050
```

### 4. Tmux Live Logs (Optional)

```bash
# Stream live docker logs from all VPS in tmux split panes
./monitor-conduits.sh
```

## VPS Configuration

Edit `conduit-vps.conf` with your server details:

```properties
# Format: alias|user|ip|port|password|comment
vps1|root|1.2.3.4|22|your_password|US Server
vps2|root|5.6.7.8|22|-|EU Server (SSH key auth)
```

- Use actual password or `-` for SSH key authentication
- Lines starting with `#` are ignored

## VPS Setup

Each VPS should have Docker installed with these containers:

```bash
# Psiphon Conduit
docker run -d --name conduit --restart unless-stopped \
  -v conduit-data:/home/conduit/data \
  --network host \
  ghcr.io/ssmirr/conduit/conduit:d8522a8 \
  start --max-clients 1000 --bandwidth 40 --stats-file

# Snowflake Proxy
docker run -d --name snowflake --restart always \
  --network host \
  thetorproject/snowflake-proxy:latest -verbose

# Tor Bridge (obfs4)
docker run -d --name tor-bridge --restart always \
  -p 9001:9001 -p 9443:9443 \
  -v tor-bridge-data:/var/lib/tor \
  -e OR_PORT=9001 -e PT_PORT=9443 \
  -e EMAIL=nobody@example.com \
  thetorproject/obfs4-bridge:latest
```

## Firewall Ports

Ensure these ports are open:
- **443, 8448** - Conduit
- **9001, 9443** - Tor Bridge

## Files

| File | Description |
|------|-------------|
| `conduit-dashboard.py` | Web dashboard server (port 5050) |
| `conduit-dashboard.html` | Static HTML fallback dashboard |
| `monitor-conduits.sh` | Tmux-based live log viewer |
| `conduit-vps.conf.example` | Example configuration file |
| `conduit-vps.conf` | Your actual config (gitignored) |

## Contributing

Contributions welcome! Please submit issues and pull requests.

## License

MIT License - See [LICENSE](LICENSE) for details.

## Disclaimer

This software is provided for educational and research purposes. Users are responsible for ensuring compliance with local laws and regulations regarding internet privacy tools.
