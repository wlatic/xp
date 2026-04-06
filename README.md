# xp — Fuzzy SSH launcher for XPipe

A lightweight CLI tool that connects to your SSH servers using [XPipe](https://xpipe.io) as the backend. Fuzzy-filter connections by name, host, or category — then connect in your current terminal with full environment setup (starship prompt, init scripts, terminal title).

**Why?** XPipe is great for managing SSH connections, but its `xpipe launch` command skips terminal customizations (starship, init scripts). `xp` bridges that gap: it reads your XPipe config, deploys your prompt tool and settings to the remote, and launches via `xpipe launch` — all in your current terminal tab.

## Demo

```
$ xp
=== SSH Connections ===

 [personal]
  1) Ai - Agents Server    root@10.10.199.199  ● running
  2) Proxmox 1 - Host      root@10.10.5.11     ● running
  3) Proxmox 2 - Host      root@10.10.5.10     ● running

 [external]
  4) Http Node - External   root@167.253.65.72  ● running

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
- **Init scripts** — XPipe-enabled init scripts are injected into the session
- **Terminal title** — set to the connection name
- **Status indicators** — shows which servers are running/stopped
- **JSON output** — `xp --list` for scripting
- **Zero dependencies** — Python 3 stdlib only

## How it works

1. Authenticates with XPipe's HTTP API
2. Queries SSH connections (names, hosts, users, categories, status)
3. Reads `~/.xpipe/settings/preferences.json` for prompt tool config and init scripts
4. Fuzzy-filters based on your input
5. Prepares the remote via XPipe API (installs prompt tool if missing, deploys config)
6. Launches via `xpipe launch` with a custom rcfile for full environment

## Requirements

- Python 3.6+
- [XPipe](https://xpipe.io) with the API enabled (Settings > API)
- XPipe API key (Settings > API)

## Install

```bash
curl -o ~/.local/bin/xp https://raw.githubusercontent.com/wlatic/xp/main/xp
chmod +x ~/.local/bin/xp
```

Or clone:

```bash
git clone https://github.com/wlatic/xp.git
ln -s "$(pwd)/xp/xp" ~/.local/bin/xp
```

Make sure `~/.local/bin` is in your PATH.

### API key setup

Option A — environment variable:
```bash
export XPIPE_API_KEY="your-api-key-here"
```

Option B — config file:
```bash
mkdir -p ~/.config/xp
echo "your-api-key-here" > ~/.config/xp/key
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
xp ai               # Single match → auto-connect to AI server
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

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `XPIPE_API_KEY` | — | XPipe API key |
| `XPIPE_BEACON_PORT` | `21721` | XPipe daemon port |

## Context

Built to address [xpipe-io/xpipe#427](https://github.com/xpipe-io/xpipe/issues/427) — `xpipe launch` doesn't apply terminal customizations (starship, init scripts) that the GUI does. `xp` replicates XPipe's terminal init by reading the same preferences and using the API to prepare remotes before connecting.

## License

MIT
