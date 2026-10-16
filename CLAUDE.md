# xp

The root `xp` is the original XPipe launcher. The `termix/` adapter is the Linux
Termix-backed launcher, installed with `python3 termix/install.py --activate`.
Updating preserves keyring credentials and the encrypted cache.

## Transport

`termix/xp-termix.py` selects native Mosh for directly reachable targets only after
checking remote `mosh-server` with the saved OpenSSH credentials and normal host
key checks. Missing client/server means SSH for this invocation, plus a detached,
120-second, best-effort package install using root or noninteractive sudo.
A failed check or install must never prevent opening SSH. Explicit `--direct`
and `--offline` retain their existing plain OpenSSH behavior. Unreachable targets
use the official Termix CLI. Keep passwords off command lines and environment;
private keys remain in memfd and askpass uses a protected local socket.

## Verification

Run `python3 -m unittest discover -s termix -p 'test_*.py'` from repository root.
Tests cover saved-auth Mosh checks, missing dependency SSH fallback, failed checks
and installs, package-manager privilege handling and remote job detachment.
Follow `termix/README.md` for installation and deployment limitations.
