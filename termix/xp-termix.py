#!/usr/bin/env python3
"""Native SSH picker with an encrypted, one-way Termix cache. No desktop app required."""
import argparse
import base64
import getpass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import queue
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

SERVICE = 'xp-termix'
MAX_BYTES = 16 * 1024 * 1024


class Error(Exception):
    pass


def paths():
    config = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / SERVICE
    data = Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local' / 'share')) / SERVICE
    return config, data


def private_dir(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or path.stat().st_uid != os.getuid():
        raise Error('Private configuration directory must be owned by you and not a symlink.')
    path.chmod(0o700)


def atomic_write(path, payload):
    private_dir(path.parent)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.pending-')
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.lexists(tmp):
            os.unlink(tmp)


def keyring_backend():
    try:
        from keyring.backends.SecretService import Keyring
        backend = Keyring()
        if backend.priority <= 0:
            raise ValueError()
        return backend
    except Exception:
        raise Error('An unlocked Linux Secret Service keyring is required (GNOME Keyring or compatible). No plaintext fallback is used.') from None


def read_config():
    try:
        cfg = json.loads((paths()[0] / 'config.json').read_text())
        validate_url(cfg['url'])
        if not isinstance(cfg['account'], str) or not re.fullmatch('[a-f0-9]{32}', cfg['account']):
            raise ValueError()
        return cfg
    except (OSError, ValueError, KeyError, TypeError):
        raise Error('Run xp-termix --setup first.') from None


def load_secrets(cfg):
    try:
        value = keyring_backend().get_password(SERVICE, cfg['account'])
        stored = json.loads(value or '')
        key = base64.b64decode(stored['cache_key'], validate=True)
        if len(key) != 32 or not isinstance(stored['api_key'], str) or not stored['api_key']:
            raise ValueError()
        return stored['api_key'], key
    except Error:
        raise
    except Exception:
        raise Error('Cannot unlock the xp Termix credentials in your Linux keyring. Unlock it, or rerun --setup.') from None


def validate_url(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise Error('Use an HTTPS Termix base URL without credentials, query or fragment.')
    return url.rstrip('/')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Error('Termix redirected the export request; check the configured base URL.')


def get_rows(url, api_key, table):
    request = urllib.request.Request(url + '/sync/' + table, headers={'Authorization': 'Bearer ' + api_key, 'Accept': 'application/json'})
    opener = urllib.request.build_opener(NoRedirect())
    with opener.open(request, timeout=5) as response:
        raw = response.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise Error('Termix sync exceeds the supported size.')
    payload = json.loads(raw)
    if not isinstance(payload, dict) or not isinstance(payload.get('rows'), list):
        raise Error('Unexpected Termix sync response; existing cache preserved.')
    return payload['rows']


def resolve_rows(hosts, credentials):
    by_id = {}
    for credential in credentials:
        if not isinstance(credential, dict) or not isinstance(credential.get('syncId'), str) or not credential['syncId'] or credential['syncId'] in by_id:
            raise Error('Invalid or duplicate credential sync ID; existing cache preserved.')
        by_id[credential['syncId']] = credential
    resolved = []
    seen = set()
    for row in hosts:
        if not isinstance(row, dict) or not isinstance(row.get('syncId'), str) or not row['syncId'] or row['syncId'] in seen:
            raise Error('Invalid or duplicate host sync ID; existing cache preserved.')
        seen.add(row['syncId'])
        host = dict(row)
        if host.get('authType') == 'credential':
            reference = host.get('credentialSyncId')
            if reference:
                if reference not in by_id:
                    raise Error('A saved credential is missing from sync; existing cache preserved.')
                credential = by_id[reference]
                # Same username/password precedence as Termix credential-username.ts.
                if not host.get('overrideCredentialUsername') and not (host.get('username') or '').strip():
                    host['username'] = credential.get('username')
                if not (host.get('password') or '').strip():
                    host['password'] = credential.get('password')
                host['key'] = credential.get('key') or credential.get('privateKey')
                host['keyPassword'] = credential.get('keyPassword')
                host['certPublicKey'] = host.get('certPublicKey') or credential.get('certPublicKey')
                host['credentialAuthType'] = credential.get('authType')
        resolved.append(host)
    return normalize_hosts({'hosts': resolved})


def download(url, api_key):
    # Bound the whole refresh, including DNS and both HTTPS reads. A stalled
    # daemon only holds in-memory data and never writes/promotes a cache.
    results = queue.Queue(maxsize=1)
    def fetch():
        try:
            hosts = get_rows(url, api_key, 'hosts')
            credentials = get_rows(url, api_key, 'sshCredentials')
            results.put((resolve_rows(hosts, credentials), None))
        except urllib.error.HTTPError as exc:
            results.put((None, Error(f'Termix refresh failed (HTTP {exc.code}).')))
        except Error as exc:
            results.put((None, exc))
        except Exception:
            results.put((None, Error('Termix refresh unavailable or invalid; existing cache preserved.')))
    threading.Thread(target=fetch, daemon=True).start()
    try:
        hosts, error = results.get(timeout=5)
    except queue.Empty:
        raise Error('Termix refresh timed out after 5 seconds.') from None
    if error:
        raise error
    return hosts


def clean(value):
    return ''.join(c if c.isprintable() else ' ' for c in str(value or ''))


def nonempty_config(value):
    if isinstance(value, str):
        try:
            return bool(json.loads(value))
        except ValueError:
            return bool(value)
    return bool(value)


def normalize_hosts(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get('hosts'), list):
        raise Error('Unexpected Termix export; existing cache preserved.')
    hosts = []
    for row in payload['hosts']:
        if not isinstance(row, dict):
            raise Error('Invalid Termix host record; existing cache preserved.')
        # Stable sync references have been resolved inline. Preserve only fields needed
        # for direct SSH, not arbitrary remote snippets or terminal configuration.
        host = {field: row.get(field) for field in ('name', 'ip', 'port', 'username', 'folder', 'tags', 'authType', 'password', 'key', 'keyPassword')}
        for field in ('password', 'key', 'keyPassword'):
            if host[field] is not None and not isinstance(host[field], str):
                raise Error('Invalid credential in Termix export; existing cache preserved.')
        for field in ('ip', 'username', 'authType'):
            if host[field] is not None and not isinstance(host[field], str):
                raise Error('Invalid connection field; existing cache preserved.')
        host['name'] = clean(host['name']) or 'Unnamed host'
        host['folder'] = clean(host['folder']) or 'Uncategorized'
        tags = host['tags'] or []
        host['tags'] = [clean(t) for t in (tags.split(',') if isinstance(tags, str) else tags)] if isinstance(tags, (list, str)) else []
        host['port'] = host['port'] or 22
        if type(host['port']) is not int or not 1 <= host['port'] <= 65535:
            raise Error('Invalid SSH port; existing cache preserved.')
        reason = ''
        ip, user = host['ip'], host['username']
        if row.get('connectionType', 'ssh') != 'ssh':
            reason = 'Not SSH'
        elif row.get('enableSsh') is False or row.get('enableTerminal') is not True:
            reason = 'Terminal disabled'
        elif not isinstance(ip, str) or not re.fullmatch(r'[A-Za-z0-9._:%-]+', ip) or ip.startswith('-') or ip == '0.0.0.0':
            reason = 'Missing or unsupported address'
        elif not isinstance(user, str) or not re.fullmatch(r'[A-Za-z0-9._@-]+', user) or user.startswith('-'):
            reason = 'Missing or unsupported username'
        elif nonempty_config(row.get('portKnockSequence')):
            reason = 'Port knocking unsupported'
        elif nonempty_config(row.get('jumpHosts')) or row.get('useSocks5') or nonempty_config(row.get('socks5ProxyChain')):
            reason = 'Jump hosts / SOCKS require separate configuration'
        elif row.get('useWarpgate') or row.get('certPublicKey') or row.get('vaultProfileId') or row.get('vaultProfileSyncId') or row.get('credentialAuthType') not in (None, 'password', 'key'):
            reason = 'Certificate, gateway or dynamic authentication unsupported'
        elif row.get('forceKeyboardInteractive') in (True, 'true'):
            reason = 'Forced keyboard-interactive authentication unsupported'
        elif host['authType'] not in ('password', 'key', 'credential'):
            reason = 'Unsupported authentication method'
        elif not host['key'] and not host['password']:
            reason = 'Needs credentials'
        elif 'needs credentials' in (host['name'] + ' ' + ' '.join(host['tags'])).lower():
            reason = 'Needs credentials'
        host['unavailable_reason'] = reason
        hosts.append(host)
    return hosts


def save_cache(cfg, key, hosts):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    snapshot = {'version': 1, 'updated_at': time.time(), 'hosts': hosts}
    nonce = secrets.token_bytes(12)
    payload = json.dumps(snapshot).encode()
    sealed = nonce + AESGCM(key).encrypt(nonce, payload, cfg['url'].encode())
    atomic_write(paths()[1] / (cfg['account'] + '.enc'), sealed)
    return snapshot


def read_cache(cfg, key):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    try:
        sealed = (paths()[1] / (cfg['account'] + '.enc')).read_bytes()
    except FileNotFoundError:
        return None
    try:
        decoded = AESGCM(key).decrypt(sealed[:12], sealed[12:], cfg['url'].encode())
        snapshot = json.loads(decoded)
        if snapshot['version'] != 1 or not isinstance(snapshot['hosts'], list) or not isinstance(snapshot['updated_at'], (int, float)):
            raise ValueError()
        return snapshot
    except Exception:
        raise Error('Encrypted cache cannot be verified. Run --sync to replace it from the server.') from None


def setup():
    cfg_dir, _ = paths()
    existing = None
    try:
        existing = read_config()
    except Error:
        pass
    default = existing['url'] if existing else ''
    entered = input(f'Termix HTTPS URL{f" [{default}]" if default else ""}: ').strip()
    url = validate_url(entered or default)
    api_key = getpass.getpass('Termix API key (hidden): ').strip()
    if not api_key.startswith('tmx_'):
        raise Error('Expected a Termix API key beginning tmx_.')
    hosts = download(url, api_key)
    cfg = {'url': url, 'account': secrets.token_hex(16)}
    key = secrets.token_bytes(32)
    try:
        keyring_backend().set_password(SERVICE, cfg['account'], json.dumps({'api_key': api_key, 'cache_key': base64.b64encode(key).decode()}))
    except Error:
        raise
    except Exception:
        raise Error('Could not save credentials in the Linux Secret Service keyring.') from None
    # Save config last so a failed initial fetch/keyring write cannot replace an
    # existing setup. Previous keyring entries remain recoverable if reconfigured.
    save_cache(cfg, key, hosts)
    atomic_write(cfg_dir / 'config.json', json.dumps(cfg).encode())
    print(f'Setup complete: {len(hosts)} hosts cached with encryption. No SSH connection opened.')


def inventory(cfg, api_key, key, *, offline=False, force=False):
    private_dir(paths()[1])
    lockpath = paths()[1] / (cfg['account'] + '.lock')
    fd = os.open(lockpath, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            cached = read_cache(cfg, key)
            if force or not cached:
                raise Error('Another refresh is already running; try again shortly.') from None
            print('xp: Refresh already running; using the previous encrypted cache.', file=sys.stderr)
            return cached
        return _inventory(cfg, api_key, key, offline=offline, force=force)
    finally:
        os.close(fd)


def _inventory(cfg, api_key, key, *, offline=False, force=False):
    try:
        cached = read_cache(cfg, key)
    except Error:
        if not force:
            raise
        cached = None
    if not offline:  # User preference: attempt refresh before every connection.
        try:
            return save_cache(cfg, key, download(cfg['url'], api_key))
        except (Error, OSError) as exc:
            if force or not cached:
                if isinstance(exc, OSError):
                    raise Error('Could not save the refreshed cache; previous copy preserved.') from None
                raise
            age = max(0, int(time.time() - cached['updated_at']))
            print(f'xp: Termix refresh failed; using encrypted cache ({age}s old).', file=sys.stderr)
    if not cached:
        raise Error('No offline cache yet. Run --setup or --sync while Termix is reachable.')
    return cached


def score(pattern, text):
    pattern, text = pattern.casefold(), text.casefold()
    if pattern in text:
        return 100 + len(pattern) + (50 if text.startswith(pattern) else 0)
    cursor = 0
    for char in text:
        if cursor < len(pattern) and char == pattern[cursor]:
            cursor += 1
    return len(pattern) if cursor == len(pattern) else 0


def select(hosts, filters):
    matched = []
    for host in hosts:
        text = f"{host['name']} {host['username']}@{host['ip']} {host['folder']} {' '.join(host['tags'])}"
        scores = [score(f, text) for f in filters]
        if all(scores):
            matched.append((sum(scores), host))
    return [h for _, h in sorted(matched, key=lambda pair: (-pair[0], pair[1]['folder'].casefold(), pair[1]['name'].casefold()))]


def display_menu(hosts):
    """Render folder hierarchy and return hosts in exactly the numbered order."""
    tree = {'hosts': [], 'children': {}}
    for host in hosts:
        node = tree
        folder = clean(host['folder']) or 'Uncategorized'
        for segment in folder.split(' / '):
            label = segment.strip() or 'Uncategorized'
            node = node['children'].setdefault(label, {'hosts': [], 'children': {}})
        node['hosts'].append(host)
    ordered = []

    def render(node, depth):
        for host in node['hosts']:
            ordered.append(host)
            note = ' [' + clean(host['unavailable_reason']) + ']' if host['unavailable_reason'] else ''
            print(f"{'  ' * depth}{len(ordered):>3}) {clean(host['name'])}  {clean(host['username'])}@{clean(host['ip'])}:{host['port']}{note}")
        for label in sorted(node['children'], key=lambda value: (value.casefold(), value)):
            print(f"{'  ' * depth}{label}")
            render(node['children'][label], depth + 1)

    render(tree, 0)
    return ordered


def public_host(host):
    return {k: host[k] for k in ('name', 'ip', 'port', 'username', 'folder', 'tags', 'authType', 'unavailable_reason')}


def askpass():
    # Called by OpenSSH; communicate only through a protected Unix socket.
    prompt = sys.argv[2] if len(sys.argv) > 2 else ''
    if os.environ.get('SSH_ASKPASS_PROMPT') == 'confirm' or 'are you sure you want to continue connecting' in prompt.lower():
        try:
            with open('/dev/tty', 'w') as tty:
                tty.write(prompt + ' ')
                tty.flush()
            with open('/dev/tty', 'r') as tty:
                answer = tty.readline().strip()
            print(answer if answer in ('yes', 'no') else 'no')
        except OSError:
            print('no')
        return 0
    kind = 'keyPassword' if 'passphrase' in prompt.lower() else 'password' if 'password' in prompt.lower() else 'unsupported'
    client = socket.socket(socket.AF_UNIX)
    try:
        client.connect(os.environ['XP_ASKPASS_SOCKET'])
        client.sendall(kind.encode())
        chunks = []
        while chunk := client.recv(4096):
            chunks.append(chunk)
        value = b''.join(chunks).decode()
        if not value and kind == 'keyPassword':
            value = getpass.getpass('SSH key passphrase: ')
        sys.stdout.write(value + '\n')
    finally:
        client.close()
    return 0


def connect(host):
    if host['unavailable_reason']:
        raise Error(host['unavailable_reason'])
    binary = shutil.which('ssh')
    if not binary:
        raise Error('OpenSSH client is required.')
    env = os.environ.copy()
    argv = [binary, '-o', 'StrictHostKeyChecking=ask', '-o', 'ConnectTimeout=10', '-o', 'NumberOfPasswordPrompts=1', '-o', 'ControlMaster=no', '-o', 'ControlPath=none']
    keyfd = None
    with tempfile.TemporaryDirectory(prefix='xp-askpass-') as folder:
        endpoint = str(Path(folder) / 'secret.sock')
        server = socket.socket(socket.AF_UNIX)
        server.bind(endpoint)
        os.chmod(endpoint, 0o600)
        server.listen(1)
        server.settimeout(0.2)
        stopped = threading.Event()

        def serve():
            while not stopped.is_set():
                try:
                    client, _ = server.accept()
                except socket.timeout:
                    continue
                except OSError:
                    return
                with client:
                    client.settimeout(2)
                    _, uid, _ = struct.unpack('3i', client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                    if uid != os.getuid():
                        continue
                    try:
                        kind = client.recv(64).decode()
                        value = host.get(kind) if kind in ('password', 'keyPassword') else None
                        client.sendall((value or '').encode())
                    except (OSError, UnicodeError):
                        pass

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        # The helper has no secret contents; its interpreter path is shell-quoted.
        import shlex
        helper = Path(folder) / 'askpass'
        helper.write_text('#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' ' + shlex.quote(str(Path(__file__).resolve())) + ' --askpass "$@"\n')
        helper.chmod(0o700)
        env.update(SSH_ASKPASS=str(helper), SSH_ASKPASS_REQUIRE='force', DISPLAY=env.get('DISPLAY') or ':0', XP_ASKPASS_SOCKET=endpoint)
        try:
            if host['key']:
                if not hasattr(os, 'memfd_create'):
                    raise Error('Private-key login requires Linux memfd support.')
                keyfd = os.memfd_create('xp-ssh-key', os.MFD_CLOEXEC)
                os.fchmod(keyfd, 0o600)
                os.write(keyfd, (host['key'].rstrip() + '\n').encode())
                argv += ['-o', 'IdentitiesOnly=yes', '-o', 'PreferredAuthentications=publickey', '-i', f'/proc/{os.getpid()}/fd/{keyfd}']
            else:
                argv += ['-o', 'PubkeyAuthentication=no', '-o', 'PreferredAuthentications=password,keyboard-interactive']
            argv += ['-p', str(host['port']), '-l', host['username'], '--', host['ip']]
            return subprocess.call(argv, env=env)
        finally:
            if keyfd is not None:
                os.close(keyfd)
            stopped.set()
            server.close()
            thread.join(timeout=3)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('filters', nargs='*')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--setup', action='store_true')
    group.add_argument('--sync', action='store_true', help='Refresh encrypted local cache and exit')
    group.add_argument('--offline', action='store_true', help='Use cache without contacting Termix')
    parser.add_argument('--list', action='store_true', help='Safe JSON inventory, no secrets')
    parser.add_argument('--all', action='store_true', help='Include disabled/unsupported hosts')
    parser.add_argument('--check', action='store_true', help='Show cache status without SSH login')
    args = parser.parse_args(argv)
    if args.setup:
        setup()
        return 0
    cfg = read_config()
    api_key, key = load_secrets(cfg)
    snapshot = inventory(cfg, api_key, key, offline=args.offline, force=args.sync)
    hosts = snapshot['hosts']
    if args.check or args.sync:
        eligible = sum(not h['unavailable_reason'] for h in hosts)
        age = max(0, int(time.time() - snapshot['updated_at']))
        print(f'{len(hosts)} cached hosts; {eligible} eligible for direct SSH; cache age {age}s. No SSH connection opened.')
        return 0
    hosts = select([h for h in hosts if args.all or not h['unavailable_reason']], args.filters)
    if args.list:
        print(json.dumps([public_host(h) for h in hosts], indent=2))
        return 0
    if not hosts:
        raise Error('No matching SSH hosts. Use --all --list to inspect incomplete or unsupported entries.')
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise Error('Run interactive xp in Ghostty, or use --list for scripts.')
    if len(hosts) == 1:
        chosen = hosts[0]
    else:
        hosts = display_menu(hosts)
        answer = input('Host number (q or Enter to cancel): ').strip()
        if not answer or answer.lower() == 'q':
            return 0
        if not answer.isdecimal() or not 1 <= int(answer) <= len(hosts):
            raise Error('Invalid host number.')
        chosen = hosts[int(answer)-1]
    return connect(chosen)


if __name__ == '__main__':
    try:
        sys.exit(askpass() if len(sys.argv) > 1 and sys.argv[1] == '--askpass' else main())
    except Error as error:
        print('xp: ' + str(error), file=sys.stderr)
        sys.exit(1)
    except OSError:
        print('xp: Local file, keyring or SSH operation failed. Check local permissions and available disk space.', file=sys.stderr)
        sys.exit(1)
    except ImportError:
        print('xp: Required Python dependencies are missing; run the installer.', file=sys.stderr)
        sys.exit(1)
    except (KeyboardInterrupt, EOFError):
        sys.exit(130)
