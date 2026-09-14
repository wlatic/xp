#!/usr/bin/env python3
"""Install xp-termix; optionally activate xp with a preserved rollback copy."""
import argparse
import datetime
import hashlib
import json
import shlex
import subprocess
import sys
import venv
import os
from pathlib import Path
import shutil
import stat
import tempfile


def install(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if os.path.lexists(target):
        if target.is_dir():
            raise SystemExit(f"Refusing to replace a directory: {target}")
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        backup = target.with_name(target.name + '.before-termix-' + stamp)
        # Preserve symlinks as symlinks; never overwrite an existing backup.
        if os.path.lexists(backup):
            raise SystemExit("Backup path already exists; run again.")
        shutil.copy2(target, backup, follow_symlinks=False)
    fd, staging = tempfile.mkstemp(prefix='.xp-install-', dir=target.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(source.read_bytes())
        os.chmod(staging, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
        os.replace(staging, target)
    finally:
        if os.path.lexists(staging):
            os.unlink(staging)
    print(f"Installed {target}")
    if backup:
        print(f"Previous entry preserved: {backup}")
        print(f"To restore it: move that backup back to {target}")


def unit_quote(value, exec_expansion=True):
    escaped = str(value).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%')
    if exec_expansion:
        escaped = escaped.replace('$', '$$')
    return '"' + escaped + '"'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--setup', action='store_true', help='Prompt for server/API key and save the initial encrypted copy')
    parser.add_argument('--activate', action='store_true', help='Also install as xp; preserve existing user-local xp')
    parser.add_argument('--bin-dir', type=Path, default=Path.home() / '.local' / 'bin')
    parser.add_argument('--no-timer', action='store_true', help='Install without enabling the daily user timer')
    args = parser.parse_args()
    if sys.platform != 'linux':
        raise SystemExit('This installer requires Linux.')
    source = Path(__file__).resolve().with_name('xp-termix.py')
    requirements = source.with_name('requirements.txt')
    digest = hashlib.sha256(source.read_bytes() + requirements.read_bytes()).hexdigest()[:16]
    data = Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local' / 'share')) / 'xp-termix'
    runtime = data / 'runtime' / digest
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    python = runtime / 'venv' / 'bin' / 'python'
    if not python.exists():
        venv.EnvBuilder(with_pip=True).create(runtime / 'venv')
    subprocess.run([str(python), '-m', 'pip', 'install', '--disable-pip-version-check', '-r', str(requirements)], check=True)
    implementation = runtime / 'xp-termix.py'
    shutil.copy2(source, implementation)
    launcher = runtime / 'launcher'
    launcher.write_text('#!/bin/sh\nexec ' + shlex.quote(str(python)) + ' ' + shlex.quote(str(implementation)) + ' "$@"\n')
    install(launcher, args.bin_dir / 'xp-termix')
    if args.setup:
        subprocess.run([str(python), str(implementation), '--setup'], check=True)
    if args.activate:
        # Keep the existing xp until this machine has a readable first snapshot.
        subprocess.run([str(python), str(implementation), '--offline', '--check'], check=True)
        previous = shutil.which('xp')
        if previous and Path(previous) != args.bin_dir / 'xp':
            print(f'Existing xp at {previous} remains untouched; PATH decides which xp runs.')
        install(launcher, args.bin_dir / 'xp')
    if not args.no_timer:
        units = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'systemd' / 'user'
        units.mkdir(parents=True, exist_ok=True)
        service = runtime / 'xp-termix-sync.service'
        unit_env = ''.join('Environment=' + unit_quote(name + '=' + os.environ[name], exec_expansion=False) + '\n' for name in ('XDG_CONFIG_HOME', 'XDG_DATA_HOME') if name in os.environ)
        service.write_text('[Unit]\nDescription=Refresh encrypted Termix SSH inventory\n\n[Service]\nType=oneshot\n' + unit_env + 'ExecStart=' + unit_quote(args.bin_dir / 'xp-termix') + ' --sync\nUMask=0077\nTimeoutStartSec=30\n')
        timer = runtime / 'xp-termix-sync.timer'
        timer.write_text('[Unit]\nDescription=Daily Termix SSH inventory refresh\n\n[Timer]\nOnCalendar=daily\nPersistent=true\nRandomizedDelaySec=10m\nUnit=xp-termix-sync.service\n\n[Install]\nWantedBy=timers.target\n')
        install(service, units / service.name)
        install(timer, units / timer.name)
        try:
            subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
            subprocess.run(['systemctl', '--user', 'enable', '--now', timer.name], check=True)
        except (OSError, subprocess.CalledProcessError):
            print('Daily timer files installed but could not enable them. Run systemctl --user enable --now xp-termix-sync.timer from your Linux login session.')
    print(f'Put {args.bin_dir} first on PATH.')
    if args.setup:
        print('Initial sync complete. Next: xp-termix --offline --check, then a real offline SSH login.')
    else:
        print('If not configured yet, run: xp-termix --setup')
        print('Then: xp-termix --offline --check, followed by a real offline SSH login.')
    print('Shell aliases/functions can shadow installed commands; check with: type -a xp')


if __name__ == '__main__':
    main()
