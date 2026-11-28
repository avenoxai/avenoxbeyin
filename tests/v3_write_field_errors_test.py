"""Field name normalization and descriptive errors for write commands (#957)."""
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
from beyin_v3_notes import LOCAL_DIRECTORY, Utf8InputError
from beyin_v3_sync import SyncEngine, parse
from v3_package_helpers import inherited_env

SPEC = importlib.util.spec_from_file_location('field_errors_cli', ROOT / 'scripts/beyin_v3.py')
cli = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cli)


class WriteFieldErrorsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='v3-field-errors-')
        self.addCleanup(self.temp.cleanup)
        self.vault = Path(self.temp.name) / 'vault'
        self.vault.mkdir()
        self.state = Path(self.temp.name) / 'state'
        self.engine = SyncEngine(self.vault, self.state)
        self.addCleanup(self.engine.store.close)
        self.environment = patch.dict(os.environ)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        os.environ.pop('CLAUDE_CODE_SESSION_ID', None)
        os.environ.pop('PI_SESSION_ID', None)
        os.environ['BEYIN_V3_NO_SPAWN'] = '1'
        os.environ['BEYIN_UPDATES_OFF'] = '1'
        self.task_meta = {
            'id': 'synthetic-task-1',
            'title': 'Synthetic Task',
            'status': 'active',
            'owner': 'Synthetic Owner',
            'project': 'alpha',
            'visibility': 'internal',
        }

    def invoke(self, command, payload, *extra_args):
        argv = ['--vault', str(self.vault), '--state', str(self.state), command, *extra_args]
        with patch.object(sys, 'stdin', io.StringIO(json.dumps(payload))):
            return cli.main(argv, return_result=True)

    def run_cli(self, command, payload, *extra_args):
        env = inherited_env(BEYIN_V3_NO_SPAWN='1', BEYIN_UPDATES_OFF='1',
                            PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8')
        return subprocess.run(
            [sys.executable, str(ROOT / 'scripts/beyin_v3.py'),
             '--vault', str(self.vault), '--state', str(self.state), command, *extra_args],
            input=json.dumps(payload),
            text=True, encoding='utf-8', capture_output=True, timeout=30, env=env,
        )

    def write(self, source='Threads.md', raw=b'alpha\nbeta\n'):
        path = self.vault / source
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        return path

    # --- note-create ---

    def test_note_create_accepts_content_alias_and_matching_duplicate(self):
        res, code = self.invoke('note-create', {
            'source': 'notes/via-content.md',
            'content': 'Body via content alias.',
            'metadata': {'id': 'note-content-1', 'project': 'alpha'},
        })
        self.assertEqual(code, 0, res)
        self.assertEqual(res['status'], 'succeeded')
        meta, body = parse((self.vault / 'notes/via-content.md').read_text(encoding='utf-8'))
        self.assertEqual(meta['id'], 'note-content-1')
        self.assertEqual(body, 'Body via content alias.\n')

        # Both text and content with identical values are accepted.
        res2, code2 = self.invoke('note-create', {
            'source': 'notes/both-same.md',
            'text': 'Same body.',
            'content': 'Same body.',
        })
        self.assertEqual(code2, 0, res2)
        self.assertEqual(res2['status'], 'succeeded')
        self.assertTrue((self.vault / 'notes/both-same.md').is_file())

    def test_note_create_idempotency_matches_across_text_and_content_aliases(self):
        first, code1 = self.invoke('note-create', {
            'source': 'notes/idemp-alias.md',
            'content': 'Stable body.',
            'idempotency_key': 'alias-create-1',
        })
        self.assertEqual(code1, 0, first)
        retry, code2 = self.invoke('note-create', {
            'source': 'notes/idemp-alias.md',
            'text': 'Stable body.',
            'idempotency_key': 'alias-create-1',
        })
        self.assertEqual(code2, 0, retry)
        self.assertTrue(retry.get('idempotent'))
        conflict, code3 = self.invoke('note-create', {
            'source': 'notes/idemp-alias.md',
            'text': 'Different body.',
            'idempotency_key': 'alias-create-1',
        })
        self.assertEqual(code3, 1)
        self.assertEqual(conflict['error'], 'ValueError')
        self.assertIn('different request', conflict['message'])

    def test_note_create_missing_conflicting_or_malformed_fields_return_value_error(self):
        cases = (
            ([], 'JSON object'),
            ('not-a-dict', 'JSON object'),
            (None, 'JSON object'),
            ({}, 'source'),
            ({'text': 'hello'}, 'source'),
            ({'source': '', 'text': 'hello'}, 'source'),
            ({'source': 123, 'text': 'hello'}, 'source'),
            ({'source': 'notes/a.md'}, 'text'),
            ({'source': 'notes/a.md', 'text': ''}, 'text'),
            ({'source': 'notes/a.md', 'content': '   '}, 'text'),
            ({'source': 'notes/a.md', 'content': 42}, 'text'),
            ({'source': 'notes/a.md', 'text': 'one', 'content': 'two'}, 'not both with different values'),
            ({'source': 'notes/a.md', 'text': 1, 'content': True}, 'not both with different values'),
            ({'source': 'notes/a.md', 'text': 'ok', 'metadata': ['bad']}, 'metadata'),
        )
        for payload, expected_substring in cases:
            with self.subTest(payload=payload):
                res, code = self.invoke('note-create', payload)
                self.assertEqual(code, 1, res)
                self.assertEqual(res['error'], 'ValueError', res)
                self.assertIn(expected_substring, res['message'])
        self.assertFalse((self.vault / 'notes/a.md').exists())

    def test_note_create_content_alias_still_rejects_surrogates(self):
        res, code = self.invoke('note-create', {
            'source': 'notes/surrogate.md',
            'content': 'bad \ud800 text',
        })
        self.assertEqual(code, 1)
        self.assertEqual(res['error'], 'Utf8InputError')
        self.assertFalse((self.vault / 'notes/surrogate.md').exists())

    # --- task-create ---

    def test_task_create_accepts_content_alias_and_matching_duplicate(self):
        res, code = self.invoke('task-create', {
            'source': 'tasks/via-content.md',
            'content': 'Task body via content.',
            'metadata': self.task_meta,
        })
        self.assertEqual(code, 0, res)
        self.assertEqual(res['kind'], 'task')
        meta, body = parse((self.vault / 'tasks/via-content.md').read_text(encoding='utf-8'))
        self.assertEqual(meta['id'], self.task_meta['id'])
        self.assertEqual(body, 'Task body via content.\n')

        res2, code2 = self.invoke('task-create', {
            'source': 'tasks/both-same.md',
            'text': 'Same task body.',
            'content': 'Same task body.',
            'metadata': dict(self.task_meta, id='synthetic-task-2'),
        })
        self.assertEqual(code2, 0, res2)
        self.assertEqual(res2['id'], 'synthetic-task-2')

    def test_task_create_missing_conflicting_or_malformed_fields_return_value_error(self):
        cases = (
            ([], 'JSON object'),
            ('not-a-dict', 'JSON object'),
            (None, 'JSON object'),
            ({}, 'source'),
            ({'text': 'body', 'metadata': self.task_meta}, 'source'),
            ({'source': '', 'text': 'body', 'metadata': self.task_meta}, 'source'),
            ({'source': 123, 'text': 'body', 'metadata': self.task_meta}, 'source'),
            ({'source': 'tasks/a.md', 'metadata': self.task_meta}, 'text'),
            ({'source': 'tasks/a.md', 'content': '   ', 'metadata': self.task_meta}, 'text'),
            ({'source': 'tasks/a.md', 'text': 'a', 'content': 'b', 'metadata': self.task_meta},
             'not both with different values'),
            ({'source': 'tasks/a.md', 'text': 'body'}, 'metadata'),
            ({'source': 'tasks/a.md', 'content': 'body'}, 'metadata'),
            ({'source': 'tasks/a.md', 'text': 'body', 'metadata': None}, 'metadata'),
            ({'source': 'tasks/a.md', 'text': 'body', 'metadata': ['bad']}, 'metadata'),
        )
        for payload, expected_substring in cases:
            with self.subTest(payload=payload):
                res, code = self.invoke('task-create', payload)
                self.assertEqual(code, 1, res)
                self.assertEqual(res['error'], 'ValueError', res)
                self.assertIn(expected_substring, res['message'])
        self.assertFalse((self.vault / 'tasks/a.md').exists())

    # --- note-edit (new file) ---

    def test_note_edit_new_file_accepts_text_alias_and_preserves_idempotency(self):
        first = self.engine.note_edit({
            'source': 'notes/edit-created.md',
            'expected_sha256': None,
            'text': '# Created via text\r\n',
            'idempotency_key': 'edit-create-alias-1',
        })
        self.assertEqual(first['status'], 'written')
        self.assertEqual((self.vault / 'notes/edit-created.md').read_bytes(), b'# Created via text\r\n')

        # Retrying with canonical 'content' and the same idempotency_key replays cleanly.
        retry = self.engine.note_edit({
            'source': 'notes/edit-created.md',
            'expected_sha256': None,
            'content': '# Created via text\r\n',
            'idempotency_key': 'edit-create-alias-1',
        })
        self.assertTrue(retry.get('idempotent'))
        self.assertEqual(retry['sha256'], first['sha256'])

        # Retrying with both equal fields also replays cleanly.
        both = self.engine.note_edit({
            'source': 'notes/edit-created.md',
            'expected_sha256': None,
            'content': '# Created via text\r\n',
            'text': '# Created via text\r\n',
            'idempotency_key': 'edit-create-alias-1',
        })
        self.assertTrue(both.get('idempotent'))

        # Retrying with changed text is refused as a different request.
        with self.assertRaisesRegex(ValueError, 'different request'):
            self.engine.note_edit({
                'source': 'notes/edit-created.md',
                'expected_sha256': None,
                'text': '# Modified\r\n',
                'idempotency_key': 'edit-create-alias-1',
            })

        # Empty string creation via text alias works just like content="".
        empty = self.engine.note_edit({
            'source': 'notes/empty-via-text.md',
            'expected_sha256': None,
            'text': '',
        })
        self.assertEqual(empty['status'], 'written')
        self.assertEqual((self.vault / 'notes/empty-via-text.md').read_bytes(), b'')

    def test_note_edit_new_file_rejects_conflicting_content_and_text_before_lock(self):
        cases = (
            {'source': 'notes/conflict.md', 'expected_sha256': None, 'content': 'a', 'text': 'b'},
            {'source': 'notes/conflict.md', 'expected_sha256': None, 'content': None, 'text': 'b'},
            {'source': 'notes/conflict.md', 'expected_sha256': None, 'content': 1, 'text': True},
        )
        for request in cases:
            with self.subTest(request=request):
                with self.assertRaisesRegex(ValueError, 'not both with different values'):
                    self.engine.note_edit(request)
        # Rejected before lock: vault remains untouched (no lock directory created).
        self.assertEqual(list(self.vault.iterdir()), [])

    def test_note_edit_new_file_missing_or_non_string_body_raises_clear_value_error(self):
        cases = (
            {'source': 'notes/new.md', 'expected_sha256': None},
            {'source': 'notes/new.md', 'expected_sha256': None, 'text': None},
            {'source': 'notes/new.md', 'expected_sha256': None, 'text': 123},
            {'source': 'notes/new.md', 'expected_sha256': None, 'text': 'hi', 'edits': []},
        )
        for request in cases:
            with self.subTest(request=request):
                with self.assertRaisesRegex(ValueError, r'new source requires content \(or text\)'):
                    self.engine.note_edit(request)
        self.assertFalse((self.vault / 'notes/new.md').exists())

    def test_note_edit_text_alias_rejects_surrogates_before_lock(self):
        with self.assertRaisesRegex(Utf8InputError, r'text is not valid UTF-8: unpaired surrogate U\+D800'):
            self.engine.note_edit({
                'source': 'notes/broken.md',
                'expected_sha256': None,
                'text': 'lone \ud800 surrogate',
            })
        self.assertEqual(list(self.vault.iterdir()), [])

    # --- note-edit (existing file) ---

    def test_note_edit_existing_file_dict_edits_gives_clear_list_error(self):
        path = self.write()
        sha = self.engine.note_read('Threads.md')['sha256']
        for bad_edits in ({'old': 'alpha', 'new': 'ALPHA'}, 'alpha->ALPHA', 123, None):
            with self.subTest(edits=bad_edits):
                with self.assertRaises(ValueError) as caught:
                    self.engine.note_edit({
                        'source': 'Threads.md',
                        'expected_sha256': sha,
                        'edits': bad_edits,
                    })
                message = str(caught.exception)
                self.assertIn('edits must be a list', message)
                self.assertNotIn('not content', message)
        self.assertEqual(path.read_bytes(), b'alpha\nbeta\n')
        self.assertFalse((self.vault / LOCAL_DIRECTORY / 'backups').exists())

    def test_note_edit_existing_file_missing_or_empty_edits_does_not_claim_content(self):
        path = self.write()
        sha = self.engine.note_read('Threads.md')['sha256']
        for request in (
            {'source': 'Threads.md', 'expected_sha256': sha},
            {'source': 'Threads.md', 'expected_sha256': sha, 'edits': []},
        ):
            with self.subTest(request=request):
                with self.assertRaises(ValueError) as caught:
                    self.engine.note_edit(request)
                self.assertIn('nonempty targeted edits', str(caught.exception))
                self.assertNotIn('not content', str(caught.exception))
        self.assertEqual(path.read_bytes(), b'alpha\nbeta\n')

    def test_note_edit_existing_file_with_content_or_text_still_rejects_replacement(self):
        path = self.write()
        sha = self.engine.note_read('Threads.md')['sha256']
        cases = (
            {'source': 'Threads.md', 'expected_sha256': sha, 'content': 'replacement'},
            {'source': 'Threads.md', 'expected_sha256': sha, 'content': 'replacement',
             'edits': [{'old': 'alpha', 'new': 'ALPHA'}]},
            {'source': 'Threads.md', 'expected_sha256': sha, 'text': 'replacement'},
            {'source': 'Threads.md', 'expected_sha256': sha, 'text': 'replacement',
             'edits': [{'old': 'alpha', 'new': 'ALPHA'}]},
        )
        for request in cases:
            with self.subTest(request=request):
                field = 'content' if 'content' in request else 'text'
                with self.assertRaisesRegex(ValueError, 'existing source requires nonempty targeted edits, not ' + field):
                    self.engine.note_edit(request)
        self.assertEqual(path.read_bytes(), b'alpha\nbeta\n')

    def test_null_alias_counts_as_absent_so_requests_without_it_are_unchanged(self):
        # A request that only names the existing field, plus a null alias, behaves as in 3.18.0.
        res, code = self.invoke('note-create', {
            'source': 'notes/null-content.md', 'text': 'Text body.', 'content': None})
        self.assertEqual(code, 0, res)
        self.assertEqual(parse((self.vault / 'notes/null-content.md').read_text(encoding='utf-8'))[1],
                         'Text body.\n')
        res, code = self.invoke('task-create', {
            'source': 'tasks/null-content.md', 'text': 'Task body.', 'content': None,
            'metadata': dict(self.task_meta, id='synthetic-task-null')})
        self.assertEqual(code, 0, res)

        created = self.engine.note_edit({'source': 'notes/null-text.md', 'expected_sha256': None,
                                         'content': 'Content body.\n', 'text': None})
        self.assertEqual(created['status'], 'written')
        self.assertEqual((self.vault / 'notes/null-text.md').read_bytes(), b'Content body.\n')

        # On an existing note a null alias does not turn targeted edits into a refused replacement,
        # and it does not change the retry identity of the same edit.
        path = self.write()
        sha = self.engine.note_read('Threads.md')['sha256']
        edits = [{'old': 'alpha', 'new': 'ALPHA'}]
        first = self.engine.note_edit({'source': 'Threads.md', 'expected_sha256': sha, 'edits': edits,
                                       'text': None, 'idempotency_key': 'null-alias-edit'})
        self.assertEqual(first['status'], 'written')
        self.assertEqual(path.read_bytes(), b'ALPHA\nbeta\n')
        retry = self.engine.note_edit({'source': 'Threads.md', 'expected_sha256': sha, 'edits': edits,
                                       'idempotency_key': 'null-alias-edit'})
        self.assertTrue(retry.get('idempotent'))

        # Both fields null still names the missing body instead of raising KeyError.
        res, code = self.invoke('note-create', {'source': 'notes/none.md', 'text': None, 'content': None})
        self.assertEqual(code, 1)
        self.assertEqual(res['error'], 'ValueError')
        self.assertIn("'text' (or 'content')", res['message'])

    def test_note_edit_wrong_edit_keys_keeps_clear_error(self):
        self.write()
        sha = self.engine.note_read('Threads.md')['sha256']
        with self.assertRaisesRegex(ValueError, 'each edit requires nonempty old and string new'):
            self.engine.note_edit({
                'source': 'Threads.md',
                'expected_sha256': sha,
                'edits': [{'old_text': 'alpha', 'new_text': 'ALPHA'}],
            })

    # --- task-update ---

    def test_task_update_missing_or_malformed_fields_return_value_error(self):
        cases = (
            ([], 'JSON object'),
            ('not-a-dict', 'JSON object'),
            (None, 'JSON object'),
            ({}, 'id'),
            ({'expected_revision': 1, 'changes': {'status': 'done'}}, 'id'),
            ({'id': '', 'expected_revision': 1, 'changes': {'status': 'done'}}, 'id'),
            ({'id': 123, 'expected_revision': 1, 'changes': {'status': 'done'}}, 'id'),
            ({'id': 't1', 'changes': {'status': 'done'}}, 'expected_revision'),
            ({'id': 't1', 'expected_revision': '1', 'changes': {'status': 'done'}}, 'expected_revision'),
            ({'id': 't1', 'expected_revision': True, 'changes': {'status': 'done'}}, 'expected_revision'),
            ({'id': 't1', 'expected_revision': 1}, 'changes'),
            ({'id': 't1', 'expected_revision': 1, 'changes': ['bad']}, 'changes'),
        )
        for payload, expected_substring in cases:
            with self.subTest(payload=payload):
                res, code = self.invoke('task-update', payload)
                self.assertEqual(code, 1, res)
                self.assertEqual(res['error'], 'ValueError', res)
                self.assertIn(expected_substring, res['message'])

    def test_task_update_valid_payload_succeeds_and_unknown_task_id_still_fails(self):
        self.engine.task_create('tasks/t1.md', 'Body.', self.task_meta)
        res, code = self.invoke('task-update', {
            'id': self.task_meta['id'],
            'expected_revision': 1,
            'changes': {'status': 'waiting'},
        })
        self.assertEqual(code, 0, res)
        self.assertEqual((res['status'], res['revision']), ('waiting', 2))

        missing, code_missing = self.invoke('task-update', {
            'id': 'no-such-task',
            'expected_revision': 1,
            'changes': {'status': 'done'},
        })
        self.assertEqual(code_missing, 1)
        self.assertEqual(missing['error'], 'KeyError')

    # --- Real CLI subprocess JSON channels ---

    def test_cli_subprocess_contracts_for_aliases_and_errors(self):
        # note-create with content alias succeeds
        nc_ok = self.run_cli('note-create', {'source': 'notes/sub.md', 'content': 'Subprocess body.'})
        self.assertEqual(nc_ok.returncode, 0, nc_ok.stderr)
        self.assertEqual(json.loads(nc_ok.stdout)['status'], 'succeeded')

        # note-create missing body returns ValueError JSON on stderr (never KeyError)
        nc_err = self.run_cli('note-create', {'source': 'notes/missing-body.md'})
        self.assertEqual(nc_err.returncode, 1)
        nc_json = json.loads(nc_err.stderr)
        self.assertEqual(nc_json['error'], 'ValueError')
        self.assertNotIn('Traceback', nc_err.stderr)

        # task-create with content alias succeeds; missing metadata returns ValueError
        tc_ok = self.run_cli('task-create', {
            'source': 'tasks/sub-task.md',
            'content': 'Subprocess task.',
            'metadata': self.task_meta,
        })
        self.assertEqual(tc_ok.returncode, 0, tc_ok.stderr)
        self.assertEqual(json.loads(tc_ok.stdout)['kind'], 'task')

        tc_err = self.run_cli('task-create', {'source': 'tasks/no-meta.md', 'content': 'Body'})
        self.assertEqual(tc_err.returncode, 1)
        self.assertEqual(json.loads(tc_err.stderr)['error'], 'ValueError')

        # note-edit new file with text alias succeeds; existing file with dict edits returns ValueError on stdout
        ne_new = self.run_cli('note-edit', {
            'source': 'notes/sub-edit.md',
            'expected_sha256': None,
            'text': 'Created via CLI text alias.\n',
        })
        self.assertEqual(ne_new.returncode, 0, ne_new.stdout + ne_new.stderr)
        created_sha = json.loads(ne_new.stdout)['sha256']

        ne_dict = self.run_cli('note-edit', {
            'source': 'notes/sub-edit.md',
            'expected_sha256': created_sha,
            'edits': {'old': 'Created', 'new': 'Updated'},
        })
        self.assertEqual(ne_dict.returncode, 1)
        ne_json = json.loads(ne_dict.stdout)
        self.assertEqual((ne_json['status'], ne_json['error']), ('error', 'ValueError'))
        self.assertIn('edits must be a list', ne_json['message'])
        self.assertNotIn('not content', ne_json['message'])

        # task-update missing changes returns ValueError on stderr
        tu_err = self.run_cli('task-update', {'id': self.task_meta['id'], 'expected_revision': 1})
        self.assertEqual(tu_err.returncode, 1)
        self.assertEqual(json.loads(tu_err.stderr)['error'], 'ValueError')


if __name__ == '__main__':
    unittest.main()
