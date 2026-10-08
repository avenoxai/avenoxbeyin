#!/usr/bin/env python3
"""#201/#206: the shared read gate for retired notes, on the store and on a real installed vault."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from v3_package_helpers import install, isolated_env, run_python, snapshot  # noqa: E402
from v3_passage_test import Vault, runtime  # noqa: E402

QUERY = 'kalibrasyon kovası yedekleme kuralı'
KEPT = ('active', 'aktif', 'current', 'verified', 'waiting later', 'Draft-review')
RETIRED = ('superseded', 'archived', 'ARŞİV', 'Eski')


class StatusGateTest(Vault):
    def setUp(self):
        super().setUp()
        for index, status in enumerate(KEPT + RETIRED):
            self.ingest(f'k{index}', f'Kalibrasyon kovası yedekleme kuralı sürüm {index}.',
                        f'notes/k{index}.md', status=status)
        self.ingest('plain', 'Kalibrasyon kovası yedekleme kuralı durumsuz.', 'notes/plain.md')

    def test_default_gate_drops_only_retired_statuses_on_every_route(self):
        kept = {f'k{index}' for index in range(len(KEPT))} | {'plain'}
        for name, read in (('note', lambda: self.store.retrieve(QUERY, limit=20)),
                           ('strict', lambda: self.strict(QUERY, limit=20))):
            with self.subTest(route=name):
                self.assertEqual({record['id'] for record in read()['records']}, kept)

    def test_explicit_statuses_still_reach_retired_notes(self):
        ids = {record['id'] for record in self.store.retrieve(QUERY, statuses=('archived', 'arşiv'), limit=20)['records']}
        self.assertEqual(ids, {f'k{len(KEPT) + 1}', f'k{len(KEPT) + 2}'})

    def test_status_word_folds_case_turkish_letters_and_punctuation(self):
        for value, word in (('ARŞİV', 'arsiv'), ('Superseded, see v3', 'superseded'), ('waiting later', 'waiting'),
                            ('GEÇERSİZ', 'gecersiz'), ('', 'active')):
            with self.subTest(value=value):
                self.assertEqual(runtime._status_word({'status': value}), word)
        self.assertEqual(runtime._status_word({'kind': 'task'}), '')


class SupersedesReportTest(Vault):
    def test_self_reference_and_unknown_link_are_reported_not_applied(self):
        self.ingest('md-self', 'Zirkon kovası tek karar.', 'knowledge/self.md', supersedes=['[[self#Karar]]'])
        self.ingest('md-dead', 'Zirkon kovası ikinci karar.', 'knowledge/dead.md', supersedes=['[[yok-boyle-not]]'])
        retired, dead = runtime.resolve_supersedes(self.store._records())
        self.assertEqual(retired, set())
        self.assertEqual({(item['id'], item['reason']) for item in dead},
                         {('md-self', 'supersedes reference points to the note itself'),
                          ('md-dead', 'unresolved supersedes reference')})

    def test_untrusted_note_cannot_retire_and_dotted_name_resolves(self):
        self.ingest('old', 'Zirkon kovası v1.2 planı.', 'knowledge/plan v1.2.md')
        self.ingest('clip', 'Kırpılmış sayfa.', 'inbox/clip.md', trust='untrusted', supersedes=['[[plan v1.2]]'])
        self.assertEqual(runtime.resolve_supersedes(self.store._records())[0], set())
        self.ingest('new', 'Zirkon kovası v1.3 planı.', 'knowledge/plan v1.3.md', supersedes=['[[plan v1.2]]'])
        self.assertEqual(runtime.resolve_supersedes(self.store._records())[0], {'old'})


class InstalledVaultTest(unittest.TestCase):
    """The per-turn hook, sync and doctor of a real install, with real Markdown sources."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='v3-supersedes-')
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.vault, self.state = root / 'vault', root / 'state'
        self.vault.mkdir()
        self.env = isolated_env(root / 'home')
        result = install(self.vault, self.state, self.env)
        self.assertEqual(result.returncode, 0, result.stderr)

    def note(self, rel, meta, body):
        path = self.vault / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('---\n' + json.dumps(meta, ensure_ascii=False) + '\n---\n' + body + '\n', encoding='utf-8')

    def cli(self, *args):
        result = run_python(self.vault / 'beyin.py', args, self.vault, self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def turn(self, prompt):
        payload = {'hook_event_name': 'UserPromptSubmit', 'session_id': 'supersedes-' + prompt,
                   'prompt': prompt, 'cwd': str(self.vault)}
        result = run_python(self.vault / '.claude/scripts/beyin_v3_hook.py',
                            ['--vault', self.vault, '--state', self.state, '--harness', 'claude'],
                            self.vault, self.env, payload)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        return output.get('hookSpecificOutput', {}).get('additionalContext', '')

    def test_issue_family_on_the_per_turn_hook(self):
        self.note('notes/route-v1.md', {'kind': 'decision', 'status': 'superseded'}, 'Account routing rule: first account first.')
        self.note('notes/route-v2.md', {'kind': 'decision'}, 'Account routing rule: second account first.')
        self.note('notes/route-v3.md', {'kind': 'decision', 'supersedes': ['notes/route-v2']}, 'Account routing rule: third account first.')
        self.note('notes/aktif.md', {'kind': 'note', 'status': 'aktif'}, 'Account routing rule ledger is kept here.')
        self.note('notes/self.md', {'kind': 'note', 'supersedes': ['[[self]]']}, 'Account routing rule owner note.')
        sync = self.cli('sync')
        self.assertEqual(sync['status'], 'succeeded')
        self.assertEqual([(item['source'], item['reason']) for item in sync['supersedes_issues']],
                         [('notes/self.md', 'supersedes reference points to the note itself')])
        text = self.turn('account routing rule')
        for source in ('notes/route-v3.md', 'notes/aktif.md', 'notes/self.md'):
            self.assertIn('"source": "' + source + '"', text)
        for source in ('notes/route-v1.md', 'notes/route-v2.md'):
            self.assertNotIn('"source": "' + source + '"', text)
        self.assertNotIn('needs attention', text)
        before = snapshot(self.state)
        doctor = self.cli('doctor')
        self.assertEqual(doctor['supersedes']['dead_count'], 1)
        self.assertEqual(snapshot(self.state), before, 'doctor must not write')


if __name__ == '__main__':
    unittest.main(verbosity=2)
