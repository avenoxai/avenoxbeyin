"""#962: doctor --human collapses unused never-seen clients into a single summary line."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from v3_package_helpers import install, isolated_env, run_python

ROOT = Path(__file__).resolve().parents[1]
ALL_CLIENTS = ('codex', 'claude', 'antigravity', 'hermes', 'opencode', 'omp', 'kiro', 'pi')


def load_entry():
    spec = importlib.util.spec_from_file_location('v3_doctor_unused_clients_entry', ROOT / 'scripts/beyin_entry.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_lifecycle(observed=(), overrides=None):
    observed_set = set(observed)
    data = {
        name: {'status': 'observed_metadata', 'events': ['SessionStart']}
        if name in observed_set
        else {'status': 'never_seen', 'events': []}
        for name in ALL_CLIENTS
    }
    if overrides:
        data.update(overrides)
    return data


class DoctorUnusedClientsUnitTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.entry = load_entry()

    def render(self, result, version='3.18.0'):
        return self.entry.human_result(result, 'doctor', version)

    def test_single_observed_client_collapses_remaining_seven(self):
        text = self.render({
            'status': 'observed_metadata',
            'pending_events': 0,
            'lifecycle': make_lifecycle(observed=('claude',)),
            'hook_paths': {'status': 'ok', 'checked': 16},
        })
        lines = text.splitlines()
        self.assertIn('claude: olay goruldu', lines)
        self.assertIn(
            'Kullanilmayan istemciler: codex, antigravity, hermes, opencode, omp, kiro, pi (7)',
            lines,
        )
        self.assertNotIn('henuz dogrulanmadi', text)

    def test_multiple_observed_clients_stay_on_own_lines_and_collapse_rest(self):
        text = self.render({
            'status': 'observed_metadata',
            'pending_events': 0,
            'lifecycle': make_lifecycle(observed=('codex', 'claude')),
        })
        lines = text.splitlines()
        self.assertIn('codex: olay goruldu', lines)
        self.assertIn('claude: olay goruldu', lines)
        self.assertIn(
            'Kullanilmayan istemciler: antigravity, hermes, opencode, omp, kiro, pi (6)',
            lines,
        )
        self.assertNotIn('henuz dogrulanmadi', text)

    def test_zero_observed_clients_collapses_all_without_eight_line_spam(self):
        text = self.render({
            'status': 'never_seen',
            'pending_events': 0,
            'lifecycle': make_lifecycle(),
        })
        lines = text.splitlines()
        self.assertIn('Henuz gercek istemci oturumu gozlenmedi.', lines)
        self.assertIn(
            'Kullanilmayan istemciler: codex, claude, antigravity, hermes, opencode, omp, kiro, pi (8)',
            lines,
        )
        self.assertNotIn('henuz dogrulanmadi', text)

    def test_all_clients_observed_omits_unused_summary_line(self):
        text = self.render({
            'status': 'observed_metadata',
            'pending_events': 0,
            'lifecycle': make_lifecycle(observed=ALL_CLIENTS),
        })
        lines = text.splitlines()
        for client in ALL_CLIENTS:
            self.assertIn(f'{client}: olay goruldu', lines)
        self.assertNotIn('Kullanilmayan istemciler', text)

    def test_empty_missing_or_malformed_lifecycle_fails_open(self):
        for payload in (
            {'status': 'never_seen', 'pending_events': 0},
            {'status': 'never_seen', 'pending_events': 0, 'lifecycle': {}},
            {'status': 'never_seen', 'pending_events': 0, 'lifecycle': None},
            {'status': 'never_seen', 'pending_events': 0, 'lifecycle': ['not-a-dict']},
        ):
            with self.subTest(payload=payload):
                text = self.render(payload)
                self.assertIn('Henuz gercek istemci oturumu gozlenmedi.', text)
                self.assertNotIn('Kullanilmayan istemciler', text)

        non_dict_entry = self.render({
            'status': 'never_seen',
            'pending_events': 0,
            'lifecycle': {'codex': None, 'claude': {'status': 'observed_metadata', 'events': ['Stop']}},
        })
        self.assertIn('claude: olay goruldu', non_dict_entry.splitlines())
        self.assertIn('Kullanilmayan istemciler: codex (1)', non_dict_entry.splitlines())

    def test_never_seen_client_with_lifecycle_error_or_warning_is_not_collapsed(self):
        text = self.render({
            'status': 'needs_attention',
            'pending_events': 0,
            'lifecycle': make_lifecycle(
                observed=('claude',),
                overrides={
                    'codex': {'status': 'error', 'events': [], 'error': 'config unreadable'},
                    'antigravity': {'status': 'never_seen', 'events': [], 'warning': 'untrusted hook'},
                    'kiro': {'status': 'warning', 'events': []},
                    'pi': {'status': 'never_seen', 'events': [], 'errors': ['bad schema']},
                },
            ),
        })
        lines = text.splitlines()
        self.assertIn('codex: hata (config unreadable)', lines)
        self.assertIn('claude: olay goruldu', lines)
        self.assertIn('antigravity: henuz dogrulanmadi (untrusted hook)', lines)
        self.assertIn('kiro: uyari', lines)
        self.assertIn('pi: henuz dogrulanmadi (bad schema)', lines)
        self.assertIn('Kullanilmayan istemciler: hermes, opencode, omp (3)', lines)

    def test_empty_error_or_warning_fields_still_collapse(self):
        text = self.render({
            'status': 'observed_metadata',
            'pending_events': 0,
            'lifecycle': make_lifecycle(
                observed=('claude',),
                overrides={
                    'codex': {'status': 'never_seen', 'events': [], 'error': None, 'warning': False, 'warnings': []},
                    'kiro': {'status': 'never_seen', 'events': [], 'error': '   ', 'errors': []},
                },
            ),
        })
        self.assertIn(
            'Kullanilmayan istemciler: codex, antigravity, hermes, opencode, omp, kiro, pi (7)',
            text.splitlines(),
        )

    def test_never_seen_client_with_stale_hook_path_is_kept_visible(self):
        text = self.render({
            'status': 'needs_attention',
            'pending_events': 0,
            'lifecycle': make_lifecycle(observed=('claude',)),
            'hook_paths': {
                'status': 'stale',
                'checked': 12,
                'stale': [
                    {'file': '.codex/hooks.json', 'reason': 'unreadable'},
                    {'file': '.kiro/hooks/beyin-v3.json', 'event': 'agentSpawn', 'reason': 'other_vault', 'path': '/old'},
                    {'file': '.kiro/hooks/beyin-v3.json', 'event': 'stop', 'reason': 'other_vault', 'path': '/old'},
                ],
            },
        })
        lines = text.splitlines()
        self.assertIn('codex: henuz dogrulanmadi (hook dosyasi okunamadi)', lines)
        self.assertIn('claude: olay goruldu', lines)
        self.assertIn('kiro: henuz dogrulanmadi (hook yolu eski)', lines)
        self.assertIn('Kullanilmayan istemciler: antigravity, hermes, opencode, omp, pi (5)', lines)

    def test_never_seen_client_with_bridge_omp_hook_error_or_hermes_warning_is_kept_visible(self):
        text = self.render({
            'status': 'needs_attention',
            'pending_events': 0,
            'lifecycle': make_lifecycle(observed=('claude',)),
            'global_bridge_paths': {
                'status': 'stale',
                'stale': [{'harness': 'opencode', 'file': '/home/u/.config/opencode/plugin.js'}],
            },
            'omp_global_hook': {'path': '/home/u/.omp/agent/hooks/pre/beyin-v3.ts', 'stale': True},
            'hook-error.json': {'at': 1.0, 'error': 'PermissionError', 'harness': 'antigravity'},
            'receipt_coverage': {'total': 0, 'covered': 0, 'ratio': None, 'hermes_without_terminal': 2},
        })
        lines = text.splitlines()
        self.assertIn('claude: olay goruldu', lines)
        self.assertIn('antigravity: henuz dogrulanmadi (hook hatasi: PermissionError)', lines)
        self.assertIn('hermes: henuz dogrulanmadi (kapanis sinyali eksik)', lines)
        self.assertIn('opencode: henuz dogrulanmadi (genel kopru yolu eski)', lines)
        self.assertIn('omp: henuz dogrulanmadi (genel hook kopyasi eski)', lines)
        self.assertIn('Kullanilmayan istemciler: codex, kiro, pi (3)', lines)

    def test_omp_error_untyped_hook_error_and_observed_client_warning(self):
        text = self.render({
            'status': 'needs_attention',
            'pending_events': 0,
            'lifecycle': make_lifecycle(observed=('claude',)),
            'hook_paths': {
                'status': 'stale',
                'checked': 4,
                'stale': [{'file': '.claude/settings.local.json', 'event': 'SessionStart', 'reason': 'other_vault', 'path': '/old'}],
            },
            'omp_global_hook': {'status': 'unavailable', 'error': 'PermissionError'},
            'hook-error.json': {'at': 1.0, 'harness': 'codex', 'error': '   '},
        })
        lines = text.splitlines()
        self.assertIn('codex: henuz dogrulanmadi (hook hatasi)', lines)
        self.assertIn('claude: olay goruldu (hook yolu eski)', lines)
        self.assertIn('omp: henuz dogrulanmadi (genel hook denetlenemedi: PermissionError)', lines)
        self.assertIn('Kullanilmayan istemciler: antigravity, hermes, opencode, kiro, pi (5)', lines)

    def test_healthy_sibling_reports_and_unscoped_hook_error_do_not_uncollapse_unused_clients(self):
        text = self.render({
            'status': 'needs_attention',
            'pending_events': 0,
            'lifecycle': make_lifecycle(observed=('claude',)),
            'hook_paths': {'status': 'ok', 'checked': 16},
            'global_bridge_paths': {'status': 'ok', 'checked': 2},
            'omp_global_hook': {'path': '/home/u/.omp/agent/hooks/pre/beyin-v3.ts', 'stale': False},
            'hook-error.json': {'at': 1.0, 'error': 'OSError'},
            'receipt_coverage': {'total': 1, 'covered': 1, 'ratio': 1.0, 'hermes_without_terminal': False},
        })
        lines = text.splitlines()
        self.assertIn('claude: olay goruldu', lines)
        self.assertIn(
            'Kullanilmayan istemciler: codex, antigravity, hermes, opencode, omp, kiro, pi (7)',
            lines,
        )

    def test_control_and_bidi_characters_are_sanitized_in_client_lines(self):
        text = self.render({
            'status': 'needs_attention',
            'pending_events': 0,
            'lifecycle': {
                'codex\nfake': {'status': 'never_seen', 'events': [], 'error': 'bad\x1b\u202ehook\nline'},
                'pi\tclient': {'status': 'never_seen', 'events': []},
            },
        })
        self.assertNotIn('\x1b', text)
        self.assertNotIn('\u202e', text)
        self.assertIn('codex / fake: henuz dogrulanmadi (bad??hook / line)', text.splitlines())
        self.assertIn('Kullanilmayan istemciler: pi client (1)', text.splitlines())


class DoctorUnusedClientsEndToEndTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='v3-doctor-unused-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.vault = self.root / 'vault'
        self.state = self.root / 'state'
        self.vault.mkdir()
        self.env = isolated_env(self.root / 'home')
        installed = install(self.vault, self.state, self.env)
        self.assertEqual(installed.returncode, 0, installed.stderr)

    def doctor(self, *flags):
        done = run_python(self.vault / 'beyin.py', ['doctor', *flags], self.vault, self.env)
        self.assertEqual(done.returncode, 0, done.stderr.decode('utf-8', errors='replace'))
        return done.stdout.decode('utf-8')

    def mark_observed(self, harness, event='SessionStart'):
        done_dir = self.state / 'hook-done'
        done_dir.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha256(f'{harness}:{event}'.encode('utf-8')).hexdigest()
        (done_dir / f'{key}.json').write_text(
            json.dumps({'harness': harness, 'event': event, 'session': 'synthetic', 'at': 1}),
            encoding='utf-8',
        )

    def test_end_to_end_human_collapses_unused_clients_and_json_stays_full(self):
        # 1. Fresh install: 0 observed clients.
        fresh_json = json.loads(self.doctor('--json'))
        self.assertEqual(fresh_json['status'], 'never_seen')
        self.assertEqual(list(fresh_json['lifecycle'].keys()), list(ALL_CLIENTS))
        for name in ALL_CLIENTS:
            self.assertEqual(fresh_json['lifecycle'][name], {'status': 'never_seen', 'events': []})

        fresh_human = self.doctor('--human')
        self.assertIn('Henuz gercek istemci oturumu gozlenmedi.', fresh_human.splitlines())
        self.assertIn(
            'Kullanilmayan istemciler: codex, claude, antigravity, hermes, opencode, omp, kiro, pi (8)',
            fresh_human.splitlines(),
        )
        self.assertNotIn('henuz dogrulanmadi', fresh_human)

        # 2. Single observed client (claude) + 7 unused clients.
        self.mark_observed('claude')
        single_json = json.loads(self.doctor('--json'))
        self.assertEqual(single_json['status'], 'observed_metadata')
        self.assertEqual(single_json['lifecycle']['claude'], {'status': 'observed_metadata', 'events': ['SessionStart']})
        for name in ALL_CLIENTS:
            if name != 'claude':
                self.assertEqual(single_json['lifecycle'][name], {'status': 'never_seen', 'events': []})

        single_human = self.doctor('--human')
        single_lines = single_human.splitlines()
        self.assertIn('claude: olay goruldu', single_lines)
        self.assertIn(
            'Kullanilmayan istemciler: codex, antigravity, hermes, opencode, omp, kiro, pi (7)',
            single_lines,
        )
        self.assertNotIn('henuz dogrulanmadi', single_human)

        # 3. Never-seen client (codex) with unreadable hook file is NOT hidden as unused.
        codex_hooks = self.vault / '.codex/hooks.json'
        original_codex_hooks = codex_hooks.read_bytes()
        codex_hooks.write_text('{broken-json', encoding='utf-8')
        try:
            broken_json = json.loads(self.doctor('--json'))
            self.assertEqual(broken_json['status'], 'needs_attention')
            self.assertEqual(broken_json['hook_paths']['status'], 'stale')
            self.assertEqual(broken_json['lifecycle']['codex']['status'], 'never_seen')

            broken_human = self.doctor('--human')
            broken_lines = broken_human.splitlines()
            self.assertIn('codex: henuz dogrulanmadi (hook dosyasi okunamadi)', broken_lines)
            self.assertIn('claude: olay goruldu', broken_lines)
            self.assertIn(
                'Kullanilmayan istemciler: antigravity, hermes, opencode, omp, kiro, pi (6)',
                broken_lines,
            )
        finally:
            codex_hooks.write_bytes(original_codex_hooks)

        # 4. All 8 clients observed -> no Kullanilmayan istemciler line.
        for name in ALL_CLIENTS:
            if name != 'claude':
                self.mark_observed(name)
        all_json = json.loads(self.doctor('--json'))
        for name in ALL_CLIENTS:
            self.assertEqual(all_json['lifecycle'][name]['status'], 'observed_metadata')

        all_human = self.doctor('--human')
        all_lines = all_human.splitlines()
        for name in ALL_CLIENTS:
            self.assertIn(f'{name}: olay goruldu', all_lines)
        self.assertNotIn('Kullanilmayan istemciler', all_human)


if __name__ == '__main__':
    unittest.main()
