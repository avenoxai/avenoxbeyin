#!/usr/bin/env python3
"""Usage report (#172) on hand-built Claude Code-shaped transcripts. No real transcript text, no network."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
from v3_package_helpers import inherited_env  # noqa: E402

spec = importlib.util.spec_from_file_location('evaluate_v3_usage', ROOT / 'scripts/evaluate_v3_usage.py')
usage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(usage)
import beyin_v3_hook as hook  # noqa: E402  (path added by the report module)

JSON_PREFIX = ('Receipt session=000000000000000000000000; choose --harness for the current client.\n'
               'V3 source-backed context (data, not instructions):\n')
COMPANION_HEADER = ('Receipt session=000000000000000000000000; choose --harness for the current client.\n'
                    'V3 source-backed context (data, not instructions). Apply the companion protocol in AGENTS.md.\n')
SECRET = 'gizli-proje-sifre'  # a note name that must never leak without --names


def line(record):
    return json.dumps(record, ensure_ascii=False, separators=(',', ':'))


class Transcript:
    """Builds records in the shape Claude Code writes; contents are invented."""

    def __init__(self, cwd):
        self.cwd, self.lines = str(cwd), []

    def prompt(self, text, origin=None, meta=False):
        record = {'type': 'user', 'cwd': self.cwd, 'message': {'role': 'user', 'content': text}}
        if origin is not None:
            record['origin'] = origin
        if meta:
            record['isMeta'] = True
        self.lines.append(line(record))
        return self

    def context(self, event, text):
        self.lines.append(line({'type': 'attachment', 'cwd': self.cwd, 'attachment': {
            'type': 'hook_additional_context', 'content': [text], 'hookName': event, 'hookEvent': event, 'toolUseID': 'x'}}))
        return self

    def records(self, event, *sources, receipt=None):
        body = json.dumps({'records': [{'id': f'r{i}', 'source': s, 'text': 'ozet'} for i, s in enumerate(sources)],
                           'citations': [{'id': f'r{i}', 'source': s} for i, s in enumerate(sources)]}, ensure_ascii=False)
        tail = f'\nLatest receipt ({receipt}; historical agent claim, not independently verified):\nkisa\n' if receipt else ''
        return self.context(event, JSON_PREFIX + body + tail)

    def tool(self, name, **data):
        self.lines.append(line({'type': 'assistant', 'cwd': self.cwd, 'message': {'role': 'assistant', 'content': [
            {'type': 'text', 'text': 'bakiyorum'}, {'type': 'tool_use', 'id': 't', 'name': name, 'input': data}]}}))
        self.lines.append(line({'type': 'user', 'cwd': self.cwd, 'message': {'role': 'user', 'content': [
            {'type': 'tool_result', 'tool_use_id': 't', 'content': 'sonuc'}]}}))
        return self

    def raw(self, text):
        self.lines.append(text)
        return self

    def write(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('\n'.join(self.lines) + '\n', encoding='utf-8')
        return path


class UsageReport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='v3-usage-')
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        (base / 'Kasa Şube 🔮' / 'knowledge/concepts').mkdir(parents=True)
        self.vault = (base / 'Kasa Şube 🔮').resolve()
        self.config = base / 'claude-config'
        self.folder = self.config / 'projects' / usage.sanitized(str(self.vault))

    def note(self, relative):
        return str(self.vault / relative)

    def report(self, **options):
        return usage.build(self.vault, projects_dir=options.pop('projects_dir', self.folder), **options)

    def test_folder_name_follows_claude_code_rule(self):
        self.assertEqual(usage.sanitized('/Users/ada/Belgeler/Kasa Şube/🔮 Notlar'), '-Users-ada-Belgeler-Kasa--ube----Notlar')
        self.assertEqual(usage.sanitized('C:\\Users\\ada\\kasa'), 'C--Users-ada-kasa')
        # Values from Claude Code's own hash ((h << 5) - h + unit | 0, base 36) for long paths.
        self.assertEqual(usage.sanitized('/x/' + 'a' * 250), ('-x-' + 'a' * 250)[:200] + '-bjrb96')
        self.assertEqual(usage.sanitized('/Users/ada/🔮 Ş/' + 'b' * 240).rsplit('-', 1)[1], 'cll0t5')
        Transcript(self.vault).records('SessionStart', 'knowledge/a.md').write(self.folder / 's.jsonl')
        old = os.environ.get('CLAUDE_CONFIG_DIR')
        os.environ['CLAUDE_CONFIG_DIR'] = str(self.config)
        try:
            found = usage.build(self.vault)
        finally:
            if old is None:
                os.environ.pop('CLAUDE_CONFIG_DIR')
            else:
                os.environ['CLAUDE_CONFIG_DIR'] = old
        self.assertEqual(found['project_folders'], 1)
        self.assertEqual(found['main']['ranked']['all']['suggested'], 1)

    def test_json_sources_rank_event_and_two_bounds(self):
        t = Transcript(self.vault)
        t.tool('Read', file_path=self.note('knowledge/concepts/once.md'))  # before the injection: not an answer to it
        t.records('SessionStart', 'knowledge/concepts/once.md')
        t.prompt('deploy ayari ne', origin={'kind': 'human'})
        t.records('UserPromptSubmit', 'knowledge/concepts/a.md', 'knowledge/concepts/b.md', 'knowledge/concepts/c.md',
                  'knowledge/concepts/d.md', 'knowledge/concepts/e.md', receipt='receipts/r1.md')
        t.tool('Read', file_path=self.note('knowledge/concepts/a.md'))
        t.tool('Grep', pattern='port', path='knowledge/concepts/b.md')
        t.tool('Bash', command='cat "knowledge/concepts/c.md" | head -20 2>/dev/null')
        t.tool('Bash', command="sed -i '' 's/x/y/' knowledge/concepts/d.md")
        t.tool('Bash', command='git add knowledge/concepts/d.md && git commit -m x')
        t.tool('Bash', command='echo hi > knowledge/concepts/e.md')
        t.tool('Bash', command='cp knowledge/concepts/e.md /tmp/kopya.md')
        t.write(self.folder / 's.jsonl')
        main = self.report()['main']
        ranked = main['ranked']
        self.assertEqual(ranked['all']['suggested'], 6)
        self.assertEqual(ranked['all']['opened_read_grep'], 2)
        self.assertEqual(ranked['all']['opened_with_shell'], 3)
        self.assertEqual(ranked['by_event']['SessionStart']['suggested'], 1)
        self.assertEqual(ranked['by_event']['SessionStart']['opened_read_grep'], 0)
        self.assertEqual(ranked['by_event']['UserPromptSubmit']['opened_with_shell'], 3)
        self.assertEqual([ranked['by_rank'][r]['suggested'] for r in ('1', '2', '3', '4+')], [2, 1, 1, 2])
        self.assertEqual(ranked['by_rank']['1']['opened_read_grep'], 1)
        self.assertEqual(ranked['by_rank']['3']['opened_with_shell'], 1)
        self.assertEqual(ranked['by_rank']['4+']['opened_with_shell'], 0)
        self.assertEqual(ranked['all']['rate_read_grep'], round(2 / 6, 4))
        self.assertEqual(main['latest_receipt']['suggested'], 1)
        self.assertEqual(main['shell_commands'], 5)
        self.assertEqual(main['shell_commands_write_excluded'], 4)

    def test_companion_files_are_their_own_rows(self):
        companion = '🔮 850-Companion/'
        text = (COMPANION_HEADER + f'\n[{companion}Core.md]\n# Kimlik\nkisa ve tam\n[not bir etiket]\n'
                f'\n[{companion}Kurallar.md]\n\n[truncated: 900 characters omitted here; read source]\nson kural\n'
                f'\n[{companion}Threads.md]\n## Aktif\n- is\n'
                '\n[Knowledge map: knowledge/index.md]\n- [[harita]]\n'
                '\n[Related source: knowledge/concepts/ilgili.md]\nmetin\n'
                '\n[Related source: knowledge/concepts/ikinci.md]\nmetin\n'
                '\nLatest receipt (receipts/son.md; historical agent claim, not independently verified):\nkisa\n')
        t = Transcript(self.vault).context('SessionStart', text)
        t.tool('Read', file_path=self.note(companion + 'Kurallar.md'))
        t.tool('Bash', command=f'cd "{companion[:-1]}" && cat Core.md')
        t.tool('Read', file_path=self.note('knowledge/concepts/ilgili.md'))
        t.write(self.folder / 's.jsonl')
        main = self.report()['main']
        full, clipped = main['companion']['full'], main['companion']['clipped']
        self.assertEqual(full['all']['suggested'], 2)
        self.assertEqual(set(full) - {'all'}, {'Core.md', 'Threads.md'})
        self.assertEqual(full['Core.md']['opened_read_grep'], 0)
        self.assertEqual(full['Core.md']['opened_with_shell'], 1)
        self.assertEqual(clipped['Kurallar.md']['opened_read_grep'], 1)
        self.assertEqual(main['knowledge_map']['suggested'], 1)
        self.assertEqual(main['ranked']['all']['suggested'], 2)
        self.assertEqual(main['ranked']['by_rank']['1']['opened_read_grep'], 1)
        self.assertEqual(main['ranked']['by_rank']['2']['opened_read_grep'], 0)
        self.assertEqual(main['latest_receipt']['suggested'], 1)
        # The bare 'Core.md' resolved against the cd target must not count as an unsuggested note.
        self.assertEqual(main['unsuggested_opened']['with_shell'], {'opens': 0, 'distinct_notes': 0})

    def test_bare_file_name_outside_the_vault_is_another_file(self):
        code = Path(self.tmp.name) / 'kod'
        t = Transcript(code).prompt('soru', origin={'kind': 'human'})
        t.records('UserPromptSubmit', 'projeler/README.md', 'projeler/plan.md')
        t.tool('Bash', command='cat README.md')  # the code repo's own README
        t.tool('Bash', command=f'cat {code}/plan.md')
        t.write(self.folder / 'a.jsonl')
        self.assertEqual(self.report()['main']['ranked']['all']['opened_with_shell'], 0)
        t.tool('Bash', command=f'cd "{self.vault}/projeler" && head -5 plan.md')
        t.write(self.folder / 'a.jsonl')
        self.assertEqual(self.report()['main']['ranked']['all']['opened_with_shell'], 1)

    def test_reverse_direction(self):
        t = Transcript(self.vault)
        t.tool('Read', file_path=self.note('knowledge/concepts/onceden.md'))  # no V3 context yet
        t.prompt('soru', origin={'kind': 'human'}).records('UserPromptSubmit', 'knowledge/concepts/a.md')
        t.tool('Read', file_path=self.note('knowledge/concepts/a.md'))
        t.tool('Read', file_path=self.note('knowledge/concepts/x.md'))
        t.tool('Read', file_path=self.note('knowledge/concepts/x.md'))
        t.tool('Read', file_path=self.note('.agents/skills/beyin/SKILL.md'))
        t.tool('Read', file_path='/tmp/baska-yer/not.md')
        t.tool('Bash', command='rg -n port knowledge/concepts/y.md')
        t.write(self.folder / 's.jsonl')
        reverse = self.report()['main']['unsuggested_opened']
        self.assertEqual(reverse['read_grep'], {'opens': 2, 'distinct_notes': 1})
        self.assertEqual(reverse['with_shell'], {'opens': 3, 'distinct_notes': 2})

    def test_synthetic_turns_use_the_hook_classifier(self):
        self.assertIs(usage.is_synthetic_prompt, hook.is_synthetic_prompt)
        t = Transcript(self.vault)
        t.prompt('<task-notification>\n<task-id>b1</task-id>\n</task-notification>')  # older transcript, no origin
        t.records('UserPromptSubmit', 'knowledge/concepts/a.md')
        t.prompt('alt ajan bitti', origin={'kind': 'task-notification'}, meta=True)
        t.records('UserPromptSubmit', 'knowledge/concepts/b.md')
        t.prompt([{'type': 'text', 'text': 'gercek soru'}], origin={'kind': 'human'})
        t.records('UserPromptSubmit', 'knowledge/concepts/c.md')
        t.tool('Read', file_path=self.note('knowledge/concepts/a.md'))
        t.write(self.folder / 's.jsonl')
        main = self.report()['main']
        self.assertEqual(main['contexts'], {'SessionStart': 0, 'UserPromptSubmit': 1, 'UserPromptSubmit_synthetic': 2})
        self.assertEqual(main['synthetic_turns']['suggested'], 2)
        self.assertEqual(main['synthetic_turns']['opened_read_grep'], 1)
        self.assertEqual(main['ranked']['all']['suggested'], 1)

    def test_subagent_transcripts_are_counted_separately(self):
        Transcript(self.vault).records('SessionStart', 'knowledge/concepts/a.md').write(self.folder / 'oturum.jsonl')
        sub = Transcript(self.vault).prompt('gorev', origin={'kind': 'human'})
        sub.records('UserPromptSubmit', 'knowledge/concepts/a.md')
        sub.tool('Read', file_path=self.note('knowledge/concepts/a.md'))
        sub.write(self.folder / 'oturum' / 'subagents' / 'agent-1.jsonl')
        report = self.report()
        self.assertEqual(report['main']['transcripts'], 1)
        self.assertEqual(report['main']['ranked']['all']['opened_read_grep'], 0)
        self.assertEqual(report['subagent']['transcripts'], 1)
        self.assertEqual(report['subagent']['ranked']['all']['opened_read_grep'], 1)
        self.assertEqual(report['subagent']['vault_note_opens']['read_grep'], 1)

    def test_broken_and_unknown_records_are_skipped_and_counted(self):
        t = Transcript(self.vault)
        t.raw('{"type":"user","message":')  # cut-off line
        t.raw('[{"type": "user"}]')
        t.context('SessionStart', 'baska bir kancanin metni')
        t.context('UserPromptSubmit', JSON_PREFIX + '{"records": [')
        t.raw(line({'type': 'attachment', 'attachment': {'type': 'hook_additional_context', 'content': {'x': 1},
                                                          'hookEvent': 'SessionStart'}}))
        t.raw(line({'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'name': 'Read', 'input': 'x'}]}}))
        t.records('SessionStart', 'knowledge/concepts/a.md')
        t.tool('Read', file_path=self.note('knowledge/concepts/a.md'))
        t.write(self.folder / 's.jsonl')
        report = self.report()
        skipped = report['skipped']
        self.assertEqual(skipped['malformed_lines'], 1)
        self.assertEqual(skipped['unrecognized_records'], 1)
        self.assertEqual(skipped['other_hook_contexts'], 1)
        self.assertEqual(skipped['unrecognized_contexts'], 2)
        self.assertEqual(skipped['unrecognized_tool_inputs'], 1)
        self.assertEqual(report['main']['ranked']['all']['opened_read_grep'], 1)

    def test_days_window_uses_file_time(self):
        Transcript(self.vault).records('SessionStart', 'knowledge/concepts/a.md').write(self.folder / 'yeni.jsonl')
        old = Transcript(self.vault).records('SessionStart', 'knowledge/concepts/b.md').write(self.folder / 'eski.jsonl')
        stamp = time.time() - 20 * 86400
        os.utime(old, (stamp, stamp))
        self.assertEqual(self.report(days=14)['main']['transcripts'], 1)
        self.assertEqual(self.report(days=0)['main']['transcripts'], 2)

    def test_names_stay_out_unless_asked_and_nothing_is_written(self):
        t = Transcript(self.vault).prompt('soru', origin={'kind': 'human'})
        t.records('UserPromptSubmit', f'knowledge/concepts/{SECRET}.md')
        t.tool('Read', file_path=self.note(f'knowledge/concepts/{SECRET}-iki.md'))
        t.write(self.folder / 's.jsonl')
        before = snapshot(Path(self.tmp.name))
        script = ROOT / 'scripts/evaluate_v3_usage.py'
        env = inherited_env(CLAUDE_CONFIG_DIR=str(self.config), PYTHONIOENCODING='utf-8')
        outputs = {}
        for flags in ([], ['--human'], ['--names'], ['--names', '--human']):
            result = subprocess.run([sys.executable, str(script), '--vault', str(self.vault), *flags],
                                    cwd=self.tmp.name, env=env, capture_output=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr.decode('utf-8', 'replace'))
            outputs[' '.join(flags)] = result.stdout.decode('utf-8')
        self.assertEqual(json.loads(outputs[''])['main']['ranked']['all']['suggested'], 1)
        for key in ('', '--human'):
            self.assertNotIn(SECRET, outputs[key])
            self.assertNotIn('concepts/', outputs[key])
            self.assertNotIn(str(self.vault), outputs[key])
        named = json.loads(outputs['--names'])['main']['names']
        self.assertEqual(named['suggested'][0]['source'], f'knowledge/concepts/{SECRET}.md')
        self.assertEqual(named['unsuggested_opened'][0]['source'], f'knowledge/concepts/{SECRET}-iki.md')
        self.assertIn(SECRET, outputs['--names --human'])
        self.assertEqual(snapshot(Path(self.tmp.name)), before)

    def test_parses_the_hooks_own_output(self):
        """The fixtures imitate the format; this ties the parser to the code that writes it."""
        import beyin_v3 as runtime
        import beyin_v3_companion as companion
        store = runtime.MemoryStore(Path(self.tmp.name) / 'runtime', self.vault)
        self.addCleanup(lambda: getattr(store, 'close', lambda: None)())
        bodies = {'Core.md': '# Kimlik\nkisa\n[parantezli satir]\n',
                  'Kurallar.md': '# Kurallar\n' + ''.join(f'- kural {i}: uzun bir kural metni burada.\n' for i in range(200)),
                  'Last-Session.md': '# Son oturum\nprototip denendi\n'}
        notes = dict({f'🔮 850-Companion/{name}': body for name, body in bodies.items()},
                     **{'knowledge/concepts/yedekleme.md': '# Yedekleme\nYedekleme zirkon kovasina her gece yazar.\n'})
        for source, body in notes.items():
            (self.vault / source).parent.mkdir(parents=True, exist_ok=True)
            (self.vault / source).write_text(body, encoding='utf-8')
            store.ingest({'id': source, 'kind': 'note', 'status': 'active', 'text': body, 'source': source,
                          'updated_at': '2026-09-18T10:00:00Z'})
        from beyin_v3_sync import SyncEngine
        SyncEngine(self.vault, Path(self.tmp.name) / 'runtime').receipt(
            'son', 'kisa', ['knowledge/concepts/yedekleme.md'], 'claude')
        receipt_source = 'receipts/' + hashlib.sha256(b'son').hexdigest() + '.md'
        receipt = hook.receipt_context(self.vault, Path(self.tmp.name) / 'runtime')
        opening = companion.context(store, 6000, 'oturum', 'claude', 'yedekleme kovasi', receipt)
        items = usage.parse_context(opening)
        kinds = {(kind, source, detail) for kind, source, detail in items}
        self.assertIn(('companion', '🔮 850-Companion/Core.md', 'full'), kinds)
        self.assertIn(('companion', '🔮 850-Companion/Kurallar.md', 'clipped'), kinds)
        self.assertIn(('ranked', 'knowledge/concepts/yedekleme.md', 1), kinds)
        self.assertIn(('receipt', receipt_source, None), kinds)
        turn = store.context_for('claude', 'yedekleme zirkon kovasi', budget_chars=2000, strict=True)
        text, delivered = runtime.render_context(turn, 2000, prefix=JSON_PREFIX, suffix=receipt)
        self.assertTrue(delivered['records'])
        self.assertEqual(usage.parse_context(text)[0], ('ranked', 'knowledge/concepts/yedekleme.md', 1))
        self.assertEqual(usage.parse_context(text)[-1], ('receipt', receipt_source, None))

    def test_not_in_release_package(self):
        builder = importlib.util.spec_from_file_location('beyin_release_builder', ROOT / 'scripts/build_v3_release.py')
        module = importlib.util.module_from_spec(builder)
        builder.loader.exec_module(module)
        target = Path(self.tmp.name) / 'paket.zip'
        with contextlib.redirect_stdout(io.StringIO()):
            module.build(target, '3.0.0')
        with zipfile.ZipFile(target) as archive:
            self.assertFalse([name for name in archive.namelist() if 'usage' in name])

    def test_utf16_units_converts_ascii_unicode_and_surrogates(self):
        """utf16_units properly extracts 16-bit code units including surrogate pairs (#172)."""
        self.assertEqual(usage.utf16_units(''), [])
        self.assertEqual(usage.utf16_units('abc'), [97, 98, 99])
        self.assertEqual(usage.utf16_units('türkçe'), [116, 252, 114, 107, 231, 101])
        # Emoji 😀 (U+1F600) splits into high surrogate 0xD83D (55357) and low surrogate 0xDE00 (56832)
        self.assertEqual(usage.utf16_units('😀'), [55357, 56832])
        # Mixed string
        self.assertEqual(usage.utf16_units('A😀B'), [65, 55357, 56832, 66])
        # Lone surrogate with surrogatepass
        self.assertEqual(usage.utf16_units('\ud800'), [0xd800])


def snapshot(root):
    return {str(p.relative_to(root)): (hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None)
            for p in sorted(root.rglob('*')) if '__pycache__' not in p.parts}


if __name__ == '__main__':
    unittest.main()
