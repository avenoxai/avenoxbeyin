"""Unicode format characters must not hide a secret the plain pattern already catches.

Synthetic secrets only. Arbitrary base64 stays undecoded; the existing Basic
auth check is unchanged. Custom literals ignore interior Cf characters unless
the stripped literal is shorter than 4 characters or contains the placeholder.
"""
import base64
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from v3_package_helpers import inherited_env, install

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
from beyin_v3_jev import _safe
from beyin_v3_preferences import save
from beyin_v3_secrets import redact
from beyin_v3_sync import SyncEngine

KEY = 'sk-' + 'A' * 40
ENCODED = base64.b64encode(KEY.encode()).decode('ascii')


class SecretVariantsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='beyin-secret-variants-')
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.vault = root / 'vault'
        self.vault.mkdir()
        self.state = root / 'state'
        self.engine = SyncEngine(self.vault, self.state)
        save(self.vault, {'secret_filter': True})

    def test_unicode_variants_mask_the_original_span(self):
        for number, character in enumerate(('\u200b', '\u200c', '\u200d', '\u2060',
                                             '\ufeff', '\u200e', '\u202e', '\u2066')):
            with self.subTest(character=ascii(character)):
                value = 'sk-' + character + 'A' * 40
                filtered, count = redact('Before (' + value + ') after.', self.state)
                self.assertEqual(count, 1)
                self.assertEqual(filtered, 'Before ([REDACTED]) after.')
                self.assertNotIn('A' * 20, filtered)
        inter = '\u200b'.join(KEY)
        filtered, count = redact(inter, self.state)
        self.assertEqual((filtered, count), ('[REDACTED]', 1))
        clean = 'Before\u200b (' + KEY + ') after\u2060.'
        self.assertEqual(redact(clean, self.state), ('Before\u200b ([REDACTED]) after\u2060.', 1))

    def test_base64_representations_are_not_decoded(self):
        self.assertEqual(redact(ENCODED, self.state), (ENCODED, 0))
        self.assertIsNone(_safe(self.engine.store, {'statement': ENCODED}))

    def test_basic_auth_still_redacts(self):
        token = base64.b64encode(b'user:secret').decode('ascii')
        text = 'Authorization: Basic ' + token
        filtered, count = redact(text, self.state)
        self.assertEqual(count, 1)
        self.assertNotIn(token, filtered)
        self.assertIn('[REDACTED]', filtered)

    def test_short_normalized_custom_literals_retain_only_raw_matches(self):
        self.state.mkdir(parents=True, exist_ok=True)
        clean = 'Bu not tamamen zararsiz bir metin; anahtar yok.'
        for literal in ('\ufeff\ufeff\ufeffa', '\ufeff' * 4, '\u200bab\u200b', 'a\u2060bc'):
            with self.subTest(literal=ascii(literal)):
                (self.state / 'secret-patterns.txt').write_text(literal + '\n', encoding='utf-8')
                self.assertEqual(redact(literal, self.state), ('[REDACTED]', 1))
                self.assertEqual(redact(clean, self.state), (clean, 0))
                self.assertIsNone(_safe(self.engine.store, {'statement': clean}))

    def test_placeholder_normalized_custom_literals_retain_only_raw_matches(self):
        self.state.mkdir(parents=True, exist_ok=True)
        for literal, clean in (('[RE\u200bDACTED]', 'Already [REDACTED].'),
                               ('prefix[RE\u200bDACTED]suffix', 'Already prefix[REDACTED]suffix.')):
            with self.subTest(literal=ascii(literal)):
                (self.state / 'secret-patterns.txt').write_text(literal + '\n', encoding='utf-8')
                self.assertEqual(redact(clean, self.state), (clean, 0))
                self.assertEqual(redact(literal, self.state), ('[REDACTED]', 1))

    def test_safe_custom_literal_normalization_preserves_surrounding_format(self):
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / 'secret-patterns.txt').write_text('my-\u200bsecret\n', encoding='utf-8')
        for value in ('my-secret', 'my-\u200bsecret', 'my-\u2060secret'):
            with self.subTest(value=ascii(value)):
                self.assertEqual(redact('Before\u200b (' + value + ') after\u2060.', self.state),
                                 ('Before\u200b ([REDACTED]) after\u2060.', 1))
                with self.assertRaisesRegex(ValueError, '^advisor_sensitive_input$'):
                    _safe(self.engine.store, {'statement': value})

    def test_note_create_masks_a_zero_width_key(self):
        value = 'sk-\u200b' + 'A' * 40
        result = self.engine.note_create('notes/variant.md', 'Before (' + value + ') after.')
        self.assertGreaterEqual(result['redacted'], 1)
        stored = (self.vault / result['source']).read_text(encoding='utf-8')
        self.assertNotIn(value, stored)
        self.assertNotIn('A' * 20, stored)
        self.assertIn('Before ([REDACTED]) after.', stored)

    def test_installed_cli_masks_unicode_and_leaves_base64_stored(self):
        home = Path(self.tmp.name) / 'home'
        home.mkdir()
        env = inherited_env(HOME=str(home), USERPROFILE=str(home), APPDATA=str(home / 'appdata'),
                            LOCALAPPDATA=str(home / 'localappdata'), BEYIN_V3_NO_SPAWN='1',
                            PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8')
        installed = install(self.vault, self.state, env)
        self.assertEqual(installed.returncode, 0, installed.stderr)
        for number, (value, expected) in enumerate((('sk-\u200b' + 'A' * 40, 1), (ENCODED, 0))):
            with self.subTest(number=number):
                done = subprocess.run([sys.executable, str(self.vault / 'beyin.py'), 'note-create', '--json'],
                                      input=json.dumps({'source': 'notes/cli-%d.md' % number, 'text': value}).encode(),
                                      cwd=self.vault, env=env, capture_output=True, timeout=60)
                self.assertEqual(done.returncode, 0, done.stderr.decode('utf-8', errors='replace'))
                result = json.loads(done.stdout)
                self.assertEqual(result['redacted'], expected)
                stored = (self.vault / result['source']).read_text(encoding='utf-8')
                if expected:
                    self.assertNotIn(value, stored)
                    self.assertIn('[REDACTED]', stored)
                else:
                    self.assertIn(value, stored)


if __name__ == '__main__':
    unittest.main()
