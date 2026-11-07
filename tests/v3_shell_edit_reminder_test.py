#!/usr/bin/env python3
"""#212: vault Markdown edits made through the Claude Code shell tool reach the Stop receipt
reminder and receipt coverage. Synthetic vaults and transcripts only."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from v3_package_helpers import clean_environ, inherited_env

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'template/.claude/scripts'
HOOK = SCRIPTS / 'beyin_v3_hook.py'
CANARY = 'SHELL_COMMAND_TEXT_CANARY_NEVER_PERSIST'


def digest(session):
    return hashlib.sha256(session.encode('utf-8')).hexdigest()[:24]


def stamp(at):
    return datetime.fromtimestamp(at, timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


class ShellEditReminderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='v3-shell-edit-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.vault = self.root / 'Sentetik Kasa Ölçüm'
        (self.vault / 'notes').mkdir(parents=True)
        for name in ('a', 'b', 'Günlük Not'):
            self.write('notes/' + name + '.md', '# ' + name + '\n')
        old = time.time() - 600
        for path in (self.vault / 'notes').iterdir():
            os.utime(path, (old, old))
        self.state = self.root / 'state outside vault'
        self.logs = self.root / 'transcripts'
        self.logs.mkdir()
        home = self.root / 'home'
        home.mkdir()
        self.env = inherited_env(HOME=str(home), USERPROFILE=str(home), BEYIN_V3_NO_SPAWN='1',
                                 BEYIN_UPDATES_OFF='1', PYTHONIOENCODING='utf-8', PYTHONDONTWRITEBYTECODE='1')
        environment = clean_environ()
        environment.start()
        self.addCleanup(environment.stop)
        sys.path.insert(0, str(SCRIPTS))
        self.addCleanup(lambda: sys.path.remove(str(SCRIPTS)))
        self.count = 0

    # -- synthetic client -------------------------------------------------------------------
    def write(self, relative, text, append=False):
        path = self.vault / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a' if append else 'w', encoding='utf-8') as handle:
            handle.write(text)
        return path

    def record(self, session, entry, at=None):
        entry = dict(entry, timestamp=stamp(time.time() if at is None else at), sessionId=session, cwd=str(self.vault))
        with (self.logs / (session + '.jsonl')).open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, separators=(',', ':')) + '\n')

    def prompt(self, session, text, at=None):
        self.record(session, {'type': 'user', 'message': {'role': 'user', 'content': text}}, at)
        return self.hook('UserPromptSubmit', session, prompt=text)

    def shell(self, session, command, write=None, tool='Bash', at=None):
        self.record(session, {'type': 'assistant', 'message': {'role': 'assistant', 'content': [
            {'type': 'text', 'text': 'Komutu çalıştırıyorum.'},
            {'type': 'tool_use', 'id': 'toolu_%d' % self.count, 'name': tool,
             'input': {'command': command, 'description': 'synthetic'}}]}}, at)
        if write:
            write()
        self.record(session, {'type': 'user', 'message': {'role': 'user', 'content': [
            {'type': 'tool_result', 'tool_use_id': 'toolu_%d' % self.count, 'content': ''}]}}, at)

    def hook(self, event, session, harness='claude', **fields):
        self.count += 1
        payload = dict({'hook_event_name': event, 'session_id': session, 'event_id': '%s-%d' % (session, self.count),
                        'cwd': str(self.vault), 'transcript_path': str(self.logs / (session + '.jsonl'))}, **fields)
        result = subprocess.run([sys.executable, str(HOOK), '--vault', str(self.vault), '--state', str(self.state),
                                 '--harness', harness], input=json.dumps(payload), text=True, encoding='utf-8',
                                capture_output=True, cwd=self.vault, env=self.env, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1, result.stdout)
        return json.loads(lines[0])

    def stop(self, session, harness='claude', **fields):
        return self.hook('Stop', session, harness, **fields)

    def engine(self):
        from beyin_v3_sync import SyncEngine
        return SyncEngine(self.vault, self.state)

    def marker(self, session):
        return self.state / 'receipt-reminders' / (hashlib.sha256(session.encode('utf-8')).hexdigest() + '.edited')

    # -- reminder ---------------------------------------------------------------------------
    def test_shell_write_to_a_vault_note_blocks_stop_once_until_a_receipt(self):
        self.prompt('sed', 'notları düzelt')
        before = time.time()
        self.shell('sed', "sed -i '' 's/a/b/' \"notes/Günlük Not.md\"  # " + CANARY,
                   lambda: self.write('notes/Günlük Not.md', 'yeni\n', append=True))
        blocked = self.stop('sed')
        self.assertEqual(blocked.get('decision'), 'block')
        self.assertIn('Receipt session=' + digest('sed') + ';', blocked['reason'])
        marker = json.loads(self.marker('sed').read_text(encoding='utf-8'))
        self.assertEqual(set(marker), {'at'})
        self.assertGreaterEqual(marker['at'], before - 1)
        self.assertLessEqual(marker['at'], time.time())
        # The answering Stop and later ones keep the one reminder.
        self.assertEqual(self.stop('sed', stop_hook_active=True), {})
        self.assertEqual(self.stop('sed'), {})
        self.engine().receipt('sed-1', 'Not düzeltildi.', ['notes/Günlük Not.md'], 'claude', session=digest('sed'))
        self.assertEqual(self.stop('sed'), {})
        self.assertFalse(self.marker('sed').exists())
        # Nothing from the command text reaches the runtime state.
        for path in self.state.rglob('*'):
            if path.is_file():
                self.assertNotIn(CANARY.encode(), path.read_bytes(), path.name)

    def test_named_paths_in_heredocs_quotes_globs_and_powershell(self):
        absolute = self.vault / 'notes/b.md'
        cases = {
            'heredoc': ("python3 - <<'EOF'\nfrom pathlib import Path\np = Path(%r)\np.write_text('x')\nEOF" % str(absolute),
                        lambda: self.write('notes/b.md', 'python\n', append=True), 'Bash'),
            'append': ("printf 'Claude'un notu\\n' >> notes/a.md", lambda: self.write('notes/a.md', 'x\n', append=True), 'Bash'),
            'glob': ("sed -i 's/eski/yeni/' notes/*.md", lambda: self.write('notes/b.md', 'glob\n', append=True), 'Bash'),
            'cd': ('cd notes && tee -a a.md < /dev/null', lambda: self.write('notes/a.md', 'tee\n', append=True), 'Bash'),
            'powershell': ('Add-Content -Path "notes/a.md" -Value x', lambda: self.write('notes/a.md', 'ps\n', append=True),
                           'PowerShell'),
        }
        for name, (command, write, tool) in cases.items():
            with self.subTest(name):
                self.prompt(name, 'düzenle')
                self.shell(name, command, write, tool=tool)
                self.assertEqual(self.stop(name).get('decision'), 'block')

    def test_reads_outside_writes_and_unnamed_paths_leave_stop_quiet(self):
        outside = self.root / 'outside.md'
        self.write('.obsidian/workspace.md', 'x\n')
        changed_before = self.write('notes/b.md', 'başka yazar\n', append=True)
        os.utime(changed_before, (time.time() - 30, time.time() - 30))
        cases = {
            'read': ('cat notes/a.md notes/b.md', None),
            'outside': ('cat > %s <<EOF\nx\nEOF' % outside, lambda: outside.write_text('x\n', encoding='utf-8')),
            'dot-folder': ('echo x >> .obsidian/workspace.md', lambda: self.write('.obsidian/workspace.md', 'y\n', append=True)),
            'variable': ('NOTE=notes/a; echo x >> "$NOTE.md"', lambda: self.write('notes/a.md', 'var\n', append=True)),
            'script': ('python3 tools/fix_links.py', lambda: self.write('notes/b.md', 'script\n', append=True)),
        }
        for name, (command, write) in cases.items():
            with self.subTest(name):
                self.prompt(name, 'bak')
                self.shell(name, command, write)
                self.assertEqual(self.stop(name), {})
                self.assertFalse(self.marker(name).exists())
        # Codex keeps the editor-only window: the same transcript opens nothing.
        self.prompt('codex', 'düzenle')
        self.shell('codex', 'echo x >> notes/a.md', lambda: self.write('notes/a.md', 'codex\n', append=True))
        self.assertEqual(self.stop('codex', harness='codex'), {})

    def test_a_later_write_to_a_note_the_command_only_read_is_not_this_sessions_edit(self):
        self.prompt('late', 'notu oku', at=time.time() - 12)
        self.shell('late', 'cat notes/a.md "notes/Günlük Not.md"', at=time.time() - 10)
        # Another session or the editor writes the same notes long after the read finished.
        self.write('notes/a.md', 'başka oturum\n', append=True)
        self.write('notes/Günlük Not.md', 'editör\n', append=True)
        self.assertEqual(self.stop('late'), {})
        self.assertFalse(self.marker('late').exists())

    def test_receipt_views_rewritten_by_the_sync_are_not_this_sessions_edit(self):
        self.prompt('views', 'özetlere bak')
        self.shell('views', 'cat daily/v3/2026-11-07.md knowledge/v3/outcomes.md', lambda: (
            self.write('daily/v3/2026-11-07.md', 'görünüm\n'), self.write('knowledge/v3/outcomes.md', 'görünüm\n')))
        self.assertEqual(self.stop('views'), {})

    def test_only_the_current_turn_counts(self):
        now = time.time()
        self.prompt('turns', 'ilk tur', at=now - 20)
        self.shell('turns', 'echo x >> notes/a.md', lambda: self.write('notes/a.md', 'x\n', append=True), at=now - 19)
        os.utime(self.vault / 'notes/a.md', (now - 18, now - 18))
        self.engine().receipt('turns-1', 'İlk tur bitti.', ['notes/a.md'], 'claude', session=digest('turns'))
        self.assertEqual(self.stop('turns'), {})
        # A later read of the same note is no new edit.
        self.prompt('turns', 'tekrar oku', at=now - 2)
        self.shell('turns', 'cat notes/a.md')
        self.assertEqual(self.stop('turns'), {})
        # A later write is, and the earlier receipt does not cover it.
        self.prompt('turns', 'bir satır daha')
        self.shell('turns', 'echo y >> notes/a.md', lambda: self.write('notes/a.md', 'y\n', append=True))
        self.assertEqual(self.stop('turns').get('decision'), 'block')

    def test_subagent_meta_and_tool_result_records_are_not_turn_boundaries(self):
        self.prompt('nested', 'işi yap')
        self.shell('nested', 'echo x >> notes/a.md', lambda: self.write('notes/a.md', 'x\n', append=True))
        self.record('nested', {'type': 'user', 'isSidechain': True, 'message': {'role': 'user', 'content': 'alt görev'}})
        self.record('nested', {'type': 'user', 'isMeta': True, 'message': {'role': 'user', 'content': 'Caveat'}})
        self.record('nested', {'type': 'user', 'message': {'role': 'user', 'content': [
            {'type': 'tool_result', 'tool_use_id': 'x', 'content': 'notes/a.md'}]}})
        self.assertEqual(self.stop('nested').get('decision'), 'block')

    def test_unusable_transcripts_fail_open(self):
        self.prompt('t', 'düzenle')
        self.shell('t', 'echo x >> notes/a.md', lambda: self.write('notes/a.md', 'x\n', append=True))
        log = self.logs / 't.jsonl'
        copy = self.root / 't.txt'
        copy.write_bytes(log.read_bytes())
        broken = self.root / 'broken.jsonl'
        broken.write_bytes(b'{not json\n\xff\xfe\n' + log.read_bytes()[:40])
        for index, value in enumerate((None, '', 't.jsonl', str(copy), str(self.logs), str(broken), str(self.root / 'yok.jsonl'), 7)):
            with self.subTest(value=value):
                self.assertEqual(self.stop('t', transcript_path=value), {})
        link = self.root / 'link.jsonl'
        try:
            link.symlink_to(log)
        except (OSError, NotImplementedError):
            pass  # Windows without the symlink privilege
        else:
            self.assertEqual(self.stop('t', transcript_path=str(link)), {})
        self.assertFalse(self.marker('t').exists())
        self.assertEqual(self.stop('t').get('decision'), 'block')

    def test_opt_outs_skip_the_transcript(self):
        self.prompt('quiet', 'bunu [kaydetme]')
        self.shell('quiet', 'echo x >> notes/a.md', lambda: self.write('notes/a.md', 'x\n', append=True))
        self.assertEqual(self.stop('quiet'), {})
        self.prompt('env', 'düzenle')
        self.shell('env', 'echo x >> notes/a.md', lambda: self.write('notes/a.md', 'x\n', append=True))
        self.env['BEYIN_V3_NO_RECEIPT_REMINDER'] = '1'
        self.assertEqual(self.stop('env'), {})
        del self.env['BEYIN_V3_NO_RECEIPT_REMINDER']
        self.assertEqual(self.stop('env').get('decision'), 'block')

    def test_knowledge_note_written_through_the_shell_counts_as_distillation(self):
        summary = 'Kavram yazıldı.\nÖğrenilen: WAL zaman aşımı en az 5 sn.'
        for name, target, expected in (('kavram', 'knowledge/concepts/wal.md', {}), ('not', 'notes/a.md', 'block')):
            with self.subTest(name):
                self.prompt(name, 'öğrenimi yaz')
                self.shell(name, 'cat >> ' + target + " <<'EOF'\nWAL\nEOF", lambda: self.write(target, 'WAL\n', append=True))
                time.sleep(0.01)
                self.engine().receipt(name + '-1', summary, ['notes/b.md'], 'claude', session=digest(name))
                answer = self.stop(name)
                self.assertEqual(answer if expected == {} else answer.get('decision'), expected)

    # -- coverage ---------------------------------------------------------------------------
    def test_shell_edited_turn_closes_coverage_despite_a_later_question(self):
        self.engine().sync()
        self.prompt('covered', 'notu güncelle')
        self.shell('covered', 'echo x >> notes/a.md', lambda: self.write('notes/a.md', 'x\n', append=True))
        time.sleep(0.01)
        self.engine().receipt('covered-1', 'Not güncellendi.', ['notes/a.md'], 'claude', session=digest('covered'))
        self.assertEqual(self.stop('covered'), {})
        time.sleep(0.01)
        self.prompt('covered', 'teşekkürler, durum ne?')
        self.assertEqual(self.stop('covered'), {})
        self.prompt('question', 'kaç not var?')
        self.assertEqual(self.stop('question'), {})
        import beyin_v3_hook as hook
        self.assertEqual(hook.drain_queue(self.vault, self.state)['failed'], 0)
        gaps = json.loads((self.state / 'receipt-gaps.json').read_text(encoding='utf-8'))
        self.assertEqual([item['session'] for item in gaps['checkpoints']], [digest('question')])
        self.assertTrue(gaps['checkpoints'][0].get('no_observed_edit'))
        coverage = gaps['receipt_coverage']
        self.assertEqual((coverage['covered'], coverage['total'], coverage['missing_no_observed_edit']), (1, 2, 1))
        events = [json.loads(path.read_text(encoding='utf-8')) for path in (self.state / 'hook-done').glob('*.json')]
        shell = [event for event in events if event.get('observed') == 'shell']
        self.assertEqual([(event['event'], event['session']) for event in shell], [('PostToolUse', digest('covered'))])

    def test_doctor_names_missing_sessions_without_observed_edits(self):
        sys.path.insert(0, str(ROOT / 'scripts'))
        self.addCleanup(lambda: sys.path.remove(str(ROOT / 'scripts')))
        import beyin_entry
        coverage = {'ratio': 0.25, 'covered': 1, 'total': 4, 'missing': 3, 'missing_no_observed_edit': 2,
                    'last_7d': {'ratio': 0.25, 'total': 4}}
        text = beyin_entry.human_result({'status': 'ok', 'receipt_coverage': coverage}, 'doctor')
        self.assertIn('Makbuz kapsami: %25 (1/4 oturum, son 7 gun: %25); 2 eksik oturumda duzenleme gozlenmedi', text)
        text = beyin_entry.human_result({'status': 'ok', 'receipt_coverage': dict(coverage, missing_no_observed_edit=0)}, 'doctor')
        self.assertIn('Makbuz kapsami: %25 (1/4 oturum, son 7 gun: %25)\n', text + '\n')
        self.assertNotIn('gozlenmedi', text)


class ShellEditUnitTest(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(SCRIPTS))
        self.addCleanup(lambda: sys.path.remove(str(SCRIPTS)))
        import beyin_v3_shell_edits
        self.module = beyin_v3_shell_edits

    def test_markdown_tokens(self):
        tokens = self.module.markdown_tokens
        self.assertEqual(tokens("sed -i '' 's/a/b/' \"🎯 100-Merkez/İş Notu.md\" notes/a.md>notes/b.md"),
                         ['🎯 100-Merkez/İş Notu.md', 'notes/a.md', 'notes/b.md'])
        self.assertEqual(tokens('cp x Şifre\\ Notu.md && beyin.py receipt --ref=notes/c.md'), ['Şifre Notu.md', 'notes/c.md'])
        self.assertEqual(tokens('cat "$HOME/x.md" https://example.test/a.md ${D}/b.md notes/{a,b}.md'), [])
        self.assertEqual(tokens('Set-Content -Path notes\\a.md -Value "x"', powershell=True), ['notes\\a.md'])
        self.assertEqual(tokens('a.mdx README.MD'), ['README.MD'])

    def test_turn_commands_reads_back_to_the_prompt_and_pairs_results(self):
        def line(entry):
            return json.dumps(entry, separators=(',', ':')).encode()

        def call(tool_id, command, name='Bash', at='2026-11-07T10:00:05.000Z'):
            return {'type': 'assistant', 'timestamp': at, 'cwd': '/v', 'message': {
                'content': [{'type': 'tool_use', 'id': tool_id, 'name': name, 'input': {'command': command}}]}}

        def result(tool_id, at='2026-11-07T10:00:07.000Z'):
            return {'type': 'user', 'timestamp': at, 'message': {'content': [
                {'tool_use_id': tool_id, 'type': 'tool_result', 'content': 'ok'}]}}

        lines = [line({'type': 'user', 'message': {'content': 'eski'}}),
                 line(call('t0', 'old')), line(result('t0')),
                 line({'type': 'user', 'message': {'content': [{'type': 'text', 'text': 'yeni tur'}]}}),
                 line(call('t1', 'echo new >> a.md')), line({'type': 'progress', 'data': 'x'}), line(result('t1')),
                 line(call('t2', 'naive', at='2026-11-07T10:00:05')), line(result('t2')),  # no zone: skipped
                 line(call('t3', 'x', name='Read')), line(result('t3')),
                 line(call('t4', 'still running')),  # no result yet: skipped
                 b'{broken', b'']
        found = self.module.turn_commands(lines)
        self.assertEqual([(command, cwd) for _, _, command, cwd, _ in found], [('echo new >> a.md', '/v')])
        self.assertEqual(found[0][:2], (datetime(2026, 11, 7, 10, 0, 5, tzinfo=timezone.utc).timestamp(),
                                        datetime(2026, 11, 7, 10, 0, 7, tzinfo=timezone.utc).timestamp()))

    def test_dated_daily_note_counts_unless_the_index_owns_it_as_a_receipt_view(self):
        import sqlite3
        with tempfile.TemporaryDirectory(prefix='v3-shell-daily-') as folder:
            root = Path(folder).resolve()
            vault, state = root / 'vault', root / 'state'
            (vault / 'daily').mkdir(parents=True)
            state.mkdir()
            for day in ('2026-11-06', '2026-11-07'):
                (vault / 'daily' / (day + '.md')).write_text('# ' + day + '\n', encoding='utf-8')
            with sqlite3.connect(state / 'memory.sqlite3') as db:
                db.execute('CREATE TABLE receipt_flat_views(path TEXT PRIMARY KEY, hash TEXT)')
                db.execute("INSERT INTO receipt_flat_views VALUES ('daily/2026-11-07.md', 'x')")
            db.close()
            log = root / 'log.jsonl'
            for day, expected in (('2026-11-07', None), ('2026-11-06', 'hit')):
                with self.subTest(day):
                    log.write_text(json.dumps({'type': 'assistant', 'timestamp': stamp(time.time() - 1), 'message': {
                        'content': [{'type': 'tool_use', 'id': 'd', 'name': 'Bash', 'input': {'command': 'cat daily/' + day + '.md'}}]}})
                                   + '\n' + json.dumps({'type': 'user', 'timestamp': stamp(time.time()), 'message': {
                                       'content': [{'type': 'tool_result', 'tool_use_id': 'd', 'content': ''}]}}) + '\n',
                                   encoding='utf-8')
                    found = self.module.shell_edit({'transcript_path': str(log), 'cwd': str(vault)}, vault, state)
                    self.assertEqual(found and 'hit', expected)
            # Without readable ownership a dated daily note is treated as a view.
            log.write_text(log.read_text(encoding='utf-8'), encoding='utf-8')
            self.assertIsNone(self.module.shell_edit({'transcript_path': str(log), 'cwd': str(vault)}, vault, None))

    def test_tail_bound_keeps_the_latest_commands(self):
        with tempfile.TemporaryDirectory(prefix='v3-shell-tail-') as folder:
            root = Path(folder).resolve()
            vault = root / 'vault'
            (vault / 'notes').mkdir(parents=True)
            note = vault / 'notes/a.md'
            note.write_text('x\n', encoding='utf-8')
            log = root / 'log.jsonl'
            at = stamp(time.time() - 1)
            entries = [{'type': 'user', 'message': {'content': 'tur'}}]
            entries += [{'type': 'user', 'message': {'content': [{'type': 'tool_result', 'content': 'y' * 2000}]}}] * 4
            entries += [{'type': 'assistant', 'timestamp': at, 'message': {'content': [
                {'type': 'tool_use', 'id': 'w', 'name': 'Bash', 'input': {'command': 'echo x >> notes/a.md'}}]}},
                        {'type': 'user', 'timestamp': stamp(time.time()), 'message': {'content': [
                            {'type': 'tool_result', 'tool_use_id': 'w', 'content': ''}]}}]
            log.write_text(''.join(json.dumps(entry) + '\n' for entry in entries), encoding='utf-8')
            payload = {'transcript_path': str(log), 'cwd': str(vault)}
            with patch.object(self.module, 'TAIL_BYTES', 4096):
                self.assertIsNotNone(self.module.shell_edit(payload, vault, root / 'state'))
            log.write_text(''.join(json.dumps(entry) + '\n' for entry in entries[:1] + entries[-2:] + entries[1:-2]),
                           encoding='utf-8')
            with patch.object(self.module, 'TAIL_BYTES', 4096):
                self.assertIsNone(self.module.shell_edit(payload, vault, root / 'state'))
            self.assertIsNotNone(self.module.shell_edit(payload, vault, root / 'state'))


if __name__ == '__main__':
    unittest.main()
