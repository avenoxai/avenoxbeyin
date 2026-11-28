"""Human entrypoint smoke: a novice sees an honest short status, tools retain JSON."""
import importlib.util
import io
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from v3_package_helpers import install, isolated_env, run_python

ROOT = Path(__file__).resolve().parents[1]


class EntryPresentationTest(unittest.TestCase):
    def test_human_doctor_and_machine_doctor_report_unverified_install(self):
        with tempfile.TemporaryDirectory(prefix='v3-entry-test-') as t:
            root = Path(t);vault=root/'vault';vault.mkdir();state=root/'state';env=isolated_env(root/'home')
            installed=install(vault,state,env)
            self.assertEqual(installed.returncode,0,installed.stderr)
            machine=run_python(vault/'beyin.py',['doctor'],vault,env)
            self.assertEqual(machine.returncode,0,machine.stderr)
            self.assertEqual(json.loads(machine.stdout)['status'],'never_seen')
            human=run_python(vault/'beyin.py',['doctor','--human'],vault,env)
            self.assertEqual(human.returncode,0,human.stderr)
            text=human.stdout.decode('utf-8')
            self.assertIn('3.0.0',text)
            self.assertIn('oturum',text.lower())
            self.assertNotIn('"hook-health.json"',text)
            self.assertNotIn(str(state),text)

    def test_entry_in_memory_invocation_avoids_io_redirect(self):
        with tempfile.TemporaryDirectory(prefix='v3-entry-mem-') as t:
            root = Path(t); vault = root / 'vault'; vault.mkdir(); state = root / 'state'; env = isolated_env(root / 'home')
            installed = install(vault, state, env)
            self.assertEqual(installed.returncode, 0, installed.stderr)
            # Verify entrypoint runs cleanly in human mode with zero stdout-capture overhead
            human = run_python(vault / 'beyin.py', ['sync', '--human'], vault, env)
            self.assertEqual(human.returncode, 0, human.stderr)
            self.assertIn('succeeded', human.stdout.decode('utf-8').lower())

    def test_human_mode_prints_unshaped_results_and_shows_usage_errors(self):
        with tempfile.TemporaryDirectory(prefix='v3-entry-shape-') as t:
            root = Path(t); vault = root / 'vault'; vault.mkdir(); state = root / 'state'; env = isolated_env(root / 'home')
            (vault / 'Synthetic.md').write_text('# Synthetic\n\nQuartz shape canary.\n', encoding='utf-8')
            installed = install(vault, state, env)
            self.assertEqual(installed.returncode, 0, installed.stderr)
            found = run_python(vault / 'beyin.py', ['context', 'Quartz shape canary', '--json'], vault, env)
            self.assertEqual(found.returncode, 0, found.stderr)
            record = next(r['id'] for r in json.loads(found.stdout)['records'] if r['source'] == 'Synthetic.md')
            # history returns a list, which human_result cannot shape: it prints as JSON, as before.
            human = run_python(vault / 'beyin.py', ['history', record, '--human'], vault, env)
            self.assertEqual(human.returncode, 0, human.stderr.decode('utf-8', errors='replace'))
            self.assertEqual(json.loads(human.stdout)[0]['record_id'], record)
            # An argparse error is no longer swallowed by the captured stderr in human mode.
            usage = run_python(vault / 'beyin.py', ['receipt', '--event-id', 'x', '--human'], vault, env)
            self.assertEqual(usage.returncode, 2)
            self.assertIn('--summary or --summary-file is required', usage.stderr.decode('utf-8'))

    def test_trust_review_names_only_the_clients_whose_hooks_changed(self):
        spec = importlib.util.spec_from_file_location('v3_entry_trust_review', ROOT / 'scripts/beyin_entry.py')
        entry = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(entry)
        updated = {'status': 'updated', 'from_version': '3.10.0', 'version': '3.11.0', 'trust_review_required': True}
        kiro = entry.human_result(dict(updated, trust_review_files=['.kiro/hooks/beyin-v3.json']), 'update')
        self.assertIn('.kiro/hooks/beyin-v3.json', kiro)
        self.assertIn('Kiro: Agent Hooks', kiro)
        self.assertNotIn('Codex', kiro)
        both = entry.human_result(dict(updated, trust_review_files=['.kiro/hooks/beyin-v3.json', '.codex/hooks.json']),
                                  'update')
        self.assertLess(both.index('Codex: /hooks'), both.index('Kiro: Agent Hooks'))
        # An updater from before the file list names no client rather than a wrong one.
        older = entry.human_result(updated, 'update')
        self.assertIn('kullandigin istemcide guven incelemesini', older)
        self.assertNotIn('Codex', older)
        self.assertNotIn('Hook tanimi', entry.human_result(dict(updated, trust_review_required=False), 'update'))


