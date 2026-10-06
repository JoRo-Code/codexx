"""Bundle transactions and conservative session-update reporting; no live accounts."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import codexx_updates as updates
from test_codexx import cx

SOURCE = Path(__file__).parent.resolve()


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name).resolve()
        self.app = cx.load_accounts()
        self.version = self.app.VERSION
        major, minor, patch_version = map(int, self.version.split("."))
        self.next_version = f"{major}.{minor}.{patch_version + 1}"
        self.app.ROOT = self.directory / 'data'
        self.bin = self.directory / 'bin'
        self.bin.mkdir()
        self.root = self.bin / '.codexx'
        self.payloads = {name: (SOURCE / name).read_bytes() for name in updates.FILES}
        self.generation = updates.install(SOURCE, self.bin, self.app)
        self.app.__file__ = str(self.generation / 'codex-accounts')

    def next_source(self):
        source = self.directory / 'source'
        source.mkdir(exist_ok=True)
        for name, payload in self.payloads.items():
            if name == 'codex-accounts':
                payload = payload.replace(("VERSION = " + repr(self.version)).encode(), ("VERSION = " + repr(self.next_version)).encode())
            (source / name).write_bytes(payload)
        return source

    def update(self, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return updates.update(self.app, **kwargs)

    def test_install_pins_pair_and_engine_watches_stable_path(self):
        self.assertEqual(self.app.executable_path(), self.root / 'current/codex-accounts')
        for name in ('codexx', 'codex-accounts'):
            flag = '--bundle-version' if name == 'codexx' else '--version'
            result = subprocess.run([sys.executable, str(self.bin / name), flag], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), self.version)

    def test_update_and_rollback_switch_whole_pair_without_touching_data(self):
        self.app.ROOT.mkdir(exist_ok=True)
        sentinel = self.app.ROOT / 'accounts-history-sentinel'
        sentinel.write_text('unchanged')
        source = self.next_source()
        with (source / 'codexx').open('a') as stream: stream.write('\n# wrapper update\n')
        self.assertTrue(self.update(source=source))
        current = (self.root / 'current').resolve()
        self.assertNotEqual(current, self.generation)
        self.assertIn(b'wrapper update', (current / 'codexx').read_bytes())
        self.assertEqual(self.app.installed_version(self.app.executable_path()), self.next_version)
        self.assertEqual((self.root / 'previous').resolve(), self.generation)
        self.assertTrue(self.update(rollback=True))
        self.assertEqual((self.root / 'current').resolve(), self.generation)
        self.assertEqual(self.app.update_settings()['policy'], 'notify')
        self.assertEqual(sentinel.read_text(), 'unchanged')

    def test_bad_wrapper_keeps_current_pair(self):
        source = self.next_source()
        (source / 'codexx').write_text('raise RuntimeError("bad startup")\n')
        with self.assertRaises(self.app.Error): self.update(source=source)
        self.assertEqual((self.root / 'current').resolve(), self.generation)
        self.assertFalse(list((self.root / 'versions').glob('.staging-*')))

    def test_running_loader_observes_bundle_switch_and_rollback_without_recreation(self):
        runtime = self.app.LiveRuntime()
        self.addCleanup(runtime.close)
        self.assertEqual(runtime.source, self.root / 'current/codex-accounts')
        with patch.dict(os.environ, {'CODEX_ACCOUNTS_NO_UPDATE': '1'}):
            self.update(source=self.next_source())
            runtime.refresh(force=True)
            self.assertEqual(runtime.module.VERSION, self.next_version)
            self.assertEqual(runtime.fingerprint, self.app.read_json(self.root / 'current/bundle.json')['engine'])
            self.update(rollback=True)
            runtime.refresh(force=True)
            self.assertEqual(runtime.module.VERSION, self.version)

    def test_interrupted_activation_keeps_previous_current(self):
        source = self.next_source()
        replace = os.replace
        def fail_commit(src, dst):
            if Path(dst) == self.root / 'current': raise OSError('interrupted')
            return replace(src, dst)
        with patch.object(updates.os, 'replace', side_effect=fail_commit), self.assertRaises(OSError):
            self.update(source=source)
        self.assertEqual((self.root / 'current').resolve(), self.generation)
        self.assertTrue(self.update(source=source))

    def test_rollback_rejects_tampered_generation(self):
        self.update(source=self.next_source())
        (self.generation / 'codexx').write_text('tampered')
        active = (self.root / 'current').resolve()
        with self.assertRaises(self.app.Error): self.update(rollback=True)
        self.assertEqual((self.root / 'current').resolve(), active)

    def test_existing_engine_update_command_delegates_to_bundle(self):
        with patch.object(self.app, 'newest_release', return_value={'tag_name': 'v' + self.next_version}), \
             patch.object(self.app, 'bundle_updates', return_value=updates), \
             patch.object(updates, 'download', return_value={
                 name: (self.next_source() / name).read_bytes() for name in updates.FILES}), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(self.app.self_update())
        self.assertEqual(self.app.installed_version(self.app.executable_path()), self.next_version)

    def test_check_and_offline_failure_never_change_current(self):
        with patch.object(self.app, 'newest_release', return_value={'tag_name': 'v' + self.next_version}), \
             patch.object(updates, 'download') as download:
            self.assertFalse(self.update(check=True))
            download.assert_not_called()
        with patch.object(self.app, 'newest_release', side_effect=self.app.Error('offline')):
            with self.assertRaises(self.app.Error): self.update()
        self.assertEqual((self.root / 'current').resolve(), self.generation)

    def test_all_sessions_records_request_without_restarting_or_claiming_legacy_updated(self):
        target = self.app.read_json(self.generation / 'bundle.json')
        rows = [dict(pid=os.getpid(), session='current', live_runtime_abi=1, launcher_generation=target['engine']),
                dict(pid=os.getpid(), session='busy', live_runtime_abi=1, launcher_generation='old', phase='working'),
                dict(pid=os.getpid(), session='legacy'),
                dict(pid=os.getpid(), session='incompatible', live_runtime_abi=2)]
        with patch.object(self.app, 'active_runs', return_value=rows), patch.object(os, 'kill') as kill:
            status = updates.session_status(self.app, request=True)
            kill.assert_not_called()
        self.assertEqual([r['update'] for r in status['sessions']],
                         ['current', 'pending_safe_point', 'restart_required', 'incompatible_restart_required'])
        self.assertEqual(self.app.read_json(self.app.ROOT / 'session-updates.json')['generation'], target['engine'])

    def test_download_requires_whole_verified_archive(self):
        version = self.version
        name = 'codexx-' + version + '.tar.gz'
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode='w:gz') as archive:
            for filename, payload in self.payloads.items():
                member = tarfile.TarInfo('codexx-' + version + '/' + filename)
                member.size = len(payload)
                archive.addfile(member, io.BytesIO(payload))
        payload = output.getvalue()
        release = {'tag_name': 'v' + version, 'assets': [
            {'name': n, 'state': 'uploaded', 'browser_download_url':
             'https://github.com/' + self.app.RELEASE_REPO + '/releases/download/v' + version + '/' + n}
            for n in (name, name + '.sha256')]}
        checksum = (updates.digest(payload) + '  ' + name).encode()
        with patch.object(self.app, 'fetch_public', side_effect=[checksum, payload]):
            self.assertEqual(updates.download(self.app, release), self.payloads)
        with patch.object(self.app, 'fetch_public', side_effect=[checksum, payload + b'bad']):
            with self.assertRaises(self.app.Error): updates.download(self.app, release)


if __name__ == '__main__': unittest.main()
