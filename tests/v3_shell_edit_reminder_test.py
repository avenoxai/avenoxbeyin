#!/usr/bin/env python3
"""#212: vault Markdown edits made through the Claude Code shell tool reach the Stop receipt
reminder and receipt coverage. Synthetic vaults and transcripts only."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
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

    def cli_write(self, session, name):
        env = dict(self.env, CLAUDE_CODE_SESSION_ID=session)
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/beyin_v3.py'), '--vault', str(self.vault),
                                 '--state', str(self.state), 'note-create', '--file', '-'],
                                input=json.dumps({'source': 'notes/' + name + '.md', 'text': '# Sentetik ' + name + '\n'}),
                                text=True, encoding='utf-8', capture_output=True, cwd=self.vault, env=env, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return time.time()

    def test_beyin_cli_write_through_the_shell_closes_coverage_despite_a_later_question(self):
        # A note or task written with beyin.py from the Bash tool opens the edit window itself
        # (mark_session_edit), so the transcript is never read for it. Its own flag file still
        # reaches coverage as an observed edit at the write's time; a later CLI turn needs its receipt.
        import beyin_v3_hook as hook
        self.engine().sync()
        flag = self.state / 'receipt-reminders' / (hashlib.sha256(b'cli').hexdigest() + '.cli')
        self.prompt('cli', 'not yaz')
        written = []
        self.shell('cli', 'python3 beyin.py note-create --file -', lambda: written.append(self.cli_write('cli', 'cli')))
        self.assertLessEqual(json.loads(flag.read_text(encoding='utf-8'))['at'], written[0])
        time.sleep(0.01)
        self.engine().receipt('cli-1', 'Not yazıldı.', ['notes/cli.md'], 'claude', session=digest('cli'))
        self.assertEqual(self.stop('cli'), {})
        self.assertFalse(flag.exists())
        time.sleep(0.01)
        self.prompt('cli', 'teşekkürler, durum ne?')
        self.assertEqual(self.stop('cli'), {})
        self.assertEqual(hook.drain_queue(self.vault, self.state)['failed'], 0)
        gaps = json.loads((self.state / 'receipt-gaps.json').read_text(encoding='utf-8'))
        self.assertEqual(gaps['checkpoints'], [])
        self.assertEqual((gaps['receipt_coverage']['covered'], gaps['receipt_coverage']['total']), (1, 1))
        events = [json.loads(path.read_text(encoding='utf-8')) for path in (self.state / 'hook-done').glob('*.json')]
        cli = [event for event in events if event.get('observed') == 'cli']
        self.assertEqual([(event['event'], event['session']) for event in cli], [('PostToolUse', digest('cli'))])
        self.assertLessEqual(cli[0]['at'], written[0])
        # A later turn that writes again through the CLI is reminded and stays missing without a receipt.
        time.sleep(0.01)
        self.prompt('cli', 'bir not daha yaz')
        self.shell('cli', 'python3 beyin.py note-create --file -', lambda: self.cli_write('cli', 'cli-2'))
        self.assertEqual(self.stop('cli').get('decision'), 'block')
        self.assertEqual(hook.drain_queue(self.vault, self.state)['failed'], 0)
        gaps = json.loads((self.state / 'receipt-gaps.json').read_text(encoding='utf-8'))
        self.assertEqual([item['session'] for item in gaps['checkpoints']], [digest('cli')])
        self.assertFalse(gaps['checkpoints'][0].get('no_observed_edit'))

    def test_beyin_cli_flag_reaches_coverage_after_both_reminders_and_only_for_claude(self):
        # [kaydetme] spends both one-shot reminders, as an editor session can: the CLI edit still
        # reaches coverage, like an editor PostToolUse. A Codex Stop never consumes the Claude flag.
        import beyin_v3_hook as hook
        self.engine().sync()
        flag = self.state / 'receipt-reminders' / (hashlib.sha256(b'quiet-cli').hexdigest() + '.cli')
        self.prompt('quiet-cli', 'not yaz [kaydetme]')
        self.cli_write('quiet-cli', 'quiet')
        self.assertEqual(self.stop('quiet-cli', harness='codex'), {})
        self.assertTrue(flag.exists())
        self.assertEqual(self.stop('quiet-cli', stop_hook_active=True), {})
        self.assertFalse(flag.exists())
        self.assertEqual(hook.drain_queue(self.vault, self.state)['failed'], 0)
        gaps = json.loads((self.state / 'receipt-gaps.json').read_text(encoding='utf-8'))
        self.assertEqual([item['session'] for item in gaps['checkpoints']], [digest('quiet-cli')])
        self.assertFalse(gaps['checkpoints'][0].get('no_observed_edit'))

    def test_bridge_and_pi_keep_their_own_windows_alongside_cli_flags(self):
        # Integration of #212 with the global bridge (#500, PR #505) and Pi (PR #385).
        import beyin_v3_hook as hook
        import beyin_v3_shell_edits
        project = self.root / 'Projects' / 'Örnek'
        project.mkdir(parents=True)
        cli = 'python3 ' + str(self.vault / 'beyin.py')

        def stop(session, **extra):
            payload = {'hook_event_name': 'Stop', 'session_id': session, 'cwd': str(project),
                       'transcript_path': str(self.logs / 'bridge.jsonl')}
            return hook.receipt_reminder(payload, self.state, 'claude', 'Stop', vault=self.vault, **extra)

        # The bridge keeps its editor-only window: an external session's transcript is never read.
        with patch.object(beyin_v3_shell_edits, 'shell_edit', return_value=None) as scan:
            self.assertIsNone(stop(json.dumps([str(project), 'quiet']), bridge_cli=cli))
            scan.assert_not_called()
            self.assertIsNone(stop('quiet'))  # the vault hook still scans its own transcript
            self.assertEqual(scan.call_count, 1)
        # A Claude CLI write from the project folder uses the bridge's session namespace; the
        # bridge Stop consumes its flag, queues the observed edit and reminds once.
        env = dict(self.env, CLAUDE_CODE_SESSION_ID='ext')
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/beyin_v3.py'), '--vault', str(self.vault),
                                 '--state', str(self.state), 'note-create', '--file', '-'],
                                input=json.dumps({'source': 'notes/dis.md', 'text': '# Dış proje\n'}),
                                text=True, encoding='utf-8', capture_output=True, cwd=project, env=env, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        namespaced = json.dumps([str(project.resolve()), 'ext'])
        folder = self.state / 'receipt-reminders'
        flag = folder / (hashlib.sha256(namespaced.encode('utf-8')).hexdigest() + '.cli')
        self.assertTrue(flag.exists())
        self.assertFalse((folder / (hashlib.sha256(b'ext').hexdigest() + '.cli')).exists())
        blocked = stop(namespaced, bridge_cli=cli)
        self.assertEqual(blocked['decision'], 'block')
        self.assertIn(cli, blocked['reason'])
        self.assertFalse(flag.exists())
        queued = [json.loads(path.read_text(encoding='utf-8')) for path in (self.state / 'hook-queue').glob('*.json')]
        self.assertEqual([(event['event'], event['observed'], event['session']) for event in queued
                          if event.get('observed')], [('PostToolUse', 'cli', digest(namespaced))])
        # Pi opens its window from PI_SESSION_ID but leaves no Claude-only flag nobody would consume.
        env = dict(self.env, PI_SESSION_ID='pi-cli')
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/beyin_v3.py'), '--vault', str(self.vault),
                                 '--state', str(self.state), 'note-create', '--file', '-'],
                                input=json.dumps({'source': 'notes/pi.md', 'text': '# Pi\n'}),
                                text=True, encoding='utf-8', capture_output=True, cwd=self.vault, env=env, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.marker('pi-cli').exists())
        self.assertFalse((folder / (hashlib.sha256(b'pi-cli').hexdigest() + '.cli')).exists())

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

    def test_receipt_views_match_case_insensitively_and_git_bash_drive_paths_map_on_windows(self):
        # A case-insensitive volume keeps the typed case in resolve(): `Daily/V3/...` is the sync's view.
        with tempfile.TemporaryDirectory(prefix='v3-shell-case-') as folder:
            probe = self.module._Probe(Path(folder), None)
            for relative in ('Daily/V3/2026-11-07.md', 'KNOWLEDGE/v3/Outcomes.md', 'Daily/2026-11-07.md'):
                self.assertTrue(probe.generated(relative), relative)
            self.assertFalse(probe.generated('Notlar/Daily/V3/2026-11-07.md'))
            probe.views = {'daily/2026-11-07.md'}
            self.assertTrue(probe.generated('DAILY/2026-11-07.md'))
            self.assertFalse(probe.generated('Daily/2026-11-06.md'))
        msys = self.module.msys_drive
        self.assertEqual(msys('/c/Users/x/Kasa Ölçüm/notes/a.md'), 'C:/Users/x/Kasa Ölçüm/notes/a.md')
        self.assertEqual(msys('/cygdrive/d/v/a.md'), 'D:/v/a.md')
        self.assertEqual(msys('/c'), 'C:/')
        for text in ('/home/x/a.md', '/cd/a.md', 'c/a.md', 'notes/a.md'):
            self.assertEqual(msys(text), text)
        if os.name == 'nt':
            base = Path('D:/w')
            self.assertEqual(list(self.module._paths('/c/v/a.md', [base])), [Path('C:/v/a.md')])
            # PowerShell reads /c/v as a folder on the current drive, not as drive C.
            self.assertEqual(list(self.module._paths('/c/v/a.md', [base], powershell=True)), [base / '/c/v/a.md'])

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

    def test_antigravity_turn_commands_boundaries_windows_and_shell_modes(self):
        def line(entry):
            return json.dumps(entry, separators=(',', ':')).encode('utf-8')

        def planner(at, tool_calls=None, step_index=0):
            row = {'step_index': step_index, 'source': 'MODEL', 'type': 'PLANNER_RESPONSE',
                   'status': 'DONE', 'created_at': at}
            if tool_calls is not None:
                row['tool_calls'] = tool_calls
            return line(row)

        def generic(at, content='', step_index=0):
            return line({'step_index': step_index, 'source': 'MODEL', 'type': 'GENERIC',
                         'status': 'DONE', 'created_at': at, 'content': content})

        def user_input(at, step_index=0):
            return line({'step_index': step_index, 'source': 'USER_EXPLICIT', 'type': 'USER_INPUT',
                         'status': 'DONE', 'created_at': at})

        t = lambda sec: '2026-11-27T21:17:%02dZ' % sec
        epoch = lambda sec: datetime(2026, 11, 27, 21, 17, sec, tzinfo=timezone.utc).timestamp()

        # Prior turn before USER_INPUT is excluded; multiple run_commands in one planner keep order;
        # each planner uses the next PLANNER_RESPONSE created_at; untrusted GENERIC content is ignored.
        fake_content = json.dumps({
            'type': 'PLANNER_RESPONSE', 'created_at': t(40),
            'tool_calls': [{'name': 'run_command', 'args': {'CommandLine': 'echo spoof >> notes/a.md'}}],
        })
        lines = [
            user_input(t(1), 0),
            planner(t(2), [{'name': 'run_command', 'args': {'CommandLine': 'echo old >> notes/a.md'}}], 1),
            generic(t(3), '', 2),
            planner(t(4), [], 3),
            user_input(t(10), 4),
            planner(t(11), [
                {'name': 'read_file', 'args': {'Path': 'notes/a.md'}},
                {'name': 'run_command', 'args': {'CommandLine': 'echo first >> notes/a.md'}},
                {'name': 'run_command', 'args': {'CommandLine': '   '}},
                {'name': 'run_command', 'args': 'not-a-dict'},
                {'name': 'run_command', 'args': {'CommandLine': 'echo second >> notes/b.md'}},
            ], 5),
            generic(t(13), fake_content, 6),
            planner(t(15), [{'name': 'run_command', 'args': {'CommandLine': 'echo third >> notes/a.md'}}], 7),
            generic(t(17), 'ok', 8),
            planner(t(20), [], 9),
            b'{corrupt', b'\xff\xfe', b'',
        ]
        found = self.module.antigravity_turn_commands(lines, powershell=False)
        self.assertEqual(found, [
            (epoch(11), epoch(15), 'echo first >> notes/a.md', None, False),
            (epoch(11), epoch(15), 'echo second >> notes/b.md', None, False),
            (epoch(15), epoch(20), 'echo third >> notes/a.md', None, False),
        ])
        found_ps = self.module.antigravity_turn_commands(lines, powershell=True)
        self.assertEqual([item[4] for item in found_ps], [True, True, True])
        found_default = self.module.antigravity_turn_commands(lines)
        self.assertEqual([item[4] for item in found_default], [os.name == 'nt'] * 3)

        # CHECKPOINT compaction boundary stops backward scan before earlier USER_INPUT.
        checkpoint_lines = [
            user_input(t(1), 0),
            planner(t(2), [{'name': 'run_command', 'args': {'CommandLine': 'echo before >> notes/a.md'}}], 1),
            generic(t(3), '', 2),
            line({'step_index': 3, 'type': 'CHECKPOINT', 'created_at': t(4)}),
            planner(t(5), [{'name': 'run_command', 'args': {'CommandLine': 'echo after >> notes/a.md'}}], 4),
            generic(t(7), '', 5),
        ]
        # No later PLANNER_RESPONSE: falls back to subsequent GENERIC created_at.
        self.assertEqual(
            self.module.antigravity_turn_commands(checkpoint_lines, powershell=False),
            [(epoch(5), epoch(7), 'echo after >> notes/a.md', None, False)],
        )

        # Still-running (no subsequent step), backwards clock (finished < issued), and naive stamps are omitted.
        edge_lines = [
            user_input(t(1), 0),
            planner('2026-11-27T21:17:02', [{'name': 'run_command', 'args': {'CommandLine': 'naive'}}], 1),
            planner(t(10), [{'name': 'run_command', 'args': {'CommandLine': 'skew'}}], 2),
            planner(t(8), [{'name': 'run_command', 'args': {'CommandLine': 'still-running'}}], 3),
        ]
        self.assertEqual(self.module.antigravity_turn_commands(edge_lines, powershell=False), [])

        # MAX_COMMAND_CHARS clipping and MAX_COMMANDS cap.
        huge = 'echo ' + ('x' * (self.module.MAX_COMMAND_CHARS + 50)) + ' >> notes/a.md'
        clipped = self.module.antigravity_turn_commands([
            user_input(t(1), 0),
            planner(t(2), [{'name': 'run_command', 'args': {'CommandLine': huge}}], 1),
            planner(t(3), [], 2),
        ], powershell=False)
        self.assertEqual(len(clipped), 1)
        self.assertEqual(len(clipped[0][2]), self.module.MAX_COMMAND_CHARS)
        with patch.object(self.module, 'MAX_COMMANDS', 2):
            capped = self.module.antigravity_turn_commands([
                user_input(t(1), 0),
                planner(t(2), [
                    {'name': 'run_command', 'args': {'CommandLine': 'cmd1'}},
                    {'name': 'run_command', 'args': {'CommandLine': 'cmd2'}},
                    {'name': 'run_command', 'args': {'CommandLine': 'cmd3'}},
                ], 1),
                planner(t(3), [], 2),
            ], powershell=False)
            self.assertEqual([item[2] for item in capped], ['cmd2', 'cmd3'])

    def test_tail_rejects_unc_reparse_point_and_samestat_mismatch(self):
        with tempfile.TemporaryDirectory(prefix='v3-shell-sec-') as folder:
            root = Path(folder).resolve()
            log = root / 'ok.jsonl'
            log.write_text('{"type":"USER_INPUT"}\n', encoding='utf-8')
            self.assertIsNotNone(self.module._tail(str(log)))
            for unc in (r'\\server\share\transcript.jsonl', '//server/share/transcript.jsonl',
                        r'\\?\C:\transcript.jsonl'):
                with self.subTest(unc=unc):
                    self.assertIsNone(self.module._tail(unc))
            real_lstat = log.lstat()

            class FakeReparse:
                st_mode = real_lstat.st_mode
                st_ino = real_lstat.st_ino
                st_dev = real_lstat.st_dev
                st_size = real_lstat.st_size
                st_file_attributes = stat.FILE_ATTRIBUTE_REPARSE_POINT

            with patch.object(Path, 'lstat', return_value=FakeReparse()):
                self.assertIsNone(self.module._tail(str(log)))
            with patch('os.path.samestat', return_value=False):
                self.assertIsNone(self.module._tail(str(log)))

    def test_shell_edit_harness_dispatch_and_powershell_vs_posix_quoting(self):
        with tempfile.TemporaryDirectory(prefix='v3-shell-ag-unit-') as folder:
            root = Path(folder).resolve()
            vault = root / 'vault'
            (vault / 'notes').mkdir(parents=True)
            note = vault / 'notes/a.md'
            note.write_text('# a\n', encoding='utf-8')
            now = time.time()
            os.utime(note, (now, now))
            log = root / 'ag.jsonl'
            log.write_text(
                json.dumps({'step_index': 0, 'type': 'USER_INPUT', 'created_at': stamp(now - 3)}) + '\n'
                + json.dumps({'step_index': 1, 'type': 'PLANNER_RESPONSE', 'created_at': stamp(now - 2),
                              'tool_calls': [{'name': 'run_command', 'args': {'CommandLine': 'echo x >> notes/a.md'}}]}) + '\n'
                + json.dumps({'step_index': 2, 'type': 'PLANNER_RESPONSE', 'created_at': stamp(now + 1)}) + '\n',
                encoding='utf-8',
            )
            # Unsupported harness returns None.
            self.assertIsNone(self.module.shell_edit({'transcriptPath': str(log), 'cwd': str(vault)}, vault,
                                                     harness='codex'))
            # Antigravity resolves relative paths from workspacePaths even without cwd.
            hit = self.module.shell_edit({'transcriptPath': str(log), 'workspacePaths': ['relative/skip', str(vault)]},
                                         vault, harness='antigravity')
            self.assertIsNotNone(hit)
            self.assertFalse(hit['knowledge'])
            # PowerShell-style quoting keeps Windows backslashes; POSIX unescapes spaces.
            self.assertEqual(
                self.module.markdown_tokens(r'Set-Content -Path "C:\vault\note.md" -Value "ok"', powershell=True),
                [r'C:\vault\note.md'],
            )
            self.assertEqual(
                self.module.markdown_tokens(r'echo x >> notes/Şifre\ Notu.md', powershell=False),
                ['notes/Şifre Notu.md'],
            )


class AntigravityShellEditReminderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='v3-ag-shell-edit-')
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
        self.steps = {}
        self.count = 0

    def write(self, relative, text, append=False):
        path = self.vault / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a' if append else 'w', encoding='utf-8') as handle:
            handle.write(text)
        return path

    def step(self, session, entry, at=None):
        index = self.steps.get(session, 0)
        self.steps[session] = index + 1
        row = dict({'step_index': index, 'status': 'DONE',
                    'created_at': stamp(time.time() if at is None else at)}, **entry)
        with (self.logs / (session + '.jsonl')).open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(',', ':')) + '\n')
        return row

    def user_turn(self, session, text, at=None):
        self.step(session, {'source': 'USER_EXPLICIT', 'type': 'USER_INPUT', 'content': text}, at=at)
        return self.hook('PreInvocation', session, invocationNum=0, lastUserInput=text)

    def run_cmd(self, session, command, write=None, issued=None, generic_at=None, done_at=None, generic_content=''):
        base = time.time() if issued is None else issued
        self.step(session, {'source': 'MODEL', 'type': 'PLANNER_RESPONSE',
                            'tool_calls': [{'name': 'run_command', 'args': {'CommandLine': command}}]}, at=base)
        if write:
            write()
        self.step(session, {'source': 'MODEL', 'type': 'GENERIC', 'content': generic_content},
                  at=base if generic_at is None else generic_at)
        self.step(session, {'source': 'MODEL', 'type': 'PLANNER_RESPONSE'},
                  at=base if done_at is None else done_at)

    def hook(self, event, session, **fields):
        self.count += 1
        payload = dict({
            'hookEventName': event,
            'conversationId': session,
            'event_id': '%s-%d' % (session, self.count),
            'workspacePaths': [str(self.vault)],
            'transcriptPath': str(self.logs / (session + '.jsonl')),
        }, **fields)
        result = subprocess.run(
            [sys.executable, str(HOOK), '--vault', str(self.vault), '--state', str(self.state),
             '--harness', 'antigravity', '--event', event],
            input=json.dumps(payload), text=True, encoding='utf-8',
            capture_output=True, cwd=self.vault, env=self.env, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1, result.stdout)
        return json.loads(lines[0])

    def stop(self, session, **fields):
        defaults = {'fullyIdle': True, 'continuationActive': False, 'executionNum': 0, 'lastUserInput': 'edit'}
        defaults.update(fields)
        return self.hook('Stop', session, **defaults)

    def engine(self):
        from beyin_v3_sync import SyncEngine
        return SyncEngine(self.vault, self.state)

    def marker(self, session):
        return self.state / 'receipt-reminders' / (hashlib.sha256(session.encode('utf-8')).hexdigest() + '.edited')

    def test_run_command_edit_opens_window_queues_observed_edit_and_continues_once(self):
        self.user_turn('ag-edit', 'notları düzelt')
        before = time.time()
        self.run_cmd('ag-edit', 'sed -i "" "s/a/b/" "notes/Günlük Not.md"  # ' + CANARY,
                     lambda: self.write('notes/Günlük Not.md', 'yeni\n', append=True))
        # Non-idle Stop does not open the window or remind.
        self.assertEqual(self.stop('ag-edit', fullyIdle=False), {'decision': 'stop'})
        self.assertFalse(self.marker('ag-edit').exists())

        res = self.stop('ag-edit')
        self.assertEqual(res.get('decision'), 'continue')
        self.assertIn('Receipt session=' + digest('ag-edit') + ';', res['reason'])
        self.assertIn('--harness antigravity', res['reason'])
        marker = json.loads(self.marker('ag-edit').read_text(encoding='utf-8'))
        self.assertEqual(set(marker), {'at'})
        self.assertGreaterEqual(marker['at'], before - 1)
        self.assertLessEqual(marker['at'], time.time())

        queued = [json.loads(p.read_text(encoding='utf-8')) for p in (self.state / 'hook-queue').glob('*.json')]
        shell_events = [e for e in queued if e.get('observed') == 'shell']
        self.assertEqual(
            [(e['harness'], e['event'], e['session']) for e in shell_events],
            [('antigravity', 'PostToolUse', digest('ag-edit'))],
        )

        # Continued Stops and repeat Stops do not remind a second time.
        self.assertEqual(self.stop('ag-edit', continuationActive=True), {'decision': 'stop'})
        self.assertEqual(self.stop('ag-edit', executionNum=1), {'decision': 'stop'})
        self.assertEqual(self.stop('ag-edit'), {'decision': 'stop'})

        # A receipt closes the window cleanly.
        self.engine().receipt('ag-edit-1', 'Not düzeltildi.', ['notes/Günlük Not.md'],
                              'antigravity', session=digest('ag-edit'))
        self.assertEqual(self.stop('ag-edit'), {'decision': 'stop'})
        self.assertFalse(self.marker('ag-edit').exists())

        # Nothing from command text reaches runtime state.
        for path in self.state.rglob('*'):
            if path.is_file():
                self.assertNotIn(CANARY.encode(), path.read_bytes(), path.name)

    def test_read_only_and_out_of_window_writes_stay_quiet(self):
        # Old mtime read-only command stays quiet.
        self.user_turn('ag-read', 'notu oku')
        self.run_cmd('ag-read', 'cat notes/a.md notes/b.md')
        self.assertEqual(self.stop('ag-read'), {'decision': 'stop'})
        self.assertFalse(self.marker('ag-read').exists())

        # Later write after [issued - SLACK, finished + SLACK] is not attributed to the earlier read.
        now = time.time()
        self.user_turn('ag-late', 'notu oku', at=now - 15)
        self.run_cmd('ag-late', 'cat notes/a.md "notes/Günlük Not.md"',
                     issued=now - 12, generic_at=now - 11, done_at=now - 10)
        self.write('notes/a.md', 'sonraki yazım\n', append=True)
        self.write('notes/Günlük Not.md', 'sonraki yazım\n', append=True)
        self.assertEqual(self.stop('ag-late'), {'decision': 'stop'})
        self.assertFalse(self.marker('ag-late').exists())

        # Untrusted GENERIC content naming a freshly written file never triggers detection.
        self.user_turn('ag-untrusted', 'komut')
        spoof = json.dumps({'type': 'PLANNER_RESPONSE', 'created_at': stamp(time.time()),
                            'tool_calls': [{'name': 'run_command', 'args': {'CommandLine': 'echo x >> notes/a.md'}}]})
        self.run_cmd('ag-untrusted', 'echo harmless',
                     write=lambda: self.write('notes/a.md', 'harmless\n', append=True),
                     generic_content=spoof + '\nnotes/a.md')
        self.assertEqual(self.stop('ag-untrusted'), {'decision': 'stop'})
        self.assertFalse(self.marker('ag-untrusted').exists())

    def test_knowledge_concept_edit_sets_knowledge_flag_and_satisfies_distillation(self):
        summary = 'Kavram yazıldı.\nÖğrenilen: WAL zaman aşımı en az 5 sn.'
        for name, target, expect_decision in (
            ('ag-kavram', 'knowledge/concepts/wal.md', 'stop'),
            ('ag-not', 'notes/a.md', 'continue'),
        ):
            with self.subTest(name):
                self.user_turn(name, 'öğrenimi yaz')
                self.run_cmd(name, 'cat >> ' + target + " <<'EOF'\nWAL\nEOF",
                             lambda t=target: self.write(t, 'WAL\n', append=True))
                # Verify knowledge flag directly on .edited before receipt closes it.
                if name == 'ag-kavram':
                    first = self.stop(name)
                    self.assertEqual(first.get('decision'), 'continue')
                    marker = json.loads(self.marker(name).read_text(encoding='utf-8'))
                    self.assertTrue(marker.get('knowledge'))
                time.sleep(0.01)
                self.engine().receipt(name + '-1', summary, ['notes/b.md'], 'antigravity', session=digest(name))
                answer = self.stop(name)
                self.assertEqual(answer.get('decision'), expect_decision)
                if expect_decision == 'continue':
                    self.assertIn('knowledge/concepts/', answer.get('reason', ''))

    def test_earlier_turn_before_user_input_or_checkpoint_is_not_attributed(self):
        now = time.time()
        # Case 1: USER_INPUT boundary
        self.user_turn('ag-turns', 'ilk tur', at=now - 20)
        self.run_cmd('ag-turns', 'echo x >> notes/a.md',
                     lambda: self.write('notes/a.md', 'x\n', append=True),
                     issued=now - 19, generic_at=now - 18, done_at=now - 17)
        os.utime(self.vault / 'notes/a.md', (now - 18, now - 18))
        self.user_turn('ag-turns', 'yeni soru', at=now - 2)
        self.step('ag-turns', {'source': 'MODEL', 'type': 'PLANNER_RESPONSE'}, at=now - 1)
        self.assertEqual(self.stop('ag-turns'), {'decision': 'stop'})
        self.assertFalse(self.marker('ag-turns').exists())

        # Case 2: CHECKPOINT compaction boundary
        self.user_turn('ag-ckpt', 'uzun tur', at=now - 20)
        self.run_cmd('ag-ckpt', 'echo x >> notes/b.md',
                     lambda: self.write('notes/b.md', 'x\n', append=True),
                     issued=now - 19, generic_at=now - 18, done_at=now - 17)
        os.utime(self.vault / 'notes/b.md', (now - 18, now - 18))
        self.step('ag-ckpt', {'source': 'SYSTEM', 'type': 'CHECKPOINT'}, at=now - 5)
        self.step('ag-ckpt', {'source': 'MODEL', 'type': 'PLANNER_RESPONSE'}, at=now - 1)
        self.assertEqual(self.stop('ag-ckpt'), {'decision': 'stop'})
        self.assertFalse(self.marker('ag-ckpt').exists())

    def test_outside_vault_dot_folder_and_receipt_views_stay_quiet(self):
        outside = self.root / 'outside.md'
        self.write('.obsidian/workspace.md', 'x\n')
        cases = {
            'outside': ('echo x >> "%s"' % outside, lambda: outside.write_text('x\n', encoding='utf-8')),
            'dot-folder': ('echo x >> .obsidian/workspace.md',
                           lambda: self.write('.obsidian/workspace.md', 'y\n', append=True)),
            'daily-view': ('cat daily/v3/2026-11-07.md',
                           lambda: self.write('daily/v3/2026-11-07.md', 'görünüm\n')),
            'outcomes-view': ('cat knowledge/v3/outcomes.md',
                              lambda: self.write('knowledge/v3/outcomes.md', 'görünüm\n')),
        }
        for name, (command, write) in cases.items():
            with self.subTest(name):
                self.user_turn(name, 'kontrol')
                self.run_cmd(name, command, write)
                self.assertEqual(self.stop(name), {'decision': 'stop'})
                self.assertFalse(self.marker(name).exists())

    def test_unusable_transcript_paths_fail_open(self):
        self.user_turn('ag-bad', 'düzenle')
        self.run_cmd('ag-bad', 'echo x >> notes/a.md', lambda: self.write('notes/a.md', 'x\n', append=True))
        log = self.logs / 'ag-bad.jsonl'
        copy = self.root / 'ag-bad.txt'
        copy.write_bytes(log.read_bytes())
        broken = self.root / 'ag-broken.jsonl'
        broken.write_bytes(b'{not json\n\xff\xfe\n')
        for value in (None, '', 'ag-bad.jsonl', str(copy), str(self.logs), str(broken),
                      str(self.root / 'missing.jsonl'), r'\\server\share\ag.jsonl', '//server/share/ag.jsonl', 7):
            with self.subTest(value=value):
                self.assertEqual(self.stop('ag-bad', transcriptPath=value), {'decision': 'stop'})
        link = self.root / 'ag-link.jsonl'
        try:
            link.symlink_to(log)
        except (OSError, NotImplementedError):
            pass  # Windows without symlink privilege
        else:
            self.assertEqual(self.stop('ag-bad', transcriptPath=str(link)), {'decision': 'stop'})
        self.assertFalse(self.marker('ag-bad').exists())
        # Real transcript still works afterwards.
        self.assertEqual(self.stop('ag-bad').get('decision'), 'continue')

    def test_powershell_and_posix_paths_and_bridge_guard(self):
        import beyin_v3_hook as hook
        import beyin_v3_shell_edits
        absolute_note = self.vault / 'notes' / 'a.md'
        notes_dir = self.vault / 'notes'
        cases = {
            'ps-abs': ('Set-Content -Path "%s" -Value "x"' % absolute_note,
                       lambda: self.write('notes/a.md', 'ps-abs\n', append=True)),
            'ps-set-location': ('Set-Location "%s"; Add-Content -Path a.md -Value "x"' % notes_dir,
                                lambda: self.write('notes/a.md', 'ps-sl\n', append=True)),
            'posix-cd': ('cd notes && printf "x\\n" >> b.md',
                         lambda: self.write('notes/b.md', 'posix-cd\n', append=True)),
            'posix-rel': ('echo x >> "notes/Günlük Not.md"',
                          lambda: self.write('notes/Günlük Not.md', 'posix-rel\n', append=True)),
        }
        for name, (command, write) in cases.items():
            with self.subTest(name):
                self.user_turn(name, 'düzenle')
                self.run_cmd(name, command, write)
                self.assertEqual(self.stop(name).get('decision'), 'continue')

        # Global bridge keeps shell_edit disabled for Antigravity too (bridge_cli is not None).
        project = self.root / 'Projects' / 'External'
        project.mkdir(parents=True)
        payload = {'hook_event_name': 'Stop', 'session_id': 'ag-bridge',
                   'workspacePaths': [str(project)], 'transcriptPath': str(self.logs / 'ag-bridge.jsonl')}
        with patch.object(beyin_v3_shell_edits, 'shell_edit', return_value=None) as scan:
            self.assertIsNone(hook.receipt_reminder(payload, self.state, 'antigravity', 'Stop',
                                                    vault=self.vault, bridge_cli='python3 beyin.py'))
            scan.assert_not_called()
            self.assertIsNone(hook.receipt_reminder(payload, self.state, 'antigravity', 'Stop', vault=self.vault))
            self.assertEqual(scan.call_count, 1)

    def test_antigravity_shell_edit_closes_coverage_despite_later_question(self):
        import beyin_v3_hook as hook
        self.engine().sync()
        self.user_turn('ag-cov', 'notu güncelle')
        self.run_cmd('ag-cov', 'echo x >> notes/a.md', lambda: self.write('notes/a.md', 'x\n', append=True))
        time.sleep(0.01)
        self.engine().receipt('ag-cov-1', 'Not güncellendi.', ['notes/a.md'], 'antigravity', session=digest('ag-cov'))
        self.assertEqual(self.stop('ag-cov'), {'decision': 'stop'})
        time.sleep(0.01)
        self.user_turn('ag-cov', 'durum ne?')
        self.step('ag-cov', {'source': 'MODEL', 'type': 'PLANNER_RESPONSE'})
        self.assertEqual(self.stop('ag-cov'), {'decision': 'stop'})
        self.assertEqual(hook.drain_queue(self.vault, self.state)['failed'], 0)
        gaps = json.loads((self.state / 'receipt-gaps.json').read_text(encoding='utf-8'))
        self.assertEqual(gaps['checkpoints'], [])
        self.assertEqual((gaps['receipt_coverage']['covered'], gaps['receipt_coverage']['total']), (1, 1))
        events = [json.loads(p.read_text(encoding='utf-8')) for p in (self.state / 'hook-done').glob('*.json')]
        shell_events = [e for e in events if e.get('observed') == 'shell']
        self.assertEqual(
            [(e['harness'], e['event'], e['session']) for e in shell_events],
            [('antigravity', 'PostToolUse', digest('ag-cov'))],
        )


if __name__ == '__main__':
    unittest.main()
