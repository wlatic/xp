import importlib.util
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import threading
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

    def test_ssh_eligibility_uses_credentials_not_ui_or_old_labels(self):
        hosts, creds = self.fixture()
        hosts[0].update(enableSsh=True, enableTerminal=False,
                        name='External [Needs credentials]', tags=['Needs credentials'])
        creds[0].update(authType='key', password=None, key='PRIVATE-KEY')
        self.assertFalse(xp.resolve_rows(hosts, creds)[0]['unavailable_reason'])

        hosts[0]['enableSsh'] = False
        self.assertEqual(xp.resolve_rows(hosts, creds)[0]['unavailable_reason'], 'SSH disabled')

        hosts[0]['enableSsh'] = True
        creds[0]['key'] = None
        self.assertEqual(xp.resolve_rows(hosts, creds)[0]['unavailable_reason'], 'Needs credentials')

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

    def test_literal_ai_excludes_migration_tag_subsequences(self):
        hosts, creds = self.fixture()
        hosts[0].update(name='AI - Agents', tags=['migration'])
        hosts[1].update(name='Router', tags=['migration'])
        rows = xp.resolve_rows(hosts, creds)
        self.assertEqual(xp.select(rows, ['AI']), [rows[0]])
        self.assertEqual(len(xp.select(rows, ['migration'])), 2)

    def test_literal_matches_are_all_kept_and_fuzzy_fallback_remains(self):
        hosts, creds = self.fixture()
        hosts[0]['name'] = 'AI - Agents'
        hosts[1]['name'] = 'Mail Server'
        rows = xp.resolve_rows(hosts, creds)
        self.assertEqual(len(xp.select(rows, ['ai'])), 2)
        self.assertEqual(xp.select(rows, ['mlsv']), [rows[1]])

    def test_literal_and_fuzzy_filters_are_and_order_independent(self):
        hosts, creds = self.fixture()
        hosts[0].update(name='AI - Agents', username='root', tags=['migration'])
        hosts[1].update(name='Router', username='admin', tags=['migration'])
        rows = xp.resolve_rows(hosts, creds)
        for terms in (['ai', 'root'], ['root', 'ai']):
            self.assertEqual(xp.select(rows, terms), [rows[0]])
        for terms in (['ai', 'admin'], ['admin', 'ai']):
            self.assertEqual(xp.select(rows, terms), [])
        for terms in (['ai', 'agts'], ['agts', 'ai']):
            self.assertEqual(xp.select(rows, terms), [rows[0]])

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

    def test_hierarchical_menu_numbering_after_interleaved_filter_scores(self):
        rows = xp.resolve_rows(*self.fixture())
        first = dict(rows[0], name='Build best match', folder='Personal / Docker')
        second = dict(rows[1], name='Build next match', folder='Work / Docker')
        third = dict(rows[0], name='Build last match', folder='Personal / Docker')
        scored = [first, second, third]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            ordered = xp.display_menu(scored)
        self.assertEqual(ordered, [first, third, second])
        lines = output.getvalue().splitlines()
        self.assertEqual(lines[0:2], ['Personal', '  Docker'])
        self.assertTrue(lines[2].lstrip().startswith('1) Build best match'))
        self.assertTrue(lines[3].lstrip().startswith('2) Build last match'))
        self.assertEqual(lines[4:6], ['Work', '  Docker'])
        self.assertTrue(lines[6].lstrip().startswith('3) Build next match'))
        with patch.object(xp, 'read_config', return_value=self.config()), patch.object(xp, 'load_secrets', return_value=('API', b'key')), patch.object(xp, 'inventory', return_value={'hosts': scored}), patch.object(xp, 'select', return_value=scored), patch.object(xp.sys.stdin, 'isatty', return_value=True), patch.object(xp.sys.stdout, 'isatty', return_value=True), patch('builtins.input', return_value='2'), patch('builtins.print'), patch.object(xp, 'connect', return_value=0) as connect:
            self.assertEqual(xp.main(['build', '--offline']), 0)
        connect.assert_called_once_with(third)

    def test_menu_sanitizes_controls_in_cached_labels(self):
        host = xp.resolve_rows(*self.fixture())[0]
        host.update(folder='Personal\x1b / Docker\nInjected', name='Host\x1b[31m', unavailable_reason='reason\nspoof')
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            xp.display_menu([host])
        self.assertNotIn('\x1b', output.getvalue())
        self.assertEqual(len(output.getvalue().splitlines()), 3)
        self.assertIn('Docker Injected', output.getvalue())

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

