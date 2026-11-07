#!/usr/bin/env python3
"""Field report (#172): are the sources the V3 hook injected later opened by the agent?

Developer tool, not part of the release package. Reads Claude Code transcripts of ONE vault
(~/.claude/projects/<folder derived from the vault path>/*.jsonl and */subagents/*.jsonl),
line by line. Read-only, offline, no model: it writes nothing to the vault, to the V3 state
directory or anywhere else, and opens no network connection.

Injected context comes only from `attachment.type == "hook_additional_context"` records that
carry the hook's `V3 source-backed context` marker. Sources are taken from the hook's own
structure, never by searching note names in free text:

  per-turn JSON   `V3 source-backed context (data, not instructions):` + JSON whose
                  records[].source are vault-relative paths (rank = order in records)
  companion text  `[<source>]` sections of the companion files, `[Related source: <source>]`
                  (rank = order), `[Knowledge map: knowledge/index.md]` and
                  `Latest receipt (<source>; ...`

An injected source counts as opened when the same transcript opens it after the injection:

  read_grep   a Read of that file, or a Grep whose path is that file
  with_shell  the above, or a Bash/PowerShell command naming that file, except commands
              that look like writes (git add/commit/mv/rm/stash/..., sed -i, cp/mv/rm/tee,
              Set-Content/Out-File/..., redirection into a file, Python write calls).
              A shell mention is not proof of reading: this is the upper bound.

The reverse direction counts vault notes (.md, outside dot-folders) opened in a transcript
with V3 context although no injection before that point named them. Turns that the hook
itself treats as synthetic (beyin_v3_hook.is_synthetic_prompt, imported, not copied) and
subagent transcripts are counted separately. Transcript records are an undocumented Claude
Code format: unrecognized records are skipped and counted, never guessed.

Default output is counts and ratios only. Note paths appear only with --names.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
from pathlib import Path
import posixpath
import re
import shlex
import sys
import time
import unicodedata

sys.dont_write_bytecode = True  # importing the hook module must not leave __pycache__ behind
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
from beyin_v3_hook import is_synthetic_prompt  # noqa: E402  same classifier the hook uses
# Claude Code's folder rule, shared with the packaged chat-trace backfill (#599).
from beyin_v3_chat_backfill import claude_projects_root, project_dirs, sanitized, utf16_units  # noqa: E402,F401


def synthetic_turn(prompt):
    """The hook's classifier, independent of this machine's BEYIN_V3_FILTER_HARNESS_TURNS opt-out:
    the report counts what a turn was, not whether retrieval was filtered for it."""
    saved = os.environ.pop('BEYIN_V3_FILTER_HARNESS_TURNS', None)
    try:
        return is_synthetic_prompt(prompt)
    finally:
        if saved is not None:
            os.environ['BEYIN_V3_FILTER_HARNESS_TURNS'] = saved

MARKER = 'V3 source-backed context'
JSON_MARKER = MARKER + ' (data, not instructions):\n'
COMPANION_NAMES = ('Core.md', 'Soul.md', 'Kurallar.md', 'Last-Session.md', 'Threads.md', 'Journal.md')
LABEL = re.compile(r'\n\[([^\]\n]+)\]\n')
RECEIPT = re.compile(r'\nLatest receipt \(([^;\n]+); historical agent claim')
TRUNCATED = '[truncated:'
READ_TOOLS = ('Read', 'Grep')
SHELL_TOOLS = ('Bash', 'PowerShell')
EVENTS = ('SessionStart', 'UserPromptSubmit')
ABSOLUTE = re.compile(r'^(?:/|~|[A-Za-z]:/)')
CHANGES_DIR = re.compile(r'(?:^|[;&|(\n])\s*(?:cd|pushd|Set-Location|sl)\s', re.I)
WRITE = re.compile(r"""
    \bgit\s+(?:-C\s+\S+\s+)?(?:add|commit|mv|rm|stash|checkout|restore|apply|reset)\b
  | \bsed\b[^|;&\n]*\s(?:-[a-zA-Z]*i|--in-place)
  | \bperl\b[^|;&\n]*\s-[a-zA-Z]*i
  | (?:^|[;&|(\n]|\bxargs|\bsudo)\s*(?:mv|rm|cp|touch|tee|truncate|ln|install|rsync|dd|unlink|chmod|chown)\b(?=\s)
  | \b(?:Set-Content|Add-Content|Out-File|New-Item|Remove-Item|Move-Item|Copy-Item|Rename-Item|Clear-Content)\b
  | (?<![0-9&>])>{1,2}(?!&)\s*(?!/dev/null|\$null|NUL\b)[^\s&|;]
  | \bwrite_(?:text|bytes)\(|\.write\(|\bopen\([^)\n]*['"][wax]b?\+?['"]
  | \bbeyin\.py\s+(?:receipt|task-create|task-update)
""", re.X | re.I)


def nfc(text):
    return unicodedata.normalize('NFC', text)


def slash(path):
    return nfc(str(path)).replace('\\', '/')


def transcripts(folder, since):
    """(path, is_subagent) for main transcripts and <session>/subagents/*.jsonl, newest window only."""
    for path, subagent in [(p, False) for p in sorted(folder.glob('*.jsonl'))] + \
                          [(p, True) for p in sorted(folder.glob('*/subagents/*.jsonl'))]:
        try:
            if path.is_file() and not path.is_symlink() and (since is None or path.stat().st_mtime >= since):
                yield path, subagent
        except OSError:
            continue


class Roots:
    """Map tool paths to vault-relative POSIX paths without touching the file system."""

    def __init__(self, vault):
        roots = set()
        for path in (os.path.abspath(vault), os.path.realpath(vault)):
            root = slash(path).rstrip('/')
            roots.add(root)
            if root.startswith('/private/'):
                roots.add(root[len('/private'):])  # macOS /tmp and /var spellings
        self.roots = sorted(roots, key=len, reverse=True)
        self.home = slash(Path.home())

    def absolute(self, path, cwd):
        path = slash(path).strip()
        if path.startswith('~/'):
            path = self.home + path[1:]
        if not (path.startswith('/') or re.match(r'^[A-Za-z]:/', path)):
            path = slash(cwd or self.roots[0]).rstrip('/') + '/' + path
        drive = re.match(r'^[A-Za-z]:', path)
        head, rest = (path[:2], path[2:]) if drive else ('', path)
        return head + posixpath.normpath(rest)

    def relative(self, path, cwd):
        if not isinstance(path, str) or not path.strip():
            return None
        path = self.absolute(path, cwd)
        for root in self.roots:
            if len(path) > len(root) + 1 and path[len(root)] == '/' and path[:len(root)].casefold() == root.casefold():
                return path[len(root) + 1:]
        return None


def clean_source(source):
    source = slash(source).strip()
    while source.startswith('./'):
        source = source[2:]
    return source


def is_note(relative):
    return relative.lower().endswith('.md') and not any(part.startswith('.') for part in relative.split('/'))


def parse_context(text):
    """Return injected items [(kind, source, detail)] or None when the V3 marker is not parseable.

    kind: ranked (detail=rank), companion (detail='full'|'clipped'), map, receipt.
    """
    start = text.find(JSON_MARKER)
    if start >= 0:
        try:
            data, end = json.JSONDecoder().raw_decode(text, start + len(JSON_MARKER))
        except ValueError:
            return None
        records = data.get('records') if isinstance(data, dict) else None
        if not isinstance(records, list):
            return None
        items, seen = [], set()
        for record in records:
            source = record.get('source') if isinstance(record, dict) else None
            if not isinstance(source, str) or not source.strip():
                return None
            source = clean_source(source)
            if source not in seen:
                seen.add(source)
                items.append(('ranked', source, len(seen)))
        receipt = RECEIPT.search(text, end)
        if receipt:
            items.append(('receipt', clean_source(receipt.group(1)), None))
        return items
    start = text.find(MARKER)
    if start < 0:
        return None
    body = text[start:]
    if 'companion protocol' not in body[:200]:
        return None
    # The receipt is appended last; a note quoting the hook's own wording must not end the sections early.
    receipt = None
    for receipt in RECEIPT.finditer(body):
        pass
    end = receipt.start() if receipt else len(body)
    # Only the hook's own section labels are boundaries; a bracketed line inside a note is body text.
    labels = [m for m in LABEL.finditer(body, 0, end) if m.group(1).startswith(('Related source: ', 'Knowledge map: '))
              or posixpath.basename(slash(m.group(1))) in COMPANION_NAMES]
    items, rank, seen = [], 0, set()
    for i, label in enumerate(labels):
        name = label.group(1)
        section = body[label.end():labels[i + 1].start() if i + 1 < len(labels) else end]
        if name.startswith('Related source: '):
            source = clean_source(name[len('Related source: '):])
            if source not in seen:
                seen.add(source)
                rank += 1
                items.append(('ranked', source, rank))
        elif name.startswith('Knowledge map: '):
            items.append(('map', clean_source(name[len('Knowledge map: '):]), None))
        else:
            items.append(('companion', clean_source(name), 'clipped' if TRUNCATED in section else 'full'))
    if receipt:
        items.append(('receipt', clean_source(receipt.group(1)), None))
    return items


def shell_paths(command, powershell=False):
    """Path-like .md tokens of a shell command (globs and URLs skipped)."""
    try:
        lexer = shlex.shlex(command, posix=not powershell, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
        if powershell:
            tokens = [t[1:-1] if len(t) > 1 and t[0] == t[-1] and t[0] in '"\'' else t for t in tokens]
    except ValueError:
        tokens = re.split(r'[\s;|&()<>]+', command)
    found = []
    for token in tokens:
        for part in token.split('='):
            part = part.strip('\'"`,;')
            if (part.lower().endswith('.md') and '://' not in part and not any(c in part for c in '*?[{$')):
                found.append(part)
    return found


def row():
    return {'suggested': 0, 'opened_read_grep': 0, 'opened_with_shell': 0}


class Scope:
    def __init__(self):
        self.counts = collections.Counter()
        self.rows = collections.defaultdict(row)
        self.unsuggested = {'read_grep': collections.Counter(), 'with_shell': collections.Counter()}
        self.note_opens = {'read_grep': 0, 'with_shell': 0}
        self.names = collections.defaultdict(row)


def analyse(path, roots, scope, errors):
    """Stream one transcript; update scope counters in place."""
    injections = []  # (position, row keys, source)
    last = {'read_grep': {}, 'with_shell': {}}  # source -> last open position
    injected = set()
    has_context = False
    prompt = None
    position = 0
    scope.counts['transcripts'] += 1
    with open(path, encoding='utf-8', errors='replace') as handle:
        for line in handle:
            position += 1
            if not ('hook_additional_context' in line or 'tool_use' in line or '"user"' in line):
                continue
            try:
                record = json.loads(line)
            except ValueError:
                errors['malformed_lines'] += 1
                continue
            if not isinstance(record, dict):
                errors['unrecognized_records'] += 1
                continue
            kind = record.get('type')
            cwd = record.get('cwd') if isinstance(record.get('cwd'), str) else None
            attachment = record.get('attachment')
            if isinstance(attachment, dict) and attachment.get('type') == 'hook_additional_context':
                content = attachment.get('content')
                texts = [content] if isinstance(content, str) else content if isinstance(content, list) else None
                if texts is None or not all(isinstance(t, str) for t in texts):
                    errors['unrecognized_contexts'] += 1
                    continue
                event = attachment.get('hookEvent')
                for text in texts:
                    if MARKER not in text:
                        errors['other_hook_contexts'] += 1
                        continue
                    items = parse_context(text)
                    if items is None or event not in EVENTS:
                        errors['unrecognized_contexts'] += 1
                        continue
                    has_context = True
                    synthetic = False
                    if event == 'UserPromptSubmit':
                        if prompt is None:
                            scope.counts['contexts_without_prompt'] += 1
                        synthetic = prompt is not None and synthetic_turn(prompt)
                    label = 'UserPromptSubmit_synthetic' if synthetic else event
                    scope.counts['contexts_' + label] += 1
                    if not any(item[0] == 'ranked' for item in items):
                        scope.counts['contexts_without_ranked_sources_' + label] += 1
                    for item_kind, source, detail in items:
                        injected.add(source)
                        if synthetic:
                            keys = ['synthetic_turns']
                        elif item_kind == 'ranked':
                            keys = ['ranked', 'ranked.' + event, 'ranked.rank_' + (str(detail) if detail <= 3 else '4+')]
                        elif item_kind == 'companion':
                            name = posixpath.basename(source)
                            keys = ['companion.' + detail, f'companion.{detail}.{name}']
                        else:
                            keys = [{'map': 'knowledge_map', 'receipt': 'latest_receipt'}[item_kind]]
                        injections.append((position, keys, source))
                continue
            if kind == 'user':
                message = record.get('message')
                content = message.get('content') if isinstance(message, dict) else None
                if isinstance(content, list) and any(isinstance(b, dict) and b.get('type') == 'tool_result' for b in content):
                    continue
                if content is None:
                    errors['unrecognized_records'] += 1
                    continue
                if not record.get('isMeta') or 'origin' in record:
                    prompt = {'prompt': content}
                    if 'origin' in record:
                        prompt['origin'] = record['origin']
                continue
            if kind != 'assistant':
                continue
            message = record.get('message')
            content = message.get('content') if isinstance(message, dict) else None
            if not isinstance(content, list):
                errors['unrecognized_records'] += 1
                continue
            for block in content:
                if not isinstance(block, dict) or block.get('type') != 'tool_use':
                    continue
                name, data = block.get('name'), block.get('input')
                if name not in READ_TOOLS + SHELL_TOOLS:
                    continue
                if not isinstance(data, dict):
                    errors['unrecognized_tool_inputs'] += 1
                    continue
                opened = set()
                if name in READ_TOOLS:
                    relative = roots.relative(data.get('file_path') if name == 'Read' else data.get('path'), cwd)
                    if relative is not None and (name == 'Read' or relative.lower().endswith('.md')):
                        opened.add(relative)
                    bounds = ('read_grep', 'with_shell')
                else:
                    command = data.get('command')
                    if not isinstance(command, str):
                        errors['unrecognized_tool_inputs'] += 1
                        continue
                    scope.counts['shell_commands'] += 1
                    if WRITE.search(command):
                        scope.counts['shell_commands_write_excluded'] += 1
                        continue
                    moves = CHANGES_DIR.search(command) is not None
                    for token in shell_paths(command, powershell=name == 'PowerShell'):
                        relative = roots.relative(token, cwd)
                        tail = clean_source(token)
                        # Inside the vault, or after a cd in the same command, a file may be named by its tail
                        # only: match sources injected so far by whole path components, and then do not also
                        # count the cwd-joined guess. An absolute path names exactly one file.
                        matches = set() if ABSOLUTE.match(tail) or (relative is None and not moves) else \
                            {s for s in injected if s == tail or s.endswith('/' + tail)}
                        if relative is not None and (relative in injected or not matches):
                            opened.add(relative)
                        opened |= matches
                    bounds = ('with_shell',)
                for relative in opened:
                    for bound in bounds:
                        last[bound][relative] = position
                    if is_note(relative):
                        for bound in bounds:
                            scope.note_opens[bound] += 1
                        if has_context and relative not in injected:
                            for bound in bounds:
                                scope.unsuggested[bound][relative] += 1
    if has_context:
        scope.counts['transcripts_with_v3_context'] += 1
    for position, keys, source in injections:
        strict = last['read_grep'].get(source, 0) > position
        broad = last['with_shell'].get(source, 0) > position
        for target in [scope.rows[key] for key in keys] + [scope.names[source]]:
            target['suggested'] += 1
            target['opened_read_grep'] += strict
            target['opened_with_shell'] += broad


def rate(part, whole):
    return round(part / whole, 4) if whole else None


def finish(row_data):
    return dict(row_data, rate_read_grep=rate(row_data['opened_read_grep'], row_data['suggested']),
                rate_with_shell=rate(row_data['opened_with_shell'], row_data['suggested']))


def scope_report(scope, names):
    rows = scope.rows
    pick = lambda key: finish(rows[key]) if key in rows else finish(row())
    companion = {}
    for detail in ('full', 'clipped'):
        companion[detail] = {'all': pick('companion.' + detail)}
        for name in COMPANION_NAMES:
            if f'companion.{detail}.{name}' in rows:
                companion[detail][name] = pick(f'companion.{detail}.{name}')
    report = {
        'transcripts': scope.counts['transcripts'],
        'transcripts_with_v3_context': scope.counts['transcripts_with_v3_context'],
        'contexts': {label: scope.counts['contexts_' + label] for label in ('SessionStart', 'UserPromptSubmit', 'UserPromptSubmit_synthetic')},
        'contexts_without_ranked_sources': {label: scope.counts['contexts_without_ranked_sources_' + label]
                                            for label in ('SessionStart', 'UserPromptSubmit', 'UserPromptSubmit_synthetic')},
        'contexts_without_prompt': scope.counts['contexts_without_prompt'],
        'ranked': {'all': pick('ranked'),
                   'by_event': {event: pick('ranked.' + event) for event in EVENTS},
                   'by_rank': {rank: pick('ranked.rank_' + rank) for rank in ('1', '2', '3', '4+')}},
        'companion': companion,
        'knowledge_map': pick('knowledge_map'),
        'latest_receipt': pick('latest_receipt'),
        'synthetic_turns': pick('synthetic_turns'),
        'unsuggested_opened': {bound: {'opens': sum(counter.values()), 'distinct_notes': len(counter)}
                               for bound, counter in scope.unsuggested.items()},
        'vault_note_opens': dict(scope.note_opens),
        'shell_commands': scope.counts['shell_commands'],
        'shell_commands_write_excluded': scope.counts['shell_commands_write_excluded'],
    }
    if names:
        report['names'] = {
            'suggested': [dict(finish(data), source=source) for source, data in
                          sorted(scope.names.items(), key=lambda item: (-item[1]['suggested'], item[0]))],
            'unsuggested_opened': [{'source': source, 'opens_read_grep': scope.unsuggested['read_grep'][source],
                                    'opens_with_shell': count} for source, count in
                                   sorted(scope.unsuggested['with_shell'].items(), key=lambda item: (-item[1], item[0]))],
        }
    return report


def build(vault, projects_dir=None, days=14, names=False, now=None):
    started = time.monotonic()
    roots = Roots(vault)
    folders = [Path(projects_dir)] if projects_dir else project_dirs(vault, claude_projects_root())
    folders = [folder for folder in folders if folder.is_dir()]
    since = None if not days else (now if now is not None else time.time()) - days * 86400
    scopes = {'main': Scope(), 'subagent': Scope()}
    errors = collections.Counter()
    for folder in folders:
        for path, subagent in transcripts(folder, since):
            try:
                analyse(path, roots, scopes['subagent' if subagent else 'main'], errors)
            except OSError:
                errors['unreadable_transcripts'] += 1
    return {
        'schema': 1,
        'window_days': days or None,
        'project_folders': len(folders),
        'skipped': {key: errors[key] for key in ('malformed_lines', 'unrecognized_records', 'unrecognized_contexts',
                                                 'unrecognized_tool_inputs', 'other_hook_contexts', 'unreadable_transcripts')},
        'main': scope_report(scopes['main'], names),
        'subagent': scope_report(scopes['subagent'], names),
        'elapsed_seconds': round(time.monotonic() - started, 3),
    }


def percent(value):
    return '-' if value is None else f'%{value * 100:.1f}'.replace('.', ',')


def human(report):
    window = f"son {report['window_days']} gün" if report['window_days'] else 'tüm geçmiş'
    lines = [f"Pencere: {window} | proje klasörü: {report['project_folders']} | süre: {report['elapsed_seconds']} sn",
             'Atlanan: ' + ', '.join(f'{k}={v}' for k, v in report['skipped'].items())]

    def table_row(label, data):
        return (f"  {label:<40} {data['suggested']:>7} {data['opened_read_grep']:>7} {percent(data['rate_read_grep']):>7}"
                f" {data['opened_with_shell']:>7} {percent(data['rate_with_shell']):>7}")

    for name in ('main', 'subagent'):
        scope = report[name]
        lines += ['', f"[{'Ana oturumlar' if name == 'main' else 'Alt ajanlar'}] transkript {scope['transcripts']}, "
                      f"V3 bağlamlı {scope['transcripts_with_v3_context']}",
                  '  Bağlam: ' + ', '.join(f'{k}={v}' for k, v in scope['contexts'].items()),
                  f"  {'satır':<40} {'öneri':>7} {'Read':>7} {'oran':>7} {'+kabuk':>7} {'oran':>7}",
                  table_row('sıralı kaynaklar (toplam)', scope['ranked']['all'])]
        lines += [table_row('  tür ' + event, data) for event, data in scope['ranked']['by_event'].items()]
        lines += [table_row('  sıra ' + rank, data) for rank, data in scope['ranked']['by_rank'].items()]
        for detail, label in (('full', 'tam basılan'), ('clipped', 'kırpılan')):
            lines += [table_row(f'companion {label}' + ('' if key == 'all' else ' ' + key), data)
                      for key, data in scope['companion'][detail].items()]
        lines += [table_row('bilgi haritası (index)', scope['knowledge_map']),
                  table_row('son receipt', scope['latest_receipt']),
                  table_row('sentetik turlar (ayrı)', scope['synthetic_turns'])]
        rev = scope['unsuggested_opened']
        lines.append(f"  Önerilmeden açılan notlar: Read/Grep {rev['read_grep']['opens']} açılış / "
                     f"{rev['read_grep']['distinct_notes']} not; kabuk dahil {rev['with_shell']['opens']} / "
                     f"{rev['with_shell']['distinct_notes']}")
        lines.append(f"  Kabuk komutu {scope['shell_commands']}, yazma sayılıp dışarıda bırakılan "
                     f"{scope['shell_commands_write_excluded']}")
        for item in scope.get('names', {}).get('suggested', []):
            lines.append(f"    öneri {item['source']}: {item['suggested']} / Read {item['opened_read_grep']} / "
                         f"kabuk {item['opened_with_shell']}")
        for item in scope.get('names', {}).get('unsuggested_opened', []):
            lines.append(f"    önerilmeden {item['source']}: Read {item['opens_read_grep']} / kabuk {item['opens_with_shell']}")
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--vault', type=Path, default=Path.cwd(), help='vault root the Claude Code sessions ran in (default: cwd)')
    parser.add_argument('--projects-dir', type=Path,
                        help="this vault's transcript folder, instead of the one derived from --vault")
    parser.add_argument('--days', type=int, default=14, help='only transcripts modified in the last N days (0: all)')
    parser.add_argument('--names', action='store_true', help='also list note paths (private; off by default)')
    parser.add_argument('--human', action='store_true', help='readable table instead of JSON')
    args = parser.parse_args(argv)
    if args.days < 0:
        parser.error('--days must be >= 0')
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    report = build(args.vault, args.projects_dir, args.days, args.names)
    print(human(report) if args.human else json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
