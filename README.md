# xp — Fuzzy SSH launcher for XPipe

A lightweight CLI tool that connects to your SSH servers using [XPipe](https://xpipe.io) as the backend. Fuzzy-filter connections by name, host, or category — then connect in your current terminal with full environment setup (starship prompt, init scripts, terminal title).

**Why?** XPipe's `xpipe launch` command skips terminal customizations (starship, init scripts) that the GUI applies. `xp` bridges that gap: it reads your XPipe config, deploys your prompt tool and settings to the remote, and launches via `xpipe launch` — all in your current terminal tab.

## Demo

```
$ xp
=== SSH Connections ===

 [personal]
  1) Ai - Agents Server    root@10.10.199.199  ● running
  2) Proxmox 1 - Host      root@10.10.5.11     ● running
  3) Proxmox 2 - Host      root@10.10.5.10     ● running

 [external]
  4) Http Node - External   root@167.*  ● running

Select (1-4) or q to quit:
```

```bash
$ xp prox       # Two matches → filtered menu with just Proxmox servers
$ xp ai         # Single match → auto-connects immediately
```

## Features

- **Fuzzy filtering** — `xp prox` matches "Proxmox", `xp ai` matches "AI Agents Server"
- **Multiple filters** — `xp prox 1` (AND logic, all must match)
- **Auto-connect** — single match connects immediately, no menu
- **XPipe auth** — passwords, keys, jump hosts — all managed by XPipe
- **Starship/Oh My Posh** — reads your XPipe prompt preference, auto-installs and configures on remotes
- **Config sync** — your starship TOML or Oh My Posh JSON from XPipe is deployed to each remote
- **Shell dialect detection** — detects remote shell (bash, zsh, fish) and adapts init commands
- **Init scripts** — XPipe-enabled init scripts are injected into the session
- **Terminal title** — set to the connection name
- **Ghostty terminfo** — auto-installs `xterm-ghostty` terminfo on remotes that don't have it
- **Status indicators** — shows which servers are running/stopped
- **JSON output** — `xp --list` for scripting
- **Cross-platform** — works on Linux, macOS, and Windows (reads platform-specific XPipe paths)
- **Zero dependencies** — Python 3.6+ stdlib only

## How it works

1. Authenticates with XPipe's HTTP API
2. Queries SSH connections (names, hosts, users, categories, status)
3. Reads XPipe preferences for prompt tool config and init scripts
4. Fuzzy-filters based on your input
5. Starts a shell session via API to prepare the remote:
   - Detects remote shell dialect (bash/zsh/fish)
   - Installs starship or Oh My Posh if missing
   - Deploys prompt config (TOML/JSON)
   - Installs Ghostty terminfo if missing
   - Writes a custom rcfile with all init commands
6. Launches via `xpipe launch` with the rcfile for full environment

## Requirements

- Python 3.6+
- [XPipe](https://xpipe.io) with the API enabled (Settings > API)
- XPipe API key (Settings > API)

## Running XPipe without the GUI

`xp` only needs the XPipe daemon — the GUI is not required. This makes it perfect for headless workflows.

### Check daemon status

```bash
xpipe daemon status
```

### Start the daemon manually

```bash
xpipe daemon start
```

### Run as a background service (auto-start on login)

Set XPipe's startup mode to background daemon:

```bash
xpipe daemon mode background
```

Or create a systemd user service (Linux):

```bash
mkdir -p ~/.config/systemd/user

cat > ~/.config/systemd/user/xpipe-daemon.service << 'XPEOF'
[Unit]
Description=XPipe Daemon
After=network.target

[Service]
ExecStart=xpipe daemon start
ExecStop=xpipe daemon stop
Restart=on-failure
Type=simple

[Install]
WantedBy=default.target
XPEOF

systemctl --user daemon-reload
systemctl --user enable --now xpipe-daemon
```

### Workflow

With the daemon running headlessly, your workflow becomes:

1. Open a terminal tab
2. Run `xp` (or `xp <filter>`)
3. Pick a server → you're connected with full environment

No XPipe windows, no GUI, no clicking — just fast SSH with all your XPipe-managed auth and terminal customizations.

## Install

### Linux / macOS

```bash
curl -o ~/.local/bin/xp https://raw.githubusercontent.com/wlatic/xp/main/xp
chmod +x ~/.local/bin/xp
```

Make sure `~/.local/bin` is in your PATH.

### Windows

```powershell
Invoke-WebRequest -Uri https://raw.githubusercontent.com/wlatic/xp/main/xp -OutFile "$env:LOCALAPPDATA\xp\xp.py"
```

Then create a batch wrapper or add a doskey alias.

### From source

```bash
git clone https://github.com/wlatic/xp.git
ln -s "$(pwd)/xp/xp" ~/.local/bin/xp
```

### API key setup

```bash
# Option A: environment variable
export XPIPE_API_KEY="your-api-key-here"

# Option B: config file (Linux/macOS)
mkdir -p ~/.config/xp
echo "your-api-key-here" > ~/.config/xp/key

# Option B: config file (Windows)
# Save key to %LOCALAPPDATA%\xp\key
```

## Usage

```
xp                      Show all SSH connections
xp <filter>             Fuzzy search and connect
xp <filter> <filter>    Multiple filters (AND)
xp --list               JSON output for scripting
xp --debug              Show API calls for troubleshooting
```

### Examples

```bash
xp                  # Show menu of all SSH connections
xp prox             # Filter to Proxmox servers
xp ai               # Single match → auto-connect
xp root docker      # Match both "root" and "docker"
xp --list           # Dump all connections as JSON
xp prox --list      # Filtered JSON output
```

## Configuration

`xp` reads XPipe's own configuration — no separate config needed. Change your prompt tool, add init scripts, or update connections in XPipe, and `xp` picks it up automatically.

### Supported prompt tools

| Tool | Auto-install | Config sync |
|------|-------------|-------------|
| Starship | Yes | Yes (TOML) |
| Oh My Posh | Yes | Yes (JSON) |

### Supported shell dialects

| Shell | rcfile method | Prompt init |
|-------|--------------|-------------|
| bash | `--rcfile` | `eval "$(starship init bash)"` |
| zsh | `source` + `exec zsh` | `eval "$(starship init zsh)"` |
| fish | fallback to bash | `starship init fish \| source` |

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `XPIPE_API_KEY` | — | XPipe API key |
| `XPIPE_BEACON_PORT` | `21721` | XPipe daemon port |

### XPipe preferences read

| Preference | Used for |
|-----------|----------|
| `terminalPrompt` | Which prompt tool + config to deploy |
| `terminalInitScript` | Custom init script to inject |
| `clearTerminalOnInit` | Whether to clear screen on connect |

## Context

Built to address [xpipe-io/xpipe#427](https://github.com/xpipe-io/xpipe/issues/427) — `xpipe launch` doesn't apply terminal customizations that the GUI does. `xp` replicates XPipe's terminal init by reading the same preferences and using the API to prepare remotes.

## License

MIT
