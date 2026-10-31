"""Private or unreadable frontmatter must never leak through triage's digest."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from v3_package_helpers import inherited_env, install


class PrivateMetadataTriageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='beyin-triage-private-')
        root = Path(cls.tmp.name).resolve()
        cls.vault, cls.state, home = root / 'vault', root / 'state', root / 'home'
        cls.vault.mkdir()
        home.mkdir()
        cls.env = inherited_env(HOME=str(home), USERPROFILE=str(home), APPDATA=str(home / 'appdata'),
                                LOCALAPPDATA=str(home / 'localappdata'), BEYIN_V3_NO_SPAWN='1',
                                PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8')
        knowledge = cls.vault / 'knowledge'
        knowledge.mkdir()
        notes = {
            'normal-private.md': '---\n{"visibility": "private", "updated_at": "2026-10-25"}\n---\n# H03-NORMAL-PRIVATE\n',
            'bom-private.md': '\ufeff---\n{"visibility": "private", "updated_at": "2026-10-25"}\n---\n# H03-BOM-PRIVATE\n',
            'long-private.md': '---\n' + json.dumps({
                'title': 'H03-LONG-PRIVATE', 'filler': 'x' * 5000,
                'visibility': 'private', 'updated_at': '2026-10-25',
            }) + '\n---\nH03-LONG-BODY\n',
            'unterminated-private.md': '---\n{"visibility": "private", "updated_at": "2026-10-25"}\n# H03-UNTERMINATED-PRIVATE\n',
            'malformed-private.md': '---\n{"visibility": "private", broken}\n---\n# H03-MALFORMED-PRIVATE\n',
            'public.md': '---\n{"title": "H03-PUBLIC", "visibility": "internal", "updated_at": "2026-10-25"}\n---\nPublic body.\n',
        }
        for name, text in notes.items():
            (knowledge / name).write_bytes(text.encode('utf-8'))
        installed = install(cls.vault, cls.state, cls.env)
        assert installed.returncode == 0, installed.stderr

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def triage(self, output):
        done = subprocess.run([sys.executable, str(self.vault / 'beyin.py'), 'triage', output],
                              cwd=self.vault, env=self.env, capture_output=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr.decode('utf-8', errors='replace'))
        return done.stdout.decode('utf-8')

    def test_private_and_unreadable_metadata_never_reaches_digest_output(self):
        raw = self.triage('--json')
        report = json.loads(raw)
        self.assertEqual([item['path'] for item in report['digest']['items']], ['knowledge/public.md'])
        human = self.triage('--human')
        for marker in ('normal-private', 'bom-private', 'long-private', 'unterminated-private', 'malformed-private',
                       'H03-NORMAL-PRIVATE', 'H03-BOM-PRIVATE', 'H03-LONG-PRIVATE', 'H03-LONG-BODY',
                       'H03-UNTERMINATED-PRIVATE', 'H03-MALFORMED-PRIVATE'):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, raw)
                self.assertNotIn(marker, human)
        self.assertIn('H03-PUBLIC', raw)
        self.assertIn('H03-PUBLIC', human)

    def test_deep_json_private_metadata_is_skipped_and_digest_continues(self):
        knowledge = self.vault / 'knowledge'
        deep_value = '[' * 30000 + '0' + ']' * 30000
        (knowledge / 'deep-private.md').write_text(
            '---\n{"visibility": "private", "nested": ' + deep_value
            + ', "updated_at": "2026-10-25"}\n---\nH03-DEEP-PRIVATE-BODY\n',
            encoding='utf-8',
        )
        digest = json.loads(self.triage('--json'))['digest']
        self.assertEqual([item['path'] for item in digest['items']], ['knowledge/public.md'])
        self.assertNotIn('deep-private', json.dumps(digest))
        self.assertNotIn('H03-DEEP-PRIVATE-BODY', json.dumps(digest))

    def test_invalid_visibility_and_untrusted_status_or_kind_stay_out_of_digest(self):
        knowledge = self.vault / 'knowledge'
        notes = {
            'secret.md': '---\nvisibility: secret\n---\n# H03-SECRET\n',
            'restricted.md': '---\n{"visibility": "restricted", "updated_at": "2026-10-25"}\n---\n# H03-RESTRICTED\n',
            'list-private.md': '---\n{"visibility": ["private"], "updated_at": "2026-10-25"}\n---\n# H03-LIST-PRIVATE\n',
            'status-untrusted.md': '---\n{"status": "untrusted", "updated_at": "2026-10-25"}\n---\n# H03-STATUS-UNTRUSTED\n',
            'kind-untrusted.md': '---\n{"kind": "untrusted", "updated_at": "2026-10-25"}\n---\n# H03-KIND-UNTRUSTED\n',
            'default-visible.md': '---\n{"title": "H03-DEFAULT", "updated_at": "2026-10-25"}\n---\nDefault body.\n',
        }
        for name, body in notes.items():
            (knowledge / name).write_bytes(body.encode('utf-8'))
        raw = self.triage('--json')
        paths = [item['path'] for item in json.loads(raw)['digest']['items']]
        self.assertIn('knowledge/public.md', paths)
        self.assertIn('knowledge/default-visible.md', paths)
        for marker in ('secret.md', 'restricted.md', 'list-private.md', 'status-untrusted.md', 'kind-untrusted.md',
                       'H03-SECRET', 'H03-RESTRICTED', 'H03-LIST-PRIVATE', 'H03-STATUS-UNTRUSTED', 'H03-KIND-UNTRUSTED'):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, raw)
        for name in notes:
            (knowledge / name).unlink()