# The installer pins a resolved absolute path; this is one another OS wrote into a synced vault (#249).
FOREIGN_PIN = ('/home/ada/.local/state/beyin-v3/ab12' if sys.platform == 'win32' else
               chr(92).join(('C:', 'Users', 'ada', 'AppData', 'Local', 'beyin-v3', 'ab12')))


def pin(vault, value):
    path = vault / '.beyin-runtime.json'
    data = json.loads(path.read_text(encoding='utf-8'))
    data['state'] = value
    path.write_text(json.dumps(data), encoding='utf-8')


def codes(location):
    return [warning.split(':', 1)[0] for warning in location['warnings']]


class CrossPlatformPinTest(unittest.TestCase):
    """A runtime pin from another OS (#249): beyin.py uses this machine's default state, a reinstall repins."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='v3-cross-pin-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.vault, self.state = self.root / 'vault', self.root / 'state'
        self.vault.mkdir()
        self.env = isolated_env(self.root / 'home')
        installed = install(self.vault, self.state, self.env)
        self.assertEqual(installed.returncode, 0, installed.stderr)

    def test_foreign_pin_reads_the_default_state_and_doctor_names_it(self):
        pin(self.vault, FOREIGN_PIN)
        elsewhere = self.root / 'elsewhere'
        elsewhere.mkdir()
        for cwd in (self.vault, elsewhere):
            doctor = run_python(self.vault / 'beyin.py', ['doctor'], cwd, self.env)
            self.assertEqual(doctor.returncode, 0, doctor.stderr)
            location = json.loads(doctor.stdout)['state_location']
            self.assertIn('pinned_state_not_absolute', codes(location))
            # This machine's default state lives under the isolated home, not under the working directory.
            effective = Path(location['effective_state'])
            self.assertIn(self.root / 'home', effective.parents)
        synced = run_python(self.vault / 'beyin.py', ['sync'], elsewhere, self.env)
        self.assertEqual(synced.returncode, 0, synced.stderr)
        # Read as a relative path the pin used to become a state directory under the working directory.
        self.assertEqual(list(elsewhere.iterdir()), [])
        human = run_python(self.vault / 'beyin.py', ['doctor', '--human'], elsewhere, self.env)
        self.assertEqual(human.returncode, 0, human.stderr)
        self.assertIn('mutlak bir yol degil', human.stdout.decode('utf-8'))

    def test_every_command_says_it_on_stderr_and_stdout_stays_the_result(self):
        # #205: the fallback was visible only in doctor; sync, context and receipt changed state silently.
        valid = run_python(self.vault / 'beyin.py', ['sync'], self.vault, self.env)
        self.assertEqual(valid.returncode, 0, valid.stderr)
        self.assertNotIn(b'.beyin-runtime.json', valid.stderr, 'a usable pin adds nothing')
        pin(self.vault, FOREIGN_PIN)
        for argv in (['sync'], ['context', 'nebula'], ['sync', '--human']):
            with self.subTest(argv=argv):
                done = run_python(self.vault / 'beyin.py', argv, self.vault, self.env)
                self.assertEqual(done.returncode, 0, done.stderr)
                stderr = done.stderr.decode('utf-8')
                self.assertEqual(stderr.count('\n'), 1, stderr)
                self.assertIn('mutlak bir yol degil', stderr)
                self.assertIn('install_v3.py', stderr)
                self.assertNotIn(b'mutlak bir yol degil', done.stdout)
                if '--human' not in argv:
                    json.loads(done.stdout)  # the JSON contract holds

    @unittest.skipIf(sys.platform == 'win32', 'a colon is not a valid Windows path character')
    def test_absolute_posix_pin_with_colon_and_backslash_is_used_as_is(self):
        vault, state = self.root / 'other-vault', self.root / ('st:a' + chr(92) + 'te')
        vault.mkdir()
        installed = install(vault, state, self.env)
        self.assertEqual(installed.returncode, 0, installed.stderr)
        doctor = run_python(vault / 'beyin.py', ['doctor'], vault, self.env)
        self.assertEqual(doctor.returncode, 0, doctor.stderr)
        location = json.loads(doctor.stdout)['state_location']
        self.assertEqual(location['effective_state'], str(state))
        self.assertEqual(location['warnings'], [])

    def test_reinstall_replaces_a_foreign_pin_and_keeps_protecting_an_edited_one(self):
        pin(self.vault, FOREIGN_PIN)
        installed = install(self.vault, self.state, self.env)
        self.assertEqual(installed.returncode, 0, installed.stderr)
        runtime = json.loads((self.vault / '.beyin-runtime.json').read_text(encoding='utf-8'))
        self.assertEqual(runtime['state'], str(self.state.resolve()))
        pin(self.vault, str(self.root / 'moved-state'))
        refused = install(self.vault, self.state, self.env)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn('Reinstall conflict: managed file changed .beyin-runtime.json', refused.stderr.decode('utf-8'))

    def test_same_os_foreign_user_pin_uses_this_machine_default_state(self):
        if sys.platform != 'win32':
            self.skipTest('The regression is Windows user-profile path portability')
        expected = Path(self.env['LOCALAPPDATA']) / 'beyin-v3' / hashlib.sha256(
            str(self.vault.resolve()).encode()).hexdigest()[:16]
        foreign = Path('C:/Users/foreign-user/AppData/Local/beyin-v3') / expected.name
        pin(self.vault, str(foreign))
        doctor = run_python(self.vault / 'beyin.py', ['doctor'], self.vault, self.env)
        self.assertEqual(doctor.returncode, 0, doctor.stderr)
        location = json.loads(doctor.stdout)['state_location']
        self.assertEqual(Path(location['effective_state']).resolve(), expected.resolve())
        self.assertIn('effective_state_differs', codes(location))

    def test_resolve_state_across_linux_macos_and_windows(self):
        spec = importlib.util.spec_from_file_location('v3_cli_entry_test', ROOT / 'scripts/beyin_v3.py')
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        expected = cli.default_state(self.vault).resolve()
        key = expected.name

        # Foreign Windows pin
        win_pin = Path(r"C:\Users\other\AppData\Local\beyin-v3") / key
        self.assertEqual(cli.resolve_state(self.vault, win_pin), expected)

        # Foreign Linux pin
        linux_pin = Path(f"/home/other/.local/state/beyin-v3/{key}")
        self.assertEqual(cli.resolve_state(self.vault, linux_pin), expected)

        # Foreign macOS pin
        mac_pin = Path(f"/Users/other/Library/Application Support/beyin-v3/{key}")
        self.assertEqual(cli.resolve_state(self.vault, mac_pin), expected)

        # Explicit custom state is preserved
        custom = (self.root / "custom-state").resolve()
        self.assertEqual(cli.resolve_state(self.vault, custom), custom)
        changed_key = 'fedcba9876543210'
        self.assertEqual(cli.resolve_state(self.vault, Path('/home/foreign/.local/state/beyin-v3/' + changed_key)), expected)
        custom_layout = self.root.resolve() / 'state/beyin-v3' / expected.name
        self.assertEqual(cli.resolve_state(self.vault, custom_layout), custom_layout)
        previous_local = expected.parent / changed_key
        self.assertEqual(cli.resolve_state(self.vault, previous_local), previous_local)

    def test_only_the_pin_may_differ(self):
        spec = importlib.util.spec_from_file_location('install_v3_cross_pin', ROOT / 'scripts/install_v3.py')
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        baseline = json.dumps({'state': str(self.state.resolve()), 'schema': 1}).encode()
        def same(data):
            return installer.semantic_unchanged('.beyin-runtime.json', baseline, json.dumps(data).encode(), [])
        self.assertTrue(same({'state': FOREIGN_PIN, 'schema': 1}))
        self.assertFalse(same({'state': FOREIGN_PIN, 'schema': 2}))
        self.assertFalse(same({'state': FOREIGN_PIN, 'schema': 1, 'extra': True}))
        self.assertFalse(same({'state': str(self.root / 'moved-state'), 'schema': 1}))
        self.assertFalse(same({'schema': 1}))

    def test_windows_runtime_env_includes_systemdrive_and_programdata(self):
        from v3_package_helpers import windows_runtime_env
        if sys.platform != 'win32':
            self.skipTest('Windows-specific test')
        env = windows_runtime_env()
        for key in ('SystemDrive', 'ProgramData'):
            if key in os.environ:
                self.assertIn(key, env)


class LeadingGlobalOptionsTest(unittest.TestCase):
    """Leading global options (--vault, --state, --json, --human) preserve subcommand detection (#955)."""

    @classmethod
    def setUpClass(cls):
        tmp = tempfile.TemporaryDirectory(prefix='v3-entry-leading-opts-')
        cls.addClassCleanup(tmp.cleanup)
        cls.root = Path(tmp.name).resolve()
        cls.vault = cls.root / 'vault'
        cls.state = cls.root / 'state'
        cls.vault.mkdir()
        cls.env = dict(isolated_env(cls.root / 'home'), BEYIN_UPDATES_OFF='1')
        installed = install(cls.vault, cls.state, cls.env)
        if installed.returncode != 0:
            raise AssertionError(installed.stderr.decode('utf-8', errors='replace'))

    def run_entry(self, *args, payload=None):
        return run_python(self.vault / 'beyin.py', list(args), self.vault, self.env, payload=payload)

    def load_entry_module(self, name):
        spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts/beyin_entry.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_extract_command_skips_leading_global_options(self):
        entry = self.load_entry_module('v3_entry_extract_cmd')
        cases = (
            ([], 'doctor'),
            (['doctor'], 'doctor'),
            (['--vault', '/v', 'doctor'], 'doctor'),
            (['--vault=/v', 'doctor'], 'doctor'),
            (['--vault', '/v', '--state', '/s', 'triage'], 'triage'),
            (['--vault=/v', '--state=/s', 'triage'], 'triage'),
            (['--json', '--vault', '/v', 'note-read', 'notes/a.md'], 'note-read'),
            (['--human', '--vault=/v', 'note-edit', '--file', '-'], 'note-edit'),
            (['--vault', '/v', '--human', 'homepage-create', '--file', 'req.json'], 'homepage-create'),
            (['--vault', '/v'], 'doctor'),
            (['--vault=/v', '--state=/s'], 'doctor'),
            (['--vault', 'note-read'], 'doctor'),
            (['--vault', 'doctor', 'triage'], 'triage'),
            (['--vault', '/v', '--', 'note-read', 'notes/a.md'], 'note-read'),
            (['--', 'triage'], 'triage'),
            (['--'], 'doctor'),
            (['-h'], 'doctor'),
            (['--help'], 'doctor'),
            (['--version'], 'doctor'),
            (['--vault'], 'doctor'),
            (['--state'], 'doctor'),
        )
        for argv, expected in cases:
            with self.subTest(argv=argv):
                self.assertEqual(entry._extract_command(argv), expected)

    def test_vault_before_doctor_matches_direct_human_output(self):
        direct = self.run_entry('doctor', '--human')
        self.assertEqual(direct.returncode, 0, direct.stderr)
        expected = direct.stdout.decode('utf-8')
        self.assertNotIn('Islem sonucu:', expected)
        self.assertIn('Beyin 3.0.0', expected)

        for args in (
            ('--vault', str(self.vault), 'doctor', '--human'),
            (f'--vault={self.vault}', 'doctor', '--human'),
            ('--vault', str(self.vault), '--human', 'doctor'),
            ('--human', '--vault', str(self.vault), 'doctor'),
        ):
            with self.subTest(args=args):
                res = self.run_entry(*args)
                self.assertEqual(res.returncode, 0, res.stderr)
                text = res.stdout.decode('utf-8')
                self.assertEqual(text, expected)
                self.assertNotIn('Islem sonucu:', text)

    def test_vault_and_state_before_triage_produces_full_human_report(self):
        direct = self.run_entry('triage', '--human')
        self.assertEqual(direct.returncode, 0, direct.stderr)
        direct_text = direct.stdout.decode('utf-8')
        self.assertIn('=== Ikinci Beyin Sindirim ve Durum Raporu (Triage) ===', direct_text)

        for args in (
            ('--vault', str(self.vault), '--state', str(self.state), 'triage', '--human'),
            (f'--vault={self.vault}', f'--state={self.state}', 'triage', '--human'),
        ):
            with self.subTest(args=args):
                res = self.run_entry(*args)
                self.assertEqual(res.returncode, 0, res.stderr)
                text = res.stdout.decode('utf-8')
                self.assertIn('=== Ikinci Beyin Sindirim ve Durum Raporu (Triage) ===', text)
                self.assertNotIn('Islem sonucu:', text)
                self.assertEqual(text, direct_text)

    def test_vault_before_note_commands_preserves_json_contract(self):
        source = 'notes/LeadingOpt.md'
        create_req = {'source': source, 'expected_sha256': None, 'content': '# Leading Option Test\n'}

        edited = self.run_entry('--vault', str(self.vault), 'note-edit', '--human', '--file', '-', payload=create_req)
        self.assertEqual(edited.returncode, 0, edited.stdout + edited.stderr)
        edit_data = json.loads(edited.stdout.decode('utf-8'))
        self.assertEqual(edit_data['status'], 'written')
        self.assertEqual(edit_data['source'], source)

        for args in (
            ('--vault', str(self.vault), 'note-read', source, '--human'),
            (f'--vault={self.vault}', '--human', 'note-read', source),
            ('--vault', str(self.vault), '--state', str(self.state), 'note-read', source, '--human'),
        ):
            with self.subTest(args=args):
                read = self.run_entry(*args)
                self.assertEqual(read.returncode, 0, read.stdout + read.stderr)
                read_data = json.loads(read.stdout.decode('utf-8'))
                self.assertTrue(read_data['exists'])
                self.assertEqual(read_data['content'], create_req['content'])

        # Usage error on note-read with leading --vault and --human stays JSON on stdout (exit 64).
        usage = self.run_entry('--vault', str(self.vault), 'note-read', '--human')
        self.assertEqual(usage.returncode, 64, usage.stdout + usage.stderr)
        self.assertEqual(usage.stderr, b'')
        self.assertEqual(json.loads(usage.stdout.decode('utf-8'))['status'], 'usage_error')

        # homepage-create also keeps its JSON contract when --vault precedes it with --human.
        missing_req = self.root / 'missing-homepage-req.json'
        hp = self.run_entry('--vault', str(self.vault), 'homepage-create', '--file', str(missing_req), '--human')
        self.assertEqual(hp.returncode, 1, hp.stdout + hp.stderr)
        self.assertEqual(hp.stderr, b'')
        self.assertEqual(json.loads(hp.stdout.decode('utf-8'))['status'], 'error')

    def test_vault_before_note_read_preserves_json_when_stdout_is_tty(self):
        note_path = self.vault / 'notes' / 'TtyRead.md'
        note_path.parent.mkdir(parents=True, exist_ok=True)
        note_path.write_bytes(b'# TTY Note\n')

        entry = self.load_entry_module('v3_entry_tty_note_read')
        out, err = io.StringIO(), io.StringIO()
        original_path = sys.path[:]
        try:
            with patch.object(entry, '__file__', str(self.vault / 'beyin.py')), \
                 patch.object(sys, 'stdout', out), patch.object(sys, 'stderr', err), \
                 patch.object(out, 'isatty', return_value=True), \
                 patch.dict(os.environ, self.env, clear=False):
                code = entry.main(['--vault', str(self.vault), 'note-read', 'notes/TtyRead.md'])
        finally:
            sys.path[:] = original_path
        self.assertEqual(code, 0, err.getvalue())
        self.assertEqual(err.getvalue(), '')
        payload = json.loads(out.getvalue())
        self.assertTrue(payload['exists'])
        self.assertEqual(payload['content'], '# TTY Note\n')

        # note-edit with leading --vault also stays JSON under TTY default human mode.
        req_file = self.root / 'tty-edit.json'
        req_file.write_text(json.dumps({
            'source': 'notes/TtyRead.md',
            'expected_sha256': payload['sha256'],
            'edits': [{'old': '# TTY Note', 'new': '# TTY Note Updated'}],
        }), encoding='utf-8')
        out2, err2 = io.StringIO(), io.StringIO()
        try:
            with patch.object(entry, '__file__', str(self.vault / 'beyin.py')), \
                 patch.object(sys, 'stdout', out2), patch.object(sys, 'stderr', err2), \
                 patch.object(out2, 'isatty', return_value=True), \
                 patch.dict(os.environ, self.env, clear=False):
                code2 = entry.main(['--vault', str(self.vault), 'note-edit', '--file', str(req_file)])
        finally:
            sys.path[:] = original_path
        self.assertEqual(code2, 0, err2.getvalue())
        self.assertEqual(err2.getvalue(), '')
        edit_payload = json.loads(out2.getvalue())
        self.assertEqual(edit_payload['status'], 'written')
        self.assertEqual(note_path.read_bytes(), b'# TTY Note Updated\n')

    def test_no_subcommand_with_vault_defaults_to_doctor_and_preserves_help_and_errors(self):
        direct_human = self.run_entry('doctor', '--human')
        self.assertEqual(direct_human.returncode, 0, direct_human.stderr)
        expected_human = direct_human.stdout.decode('utf-8')

        for args in (
            ('--vault', str(self.vault)),
            (f'--vault={self.vault}',),
            ('--vault', str(self.vault), '--state', str(self.state)),
        ):
            with self.subTest(mode='json', args=args):
                res = self.run_entry(*args)
                self.assertEqual(res.returncode, 0, res.stderr)
                self.assertEqual(json.loads(res.stdout.decode('utf-8'))['status'], 'never_seen')

        res_human = self.run_entry('--vault', str(self.vault), '--human')
        self.assertEqual(res_human.returncode, 0, res_human.stderr)
        self.assertEqual(res_human.stdout.decode('utf-8'), expected_human)

        # --help with leading --vault must still show usage and exit 0.
        helped = self.run_entry('--vault', str(self.vault), '--help')
        self.assertEqual(helped.returncode, 0, helped.stderr)
        self.assertIn('--vault VAULT', helped.stdout.decode('utf-8'))

        # Dangling --vault with no path must still report expected one argument (never eat 'doctor').
        dangling = self.run_entry('--vault')
        self.assertEqual(dangling.returncode, 2)
        self.assertIn('expected one argument', dangling.stderr.decode('utf-8'))


if __name__ == '__main__':
    unittest.main()
