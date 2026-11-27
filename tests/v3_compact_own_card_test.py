#!/usr/bin/env python3
"""After compaction a session gets its own handoff card back, not only the newest one."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from v3_package_helpers import isolated_env

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'template/.claude/scripts'
HOOK = SCRIPTS / 'beyin_v3_hook.py'
sys.path.insert(0, str(SCRIPTS))

SESSION = 'own-card-session'
SHORT = hashlib.sha256(SESSION.encode()).hexdigest()[:8]
MARKER = 'Next: re-render shot 25 with the arm out of frame (marker otter-own).'
HEADER = "This session's own handoff card"
CHECKPOINT_HEADING = (f'## 2026-11-27 01:05 · video · {SHORT} <!-- beyin-checkpoint:{"a" * 64}:{"b" * 64} -->\n'
                      f'- {MARKER}\n<!-- /beyin-checkpoint:{"a" * 64} -->\n')


def last_session(own_first=False, filler=120):
    other = ('## 2026-11-27 01:30 · other · A parallel session wrote this card last · aaaaaaaa\n'
             '- What: parallel work. ' + 'Filler sentence for the newest card. ' * filler + '\n')
    own = (f'## 2026-11-27 01:05 · video · Rendered the second episode · {SHORT}\n'
           f'- What: rendered and sent the video.\n- {MARKER}\n')
    cards = own + '\n' + other if own_first else other + '\n' + own
    return '# Last session\n\n' + cards + '\n## Previous sessions\n- older\n'


def own_handoff_card(*args, **kwargs):
    from beyin_v3_companion import own_handoff_card as find
    return find(*args, **kwargs)


class OwnHandoffCardTest(unittest.TestCase):
    def test_finds_the_session_card_below_the_top(self):
        card = own_handoff_card(last_session(), SHORT)
        self.assertIsNotNone(card)
        self.assertIn(MARKER, card)
        self.assertNotIn('parallel session', card)
        self.assertNotIn('Previous sessions', card)

    def test_top_card_is_already_in_the_opening(self):
        self.assertIsNone(own_handoff_card(last_session(own_first=True), SHORT))

    def test_unknown_or_short_session_finds_nothing(self):
        self.assertIsNone(own_handoff_card(last_session(), 'bbbbbbbb'))
        self.assertIsNone(own_handoff_card(last_session(), SHORT[:4]))

    def test_a_long_card_is_bounded(self):
        text = last_session().replace(MARKER, MARKER + ' ' + 'x' * 5000)
        card = own_handoff_card(text, SHORT, limit=600)
        self.assertLessEqual(len(card), 700)
        self.assertIn('[truncated: read source]', card)

    def test_a_checkpoint_card_heading_still_names_the_session(self):
        # `beyin.py checkpoint` puts its replay marker after the digest in the heading.
        text = last_session().split('## 2026-11-27 01:05', 1)[0] + CHECKPOINT_HEADING
        card = own_handoff_card(text, SHORT)
        self.assertIsNotNone(card)
        self.assertIn(MARKER, card)


class CompactHookTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='v3-compact-card-')
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name).resolve()
        self.vault = root / 'vault'
        self.state = root / 'state'
        self.companion = self.vault / '🔮 850-Companion'
        self.companion.mkdir(parents=True)
        self.env = isolated_env(root / 'home')
        self.write(last_session())

    def write(self, text):
        (self.companion / 'Last-Session.md').write_text(text, encoding='utf-8', newline='\n')
        from beyin_v3_sync import SyncEngine
        SyncEngine(self.vault, self.state).sync()

    def opening(self, source):
        payload = {'hook_event_name': 'SessionStart', 'session_id': SESSION, 'source': source,
                   'event_id': 'evt-start', 'cwd': str(self.vault)}
        result = subprocess.run([sys.executable, str(HOOK), '--vault', str(self.vault), '--state', str(self.state),
                                 '--harness', 'claude'], input=json.dumps(payload), capture_output=True,
                                text=True, encoding='utf-8', env=self.env, check=True, timeout=120)
        return json.loads(result.stdout).get('hookSpecificOutput', {}).get('additionalContext', '')

    def test_compaction_brings_back_the_session_card(self):
        text = self.opening('compact')
        self.assertIn(HEADER, text)
        self.assertIn(MARKER, text)
        self.assertLessEqual(len(text.encode('utf-16-le')) // 2, 10000)

    def test_a_fresh_start_is_unchanged(self):
        self.assertNotIn(HEADER, self.opening('startup'))

    def test_a_private_last_session_stays_out(self):
        # The opening never shows a private Last-Session.md; the compact card must not either.
        self.write('---\nvisibility: private\n---\n' + last_session())
        text = self.opening('compact')
        self.assertNotIn(HEADER, text)
        self.assertNotIn(MARKER, text)

    def test_a_card_the_opening_already_shows_is_not_repeated(self):
        self.write(last_session(filler=2))
        text = self.opening('compact')
        self.assertEqual(text.count(MARKER), 1)
        self.assertNotIn(HEADER, text)

    def test_a_checkpoint_written_card_comes_back(self):
        from beyin_v3_checkpoint import run_checkpoint
        from beyin_v3_sync import SyncEngine
        self.write('# Last session\n\n## Previous sessions\n- older\n')
        sync = SyncEngine(self.vault, self.state)
        run_checkpoint(sync, self.vault, self.state, {'card': {'label': 'video', 'body': '- ' + MARKER}},
                       harness='claude', session=SESSION)
        path = self.companion / 'Last-Session.md'
        current = path.read_text(encoding='utf-8')
        newer = ('## 2099-01-01 09:00 · other · A parallel session wrote this card last · aaaaaaaa\n'
                 '- What: parallel work. ' + 'Filler sentence for the newest card. ' * 120 + '\n\n')
        self.write(current.replace('# Last session\n\n', '# Last session\n\n' + newer, 1))
        text = self.opening('compact')
        self.assertIn(HEADER, text)
        self.assertIn(MARKER, text)


if __name__ == '__main__':
    unittest.main()
