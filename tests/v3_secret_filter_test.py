"""Opt-in write-path secret filtering; all fixtures are synthetic."""
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'template/.claude/scripts'))
from beyin_v3_preferences import save
from beyin_v3_secrets import redact
from beyin_v3_sync import SyncEngine


class SecretFilterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.vault = root/'vault'; self.vault.mkdir()
        self.state = root/'state'
        self.engine = SyncEngine(self.vault, self.state)
        self.token = 'sk-' + ('SyntheticOnly' * 3)

    def test_default_off_preserves_text(self):
        result = self.engine.note_create('notes/default.md', 'Demo '+self.token)
        self.assertEqual(result['secrets_redacted'], 0)
        self.assertIn(self.token, (self.vault/'notes/default.md').read_text())

    def test_enabled_filter_redacts_all_three_write_paths_and_reports_health(self):
        save(self.vault, {'secret_filter': True})
        reference = self.vault/'reference.md'; reference.write_text('Synthetic reference.\n')
        self.engine.sync()
        note = self.engine.note_create('notes/filtered.md', 'Demo '+self.token)
        task = self.engine.task_create('tasks/filtered.md', 'Bearer '+'x'*24,
            {'id':'filtered-task','title':'Synthetic','status':'active','owner':'Synthetic Owner'})
        receipt = self.engine.receipt('filtered-event', 'api_key='+self.token,
                                      ['reference.md'], 'codex')
        for result in (note, task, receipt):
            self.assertGreaterEqual(result['redacted'], 1)
            self.assertNotIn(self.token, (self.vault/result['source']).read_text())
            self.assertIn('[REDACTED]', (self.vault/result['source']).read_text())
        doctor = subprocess.run([sys.executable, str(ROOT/'scripts/beyin_v3.py'), '--vault', str(self.vault),
                                 '--state', str(self.state), 'doctor'], capture_output=True, text=True)
        self.assertEqual(doctor.returncode, 0, doctor.stderr)
        self.assertGreaterEqual(json.loads(doctor.stdout)['secret_filter']['total'], 3)
        from beyin_v3_hook import drain_queue
        drain_queue(self.vault, self.state)
        hook_health = json.loads((self.state/'hook-health.json').read_text(encoding='utf-8'))
        self.assertGreaterEqual(hook_health['sync']['secrets_redacted'], 3)

    def test_builtin_patterns_cover_documented_token_families(self):
        values = [
            'ghp_' + 'A'*24,
            'github_pat_' + 'B'*24,
            'sk-proj-' + 'A'*24,
            'sk-admin-' + 'B'*24,
            'hf_' + 'D'*24,
            'ASIA' + 'E'*16,
            '-----BEGIN CERTIFICATE-----\nABC\n-----END CERTIFICATE-----',
            '-----BEGIN CERTIFICATE-----\r\nDEF\r\n-----END CERTIFICATE-----',
            'sk-' + 'C'*24,
            'AKIA' + 'D'*16,
            'Bearer ' + 'e'*24,
            'password=synthetic-password',
            '-----BEGIN PRIVATE KEY-----\nSYNTHETIC\n-----END PRIVATE KEY-----',
        ]
        filtered, count = redact('\n'.join(values), self.state)
        self.assertEqual(count, len(values))
        self.assertNotIn('SYNTHETIC', filtered)
        self.assertEqual(filtered.count('[REDACTED]'), len(values))
        clean = 'Temiz bir Turkce ve English sentence; anahtar degeri icermiyor.'
        self.assertEqual(redact(clean, self.state), (clean, 0))

    def test_provider_token_formats_are_redacted(self):
        """#151: provider formats the dashed sk- pattern never saw. Repeated characters
        keep every fixture structurally valid and impossible as a real credential."""
        values = [
            'sk_live_' + '1'*24,
            'sk_test_' + '2'*24,
            'rk_live_' + '3'*24,
            'xoxb-' + '3'*12 + '-' + '4'*13 + '-' + 'a'*24,
            'xoxp-' + '3'*12 + '-' + '4'*12 + '-' + '5'*12 + '-' + 'a'*24,
            'xapp-1-' + 'A'*11 + '-' + '6'*13 + '-' + 'b'*64,
            'AIza' + 'C'*35,
            'AIza' + 'C'*33 + '-_',
            'npm_' + 'D'*36,
            'SG.' + 'E'*22 + '.' + 'F'*43,
            'eyJhbGciOiJIUzI1NiJ9.' + 'G'*24 + '.' + 'H'*24,
            'eyJhbGciOiJSU0EtT0FFUCJ9.' + 'I'*20 + '.' + 'J'*16 + '.' + 'K'*30 + '.' + 'L'*22,
            'M' + 'T'*25 + '.' + 'G'*6 + '.' + 'x'*38,
            'N' + 'z'*23 + '.' + 'Y'*6 + '.' + 'q'*27,
        ]
        for value in values:
            with self.subTest(value=value[:12]):
                for text in (value, f'Anahtar: {value} (sentetik).', f'"{value}"', f'key={value}&x=1'):
                    filtered, count = redact(text, self.state)
                    self.assertGreaterEqual(count, 1)
                    self.assertNotIn(value, filtered)
                    self.assertNotIn(value[8:], filtered)

    def test_provider_patterns_leave_ordinary_text_and_identifiers_alone(self):
        png = ('data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk'
               'YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==')
        clean = [
            'Bugün Şifre ekranını düzelttik; Müşteri Arşiv klasörü yerinde kaldı.',
            'We shipped the release notes and moved the sk_live rotation doc to the wiki.',
            'commit 1f0e9d3c2b4a5968778695a4b3c2d1e0f9a8b7c6 fixed the updater',
            'id 123e4567-e89b-12d3-a456-426614174000 and 00000000-0000-0000-0000-000000000000',
            png,
            'eyJhbGciOiJIUzI1NiJ9 alone is only a header, not a token',
            '[Kurulum rehberi](https://github.com/avenoxai/avenoxbeyin/blob/main/docs/v3/INSTALL.md#windows-kurulumu)',
            '![diagram](assets/Mxxxxxxxxxxxxxxxxxxxxxxxxx.abcdef.png)',
            'com.Mycompanyinternalservicesxx.abcdef.somethingveryverylongidentifier.Main',
            'npm_config_registry=https://registry.npmjs.org/ and npm_package_version',
            'task_live_rotation_helper_function and desk_test_fixture_loader_value',
            'xoxb- tokens are Slack bot tokens; AIza keys belong to Google; SG. keys to SendGrid.',
            'MNOPQRSTUVWXYZabcdefghijkl.mnopqr is a sentence fragment, not a token.',
        ]
        for text in clean:
            with self.subTest(text=text[:30]):
                self.assertEqual(redact(text, self.state), (text, 0))

    def test_enabled_filter_keeps_a_stripe_key_out_of_receipt_and_index(self):
        """#151 end to end: the key reached both the receipt file and memory.sqlite3."""
        save(self.vault, {'secret_filter': True})
        reference = self.vault/'reference.md'; reference.write_text('Synthetic reference.\n')
        self.engine.sync()
        key = 'sk_live_' + '1'*24
        result = self.engine.receipt('stripe-event', 'Rotated '+key+' today.', ['reference.md'], 'codex')
        self.assertGreaterEqual(result['redacted'], 1)
        self.engine.sync()
        self.assertNotIn(key, (self.vault/result['source']).read_text(encoding='utf-8'))
        for database in self.state.rglob('*.sqlite3*'):
            self.assertNotIn(key.encode(), database.read_bytes(), database.name)

    def test_custom_state_literal_is_redacted_and_never_written_to_health(self):
        save(self.vault, {'secret_filter': True})
        self.state.mkdir(parents=True, exist_ok=True)
        literal = 'CUSTOM-SYNTHETIC-CANARY'
        (self.state/'secret-patterns.txt').write_text(literal+'\n', encoding='utf-8')
        result = self.engine.note_create('knowledge/custom.md', 'Value '+literal)
        self.assertEqual(result['secrets_redacted'], 1)
        self.assertNotIn(literal, (self.vault/'knowledge/custom.md').read_text())
        database = (self.state/'secret-filter.sqlite3').read_bytes()
        self.assertNotIn(literal.encode(), database)

    def test_quoted_json_secrets_are_redacted_and_natural_text_preserved(self):
        """JSON credentials must be redacted while natural text phrases with spaces are not."""
        fixtures = [
            ('{"api_key": "synthetic_key_123456"}', 'synthetic_key_123456'),
            ('{"access_token": "synthetic_token_123"}', 'synthetic_token_123'),
            ('{"token": "synthetic_token_val_123"}', 'synthetic_token_val_123'),
            ('{"secret": "synthetic_secret_val_123"}', 'synthetic_secret_val_123'),
            ('{"password": "synthetic_password_123"}', 'synthetic_password_123'),
            ('{"passwd": "synthetic_passwd_val_123"}', 'synthetic_passwd_val_123'),
            ('{\'api_key\': \'synthetic_key_123456\'}', 'synthetic_key_123456'),
            ('api_key: "synthetic_key_123456"', 'synthetic_key_123456'),
        ]
        for text, secret in fixtures:
            with self.subTest(text=text):
                filtered, count = redact(text, self.state)
                self.assertGreaterEqual(count, 1)
                self.assertNotIn(secret, filtered)
                self.assertIn('[REDACTED]', filtered)

        natural_phrases = [
            'password: "must be rotated every month"',
            '{"password": "must be rotated every month"}',
            'secret: "follow the instructions carefully"',
        ]
        for phrase in natural_phrases:
            with self.subTest(phrase=phrase):
                filtered, count = redact(phrase, self.state)
                self.assertEqual(count, 0)
                self.assertEqual(filtered, phrase)

    def test_url_query_parameter_delimiter(self):
        """URL query parameter secret redaction must stop at & delimiter."""
        url = "https://api.example.com/v1?api_key=synthetic_secret_token_123&format=json"
        filtered, count = redact(url, self.state)
        self.assertGreaterEqual(count, 1)
        self.assertNotIn("synthetic_secret_token_123", filtered)
        self.assertIn("&format=json", filtered)

    def test_unquoted_secrets_capture_special_characters(self):
        """Unquoted credential values must capture &, ), }, ] rather than splitting or leaking."""
        cases = [
            ('password=abc&defghijk', 'abc&defghijk', '[REDACTED]'),
            ('password=p@ss)word123', 'p@ss)word123', '[REDACTED]'),
            ('token=abcdefgh}ijklmn', 'abcdefgh}ijklmn', '[REDACTED]'),
        ]
        for text, secret, expected in cases:
            with self.subTest(text=text):
                filtered, count = redact(text, self.state)
                self.assertGreaterEqual(count, 1)
                self.assertNotIn(secret, filtered)
                self.assertEqual(filtered, expected)

    def test_database_uri_credentials_redaction(self):
        """Database connection strings with auth must have their credentials redacted, including @ in passwords."""
        fixtures = [
            ('postgres://user:password123@localhost:5432/mydb', 'password123', 'postgres://[REDACTED]@localhost:5432/mydb'),
            ('postgres://user:p@ss@host:5432/mydb', 'p@ss', 'postgres://[REDACTED]@host:5432/mydb'),
            ('postgresql://admin:secret_pass@db.internal:5432/app', 'secret_pass', 'postgresql://[REDACTED]@db.internal:5432/app'),
            ('mysql://root:supersecret@127.0.0.1:3306/db', 'supersecret', 'mysql://[REDACTED]@127.0.0.1:3306/db'),
            ('mongodb+srv://appuser:clusterpass99@cluster0.mongodb.net/test', 'clusterpass99', 'mongodb+srv://[REDACTED]@cluster0.mongodb.net/test'),
            ('redis://:myredispass123@cache.host:6379/0', 'myredispass123', 'redis://[REDACTED]@cache.host:6379/0'),
            ('redis://appuser:myredispass123@cache.host:6379/0', 'myredispass123', 'redis://[REDACTED]@cache.host:6379/0'),
            ('amqp://guest:secretguest@localhost:5672/', 'secretguest', 'amqp://[REDACTED]@localhost:5672/'),
        ]
        for url, secret, expected in fixtures:
            with self.subTest(url=url):
                filtered, count = redact(url, self.state)
                self.assertGreaterEqual(count, 1, f"URI auth not redacted in: {url}")
                self.assertNotIn(secret, filtered)
                self.assertEqual(filtered, expected)

        clean_urls = [
            'https://normal-domain.com/path?query=value',
            'http://localhost:8080/health',
            'postgres://localhost:5432/mydb',
        ]
        for url in clean_urls:
            with self.subTest(clean_url=url):
                filtered, count = redact(url, self.state)
                self.assertEqual(count, 0)
                self.assertEqual(filtered, url)

    def test_metadata_redaction_scope_and_key_aware_facts(self):
        """Free-text metadata fields and key-aware facts must be redacted without mutating structural fields like task id and supersedes."""
        save(self.vault, {'secret_filter': True})

        # 1. task-create with task ID starting with sk- and facts with password key
        task_meta = {
            'id': 'sk-quarterly-planning-review',
            'title': 'Setup postgres://admin:dbpass123@host:5432/db',
            'status': 'active',
            'owner': 'lead-dev',
            'next_action': 'token=mysecrettokenval123',
            'facts': {'password': 'hunter2hunter2', 'environment': 'production'},
        }
        res = self.engine.task_create('tasks/plan.md', 'Clean task body.', metadata=task_meta)
        self.assertGreaterEqual(res.get('secrets_redacted', 0), 3)

        # Structural id must be preserved exactly, NOT redacted to [REDACTED]
        self.assertEqual(res['id'], 'sk-quarterly-planning-review')
        content = (self.vault / 'tasks/plan.md').read_text(encoding='utf-8')
        self.assertNotIn('dbpass123', content)
        self.assertNotIn('mysecrettokenval123', content)
        self.assertNotIn('hunter2hunter2', content)
        self.assertIn('sk-quarterly-planning-review', content)

        # 2. A second task with an sk- id must succeed without id collision
        task2_meta = {
            'id': 'sk-second-planning-review',
            'title': 'Second task',
            'status': 'active',
            'owner': 'lead-dev',
        }
        res2 = self.engine.task_create('tasks/plan2.md', 'Second body.', metadata=task2_meta)
        self.assertEqual(res2['id'], 'sk-second-planning-review')

        # 3. note_create with supersedes containing sk- id
        note_meta = {
            'id': 'sk-note-record',
            'title': 'Note superseding sk-quarterly-planning-review',
            'supersedes': ['sk-quarterly-planning-review'],
        }
        note_res = self.engine.note_create('notes/superseding.md', 'Note text.', metadata=note_meta)
        self.assertEqual(note_res['status'], 'succeeded')
        note_content = (self.vault / 'notes/superseding.md').read_text(encoding='utf-8')
        self.assertIn('sk-quarterly-planning-review', note_content)
        self.assertIn('sk-note-record', note_content)

        # 4. update_task changes redaction
        updated = self.engine.update_task('sk-quarterly-planning-review', 1, {
            'next_action': 'password=updatedsecretpass123',
            'facts': {'api_key': 'newkeytokenval123'},
            'supersedes': ['sk-second-planning-review'],
        })
        self.assertGreaterEqual(updated.get('secrets_redacted', 0), 2)
        content_up = (self.vault / 'tasks/plan.md').read_text(encoding='utf-8')
        self.assertNotIn('updatedsecretpass123', content_up)
        self.assertNotIn('newkeytokenval123', content_up)
        self.assertIn('sk-second-planning-review', content_up)

    def test_jev_safe_blocks_cards_with_json_secrets(self):
        """Jev _safe must raise ValueError('advisor_sensitive_input') when card payload contains JSON secrets."""
        from beyin_v3 import MemoryStore
        from beyin_v3_jev import _safe
        store = MemoryStore(self.state, self.vault)
        card_with_secret = [
            "search query",
            [
                {
                    "id": "card-1",
                    "title": "Secret Note",
                    "statement": json.dumps({"text": 'config: {"api_key": "synthetic_key_123456"}'}),
                    "scope": "project:test",
                    "domains": ["note"],
                }
            ],
        ]
        with self.assertRaises(ValueError) as cm:
            _safe(store, card_with_secret)
        self.assertEqual(str(cm.exception), 'advisor_sensitive_input')

    def test_quoted_values_with_quotes_or_backslashes_stay_redacted(self):
        """Main redacted these through the generic value run; keep parity for quoted values."""
        for text, secret in QUOTED_REGRESSIONS:
            with self.subTest(text=text):
                filtered, count = redact(text, self.state)
                self.assertGreaterEqual(count, 1)
                self.assertNotIn(secret, filtered)
                self.assertNotIn(_tail(secret), filtered)

    def test_planted_quoted_secrets_never_reach_note_or_task_files(self):
        save(self.vault, {'secret_filter': True})
        for number, (text, secret) in enumerate(QUOTED_REGRESSIONS):
            with self.subTest(text=text):
                note = self.engine.note_create(f'notes/planted-{number}.md', 'Body ' + text,
                                               {'title': 'Note ' + text})
                task = self.engine.task_create(f'tasks/planted-{number}.md', 'Body.', {
                    'id': f'planted-{number}', 'title': 'Task', 'status': 'active',
                    'owner': 'me', 'next_action': text})
                self.assertGreaterEqual(note['secrets_redacted'], 2)
                self.assertGreaterEqual(task['secrets_redacted'], 1)
                for result in (note, task):
                    content = (self.vault/result['source']).read_text(encoding='utf-8')
                    self.assertNotIn(_tail(secret), content)

    def test_key_aware_facts_cover_non_ascii_and_escaped_values(self):
        save(self.vault, {'secret_filter': True})
        facts = {'password': 'şifre12345', 'token': "Xk9'mQ2!vL", 'secret': 'abc\\defghij',
                 'passwd': 'hunter2hunter2', 'api_key': [['güvenlik2026']], 'environment': 'production'}
        result = self.engine.task_create('tasks/tr.md', 'Body.', {
            'id': 'tr-task', 'title': 'TR', 'status': 'active', 'owner': 'me', 'facts': facts})
        self.assertEqual(result['facts'], dict({key: '[REDACTED]' for key in facts},
                                               api_key=[['[REDACTED]']], environment='production'))
        self.assertGreaterEqual(result['secrets_redacted'], 5)
        content = (self.vault/'tasks/tr.md').read_text(encoding='utf-8')
        for fragment in ('ifre12345', 'mQ2!vL', 'defghij', 'hunter2hunter2', 'venlik2026'):
            self.assertNotIn(fragment, content)
        self.assertIn('production', content)
        updated = self.engine.update_task('tr-task', 1, {
            'facts': {'password': 'güçlüParola99', 'token': "it'sasecret1"},
            'next_action': 'password: "Pa\\"ssword123"'})
        self.assertEqual(updated['facts'], {'password': '[REDACTED]', 'token': '[REDACTED]'})
        self.assertGreaterEqual(updated['secrets_redacted'], 3)
        content = (self.vault/'tasks/tr.md').read_text(encoding='utf-8')
        for fragment in ('Parola99', 'sasecret1', 'ssword123'):
            self.assertNotIn(fragment, content)

    def test_completion_criterion_is_redacted_like_other_free_text_metadata(self):
        save(self.vault, {'secret_filter': True})
        created = self.engine.task_create('tasks/strict.md', 'Body.', {
            'id': 'strict-task', 'title': 'Strict', 'status': 'active', 'owner': 'Synthetic Owner',
            'completion_contract': 'strict', 'completion_criterion': 'Deploy with token=abcdefghijkl'})
        self.assertEqual(created['completion_criterion'], 'Deploy with [REDACTED]')
        updated = self.engine.update_task('strict-task', 1, {'completion_criterion': 'Rotate '+self.token})
        self.assertGreaterEqual(updated['secrets_redacted'], 1)
        self.assertEqual(updated['completion_criterion'], 'Rotate [REDACTED]')
        content = (self.vault/'tasks/strict.md').read_text(encoding='utf-8')
        self.assertNotIn('abcdefghijkl', content)
        self.assertNotIn(self.token, content)

    def test_redos_limits(self):
        """Verify that extremely long repetitive inputs do not trigger ReDoS."""
        import time
        from beyin_v3 import REJECTED_AT

        # 1. Assignment ReDoS
        text = 'password' + '=' + ' ' * 100000 + '"' + 'A'*7
        t0 = time.time()
        redact(text, self.state)
        self.assertLess(time.time() - t0, 1.0)

        # 2. CERT ReDoS
        text = "-----BEGIN CERTIFICATE-----\n" + "A" * 100000 + "\n-----END CERTIFICATE-----"
        t0 = time.time()
        redact(text, self.state)
        self.assertLess(time.time() - t0, 1.0)

        # 3. REJECTED_AT ReDoS
        text = "2023-01-01T" + "1" * 100000
        t0 = time.time()
        REJECTED_AT.match(text)
        self.assertLess(time.time() - t0, 1.0)

    def test_pathological_runs_stay_linear(self):
        for text in ('\\' * 20000 + 'password', 'x://' + ':' * 20000, 'x://' + 'a:' * 10000,
                     'password: "' + 'a' * 20000, 'password: "a' * 2000):
            with self.subTest(text=text[:12], length=len(text)):
                started = time.monotonic()
                redact(text, self.state)
                self.assertLess(time.monotonic() - started, 2.0)

    def test_jev_safe_blocks_secret_on_a_later_line(self):
        """JSON escapes a newline as \\n, so a key at the start of line 2+ needs the raw-string scan."""
        from beyin_v3 import MemoryStore
        from beyin_v3_jev import _safe
        store = MemoryStore(self.state, self.vault)
        statements = ['Setup notes\npassword: hunter2hunter2', 'Deploy\ttoken=abcdefghijkl',
                      'Windows notes\r\nsecret=abcdefghijkl', 'Kurulum\npassword: şifre12345']
        statements += ['Notes\n' + text for text, _ in QUOTED_REGRESSIONS]
        for statement in statements:
            for card in (dict(id='c', title='t', statement=statement, scope='s', domains=['d']),
                         dict(id='c', title=statement, statement='clean', scope='s', domains=['d'])):
                with self.subTest(statement=statement, field='title' if card['title'] != 't' else 'statement'):
                    with self.assertRaises(ValueError) as cm:
                        _safe(store, ['query', [card]])
                    self.assertEqual(str(cm.exception), 'advisor_sensitive_input')
        clean = dict(id='c', title='t', statement='Setup notes\nrotate the password monthly', scope='s', domains=['d'])
        self.assertIsNone(_safe(store, ['query', [clean]]))


# Every quoted-value regression example from the maintainer review of #76:
# (planted text, secret). _tail() is an escape-free fragment that must not survive JSON frontmatter.
QUOTED_REGRESSIONS = [
    ('password: "Xk9\'mQ2!vL"', "Xk9'mQ2!vL"),
    ("password: 'Pa\"ssword123'", 'Pa"ssword123'),
    ('password: "abc\\\\defghij"', 'abc\\\\defghij'),
    ('password: "it\'sasecret1"', "it'sasecret1"),
    ('{"password": "Xk9\'mQ2!vLzz"}', "Xk9'mQ2!vLzz"),
    ('{"password": "abc\\"defghij"}', 'abc\\"defghij'),
    ('Set password: "hunter2hunter2".', 'hunter2hunter2'),
]


def _tail(secret):
    return re.split(r'[\'"\\]', secret)[-1]


if __name__ == '__main__': unittest.main()
