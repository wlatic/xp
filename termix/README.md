# xp with Termix sync and native SSH

Manage hosts and credentials on the Termix website, then type `xp` in Ghostty on Linux. Connections use your computer's OpenSSH client. Neither the Termix desktop app nor the official Termix CLI needs to run or be installed.

Setup downloads an initial copy of your hosts and credentials. A user timer refreshes it daily; `xp` also attempts a refresh before connecting. Refresh has a five-second total deadline. If the server is unavailable, the last complete encrypted copy remains usable. `xp --offline` skips the server entirely.

The target machine must still be reachable from your computer. Existing connections continue if the Termix server goes down.

## Install

Requires Linux, Python 3.10+, Python's `venv`/pip support, OpenSSH, and an unlocked Secret Service keyring such as GNOME Keyring. The installer uses a private virtual environment for its Python dependencies. It requires no root privileges and does not modify your shell startup files.

1. In Termix, create an API key for your account. Keep it ready for the hidden setup prompt. Termix API keys currently grant access to your account's data; the app does not offer a read-only sync scope.
2. From this directory run:

   ```bash
   python3 install.py --setup --activate
   ```

3. Enter your Termix HTTPS website URL and API key when prompted.
4. Ensure `~/.local/bin` appears before other command directories on your PATH, then run:

   ```bash
   hash -r
   type -a xp
   xp --offline --check
   xp --offline
   ```

The installer verifies the initial encrypted copy before replacing a user-local `xp`. It preserves an existing file or symlink under a timestamped `xp.before-termix-*` name and prints its location. An existing `xp` elsewhere stays untouched. A shell alias or function named `xp` can still take precedence; `type -a xp` reveals that.

To try it without changing `xp`, omit `--activate` and use `xp-termix`. To skip the daily timer, add `--no-timer`.

## Update an existing installation

From the new release's `termix/` directory, run:

```bash
python3 install.py --activate
hash -r
```

This keeps your existing keyring credentials and encrypted cache. It checks the local copy before activating the updated command; you do not need `--setup` again.

The interactive picker now shows the folders from Termix as a hierarchy, with one continuous set of host numbers:

```text
Personal
  Docker
      1) Build server  admin@192.0.2.10:22
  Proxmox
      2) Hypervisor  root@192.0.2.20:22
Work
  Docker
      3) Deployment server  deploy@198.51.100.10:22
```

Filters search host names, addresses, usernames, folders and tags. Each term prefers all case-insensitive literal matches; fuzzy matching is used only when that term has no literal match. Multiple terms use AND matching regardless of their order. This lets `xp ai` select an AI host without unrelated subsequence matches in migration tags. Matching hosts stay grouped under their folder headings; their order within each folder follows match quality. A single matching host still connects immediately.

## Commands

```bash
xp                         # refresh, show picker, connect in the current terminal
xp prox root               # AND fuzzy filters; one match connects immediately
xp --offline               # connect using only the local copy
xp --list                  # safe JSON metadata; no passwords or private keys
xp --all --list            # include disabled/unsupported hosts and reasons
xp --sync                  # require a successful refresh, then exit
xp --check                 # refresh/status check; no SSH connection
xp --offline --check       # inspect the local copy without any server request
xp --setup                 # configure/reconfigure URL and key, requiring an initial copy
```

A failed automatic refresh prints a short notice and uses the previous copy. An explicit `--sync` fails instead of reporting stale data as refreshed. New or changed credentials only become available offline after a successful refresh. Successful sync also applies removals made on the website. Interrupted, malformed, incomplete, or overlapping refreshes preserve the previous usable copy.

The picker hides hosts with SSH explicitly disabled or missing credentials. The web terminal button and old "Needs credentials" names or tags do not control native SSH eligibility. It supports direct SSH with saved passwords and private keys, including encrypted keys with saved or interactively entered passphrases. Saved credential usernames follow Termix's host override rules.

OpenSSH verifies server host keys normally. The first connection asks you to verify/accept the host fingerprint; changed host keys are rejected. Existing OpenSSH `known_hosts` is used. SSH configuration from your normal OpenSSH files still applies. The wrapper selects the exported host, port, username and saved authentication explicitly.

Your normal remote shell startup files and commands such as `ai` continue to work. XPipe-only prompt customization, injected init scripts and temporary starship installations are not copied or executed. Termix desktop session recordings, Docker/Proxmox consoles, file-manager actions, shared-host overrides, SSH certificates, Warpgate/Vault/OPKSSH, SOCKS, jump hosts, forced keyboard-interactive authentication and port knocking are outside this adapter's scope. Unsupported transports/authentication are marked unavailable rather than silently bypassed.

