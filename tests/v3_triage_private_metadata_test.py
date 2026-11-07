"""Private or unreadable frontmatter must never leak through triage's digest."""
from datetime import datetime, timedelta, timezone
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
        # UTC yesterday stays inside the seven-day digest window in every local zone.
        # Use it for private fixtures too: age must never mask a privacy regression.
        cls.recent_date = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
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
            'normal-private.md': '---\n{"visibility": "private", "updated_at": "%s"}\n---\n# H03-NORMAL-PRIVATE\n' % cls.recent_date,
            'bom-private.md': '\ufeff---\n{"visibility": "private", "updated_at": "%s"}\n---\n# H03-BOM-PRIVATE\n' % cls.recent_date,
            'long-private.md': '---\n' + json.dumps({
                'title': 'H03-LONG-PRIVATE', 'filler': 'x' * 5000,
                'visibility': 'private', 'updated_at': cls.recent_date,
            }) + '\n---\nH03-LONG-BODY\n',
            'unterminated-private.md': '---\n{"visibility": "private", "updated_at": "%s"}\n# H03-UNTERMINATED-PRIVATE\n' % cls.recent_date,
            'malformed-private.md': '---\n{"visibility": "private", broken}\n---\n# H03-MALFORMED-PRIVATE\n',
            'public.md': '---\n{"title": "H03-PUBLIC", "visibility": "internal", "updated_at": "%s"}\n---\nPublic body.\n' % cls.recent_date,
        }
        for name, text in notes.items():
            (knowledge / name).write_bytes(text.encode('utf-8'))
        installed = install(cls.vault, cls.state, cls.env)
        assert installed.returncode == 0, installed.stderr

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def triage(self, output):
        before = {path: path.read_bytes() for path in (self.vault / 'knowledge').glob('*.md')}
        done = subprocess.run([sys.executable, str(self.vault / 'beyin.py'), 'triage', output],
                              cwd=self.vault, env=self.env, capture_output=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr.decode('utf-8', errors='replace'))
        self.assertEqual(before, {path: path.read_bytes() for path in before})
        return done.stdout.decode('utf-8')

    def test_private_and_unreadable_metadata_never_reaches_digest_output(self):
        raw = self.triage('--json')
        report = json.loads(raw)
        self.assertEqual([item['path'] for item in report['digest']['items']], ['knowledge/public.md'])
        self.assertEqual(report['digest']['items'][0]['date_source'], 'updated')
        self.assertGreaterEqual(report['digest']['items'][0]['age_days'], 0)
        self.assertLess(report['digest']['items'][0]['age_days'], 3)
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
            + ', "updated_at": "%s"}\n---\nH03-DEEP-PRIVATE-BODY\n' % self.recent_date,
            encoding='utf-8',
        )
        digest = json.loads(self.triage('--json'))['digest']
        self.assertEqual([item['path'] for item in digest['items']], ['knowledge/public.md'])
        self.assertNotIn('deep-private', json.dumps(digest))
        self.assertNotIn('H03-DEEP-PRIVATE-BODY', json.dumps(digest))

    def test_header_limit_is_fail_closed_without_hiding_plain_body(self):
        knowledge = self.vault / 'knowledge'
        limit = 1024 * 1024
        notes = {
            'bound-private.md': '---\n' + json.dumps({'pad': 'x' * (limit - 100), 'visibility': 'private'}) + '\n---\n# PRIVATE_BOUND_CANARY',
            'bound-over-public.md': '---\n' + json.dumps({'pad': 'x' * limit, 'visibility': 'public'}) + '\n---\n# OVER_BOUND_CANARY',
            'bound-public.md': '---\nvisibility: public\n---\n# PUBLIC_BOUND_CONTROL\n' + 'x' * (limit + 100),
            'bound-plain.md': '# PLAIN_BOUND_CONTROL\n' + 'x' * (limit + 100),
        }
        try:
            for name, text in notes.items():
                (knowledge / name).write_bytes(text.encode('utf-8'))
            before = {name: (knowledge / name).read_bytes() for name in notes}
            raw = self.triage('--json')
            self.assertNotIn('bound-private.md', raw)
            self.assertNotIn('bound-over-public.md', raw)
            self.assertIn('PUBLIC_BOUND_CONTROL', raw)
            self.assertIn('PLAIN_BOUND_CONTROL', raw)
            self.assertEqual(before, {name: (knowledge / name).read_bytes() for name in notes})
        finally:
            for name in notes:
                (knowledge / name).unlink()

    def test_unicode_line_boundaries_keep_private_headers_out(self):
        knowledge = self.vault / 'knowledge'
        created = []
        try:
            for index, separator in enumerate(('\n', '\r', '\r\n', '\x85', '\u2028', '\u2029', '\v', '\f', '\x1c', '\x1d', '\x1e')):
                path = knowledge / ('boundary-private-' + str(index) + '.md')
                path.write_bytes(separator.join(('---', 'visibility: private', '---',
                                                 '# PRIVATE_BOUNDARY_CANARY')).encode('utf-8'))
                created.append(path)
            before = {path: path.read_bytes() for path in created}
            for output in ('--json', '--human'):
                raw = self.triage(output)
                self.assertNotIn('boundary-private-', raw)
                self.assertNotIn('PRIVATE_BOUNDARY_CANARY', raw)
                self.assertIn('H03-PUBLIC', raw)
            self.assertEqual(before, {path: path.read_bytes() for path in created})
        finally:
            for path in created:
                path.unlink()

    def test_invalid_visibility_and_untrusted_status_or_kind_stay_out_of_digest(self):
        knowledge = self.vault / 'knowledge'
        notes = {
            'secret.md': '---\nvisibility: secret\n---\n# H03-SECRET\n',
            'restricted.md': '---\n{"visibility": "restricted", "updated_at": "%s"}\n---\n# H03-RESTRICTED\n' % self.recent_date,
            'list-private.md': '---\n{"visibility": ["private"], "updated_at": "%s"}\n---\n# H03-LIST-PRIVATE\n' % self.recent_date,
            'status-untrusted.md': '---\n{"status": "untrusted", "updated_at": "%s"}\n---\n# H03-STATUS-UNTRUSTED\n' % self.recent_date,
            'kind-untrusted.md': '---\n{"kind": "untrusted", "updated_at": "%s"}\n---\n# H03-KIND-UNTRUSTED\n' % self.recent_date,
            'default-visible.md': '---\n{"title": "H03-DEFAULT", "updated_at": "%s"}\n---\nDefault body.\n' % self.recent_date,
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

