"""Doctor human output and SessionStart notices for unreadable notes and missing companion files (#964)."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from v3_package_helpers import ROOT, install, isolated_env, run_python, snapshot

CLI = ROOT / 'scripts/beyin_v3.py'
HOOK = ROOT / 'template/.claude/scripts/beyin_v3_hook.py'
ENTRY_PATH = ROOT / 'scripts/beyin_entry.py'
SCRIPTS = ROOT / 'template/.claude/scripts'


SHORT_NOTICE = 'V3 sync needs attention; consult current sources and doctor.\n'


def attention_notice(count):
    """The opening line while the last sync has actionable warnings: a count and where the names are.

    A source the sync could not parse has unknown visibility, so its path never enters the agent
    context (beyin_v3_projections._hidden_ref_sources); doctor, a local command, names it."""
    label = 'warning' if count == 1 else 'warnings'
    return (f'V3 sync needs attention ({count} sync {label}, such as notes it could not read; '
            '`beyin.py doctor --human` names them); consult current sources and doctor.\n')


def load_entry():
    spec = importlib.util.spec_from_file_location('beyin_entry_964_test', ENTRY_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DoctorUnreadableAndCompanionTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='v3-doctor-unreadable-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.vault = self.root / 'vault'
        self.state = self.root / 'state'
        self.vault.mkdir()
        self.env = isolated_env(self.root / 'home')
        installed = install(self.vault, self.state, self.env)
        self.assertEqual(installed.returncode, 0, installed.stderr.decode('utf-8', errors='replace'))

    def beyin(self, *args, env=None):
        done = run_python(self.vault / 'beyin.py', list(args), self.vault, env or self.env)
        self.assertEqual(done.returncode, 0, done.stderr.decode('utf-8', errors='replace'))
        return done.stdout.decode('utf-8', errors='replace')

    def beyin_json(self, *args):
        return json.loads(self.beyin(*args, '--json'))

    def invoke_session_start(self, prompt='Proje durumu nedir?'):
        payload = {
            'hook_event_name': 'SessionStart',
            'session_id': 'synthetic-unreadable-964',
            'event_id': 'evt-964-start',
            'prompt': prompt,
        }
        done = run_python(
            self.vault / '.claude/scripts/beyin_v3_hook.py',
            ['--vault', self.vault, '--state', self.state, '--harness', 'claude'],
            self.vault,
            self.env,
            payload=payload,
        )
        self.assertEqual(done.returncode, 0, done.stderr.decode('utf-8', errors='replace'))
        data = json.loads(done.stdout.decode('utf-8'))
        return data['hookSpecificOutput']['additionalContext']

    def drain_hook(self):
        done = run_python(
            self.vault / '.claude/scripts/beyin_v3_hook.py',
            ['--vault', self.vault, '--state', self.state, '--harness', 'claude', '--drain-queue'],
            self.vault,
            self.env,
        )
        self.assertEqual(done.returncode, 0, done.stderr.decode('utf-8', errors='replace'))
        return json.loads(done.stdout.decode('utf-8'))

    def test_clean_installed_vault_has_no_unreadable_or_missing_companion_warnings(self):
        sync = self.beyin_json('sync')
        self.assertEqual(sync['status'], 'succeeded')
        doc_json = self.beyin_json('doctor')
        self.assertEqual(doc_json['sync_warnings']['count'], 0)
        self.assertEqual(doc_json['sync_warnings']['warnings'], [])
        self.assertEqual(doc_json['companion_sources']['status'], 'ok')
        self.assertEqual(doc_json['companion_sources']['missing'], [])
        human = self.beyin('doctor', '--human')
        self.assertNotIn('Okunamayan', human)
        self.assertNotIn('Eksik hafiza', human)
        self.drain_hook()
        ctx = self.invoke_session_start()
        self.assertNotIn('V3 sync needs attention', ctx)

    def test_unreadable_notes_surfaced_in_doctor_human_after_cli_sync_and_cleared_when_fixed(self):
        notes = self.vault / 'notes'
        notes.mkdir()
        broken_fm = notes / 'broken.md'
        broken_fm.write_text('---\ntitle: Acik kalmis\n\nUNREADABLE_SECRET_BODY_CANARY_1\n', encoding='utf-8')
        utf16_note = notes / 'other-editor-utf16.md'
        utf16_note.write_bytes('# Başlık\nUTF-16 içerik\n'.encode('utf-16'))
        win1254_note = notes / 'other-editor-win1254.md'
        win1254_note.write_bytes('# Türkçe not: ğüşiöç\n'.encode('cp1254'))
        bad_field = notes / 'bad-field.md'
        bad_field.write_text('---\nvisibility: invalid_scope\n---\nBody.\n', encoding='utf-8')

        sync = self.beyin_json('sync')
        self.assertEqual(sync['status'], 'degraded')
        self.assertEqual(len(sync['warnings']), 4)

        state_before = snapshot(self.state)
        doc_json = self.beyin_json('doctor')
        self.assertEqual(doc_json['status'], 'needs_attention')
        self.assertEqual(doc_json['sync_warnings']['count'], 4)
        warn_sources = {w['source'] for w in doc_json['sync_warnings']['warnings']}
        self.assertEqual(warn_sources, {
            'notes/broken.md',
            'notes/other-editor-utf16.md',
            'notes/other-editor-win1254.md',
            'notes/bad-field.md',
        })

        human = self.beyin('doctor', '--human')
        self.assertEqual(snapshot(self.state), state_before, 'doctor must remain read-only')
        self.assertIn('Okunamayan veya indekslenmeyen notlar (4):', human)
        self.assertIn('notes/broken.md', human)
        self.assertIn("'---'", human)
        self.assertIn('notes/other-editor-utf16.md', human)
        self.assertIn('notes/other-editor-win1254.md', human)
        self.assertIn('UTF-8', human)
        self.assertIn('notes/bad-field.md', human)
        self.assertIn('visibility', human)
        self.assertNotIn('UNREADABLE_SECRET_BODY_CANARY_1', human)
        # Surfaced near the top (before Jev line).
        self.assertLess(human.index('Okunamayan veya indekslenmeyen notlar'), human.index('Jev:'))

        # Fixing all 4 notes and running sync clears doctor warnings cleanly.
        for path in (broken_fm, utf16_note, win1254_note, bad_field):
            path.write_text('# Temiz\nDuzeltildi.\n', encoding='utf-8')
        clean_sync = self.beyin_json('sync')
        self.assertEqual(clean_sync['status'], 'succeeded')
        clean_doc = self.beyin_json('doctor')
        self.assertEqual(clean_doc['sync_warnings']['count'], 0)
        self.assertNotEqual(clean_doc['status'], 'needs_attention')
        clean_human = self.beyin('doctor', '--human')
        self.assertNotIn('Okunamayan', clean_human)

    def test_unreadable_notes_bounded_to_five_in_doctor_human_and_session_start(self):
        notes = self.vault / 'notes'
        notes.mkdir()
        for idx in range(1, 8):
            (notes / f'broken-{idx}.md').write_text(f'---\ntitle: B{idx}\nBODY_CANARY_{idx}\n', encoding='utf-8')

        # Run hook drain so hook-health.json records the degraded sync with all 7 warnings.
        self.drain_hook()
        health = json.loads((self.state / 'hook-health.json').read_text(encoding='utf-8'))
        self.assertEqual(health['sync']['status'], 'degraded')
        self.assertEqual(len(health['sync']['warnings']), 7)

        human = self.beyin('doctor', '--human')
        self.assertIn('Okunamayan veya indekslenmeyen notlar (7):', human)
        for idx in range(1, 6):
            self.assertIn(f'notes/broken-{idx}.md', human)
        self.assertNotIn('notes/broken-6.md', human)
        self.assertNotIn('notes/broken-7.md', human)
        self.assertIn('+2 not daha', human)

        ctx = self.invoke_session_start()
        self.assertIn(attention_notice(7), ctx)
        for idx in range(1, 8):
            self.assertNotIn(f'broken-{idx}.md', ctx)
            self.assertNotIn(f'BODY_CANARY_{idx}', ctx)

    def test_session_start_reports_the_count_of_two_broken_notes_and_doctor_names_them(self):
        notes = self.vault / 'notes'
        notes.mkdir()
        (notes / 'broken.md').write_text('---\ntitle: Unclosed\nSECRET_NOTE_BODY\n', encoding='utf-8', newline='\n')
        (notes / 'other-editor.md').write_bytes('Türkçe metin'.encode('cp1254'))

        self.drain_hook()
        ctx = self.invoke_session_start()
        self.assertIn(attention_notice(2), ctx)
        self.assertNotIn('broken.md', ctx)
        self.assertNotIn('other-editor.md', ctx)
        self.assertNotIn('SECRET_NOTE_BODY', ctx)
        human = self.beyin('doctor', '--human')
        self.assertIn('Okunamayan veya indekslenmeyen notlar (2):', human)
        self.assertIn('notes/broken.md', human)
        self.assertIn('notes/other-editor.md', human)

    def test_session_start_names_no_crafted_warning_source(self):
        # First produce a valid index so SessionStart renders context.
        self.drain_hook()
        health_path = self.state / 'hook-health.json'
        health_data = json.loads(health_path.read_text(encoding='utf-8'))
        health_data['sync'] = {
            'status': 'degraded',
            'warnings': [
                {'source': 'notes/bad\nINJECTED: line\u202ename.md', 'reason': 'unterminated frontmatter'},
                {'source': 'Kasa/secret-note.md', 'reason': 'unterminated frontmatter'},
                {'source': 'notes/ok-name.md', 'reason': 'unterminated frontmatter'},
            ],
            'conflicts': [],
        }
        health_path.write_text(json.dumps(health_data), encoding='utf-8', newline='\n')
        ctx = self.invoke_session_start()
        notice_lines = [line for line in ctx.splitlines() if line.startswith('V3 sync needs attention')]
        self.assertEqual(notice_lines, [attention_notice(3).rstrip('\n')], ctx)
        for fragment in ('ok-name.md', 'secret-note.md', 'INJECTED', '\u202e', 'name.md'):
            self.assertNotIn(fragment, ctx)

    def test_deleted_core_md_is_reported_in_doctor_human_and_json(self):
        core = self.vault / '🔮 850-Companion/Core.md'
        self.assertTrue(core.is_file())
        core.unlink()

        doc_json = self.beyin_json('doctor')
        comp = doc_json['companion_sources']
        self.assertEqual(comp['status'], 'missing')
        self.assertEqual(comp['missing'], ['Core.md'])
        self.assertTrue(comp['identity_missing'])

        human = self.beyin('doctor', '--human')
        self.assertIn('Eksik hafiza (companion) dosyalari', human)
        self.assertIn('Core.md (veya Soul.md)', human)
        self.assertLess(human.index('Eksik hafiza (companion) dosyalari'), human.index('Jev:'))

    def test_deleted_non_identity_companion_files_do_not_blame_core(self):
        (self.vault / '🔮 850-Companion/Kurallar.md').unlink()
        (self.vault / '🔮 850-Companion/Threads.md').unlink()

        doc_json = self.beyin_json('doctor')
        comp = doc_json['companion_sources']
        self.assertEqual(comp['status'], 'missing')
        self.assertEqual(comp['missing'], ['Kurallar.md', 'Threads.md'])
        self.assertFalse(comp['identity_missing'])

        human = self.beyin('doctor', '--human')
        self.assertIn('Kurallar.md, Threads.md', human)
        self.assertNotIn('Core.md', human)

    def test_doctor_human_survives_cp1252_console_with_unicode_paths(self):
        notes = self.vault / 'notes'
        notes.mkdir()
        (notes / 'Çığ-bozuk.md').write_text('---\ntitle: Unclosed\n', encoding='utf-8')
        (self.vault / '🔮 850-Companion/Core.md').unlink()
        self.beyin_json('sync')

        cp1252_env = dict(self.env, PYTHONIOENCODING='cp1252:replace')
        done = subprocess.run(
            [sys.executable, str(self.vault / 'beyin.py'), 'doctor', '--human'],
            cwd=self.vault,
            env=cp1252_env,
            capture_output=True,
            timeout=60,
        )
        self.assertEqual(done.returncode, 0, done.stderr.decode('utf-8', errors='replace'))
        text = done.stdout.decode('utf-8')
        self.assertIn('notes/Çığ-bozuk.md', text)
        self.assertIn('Core.md', text)

    def test_corrupt_hook_health_fails_open_in_session_start(self):
        self.drain_hook()
        (self.state / 'hook-health.json').write_text('{not valid json', encoding='utf-8')
        ctx = self.invoke_session_start()
        self.assertIn('V3 sync needs attention; consult current sources and doctor.', ctx)

    def test_newer_cli_sync_supersedes_stale_hook_health_in_both_directions(self):
        # 1. Hook runs when vault is clean -> hook-health.json says succeeded.
        self.drain_hook()
        notes = self.vault / 'notes'
        notes.mkdir()
        broken = notes / 'late-break.md'
        broken.write_text('---\ntitle: Broken after hook\n', encoding='utf-8')
        # 2. CLI sync catches the newly broken note -> doctor must surface it immediately.
        self.beyin_json('sync')
        doc1 = self.beyin_json('doctor')
        self.assertEqual(doc1['status'], 'needs_attention')
        self.assertEqual(doc1['sync_warnings']['count'], 1)
        self.assertIn('notes/late-break.md', self.beyin('doctor', '--human'))
        # 3. Now hook runs while degraded -> hook-health.json becomes degraded.
        self.drain_hook()
        # 4. User fixes the note and runs CLI sync -> doctor must clear the stale hook warning.
        broken.write_text('# Fixed\nNow valid.\n', encoding='utf-8')
        self.beyin_json('sync')
        doc2 = self.beyin_json('doctor')
        self.assertEqual(doc2['sync_warnings']['count'], 0)
        self.assertNotEqual(doc2['status'], 'needs_attention')
        self.assertNotIn('Okunamayan', self.beyin('doctor', '--human'))

    def test_prompt_turn_keeps_the_short_sync_line_without_filenames(self):
        # Names open the session only; every turn while a note stays broken keeps 3.18.0's line.
        notes = self.vault / 'notes'
        notes.mkdir()
        for idx in range(1, 4):
            (notes / f'kirik-not-{idx}.md').write_text('---\ntitle: Kapanmadi\n', encoding='utf-8', newline='\n')
        (notes / 'devekusu.md').write_text('# Devekusu besleme plani\n\nDevekusu geceleri dari tohumu yer.\n',
                                           encoding='utf-8', newline='\n')
        self.drain_hook()
        payload = {'hook_event_name': 'UserPromptSubmit', 'session_id': 'synthetic-unreadable-964-turn',
                   'event_id': 'evt-964-turn', 'prompt': 'devekusu besleme plani nedir?'}
        done = run_python(self.vault / '.claude/scripts/beyin_v3_hook.py',
                          ['--vault', self.vault, '--state', self.state, '--harness', 'claude'],
                          self.vault, self.env, payload=payload)
        self.assertEqual(done.returncode, 0, done.stderr.decode('utf-8', errors='replace'))
        ctx = json.loads(done.stdout.decode('utf-8'))['hookSpecificOutput']['additionalContext']
        self.assertIn('notes/devekusu.md', ctx)
        self.assertTrue(ctx.startswith('V3 sync needs attention; consult current sources and doctor.\n'), ctx[:300])
        self.assertNotIn('kirik-not-', ctx)
        opening = self.invoke_session_start()
        self.assertIn(attention_notice(3), opening)
        self.assertNotIn('kirik-not-', opening)

    def test_newer_cli_sync_keeps_homepage_degradation_reported_by_the_hook(self):
        # sync() refreshes homepages after its row is committed: a newer CLI row must not hide
        # the degraded status the hook recorded for them (3.18.0 doctor reports it).
        self.beyin_json('sync')
        health_path = self.state / 'hook-health.json'
        health_path.write_text(json.dumps({'at': 1.0, 'sync': {
            'status': 'degraded', 'indexed': 1, 'deleted': 0, 'warnings': [], 'conflicts': [],
            'workspace_homepages': {'updated': 0, 'conflicts': [], 'warnings': [{'reason': 'homepage refresh failed'}]},
        }}), encoding='utf-8', newline='\n')
        os.utime(health_path, (1.0, 1.0))
        self.assertEqual(self.beyin_json('sync')['status'], 'succeeded')
        doc = self.beyin_json('doctor')
        self.assertEqual(doc['sync_warnings']['count'], 0)
        self.assertEqual(doc['status'], 'needs_attention')

    def session_start_output(self, harness):
        payload = {'hook_event_name': 'SessionStart', 'session_id': f'synthetic-unreadable-964-{harness}',
                   'event_id': f'evt-964-start-{harness}', 'prompt': 'Zirkon kayitlari'}
        done = run_python(self.vault / '.claude/scripts/beyin_v3_hook.py',
                          ['--vault', self.vault, '--state', self.state, '--harness', harness],
                          self.vault, self.env, payload=payload)
        self.assertEqual(done.returncode, 0, done.stderr.decode('utf-8', errors='replace'))
        text = done.stdout.decode('utf-8')
        try:  # the decoded context, so a name escaped inside JSON is still found
            data = json.loads(text)
            text += data['hookSpecificOutput']['additionalContext']
        except (ValueError, KeyError, TypeError):
            pass
        return text

    def assert_no_names(self, opening, sources):
        import unicodedata
        for source in sources:
            for form in ('NFC', 'NFD'):
                for name in (source, source.rsplit('/', 1)[-1]):
                    self.assertNotIn(unicodedata.normalize(form, name), opening)

    def test_broken_private_note_is_not_named_when_the_hook_syncs(self):
        # v3_receipt_unreadable_source_test synced by the hook, as in a real session: breaking a
        # private note's metadata must not put its path into the opening of any harness.
        private = self.vault / 'notlar/gizli.md'
        private.parent.mkdir()
        private.write_text('---\nvisibility: private\n---\nGizli not.\n', encoding='utf-8', newline='\n')
        self.drain_hook()
        self.beyin('receipt', '--event-id', 'synthetic-private-964', '--summary', 'Zirkongizli ayrinti kaydi.',
                   '--ref', 'notlar/gizli.md', '--json')
        private.write_text('---\nvisibility: private\ninvalid: [\n---\nGizli not.\n', encoding='utf-8', newline='\n')
        self.drain_hook()
        health = json.loads((self.state / 'hook-health.json').read_text(encoding='utf-8'))
        self.assertIn('notlar/gizli.md', [w.get('source') for w in health['sync']['warnings']])
        for harness in ('claude', 'codex'):
            opening = self.session_start_output(harness)
            self.assertNotIn('gizli.md', opening, harness)
            self.assertNotIn('Zirkongizli', opening, harness)
            self.assertIn(attention_notice(1), opening, harness)
        self.assertIn('notlar/gizli.md', self.beyin('doctor', '--human'))

    def test_session_start_names_no_sync_warning_source_of_a_real_sync(self):
        # The class, not one case: whatever made a source unreadable or invalid (private or
        # workspace-scoped metadata, a validation error on a note that parses, another encoding,
        # a Turkish name, a file-sync clash copy), its path stays out of the opening.
        cases = {
            'notlar/gizli.md': b'---\nvisibility: private\ninvalid: [\n---\nGizli not.\n',
            'notlar/okurlar.md': b'---\nworkspace_readers: [ekip-a]\ninvalid: [\n---\nKisitli not.\n',
            'notlar/gecersiz-revizyon.md': b'---\nvisibility: private\nrevision: 0\n---\nGizli not.\n',
            'notlar/kapanmamis.md': b'---\ntitle: Acik kalmis\n\nGovde.\n',
            'notlar/baska-duzenleyici.md': '# Başlık\nUTF-16 içerik\n'.encode('utf-16'),
            'notlar/Çığ kırık.md': '---\ntitle: Kapanmadı\n'.encode('utf-8'),
            'notlar/gizli.sync-conflict-20261128-101010-ABCDEF1.md': b'---\nvisibility: private\n---\nGizli kopya.\n',
        }
        (self.vault / 'notlar').mkdir()
        for relative, data in cases.items():
            (self.vault / relative).write_bytes(data)
        self.drain_hook()
        health = json.loads((self.state / 'hook-health.json').read_text(encoding='utf-8'))
        import unicodedata
        warned = {unicodedata.normalize('NFC', w.get('source', '')) for w in health['sync']['warnings']}
        self.assertLessEqual({unicodedata.normalize('NFC', name) for name in cases}, warned)
        count = self.beyin_json('doctor')['sync_warnings']['count']
        self.assertGreaterEqual(count, len(cases))
        for harness in ('claude', 'codex'):
            opening = self.session_start_output(harness)
            self.assert_no_names(opening, cases)
            self.assertIn(attention_notice(count), opening, harness)
        human = self.beyin('doctor', '--human')
        self.assertIn('notlar/gizli.md', human)

    def test_session_start_without_actionable_warnings_keeps_the_3_18_line(self):
        # Difference rule: with no actionable warning the opening is 3.18.0's line, and a conflict
        # source is never named either.
        self.drain_hook()
        health_path = self.state / 'hook-health.json'
        for sync in (
            {'status': 'degraded', 'conflicts': [], 'warnings': [
                {'source': 'notlar/kopya 2.md', 'reason': 'possible file sync copy; compare it with the original'},
                {'source': 'receipts/abc.md', 'reason': 'missing source', 'kind': 'missing_receipt_source'}],
             'workspace_homepages': {'warnings': [{'reason': 'homepage refresh failed'}]}},
            {'status': 'conflict', 'warnings': [],
             'conflicts': [{'id': 'md-1', 'source': 'notlar/gizli-cakisma.md', 'reason': 'revision conflict'}]},
        ):
            data = json.loads(health_path.read_text(encoding='utf-8'))
            data['sync'] = sync
            health_path.write_text(json.dumps(data), encoding='utf-8', newline='\n')
            opening = self.session_start_output('claude')
            self.assertIn(SHORT_NOTICE, opening)
            self.assertNotIn('V3 sync needs attention (', opening)
            self.assert_no_names(opening, ['notlar/kopya 2.md', 'notlar/gizli-cakisma.md'])

    def test_deleted_bootstrapped_companion_directory_reports_all_missing_sources(self):
        import shutil
        shutil.rmtree(self.vault / '🔮 850-Companion')
        doc_json = self.beyin_json('doctor')
        comp = doc_json['companion_sources']
        self.assertEqual(comp['status'], 'missing')
        self.assertTrue(comp.get('directory_missing'))
        self.assertEqual(comp['missing'], ['Core.md', 'Kurallar.md', 'Last-Session.md', 'Threads.md', 'Journal.md'])
        human = self.beyin('doctor', '--human')
        self.assertIn('Eksik hafiza (companion) dosyalari', human)
        self.assertIn('Core.md (veya Soul.md)', human)
        self.assertIn('Journal.md', human)


class SyncWarningsRowTest(unittest.TestCase):
    def test_a_warning_source_that_is_not_utf8_does_not_abort_the_sync(self):
        # On Linux os.walk returns a file name that is not UTF-8 with lone surrogates. A file-sync
        # clash copy is only warned about, never read, so 3.18.0 synced past it; the #964 row that
        # stores the warnings must stay storable in SQLite instead of aborting every sync.
        if str(SCRIPTS) not in sys.path:
            sys.path.insert(0, str(SCRIPTS))
        import beyin_v3_sync
        import sqlite3
        tmp = tempfile.TemporaryDirectory(prefix='v3-sync-warning-row-')
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        vault, state = root / 'vault', root / 'state'
        (vault / 'notes').mkdir(parents=True)
        (vault / 'notes/plain.md').write_text('# Plain\nHello.\n', encoding='utf-8', newline='\n')
        clash = '\udcff (conflicted copy).md'
        real_walk = os.walk

        def walk(top, *args, **kwargs):
            for directory, dirs, files in real_walk(top, *args, **kwargs):
                yield directory, dirs, (files + [clash] if Path(directory) == vault / 'notes' else files)

        with mock.patch.object(beyin_v3_sync.os, 'walk', walk):
            result = beyin_v3_sync.SyncEngine(vault, state).sync()
        self.assertEqual(result['status'], 'degraded')
        self.assertIn('notes/' + clash, [w.get('source') for w in result['warnings']])
        db = sqlite3.connect(str(state / 'memory.sqlite3'))
        try:
            row = db.execute("SELECT value FROM metadata WHERE key='last_sync_warnings'").fetchone()
        finally:
            db.close()
        stored = json.loads(row[0])
        self.assertEqual(stored['count'], 1)
        self.assertEqual(stored['warnings'][0]['source'], 'notes/' + clash)


class CompanionEdgeCasesTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='v3-companion-edges-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.vault = self.root / 'vault'
        self.state = self.root / 'state'
        self.vault.mkdir()
        self.state.mkdir()
        self.env = isolated_env(self.root / 'home')

    def run_doctor(self):
        done = run_python(CLI, ['--vault', self.vault, '--state', self.state, 'doctor'], self.vault, self.env)
        self.assertEqual(done.returncode, 0, done.stderr.decode('utf-8', errors='replace'))
        return json.loads(done.stdout.decode('utf-8'))

    def test_fresh_bare_vault_without_companion_folder_does_not_false_alarm(self):
        (self.vault / 'notes').mkdir()
        (self.vault / 'notes/plain.md').write_text('# Plain\nHello.\n', encoding='utf-8')
        doc = self.run_doctor()
        self.assertEqual(doc['companion_sources']['status'], 'absent')
        self.assertEqual(doc['companion_sources']['missing'], [])
        entry = load_entry()
        human = entry.human_result(doc, 'doctor', '3.18.0')
        self.assertNotIn('Eksik hafiza', human)

    def test_v2_soul_md_without_core_md_does_not_false_alarm(self):
        echo = self.vault / 'Echo'
        echo.mkdir()
        for name in ('Soul.md', 'Kurallar.md', 'Last-Session.md', 'Threads.md', 'Journal.md'):
            (echo / name).write_text(f'# {name}\nBody.\n', encoding='utf-8')
        doc = self.run_doctor()
        self.assertEqual(doc['companion_sources']['status'], 'ok')
        self.assertEqual(doc['companion_sources']['missing'], [])
        entry = load_entry()
        self.assertNotIn('Eksik hafiza', entry.human_result(doc, 'doctor', '3.18.0'))

    def test_custom_bootstrapped_folder_detects_deleted_core(self):
        custom = self.vault / 'MyBuddy'
        custom.mkdir()
        for name in ('Kurallar.md', 'Last-Session.md', 'Threads.md', 'Journal.md'):
            (custom / name).write_text(f'# {name}\nBody.\n', encoding='utf-8')
        (self.state / 'companion-bootstrap.json').write_text(
            json.dumps({'schema': 1, 'directory': 'MyBuddy'}), encoding='utf-8'
        )
        doc = self.run_doctor()
        self.assertEqual(doc['companion_sources']['status'], 'missing')
        self.assertEqual(doc['companion_sources']['directory'], 'MyBuddy')
        self.assertEqual(doc['companion_sources']['missing'], ['Core.md'])

    def test_directory_named_core_md_is_treated_as_missing(self):
        comp = self.vault / '🔮 850-Companion'
        comp.mkdir()
        for name in ('Kurallar.md', 'Last-Session.md', 'Threads.md', 'Journal.md'):
            (comp / name).write_text(f'# {name}\nBody.\n', encoding='utf-8')
        (comp / 'Core.md').mkdir()
        doc = self.run_doctor()
        self.assertEqual(doc['companion_sources']['status'], 'missing')
        self.assertEqual(doc['companion_sources']['missing'], ['Core.md'])

    def test_symlinked_core_md_is_treated_as_missing(self):
        comp = self.vault / '🔮 850-Companion'
        comp.mkdir()
        for name in ('Kurallar.md', 'Last-Session.md', 'Threads.md', 'Journal.md'):
            (comp / name).write_text(f'# {name}\nBody.\n', encoding='utf-8')
        outside = self.root / 'outside.md'
        outside.write_text('# Outside\n', encoding='utf-8')
        try:
            (comp / 'Core.md').symlink_to(outside)
        except OSError as error:
            if getattr(error, 'winerror', None) != 1314:  # ERROR_PRIVILEGE_NOT_HELD
                raise
            self.skipTest(f'symlink not permitted here: {error}')
        doc = self.run_doctor()
        self.assertEqual(doc['companion_sources']['status'], 'missing')
        self.assertEqual(doc['companion_sources']['missing'], ['Core.md'])

    def test_human_entry_fallback_reads_hook_health_and_ignores_info_and_receipt_view_warnings(self):
        entry = load_entry()
        doc = {
            'status': 'needs_attention',
            'hook-health.json': {
                'sync': {
                    'status': 'degraded',
                    'warnings': [
                        {'source': 'notes/copy 2.md', 'reason': 'possible file sync copy; compare it with the original'},
                        {'source': 'receipts/abc.md', 'reason': 'missing source', 'kind': 'missing_receipt_source'},
                        {'source': 'knowledge/v3/outcomes.md', 'reason': 'manual receipt view edit preserved', 'kind': 'receipt_view'},
                        {'source': 'notes/unclosed.md', 'reason': 'unterminated frontmatter'},
                    ],
                }
            },
        }
        human = entry.human_result(doc, 'doctor', '3.18.0')
        self.assertIn('Okunamayan veya indekslenmeyen notlar (1):', human)
        self.assertIn('notes/unclosed.md', human)
        self.assertNotIn('notes/copy 2.md', human)
        self.assertNotIn('knowledge/v3/outcomes.md', human)

    def test_mocked_symlink_on_core_md_is_rejected_on_all_platforms(self):
        if str(SCRIPTS) not in sys.path:
            sys.path.insert(0, str(SCRIPTS))
        import beyin_v3_companion as companion
        comp = self.vault / '🔮 850-Companion'
        comp.mkdir()
        for name in ('Core.md', 'Kurallar.md', 'Last-Session.md', 'Threads.md', 'Journal.md'):
            (comp / name).write_text(f'# {name}\nBody.\n', encoding='utf-8')
        real_is_symlink = Path.is_symlink

        def fake_is_symlink(path_obj):
            if path_obj.name == 'Core.md':
                return True
            return real_is_symlink(path_obj)

        with mock.patch.object(Path, 'is_symlink', fake_is_symlink):
            report = companion.sources_health(self.vault, self.state)
        self.assertEqual(report['status'], 'missing')
        self.assertEqual(report['missing'], ['Core.md'])

    def test_unreadable_note_reason_maps_common_reasons_and_sanitizes_fallback(self):
        entry = load_entry()
        self.assertIn("'---'", entry.unreadable_note_reason('unterminated frontmatter'))
        self.assertIn('UTF-8', entry.unreadable_note_reason('source is not valid UTF-8: invalid UTF-8 sequence'))
        self.assertIn('YAML', entry.unreadable_note_reason('unsupported YAML metadata; use JSON frontmatter'))
        self.assertIn('JSON', entry.unreadable_note_reason('Expecting property name enclosed in double quotes: line 2'))
        self.assertIn('git cakisma', entry.unreadable_note_reason('unresolved git conflict markers; resolve the merge before sync'))
        self.assertIn('visibility', entry.unreadable_note_reason('invalid visibility'))
        self.assertIn('symlink', entry.unreadable_note_reason('symlink source rejected'))
        self.assertIn('izin hatasi', entry.unreadable_note_reason('[Errno 13] Permission denied'))
        fallback = entry.unreadable_note_reason('custom\nline\u202e\x1b[31merror')
        self.assertNotIn('\n', fallback)
        self.assertNotIn('\u202e', fallback)
        self.assertNotIn('\x1b', fallback)


if __name__ == '__main__':
    unittest.main()