## Optional primary and standby servers

After updating with `python3 termix/install.py --activate`, existing users can set
an ordered server pair without repeating setup or entering another API key:

```sh
xp --servers https://termix-primary.example.com https://termix-standby.example.com
```

This requires the standby to be a verified full-data clone of the primary, including
the existing API key and owner identity. It is not for two unrelated Termix accounts.
The primary must be reachable for this configuration step; the standby can remain
cold. The command verifies a complete primary inventory and authenticated account,
compares the previous account when known/reachable, then changes only the server
configuration. The original encryption identity, keyring entries and cached data
remain intact. Installation and endpoint changes are performed by you locally.

Every normal `xp` invocation then tries the primary, the standby, and finally its
existing encrypted cache. Both network attempts share a five-second total deadline:
the primary receives up to 2.5 seconds, and the standby receives the remaining time.
Each candidate reads account identity, hosts and credentials concurrently from that
one server. All three responses must pass account and snapshot validation before
any cache update; slow requests cannot delay the deadline or write data later.
Hosts and credentials always come from a complete response pair on the same server;
account identity is checked even for an empty inventory. A failed, partial or
unauthorized response cannot overwrite the cache. `--offline` skips both servers;
`--sync` reports failure when neither provides a usable refresh.

The standby gate must attach identical `X-Termix-Snapshot-Time` and
`X-Termix-Snapshot-SHA256` headers to its account and inventory responses. Missing,
inconsistent or implausibly future snapshot metadata is rejected. An older standby
snapshot never replaces a newer client cache: ordinary connections keep that newer
copy, while explicit `--sync` reports the stale snapshot instead of claiming success.
Existing single-server configurations continue working without these headers.

## Daily sync and local security

```bash
systemctl --user status xp-termix-sync.timer
systemctl --user start xp-termix-sync.service
journalctl --user -u xp-termix-sync.service --since today
```

The daily timer uses `OnCalendar=daily`, a short randomized delay, and `Persistent=true` for missed runs. It runs in your user session and needs your keyring unlocked. A locked keyring or unavailable server causes that run to fail without deleting the cache. It does not create a permanent background app or enable systemd lingering.

API credentials and the encryption master key are stored in Linux Secret Service. There is no plaintext keyring fallback. Host snapshots use AES-256-GCM authenticated encryption in owner-only files under `$XDG_DATA_HOME/xp-termix` (normally `~/.local/share/xp-termix`). The URL and a random account identifier live under `$XDG_CONFIG_HOME/xp-termix`; they contain no secrets. Private SSH keys are held in a Linux memory file during a connection, and passwords/passphrases reach OpenSSH through a protected local askpass socket. Secrets are not written to command arguments, environment variables or plaintext cache files.

The Linux keyring must remain available even when the central server is offline. Test `xp --offline` in a fresh terminal with Termix desktop fully quit and the central server temporarily unreachable before relying on this fallback.

## Roll back

Disable the timer:

```bash
systemctl --user disable --now xp-termix-sync.timer
```

Restore the timestamped `xp.before-termix-*` file reported by the installer to `~/.local/bin/xp`. If no user-local `xp` existed before, remove only the newly installed `~/.local/bin/xp` and let the original PATH command take precedence. Run `hash -r` and `type -a xp` afterward. The original XPipe application and its data are untouched.

## Implementation and verification

The adapter reads Termix's supported `/sync/hosts` and `/sync/sshCredentials` endpoints, resolves credential references by stable sync UUID, validates both responses, then atomically promotes the encrypted snapshot. It never writes to Termix. A nonblocking lock prevents the daily job and an interactive refresh from overwriting one another. Refreshing uses the saved API key and does not require an Authentik sign-in; actual SSH uses the target's cached credentials.

Source checked against Termix 2.7.1 commit `76fd9eedbf0f7e853d5ffe40717cac126ffe6a98`. Tests run with:

```bash
python3 -m unittest discover -s . -p 'test_*.py'
```

An isolated actual Termix backend and SSH server were used to verify API-key data access after backend restart, saved-password SSH, encrypted-private-key SSH and continued SSH from an encrypted cache after the backend stopped. Separate-process Secret Service save/load and unknown/changed SSH host-key handling were also verified in the isolated environment. The final installation, desktop keyring unlock behavior, daily timer and network reachability still require verification on your own Linux computer.