class CascadeTests(unittest.TestCase):
    def fixture(self):
        hosts, credentials = NativeTests.fixture(self)
        for row in hosts + credentials:
            row['userId'] = 'fixture-owner'
        return hosts, credentials

    def cfg(self):
        return {'url': 'https://legacy.invalid', 'account': 'a'*32,
                'servers': ['https://primary.invalid', 'https://standby.invalid'],
                'owner_id': 'fixture-owner'}

    def responses(self, *, down=(), mismatch=False, missing=False, owner='fixture-owner'):
        hosts, credentials = self.fixture()
        timestamp = xp.datetime.fromtimestamp(time.time()-60, xp.timezone.utc).isoformat()
        def request(url, key, path, deadline):
            if url in down:
                raise xp.Error('server unavailable')
            headers = {}
            if url == 'https://standby.invalid' and not missing:
                headers = {'x-termix-snapshot-time': timestamp,
                           'x-termix-snapshot-sha256': ('b' if mismatch and path.endswith('sshCredentials') else 'a')*64}
            if path == '/users/me':
                return {'userId': owner}, headers
            return {'rows': hosts if path.endswith('/hosts') else credentials}, headers
        return request

    def test_primary_failure_uses_complete_standby_and_checks_owner(self):
        with patch.object(xp, 'request_json', side_effect=self.responses(down=['https://primary.invalid'])):
            hosts, metadata = xp.download_servers(self.cfg(), 'SECRET', None)
        self.assertEqual(len(hosts), 2)
        self.assertEqual(metadata['source'], 'https://standby.invalid')
        self.assertEqual(metadata['snapshot_sha256'], 'a'*64)
        with patch.object(xp, 'request_json', side_effect=self.responses(owner='other-owner')):
            with self.assertRaisesRegex(xp.Error, 'identity'):
                xp.download_servers(self.cfg(), 'SECRET', None)

    def test_partial_sources_and_unverified_generations_are_not_combined(self):
        base = self.responses()
        def partial(url, key, path, deadline):
            if (url.endswith('primary.invalid') and path.endswith('sshCredentials')) or (url.endswith('standby.invalid') and path.endswith('/hosts')):
                raise xp.Error('partial inventory')
            return base(url,key,path,deadline)
        for responder in (partial,
                          self.responses(down=['https://primary.invalid'], missing=True),
                          self.responses(down=['https://primary.invalid'], mismatch=True)):
            with patch.object(xp, 'request_json', side_effect=responder):
                with self.assertRaises(xp.Error):
                    xp.download_servers(self.cfg(), 'SECRET', None)

    def test_both_down_and_older_standby_preserve_cache(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(xp, 'paths', return_value=(Path(directory)/'config',Path(directory)/'data')):
            cfg,key=self.cfg(),os.urandom(32)
            xp.save_cache(cfg,key,xp.resolve_rows(*self.fixture()))
            path=xp.paths()[1]/(cfg['account']+'.enc');original=path.read_bytes()
            for responder in (self.responses(down=cfg['servers']), self.responses(down=[cfg['servers'][0]])):
                with patch.object(xp, 'request_json', side_effect=responder):
                    self.assertEqual(len(xp.inventory(cfg,'SECRET',key)['hosts']),2)
                    self.assertEqual(path.read_bytes(),original)
                    with self.assertRaises(xp.Error): xp.inventory(cfg,'SECRET',key,force=True)
            with patch.object(xp,'download_servers') as download:
                xp.inventory(cfg,'SECRET',key,offline=True)
                download.assert_not_called()

    def test_empty_inventory_still_checks_owner_and_future_generation_rejected(self):
        with patch.object(xp, 'request_json', return_value=({'userId':'other-owner'},{})):
            with self.assertRaisesRegex(xp.Error,'identity'):
                xp.download_candidate('https://primary.invalid','SECRET','fixture-owner')
        for stamp in (xp.datetime.fromtimestamp(time.time()+3600,xp.timezone.utc).isoformat(),'2020-01-01T00:00:00','invalid'):
            with self.assertRaises(xp.Error):
                xp.standby_generation({'x-termix-snapshot-time':stamp,'x-termix-snapshot-sha256':'a'*64},time.time())

    def test_candidate_reads_overlap_and_keep_shared_origin_deadline(self):
        barrier = threading.Barrier(3)
        calls = []
        base = self.responses()
        def simultaneous(url, key, path, deadline):
            calls.append((url, deadline))
            barrier.wait(timeout=0.5)
            return base(url, key, path, deadline)
        with patch.object(xp, 'request_json', side_effect=simultaneous):
            hosts, metadata = xp.download_candidate('https://standby.invalid', 'SECRET', 'fixture-owner', standby=True, timeout=1)
        self.assertEqual(len(hosts), 2)
        self.assertEqual(len(set(calls)), 1)
        self.assertEqual(metadata['snapshot_sha256'], 'a'*64)

    def test_candidate_partial_error_returns_without_waiting_for_stalled_sibling(self):
        release = threading.Event()
        base = self.responses()
        def partial(url, key, path, deadline):
            if path.endswith('sshCredentials'):
                raise xp.Error('credential read failed')
            release.wait(timeout=1)
            return base(url, key, path, deadline)
        try:
            with patch.object(xp, 'request_json', side_effect=partial), patch.object(xp, 'save_cache') as save:
                started = time.monotonic()
                with self.assertRaisesRegex(xp.Error, 'credential read failed'):
                    xp.download_candidate('https://primary.invalid', 'SECRET', 'fixture-owner', timeout=0.2)
                self.assertLess(time.monotonic()-started, 0.3)
                save.assert_not_called()
        finally:
            release.set()

    def test_total_budget_and_late_workers_cannot_promote(self):
        def stalled(*args):
            time.sleep(0.25)
            return {'rows':[]},{}
        with patch.object(xp,'CASCADE_TIMEOUT',0.12), patch.object(xp,'request_json',side_effect=stalled), patch.object(xp,'save_cache') as save:
            started=time.monotonic()
            with self.assertRaises(xp.Error): xp.download_servers(self.cfg(),'SECRET',None)
            self.assertLess(time.monotonic()-started,0.22)
            time.sleep(0.3)
            save.assert_not_called()

    def test_servers_update_preserves_aad_cache_and_checks_previous_owner(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(xp,'paths',return_value=(Path(directory)/'config',Path(directory)/'data')):
            cfg={'url':'https://legacy.invalid','account':'a'*32};key=os.urandom(32)
            xp.atomic_write(xp.paths()[0]/'config.json',json.dumps(cfg).encode())
            xp.save_cache(cfg,key,xp.resolve_rows(*self.fixture()))
            cache=xp.paths()[1]/(cfg['account']+'.enc');before=cache.read_bytes()
            with patch.object(xp,'request_json',side_effect=self.responses(down=['https://standby.invalid'])):
                xp.configure_servers(cfg,'SECRET',self.cfg()['servers'])
            updated=xp.read_config()
            self.assertEqual(updated['url'],cfg['url'])
            self.assertEqual(updated['account'],cfg['account'])
            self.assertEqual(updated['owner_id'],'fixture-owner')
            self.assertEqual(cache.read_bytes(),before)
            self.assertEqual(len(xp.read_cache(updated,key)['hosts']),2)
            config_before=(xp.paths()[0]/'config.json').read_bytes()
            with patch.object(xp,'request_json',side_effect=self.responses(owner='wrong-owner')):
                with self.assertRaises(xp.Error): xp.configure_servers(updated,'SECRET',self.cfg()['servers'])
            self.assertEqual((xp.paths()[0]/'config.json').read_bytes(),config_before)

    def test_server_configuration_requires_distinct_https_origins(self):
        for urls in (['https://one.invalid'],['https://one.invalid','https://one.invalid/'],
                     ['http://one.invalid','https://two.invalid'],['https://one.invalid/path','https://two.invalid']):
            with self.assertRaises(xp.Error): xp.validate_servers(urls)


if __name__ == '__main__':
    unittest.main()
