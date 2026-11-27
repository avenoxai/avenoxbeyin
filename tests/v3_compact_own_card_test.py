#!/usr/bin/env python3
"""After compaction a session gets its own handoff card back, not only the newest one."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from v3_package_helpers import isolated_path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'template/.claude/scripts'
HOOK = SCRIPTS / 'beyin_v3_hook.py'
sys.path.insert(0, str(SCRIPTS))

SESSION = 'own-card-session'
SHORT = hashlib.sha256(SESSION.encode()).hexdigest()[:8]
MARKER = 'Next: re-render shot 25 with the arm out of frame (marker otter-own).'
HEADER = "This session's own handoff card"


def last_session(own_first=False):
    other = ('## 2026-11-27 01:30 · other · A parallel session wrote this card last · aaaaaaaa\n'
             '- What: parallel work. ' + 'Filler sentence for the newest card. ' * 120 + '\n')
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


class CompactHookTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='v3-compact-card-')
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.vault = root / 'vault'
        self.state = root / 'state'
        home = root / 'home'
        home.mkdir()
        companion = self.vault / '🔮 850-Companion'
        companion.mkdir(parents=True)
        (companion / 'Last-Session.md').write_text(last_session(), encoding='utf-8')
        self.env = {'HOME': str(home), 'USERPROFILE': str(home), 'APPDATA': str(home / 'appdata'),
                    'LOCALAPPDATA': str(home / 'localappdata'), 'TEMP': str(root), 'TMP': str(root),
                    'PATH': isolated_path(), 'PYTHONIOENCODING': 'utf-8', 'PYTHONDONTWRITEBYTECODE': '1',
                    'BEYIN_V3_NO_SPAWN': '1'}
        for key in ('SYSTEMROOT', 'WINDIR'):
            if key in os.environ:
                self.env[key] = os.environ[key]
        from beyin_v3_sync import SyncEngine
        SyncEngine(self.vault, self.state).sync()

    def opening(self, source):
        payload = {'hook_event_name': 'SessionStart', 'session_id': SESSION, 'source': source,
                   'event_id': 'evt-start', 'cwd': str(self.vault)}
        result = subprocess.run([sys.executable, str(HOOK), '--vault', str(self.vault), '--state', str(self.state),
                                 '--harness', 'claude'], input=json.dumps(payload), capture_output=True,
                                text=True, encoding='utf-8', env=self.env, check=True)
        return json.loads(result.stdout).get('hookSpecificOutput', {}).get('additionalContext', '')

    def test_compaction_brings_back_the_session_card(self):
        text = self.opening('compact')
        self.assertIn(HEADER, text)
        self.assertIn(MARKER, text)

    def test_a_fresh_start_is_unchanged(self):
        self.assertNotIn(HEADER, self.opening('startup'))


if __name__ == '__main__':
    unittest.main()
