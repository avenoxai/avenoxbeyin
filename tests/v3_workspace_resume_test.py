"""Installed CLI acceptance for resuming work from a linked canonical note."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from urllib.parse import unquote

from v3_package_helpers import install, isolated_env
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'template/.claude/scripts'))
from beyin_v3 import MemoryStore, _delivery_view, workspace_read_grant
from beyin_v3_jev import remote_allowed


class WorkspaceResumeTest(unittest.TestCase):
    def test_linked_canonical_note_is_found_from_workspace_context(self):
        with tempfile.TemporaryDirectory(prefix='workspace-resume-') as tmp:
            base = Path(tmp).resolve()
            vault, state = base / 'vault', base / 'state'
            vault.mkdir()
            env = isolated_env(base / 'home')
            setup = install(vault, state, env)
            self.assertEqual(setup.returncode, 0, setup.stderr.decode(errors='replace'))

            document = vault / 'notes/belgeler/Pompa Bakım Formu.txt'
            document.parent.mkdir(parents=True)
            document.write_text('Pompa bakım formu: Conta ölçüsünü doğrula.\n', encoding='utf-8')
            other = vault / 'notes/diger/Pompa Bakım Formu.txt'
            other.parent.mkdir(parents=True)
            other.write_text('Farklı form.\n', encoding='utf-8')

            def cli(*args):
                run = subprocess.run([sys.executable, str(vault / 'beyin.py'),
                    '--vault', str(vault), '--state', str(state), '--json', *args],
                    env=env, capture_output=True, text=True, encoding='utf-8', timeout=40)
                self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
                return json.loads(run.stdout)

            for workspace, label, record_id, next_step in (
                ('bakim', 'Pompa bakımı', 'pompa-esas', 'Conta ölçüsünü doğrula'),
                ('sergi', 'Gece sergisi', 'sergi-esas', 'Işık provasını yap')):
                cli('workspace', 'provision', workspace, '--name', label,
                    '--kind', 'general', '--description', label + ' çalışması')
                source = f'notes/kayitlar/{record_id}.md'
                target = vault / source
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text('---\n' + json.dumps({'id': record_id,
                    'kind': 'note', 'project': 'arsiv', 'visibility': 'internal',
                    'facts': {'durum': 'açık'}, 'workspace_readers': [workspace]},
                    ensure_ascii=False) + '\n---\n# ' + label + '\n\n'
                    + 'Sonraki adım: ' + next_step + '\n'
                    + ('Belge: [Bakım formu](../belgeler/Pompa%20Bak%C4%B1m%20Formu.txt)\n'
                       if workspace == 'bakim' else ''), encoding='utf-8')
                homepage = vault / 'projects' / workspace / 'Ana Sayfa.md'
                homepage.write_text('# ' + label + '\n\nEsas kayıt: '
                    + f'[Çalışma notu](../../{source})\n', encoding='utf-8')
                self.assertEqual((homepage.parent / f'../../{source}').resolve(), target)

            cli('sync')
            failures = []
            for workspace, label, record_id, next_step in (
                ('bakim', 'Pompa bakımı', 'pompa-esas', 'Conta ölçüsünü doğrula'),
                ('sergi', 'Gece sergisi', 'sergi-esas', 'Işık provasını yap')):
                result = cli('context', f'{label}: Bu işe nereden devam ederim?',
                    '--project', workspace)
                records = result.get('records', [])
                ids = [item.get('id') for item in records]
                if record_id not in ids or not any(next_step in str(item) for item in records):
                    failures.append(f'{workspace}: expected={record_id!r}, got={ids!r}, '
                        f'next_step_found={any(next_step in str(item) for item in records)}')
                other_id = 'sergi-esas' if workspace == 'bakim' else 'pompa-esas'
                self.assertNotIn(other_id, ids)
            self.assertFalse(failures, '\n'.join(failures))
            natural_owner = cli('context', 'Pompa bakımı işine nereden devam ederim?')
            self.assertIn('pompa-esas', [item['id'] for item in natural_owner['records']])
            self.assertNotIn('sergi-esas', [item['id'] for item in natural_owner['records']])
            global_lookup = cli('context', 'Conta ölçüsünü doğrula')
            self.assertIn('pompa-esas', [item['id'] for item in global_lookup['records']])
            ambiguous = subprocess.run([sys.executable, str(vault / 'beyin.py'),
                '--vault', str(vault), '--state', str(state), '--json', 'context',
                'Pompa bakımı ve Gece sergisi ne durumda?'], env=env,
                capture_output=True, text=True, encoding='utf-8', timeout=40)
            self.assertNotEqual(ambiguous.returncode, 0)
            self.assertIn('multiple Workspaces', ambiguous.stdout + ambiguous.stderr)
            explicit = cli('context', 'Pompa bakımı ve Gece sergisi ne durumda?',
                '--project', 'bakim')
            self.assertNotIn('sergi-esas', [item['id'] for item in explicit['records']])
            request = base / 'cross-homepage.json'
            request.write_text(json.dumps({'source': 'projects/bakim/Ilerleme.md',
                'expected_sha256': None, 'title': 'Bakım ilerleme', 'project': 'bakim',
                'selections': [{'source': 'notes/kayitlar/pompa-esas.md',
                    'fields': [{'field': 'facts.durum', 'label': 'Durum',
                                'editable': True}]}]}, ensure_ascii=False), encoding='utf-8')
            denied = subprocess.run([sys.executable, str(vault / 'beyin.py'),
                '--vault', str(vault), '--state', str(state), '--json',
                'homepage-create', '--file', str(request)], env=env,
                capture_output=True, text=True, encoding='utf-8', timeout=40)
            self.assertNotEqual(denied.returncode, 0, denied.stdout + denied.stderr)
            self.assertFalse((vault / 'projects/bakim/Ilerleme.md').exists())
            # The retrieved canonical source carries an exact relative document
            # locator. A same-name file elsewhere must not be chosen by basename.
            canonical = next(row for row in cli('context',
                'Pompa bakımı conta ölçüsünü doğrula', '--project', 'bakim')['records']
                if row['id'] == 'pompa-esas')
            locator = '../belgeler/Pompa%20Bak%C4%B1m%20Formu.txt'
            self.assertIn(locator, canonical['text'])
            resolved = (vault / canonical['source']).parent / unquote(locator)
            self.assertEqual(resolved.resolve(strict=True), document)
            self.assertNotEqual(resolved.resolve(strict=True), other)
            old = (vault / canonical['source']).parent / '../belgeler/Eski Form.txt'
            self.assertFalse(old.exists())
            self.assertEqual(len(list(vault.rglob('Pompa Bakım Formu.txt'))), 2)

            if os.environ.get('WORKSPACE_RESUME_OUTPUT'):
                output = Path(os.environ['WORKSPACE_RESUME_OUTPUT'])
                output.mkdir(parents=True, exist_ok=True)
                shutil.copytree(vault, output / 'vault', dirs_exist_ok=True)
                shutil.copytree(state, output / 'state', dirs_exist_ok=True)
                (output / 'acceptance.json').write_text(json.dumps({
                    'bakim': {'id': 'pompa-esas', 'next_step': 'Conta ölçüsünü doğrula'},
                    'sergi': {'id': 'sergi-esas', 'next_step': 'Işık provasını yap'},
                }, ensure_ascii=False, indent=2), encoding='utf-8')

            # A third Workspace and a homepage link do not grant access by themselves.
            cli('workspace', 'provision', 'izleyici', '--name', 'Gözlem Panosu', '--kind', 'general')
            viewer_home = vault / 'projects/izleyici/Ana Sayfa.md'
            viewer_home.write_text('# İzleyici\n\n[Pompa](../../notes/kayitlar/pompa-esas.md)\n',
                encoding='utf-8')
            cli('sync')
            viewer = cli('context', 'Pompa bakımı conta ölçüsünü doğrula', '--project', 'izleyici')
            self.assertNotIn('pompa-esas', [item['id'] for item in viewer['records']])
            natural_viewer = cli('context', 'Gözlem Panosu’nda pompa bakımına nereden devam ederim?')
            self.assertNotIn('pompa-esas', [item['id'] for item in natural_viewer['records']])
            for short_id, short_label, question in (
                ('ai', 'AI', 'AI alanında pompa bakımına nereden devam ederim?'),
                ('ik', 'İK', 'İK’da pompa bakımına nereden devam ederim?'),
                ('x', 'X', 'X alanında pompa bakımına nereden devam ederim?')):
                cli('workspace', 'provision', short_id, '--name', short_label,
                    '--kind', 'general')
                short_name = cli('context', question)
                self.assertNotIn('pompa-esas', [item['id'] for item in short_name['records']])
            request = base / 'context-file.json'
            for file_project in (None, 'sergi'):
                request.write_text(json.dumps({'query': 'Pompa bakımı ve Gece sergisi ne durumda?',
                    'project': file_project}, ensure_ascii=False), encoding='utf-8')
                from_file = cli('context', '--file', str(request), '--project', 'bakim')
                self.assertNotIn('sergi-esas', [item['id'] for item in from_file['records']])

            # A granted record stops appearing as soon as its actual source narrows.
            source = vault / 'notes/kayitlar/pompa-esas.md'
            original = source.read_text(encoding='utf-8')
            source.write_text(original.replace('"workspace_readers": ["bakim"]',
                '"workspace_readers": []'), encoding='utf-8')
            stale = cli('context', 'Pompa bakımı conta ölçüsünü doğrula',
                '--project', 'bakim', '--no-sync')
            self.assertNotIn('pompa-esas', [item['id'] for item in stale['records']])
            cli('sync')
            revoked = cli('context', 'Pompa bakımı conta ölçüsünü doğrula', '--project', 'bakim')
            self.assertNotIn('pompa-esas', [item['id'] for item in revoked['records']])

            # The same read membership never overrides private or untrusted policy.
            source.write_text(original.replace('"visibility": "internal"',
                '"visibility": "private"'), encoding='utf-8')
            cli('sync')
            private = cli('context', 'Pompa bakımı conta ölçüsünü doğrula', '--project', 'bakim')
            self.assertNotIn('pompa-esas', [item['id'] for item in private['records']])
            store = MemoryStore(state, vault, read_only=True)
            self.assertNotIn('pompa-esas', [item['id'] for item in
                store._eligible('private', 'bakim')[0]])
            self.assertIn('pompa-esas', [item['id'] for item in
                store._eligible('private', 'arsiv')[0]])
            source.write_text(original.replace('"kind": "note"',
                '"kind": "note", "trust": "untrusted"'), encoding='utf-8')
            cli('sync')
            untrusted = cli('context', 'Pompa bakımı conta ölçüsünü doğrula', '--project', 'bakim')
            self.assertNotIn('pompa-esas', [item['id'] for item in untrusted['records']])

            # An invalid scope boundary blocks a cross-Workspace grant even when the
            # source explicitly names another project.
            source.write_text(original, encoding='utf-8')
            scopes_file = vault / '.beyin-projects.json'
            scopes = json.loads(scopes_file.read_text(encoding='utf-8'))
            scopes['folders']['notes/kayitlar'] = ''
            scopes_file.write_text(json.dumps(scopes, ensure_ascii=False), encoding='utf-8')
            cli('sync')
            blocked = cli('context', 'Pompa bakımı conta ölçüsünü doğrula', '--project', 'bakim')
            self.assertNotIn('pompa-esas', [item['id'] for item in blocked['records']])

    def test_malformed_membership_and_delivery_boundaries(self):
        base = {'source': 'notes/example.md', 'project': 'owner',
                'workspace_readers': ['reader', 'reader']}
        self.assertFalse(workspace_read_grant(base, 'reader', None))
        for readers in (['reader', 17], ['reader', ''], 'reader', ['reader', 'reader']):
            self.assertFalse(workspace_read_grant(dict(base, workspace_readers=readers),
                                                  'reader', None))
        self.assertTrue(workspace_read_grant(dict(base, workspace_readers=['reader']),
                                             'reader', None))
        self.assertNotIn('workspace_readers', _delivery_view(base))
        self.assertFalse(remote_allowed(dict(base, remote_allowed=False)))
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / 'vault'
            vault.mkdir()
            source = vault / 'notes/example.md'
            source.parent.mkdir()
            source.write_text('# Example\n', encoding='utf-8')
            store = MemoryStore(Path(tmp) / 'state', vault)
            for readers in (['reader', 'reader'], ['reader', 17], 'reader'):
                with self.assertRaises(ValueError):
                    store.ingest({'id': 'example', 'text': 'Example',
                        'source': 'notes/example.md', 'workspace_readers': readers})
            self.assertEqual(store._records(), [])
            scopes = vault / '.beyin-projects.json'
            scopes.write_text(json.dumps({'folders': {}, 'shared_unscoped': True}),
                              encoding='utf-8')
            store.ingest({'id': 'shared-private', 'text': 'shared private calibration',
                          'source': 'notes/example.md', 'visibility': 'private'})
            self.assertIn('shared-private', [row['id'] for row in
                store._eligible('private', 'demo')[0]])

    def test_malformed_registry_never_falls_back_to_global_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            vault, state = base / 'vault', base / 'state'
            vault.mkdir()
            env = isolated_env(base / 'home')
            installed = install(vault, state, env)
            self.assertEqual(installed.returncode, 0)
            initialized = subprocess.run([sys.executable, str(vault / 'beyin.py'),
                '--vault', str(vault), '--state', str(state), '--json', 'sync'],
                env=env, capture_output=True, text=True, encoding='utf-8', timeout=40)
            self.assertEqual(initialized.returncode, 0, initialized.stdout + initialized.stderr)
            (vault / '.beyin-workspaces.json').write_text('{broken', encoding='utf-8')
            run = subprocess.run([sys.executable, str(vault / 'beyin.py'),
                '--vault', str(vault), '--state', str(state), '--json',
                'context', 'Gözlem Panosu’nda pompa bakımına devam', '--no-sync'],
                env=env, capture_output=True, text=True, encoding='utf-8', timeout=40)
            self.assertNotEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertIn('workspace registry', run.stdout + run.stderr)


if __name__ == '__main__':
    unittest.main()
