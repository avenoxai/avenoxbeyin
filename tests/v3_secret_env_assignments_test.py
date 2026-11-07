"""Prefixed environment assignments: synthetic credentials and offline transports only."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from v3_package_helpers import install, isolated_env, run_python

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
import beyin_v3 as runtime
import beyin_v3_jev as advisor
import beyin_v3_jev_client as client
from beyin_v3_secrets import redact

VALUE = 'abcd1234efgh5678ijkl'
NAMES = ('TYPESAFE_API_KEY', 'OPENAI_API_KEY', 'MY_SECRET', 'DB_PASSWORD')
QUERY = 'how do we deploy the quartz site'


class SecretEnvAssignmentsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='beyin-secret-env-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.vault = self.root / 'vault'
        self.vault.mkdir()
        self.state = self.root / 'state'

    def test_prefixed_assignments_preserve_key_and_value_syntax(self):
        for name in (*NAMES, 'SERVICE_V2_ACCESS_TOKEN', 'CI_TOKEN', 'DB_PASSWD',
                     '_API_KEY', '_SERVICE_API_KEY', 'my_service_api_key'):
            for before, assignment, after in (
                    ('', f'{name}={VALUE}', ''),
                    ('export ', f'{name}="{VALUE}"', ''),
                    ('export ', f"{name}='{VALUE}'", ''),
                    ('before\n', f'{name} = {VALUE}', '\nafter'),
                    ('{', f'"{name}": "{VALUE}"', '}'),
                    ('before ', f"'{name}': '{VALUE}'", ' after')):
                with self.subTest(name=name, assignment=assignment):
                    text = before + assignment + after
                    self.assertEqual(redact(text, self.state), (before + '[REDACTED]' + after, 1))

    def test_public_config_and_nonsecret_identifiers_are_unchanged(self):
        for name in ('PUBLIC_API_KEY_NAME', 'API_KEY_COUNT', 'MY_SECRET_NAME',
                     'DB_PASSWORD_FILE', 'OPENAI_API_KEY_ID', 'TOKENIZER_MODEL',
                     'API_KEYBOARD', 'MYSECRET', 'DBPASSWORD', 'PUBLIC_KEY',
                     'SERVICE_URL', '123_API_KEY'):
            text = f'{name}={VALUE}'
            with self.subTest(name=name):
                self.assertEqual(redact(text, self.state), (text, 0))
        for text in ('TYPESAFE_API_KEY is the variable name.',
                     'OPENAI_API_KEY=short', 'MY_SECRET="short"',
                     'export DB_PASSWORD=""', 'PUBLIC_API_URL=https://example.com/docs',
                     'npm_config_registry=https://registry.npmjs.org/'):
            with self.subTest(text=text):
                self.assertEqual(redact(text, self.state), (text, 0))

    def test_existing_unprefixed_key_forms_still_redact(self):
        for name in ('api_key', 'api-key', 'API KEY', 'access_token', 'token',
                     'secret', 'password', 'passwd'):
            with self.subTest(name=name):
                self.assertEqual(redact(name + '=' + VALUE, self.state), ('[REDACTED]', 1))

    def test_turkish_letters_in_a_prefix_never_hide_the_value(self):
        # Unicode IGNORECASE lets [a-z] match İ and ı. A name boundary that did so
        # left the whole value of these names visible while MÜŞTERI_API_KEY masked.
        for name in ('KİŞİSEL_API_KEY', 'GİZLİ_TOKEN', 'ŞİFRE_PASSWORD', 'MÜŞTERİ_SECRET',
                     'MÜŞTERI_API_KEY', 'çalışma_alanı_token', 'ÖZEL_PASSWD'):
            for text in (f'{name}={VALUE}', f'export {name}="{VALUE}"', f'"{name}": "{VALUE}"'):
                with self.subTest(text=text):
                    filtered, count = redact(text, self.state)
                    self.assertEqual(count, 1)
                    self.assertNotIn(VALUE, filtered)
                    self.assertTrue(filtered.endswith('[REDACTED]'), filtered)
                    kept = filtered[:-len('[REDACTED]')]
                    self.assertTrue(text.startswith(kept), filtered)

    def test_tokenizer_special_token_attributes_are_not_credentials(self):
        # Prefix support must not turn common model code into a secret: a masked
        # note loses the snippet and _safe skips Jev for every query that finds it.
        store = runtime.MemoryStore(self.state, self.vault)
        for text in ('tokenizer.pad_token = tokenizer.eos_token',
                     'tokenizer.bos_token = "<|begin_of_text|>"',
                     "tokenizer.add_special_tokens({'mask_token': '<mask_token>'})",
                     '{"sep_token": "<|separator|>", "cls_token": "<|classify|>"}',
                     'cls_token=tokenizer.cls_token', 'self.unk_token = tokenizer.unk_token'):
            with self.subTest(text=text):
                self.assertEqual(redact(text, self.state), (text, 0))
                advisor._safe(store, {'excerpt': text})
        for name in ('SPLUNK_TOKEN', 'PAD_TOKEN_SECRET', 'MY_EOS_TOKEN_PASSWORD'):
            with self.subTest(name=name):
                self.assertEqual(redact(f'{name}={VALUE}', self.state), ('[REDACTED]', 1))

    def test_secret_and_private_key_names_redact(self):
        for name in ('AWS_SECRET_ACCESS_KEY', 'SECRET_KEY', 'DJANGO_SECRET_KEY', 'secretKey',
                     'aws_secret_access_key', 'PRIVATE_KEY', 'WALLET_PRIVATE_KEY'):
            for text in (f'{name}={VALUE}', f'export {name}="{VALUE}"', f'"{name}": "{VALUE}"'):
                with self.subTest(text=text):
                    self.assertEqual(redact(text, self.state)[1], 1)
                    self.assertNotIn(VALUE, redact(text, self.state)[0])
        self.assertEqual(redact(f'https://example.com/?secret_key={VALUE}&format=md', self.state),
                         ('https://example.com/?[REDACTED]&format=md', 1))
        for text in (f'PUBLIC_KEY={VALUE}', 'SSH_PRIVATE_KEY_PATH=/home/user/.ssh/id_ed25519',
                     f'SECRET_KEY_ID={VALUE}', f'FOREIGN_KEY={VALUE}', 'SECRET_KEY=short'):
            with self.subTest(text=text):
                self.assertEqual(redact(text, self.state), (text, 0))

    def test_prefixed_query_assignment_preserves_other_parameters(self):
        for name in NAMES:
            text = f'https://example.com/?{name}={VALUE}&format=markdown'
            with self.subTest(name=name):
                self.assertEqual(redact(text, self.state),
                                 ('https://example.com/?[REDACTED]&format=markdown', 1))

    def test_jev_safe_rejects_prefixed_keys_and_values_after_json_escaping(self):
        store = runtime.MemoryStore(self.state, self.vault)
        for name in NAMES:
            assignment = f'export {name}="{VALUE}"'
            for value in (assignment, {'excerpt': assignment}, {name: VALUE},
                          {'statement': json.dumps({'text': assignment})}):
                with self.subTest(name=name, value=value):
                    with self.assertRaisesRegex(ValueError, '^advisor_sensitive_input$'):
                        advisor._safe(store, value)

    def test_installed_note_create_keeps_prefixed_secrets_out_of_note_and_index(self):
        env = isolated_env(self.root / 'home')
        installed = install(self.vault, self.state, env)
        self.assertEqual(installed.returncode, 0, installed.stderr)
        cli = self.vault / 'beyin.py'
        enabled = run_python(cli, ['preferences', '--secret-filter', 'on'], self.vault, env)
        self.assertEqual(enabled.returncode, 0, enabled.stderr)
        body = '\n'.join(f'export {name}="{VALUE}"' for name in NAMES)
        created = run_python(cli, ['note-create', '--json'], self.vault, env,
                             {'source': 'notes/env-assignments.md', 'text': body})
        self.assertEqual(created.returncode, 0, created.stderr)
        result = json.loads(created.stdout)
        self.assertEqual(result['redacted'], len(NAMES))
        stored = (self.vault / result['source']).read_text(encoding='utf-8')
        self.assertEqual(stored.count('[REDACTED]'), len(NAMES))
        self.assertNotIn(VALUE, stored)
        synced = run_python(cli, ['sync'], self.vault, env)
        self.assertEqual(synced.returncode, 0, synced.stderr)
        for database in self.state.rglob('*.sqlite3*'):
            self.assertNotIn(VALUE.encode(), database.read_bytes(), database.name)

    def test_auto_context_blocks_prefixed_assignments_before_transport(self):
        # The public control must actually reach the injected transport. No real
        # HTTP call is possible, even while reproducing the pre-fix leak.
        for name in (*NAMES, 'PUBLIC_API_KEY_NAME'):
            for location in ('prompt', 'note'):
                with self.subTest(name=name, location=location), tempfile.TemporaryDirectory(
                        dir=self.root) as directory, patch.dict(
                            os.environ, {'TYPESAFE_API_KEY': 'synthetic-key'}, clear=True):
                    root = Path(directory)
                    vault = root / 'vault'
                    vault.mkdir()
                    store = runtime.MemoryStore(root / 'state', vault)
                    assignment = f'export {name}="{VALUE}"'
                    text = 'Quartz deploy site instructions.'
                    query = QUERY
                    if location == 'note':
                        text += '\n' + assignment
                    else:
                        query += '\n' + assignment
                    (vault / 'deploy.md').write_text(text, encoding='utf-8')
                    store.ingest(dict(id='deploy', title='deploy', text=text, source='deploy.md'))
                    client.set_mode(store.state_dir, 'on', enable=['auto_context'])
                    calls = []

                    def transport(url, body, key, timeout):
                        calls.append(body)
                        answers = {'topical': {'type': 'noul', 'noul': 0.9}}
                        answers.update({ident: {'type': 'noul', 'noul': 0.9}
                                        for ident in body['state']['notes']})
                        return {'answers': answers}

                    local = store.context_for('claude', query, strict=True, budget_chars=6000)
                    self.assertEqual([record['id'] for record in local['records']], ['deploy'])
                    result = advisor.auto_context(store, 'claude', query, local, budget_chars=6000,
                                                  transport=transport)
                    if name == 'PUBLIC_API_KEY_NAME':
                        self.assertEqual(len(calls), 1)
                        self.assertIn(VALUE, json.dumps(calls))
                    else:
                        self.assertFalse(calls, 'Sensitive assignment reached the offline transport')
                        self.assertEqual(result, local)
                        log = (store.state_dir / 'jev-calls.jsonl').read_text(encoding='utf-8')
                        rows = [json.loads(line) for line in log.splitlines()]
                        self.assertEqual([row['outcome'] for row in rows], ['skipped_sensitive'])
                        self.assertNotIn(VALUE, log)


if __name__ == '__main__':
    unittest.main()
