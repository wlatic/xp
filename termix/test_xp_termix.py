import importlib.util
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


xp = load('xp_termix', 'xp-termix.py')
installer = load('installer', 'install.py')


class NativeTests(unittest.TestCase):
    def fixture(self):
        hosts = [{'syncId': 'host-a', 'name': 'Docker - Example', 'ip': '192.0.2.10', 'port': 22, 'username': 'root', 'enableTerminal': True, 'folder': 'Home', 'tags': 'docker', 'authType': 'credential', 'credentialSyncId': 'cred-a'},
                 {'syncId': 'host-b', 'name': 'Build Agents', 'ip': '198.51.100.20', 'port': 22, 'username': '', 'enableTerminal': True, 'authType': 'credential', 'credentialSyncId': 'cred-a'}]
        credentials = [{'syncId': 'cred-a', 'username': 'fallback', 'authType': 'password', 'password': 'SECRET-PASSWORD'}]
        return hosts, credentials

    def config(self, account='a' * 32):
        return {'url': 'https://termix.invalid', 'account': account}

    def test_credential_resolution_and_username_override(self):
        hosts, creds = self.fixture()
        rows = xp.resolve_rows(hosts, creds)
        self.assertEqual(rows[0]['username'], 'root')
        self.assertEqual(rows[1]['username'], 'fallback')
        self.assertEqual(rows[0]['password'], 'SECRET-PASSWORD')
        hosts[1]['overrideCredentialUsername'] = True
        rows = xp.resolve_rows(hosts, creds)
        self.assertIn('username', rows[1]['unavailable_reason'])
        hosts[0]['password'] = 'HOST-PASSWORD'
        self.assertEqual(xp.resolve_rows(hosts, creds)[0]['password'], 'HOST-PASSWORD')

    def test_missing_duplicate_credential_ref_rejects_snapshot(self):
        hosts, creds = self.fixture()
        for credentials in ([], creds + creds):
            with self.assertRaises(xp.Error):
                xp.resolve_rows(hosts, credentials)

    def test_advanced_settings_are_not_silently_bypassed(self):
        for field, value in [('jumpHosts', '[{"id":1}]'), ('useWarpgate', True), ('certPublicKey', 'CERT'), ('vaultProfileSyncId', 'vault'), ('portKnockSequence', '[1,2]'), ('useSocks5', True), ('enableSsh', False), ('forceKeyboardInteractive', 'true')]:
            hosts, creds = self.fixture()
            hosts[0][field] = value
            with self.subTest(field=field):
                self.assertTrue(xp.resolve_rows(hosts, creds)[0]['unavailable_reason'])
        hosts, creds = self.fixture()
        hosts[0]['jumpHosts'] = '[]'
        hosts[0]['port'] = 22222
        hosts[0]['sshPort'] = 22  # Independent multi-protocol default, not a proxy.
        self.assertFalse(xp.resolve_rows(hosts, creds)[0]['unavailable_reason'])

    def test_private_key_resolution_and_certificate_gate(self):
        hosts, creds = self.fixture()
        creds[0].update(authType='key', key='PRIVATE-KEY', keyPassword='KEY-PASSPHRASE')
        row = xp.resolve_rows(hosts, creds)[0]
        self.assertEqual(row['keyPassword'], 'KEY-PASSPHRASE')
        self.assertFalse(row['unavailable_reason'])
        creds[0]['certPublicKey'] = 'CERT'
        self.assertTrue(xp.resolve_rows(hosts, creds)[0]['unavailable_reason'])

    def test_safe_json_filters_and_terminal_labels(self):
        hosts, creds = self.fixture()
        hosts[0]['name'] = '\x1b[31mDocker\nspoof'
        rows = xp.resolve_rows(hosts, creds)
        self.assertEqual(len(xp.select(rows, ['docker', 'root'])), 1)
        self.assertEqual(xp.select(rows, ['docker', 'agents']), [])
        encoded = json.dumps([xp.public_host(h) for h in rows])
        self.assertNotIn('SECRET-PASSWORD', encoded)
        self.assertNotIn('\x1b', rows[0]['name'])
        self.assertNotIn('\n', rows[0]['name'])

    def test_shell_injection_addresses_disabled(self):
        for value in ('-oProxyCommand=evil', 'host;echo evil', 'host\n', '$(echo evil)'):
            hosts, creds = self.fixture()
            hosts[0]['ip'] = value
            self.assertTrue(xp.resolve_rows(hosts, creds)[0]['unavailable_reason'])

    def test_cache_encrypted_authenticated_bound_to_server(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(xp, 'paths', return_value=(Path(folder)/'config', Path(folder)/'data')):
            cfg, key = self.config(), os.urandom(32)
            hosts = xp.resolve_rows(*self.fixture())
            xp.save_cache(cfg, key, hosts)
            cache = xp.paths()[1] / (cfg['account'] + '.enc')
            self.assertNotIn(b'SECRET-PASSWORD', cache.read_bytes())
            self.assertEqual(cache.stat().st_mode & 0o777, 0o600)
            self.assertEqual(xp.read_cache(cfg, key)['hosts'][0]['password'], 'SECRET-PASSWORD')
            with self.assertRaises(xp.Error):
                xp.read_cache(dict(cfg, url='https://another.invalid'), key)
            damaged = bytearray(cache.read_bytes()); damaged[-1] ^= 1; cache.write_bytes(damaged)
            with self.assertRaises(xp.Error):
                xp.read_cache(cfg, key)

    def test_failed_refresh_keeps_good_cache_and_offline_skips_network(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(xp, 'paths', return_value=(Path(folder)/'config', Path(folder)/'data')):
            cfg, key = self.config(), os.urandom(32)
            xp.save_cache(cfg, key, xp.resolve_rows(*self.fixture()))
            cache = xp.paths()[1] / (cfg['account'] + '.enc')
            original = cache.read_bytes()
            with patch.object(xp, 'download', side_effect=xp.Error('offline')) as download:
                snapshot = xp.inventory(cfg, 'API-SECRET', key)
                self.assertEqual(len(snapshot['hosts']), 2)
                download.assert_called_once()
                with self.assertRaises(xp.Error):
                    xp.inventory(cfg, 'API-SECRET', key, force=True)
                download.reset_mock()
                xp.inventory(cfg, 'API-SECRET', key, offline=True)
                download.assert_not_called()
            self.assertEqual(cache.read_bytes(), original)

    def test_concurrent_refresh_uses_previous_copy(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(xp, 'paths', return_value=(Path(folder)/'config', Path(folder)/'data')):
            cfg, key = self.config(), os.urandom(32)
            xp.save_cache(cfg, key, xp.resolve_rows(*self.fixture()))
            with patch.object(xp.fcntl, 'flock', side_effect=BlockingIOError), patch.object(xp, 'download') as download:
                self.assertEqual(len(xp.inventory(cfg, 'SECRET', key)['hosts']), 2)
                download.assert_not_called()

    def test_reconfiguration_does_not_overwrite_previous_account_cache(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(xp, 'paths', return_value=(Path(folder)/'config', Path(folder)/'data')):
            oldkey, newkey = os.urandom(32), os.urandom(32)
            oldcfg, newcfg = self.config(), self.config('b'*32)
            hosts = xp.resolve_rows(*self.fixture())
            xp.save_cache(oldcfg, oldkey, hosts)
            xp.save_cache(newcfg, newkey, [])
            self.assertEqual(len(xp.read_cache(oldcfg, oldkey)['hosts']), 2)

    def test_https_and_redirect_restrictions(self):
        for url in ('http://localhost', 'https://user:password@host', 'https://host/?token=secret'):
            with self.assertRaises(xp.Error):
                xp.validate_url(url)
        with self.assertRaises(xp.Error):
            xp.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://other.invalid')

    def test_refresh_has_total_deadline(self):
        with patch.object(xp, 'get_rows', side_effect=lambda *args: time.sleep(0.1)), patch.object(xp.queue.Queue, 'get', side_effect=xp.queue.Empty):
            with self.assertRaisesRegex(xp.Error, '5 seconds'):
                xp.download('https://host', 'API-SECRET')

    def test_no_plaintext_keyring_fallback(self):
        with patch.dict('sys.modules', {'keyring.backends.SecretService': None}):
            with self.assertRaisesRegex(xp.Error, 'No plaintext fallback'):
                xp.keyring_backend()

    def test_installer_preserves_original_symlink(self):
        with tempfile.TemporaryDirectory() as folder:
            d = Path(folder)
            source, previous, target = d/'new', d/'old', d/'xp'
            source.write_text('new launcher'); previous.write_text('original launcher'); target.symlink_to(previous)
            installer.install(source, target)
            backups = list(d.glob('xp.before-termix-*'))
            self.assertTrue(backups[0].is_symlink())
            self.assertEqual(backups[0].read_text(), 'original launcher')
            self.assertEqual(previous.read_text(), 'original launcher')
            self.assertEqual(target.read_text(), 'new launcher')


if __name__ == '__main__':
    unittest.main()
