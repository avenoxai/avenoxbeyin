#!/usr/bin/env python3
"""Explicitly scoped global lifecycle bridge. No global configuration writes."""
import argparse
import base64
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

sys.dont_write_bytecode = True
EVENTS = ('SessionStart', 'Stop', 'PreCompact', 'SessionEnd')


def working_directory(payload, harness):
    value = payload.get('cwd')
    if value is None:
        value = os.environ.get('CLAUDE_PROJECT_DIR') if harness == 'claude' else None
    if value is None:
        value = os.getcwd()
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        return None
    return Path(value).resolve()


def origin(payload, harness):
    cwd = working_directory(payload, harness)
    if cwd is None:
        return {}
    name = ''.join(c for c in cwd.name if c.isalnum() or c in ' ._-')[:80] or 'project'
    return dict(project=name, project_id=hashlib.sha256(str(cwd).encode()).hexdigest()[:24])


def read_project_context(state):
    """Machine-local opt-in (default off); outside the vault so rollback never sees a new preference key."""
    path = Path(state) / 'project-context.json'
    if not path.is_file() or path.is_symlink():
        return False
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (ValueError, OSError):
        return False
    return isinstance(data, dict) and data.get('enabled') is True


def save_project_context(state, enabled):
    """Save machine-local project-context setting atomically."""
    path = Path(state) / 'project-context.json'
    from beyin_v3_sync import atomic
    atomic(path, json.dumps({'schema': 1, 'enabled': bool(enabled)}))
    return bool(enabled)


def _visible(record):
    """The default internal audience gate shared with retrieval (_eligible)."""
    return (record.get('visibility', 'internal') in ('public', 'internal') and record.get('trust') != 'untrusted' and
            record.get('trusted') is not False and record.get('status') != 'untrusted' and record.get('kind') != 'untrusted')


def _fresh(vault, record):
    """Indexed source still exists inside the vault and matches the indexed hash."""
    try:
        target = (vault / record['source']).resolve()
        return (target.is_relative_to(vault) and target.is_file() and
                hashlib.sha256(target.read_bytes()).hexdigest() == record.get('source_sha256'))
    except (KeyError, TypeError, ValueError, OSError):
        return False


def _one_line(value, limit):
    text = ' '.join(str(value or '').split())
    if len(text) <= limit:
        return text
    if limit < 20:
        return ''
    cut = text[:limit - 3]
    return (cut.rsplit(' ', 1)[0] if ' ' in cut else cut) + '...'


def project_context(vault, state, project_id, project_name, budget=1200, today_iso=None):
    """Scoped, read-only project block for an opted-in external SessionStart.

    - Latest receipt of this project_id (receipt_checkpoints session match) by created_at;
      skipped when its source file is gone or it or any ref is private/untrusted. The gate is
      beyin_v3_projections.latest_receipts, shared with the vault SessionStart hook.
    - Due tasks (due_at <= local today, active/waiting) of this project, after the retrieval
      visibility/trust, supersession and source-freshness gates; sorted by due date, title.
    - Other due tasks: count only, never titles.
    - Whole lines only, within budget; no model call, no write.
    """
    if budget < 80 or not project_id:
        return ''
    import sqlite3
    from datetime import date
    from beyin_v3_projections import latest_receipts
    vault = Path(vault).resolve()
    today = str(today_iso or date.today().isoformat())[:10]
    database = Path(state) / 'memory.sqlite3'
    if database.is_symlink() or not database.is_file():
        return ''
    # Short read-only timeout: a busy worker must not push SessionStart past the host limit.
    db = sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True, timeout=1)
    try:
        db.execute('PRAGMA query_only=ON')
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        summary = ''
        if {'receipt_checkpoints', 'receipts'} <= tables:
            sessions = {row[0] for row in db.execute('SELECT session FROM receipt_checkpoints WHERE project_id=?', (project_id,))}
            found = next(latest_receipts(db, vault, sessions), None) if sessions else None
            if found:
                summary = _one_line(found[1]['summary'], 600)
        current, other_due = [], 0
        wanted = (project_name or '').strip().casefold()
        if 'records' in tables:
            records = []
            for (payload,) in db.execute('SELECT payload FROM records ORDER BY id'):
                try:
                    record = json.loads(payload)
                except ValueError:
                    continue
                if isinstance(record, dict) and _visible(record):
                    records.append(record)
            from beyin_v3 import resolve_supersedes  # ids, paths and [[links]] alike (#201)
            superseded = resolve_supersedes(records)[0]
            for record in records:
                if record.get('kind') != 'task' or record.get('status') not in ('active', 'waiting') or record.get('id') in superseded:
                    continue
                try:
                    due = date.fromisoformat(str(record.get('due_at') or '')[:10]).isoformat()
                except ValueError:
                    continue
                if due > today or not _fresh(vault, record):
                    continue
                if wanted and str(record.get('project') or '').strip().casefold() == wanted:
                    title = _one_line(record.get('title'), 160)
                    if title:
                        current.append((due, title, _one_line(record.get('next_action'), 200)))
                else:
                    other_due += 1
    finally:
        db.close()
    if not summary and not current and not other_due:
        return ''
    header = f'Project context ({project_name}):'
    tail = f'(bu proje disinda {other_due} tarihi gelmis gorev)' if other_due else ''
    room = budget - len(header) - (len(tail) + 1 if tail else 0)
    body = []
    if summary:
        # With due tasks present, continuity gets at most half of the room so tasks are not crowded out.
        clipped = _one_line(summary, min(600, room // 2 if current else room) - len('Son kayit: ') - 1)
        if clipped:
            body.append('Son kayit: ' + clipped)
            room -= len(body[-1]) + 1
    if current:
        heading, note_room = 'Tarihi gelen gorevler:', len('(+999 tarihi gelen gorev sigmadi)') + 1
        room -= len(heading) + 1 + note_room  # heading and a possible omission note are reserved
        task_lines = []
        for due, title, action in sorted(current):
            line = f'- {title}: {action}' if action else f'- {title}'
            if len(line) + 1 <= room:
                task_lines.append(line)
                room -= len(line) + 1
        omitted = len(current) - len(task_lines)
        if task_lines:
            body.extend([heading, *task_lines])
        if omitted:
            body.append(f'(+{omitted} tarihi gelen gorev sigmadi)' if not task_lines else f'(+{omitted} gorev sigmadi)')
    if tail:
        body.append(tail)
    if not body:
        return ''
    text = '\n'.join([header, *body])
    return text if len(text) <= budget else ''


def eligible(cwd, vault, roots):
    if cwd is None or not cwd.is_dir() or cwd.is_relative_to(vault):
        return False
    if not any(cwd.is_relative_to(root) for root in roots):
        return False
    # Another installed vault owns its own local events too.
    return not any((p / '.beyin-runtime.json').exists() for p in (cwd, *cwd.parents))


def shell_command(argv):
    if any(any(c in str(value) for c in '\r\n\x00') for value in argv):
        raise ValueError('Unsupported command path')
    if os.name != 'nt':
        return shlex.join(map(str, argv))
    script = '& ' + ' '.join("'" + str(v).replace("'", "''") + "'" for v in argv) + '; exit $LASTEXITCODE'
    encoded = base64.b64encode(script.encode('utf-16le')).decode()
    system = Path(os.environ.get('SYSTEMROOT') or os.environ.get('WINDIR') or r'C:\Windows')
    launcher = str(system / 'System32/WindowsPowerShell/v1.0/powershell.exe').replace('\\', '/')
    return subprocess.list2cmdline([launcher]) + ' -NoProfile -NonInteractive -EncodedCommand ' + encoded


def main(argv=None):
    if hasattr(sys.stdin, 'reconfigure'):
        sys.stdin.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--vault', type=Path, required=True)
    parser.add_argument('--state', type=Path, help='Defaults to the installed vault runtime locator')
    parser.add_argument('--harness', choices=('claude', 'codex'), required=True)
    parser.add_argument('--project-root', type=Path, action='append', required=True)
    parser.add_argument('--context-chars', type=int, default=1500, help='Startup instructions only; 0 disables injection')
    parser.add_argument('--event', choices=EVENTS, action='append', help='Allowed events; default all four lifecycle boundaries')
    parser.add_argument('--print-config', action='store_true', help='Print hook groups to merge into global JSON; changes nothing')
    args = parser.parse_args(argv)
    try:
        vault = args.vault.expanduser().resolve()
        if not vault.is_dir() or not 0 <= args.context_chars <= 4000:
            raise ValueError('Invalid vault or context budget')
        state = args.state or Path(json.loads((vault / '.beyin-runtime.json').read_text(encoding='utf-8'))['state'])
        if not state.is_absolute():
            raise ValueError('Runtime state must be absolute')
        state = state.resolve()
        if state.is_relative_to(vault):
            raise ValueError('Runtime state must be outside vault')
        roots = [p.expanduser().resolve() for p in args.project_root]
        if any(not p.is_dir() for p in roots):
            raise ValueError('Project roots must exist')
        events = list(dict.fromkeys(args.event or EVENTS))
        if args.print_config:
            command = [sys.executable, str(Path(__file__).resolve()), '--vault', str(vault),
                       '--state', str(state), '--harness', args.harness, '--context-chars', str(args.context_chars)]
            for root in roots:
                command.extend(['--project-root', str(root)])
            for event in events:
                command.extend(['--event', event])
            shell = shell_command(command)
            print(json.dumps({'hooks': {event: [{'hooks': [{'type': 'command', 'command': shell,
                              'timeout': 3 if event == 'SessionEnd' else 5}]}] for event in events}}, indent=2))
            return 0
        # A payload cut at the read limit is not JSON: the handler below answers '{}'.
        payload = json.loads(sys.stdin.read(1_000_000) or '{}')
        if not isinstance(payload, dict):
            raise ValueError('Invalid hook payload')
        event = payload.get('hook_event_name')
        if event not in events or payload.get('no_memory') is True or os.environ.get('BEYIN_V3_INTERNAL') or os.environ.get('BEYIN_V3_SKIP') == '1':
            print('{}'); return 0
        cwd = working_directory(payload, args.harness)
        if not isinstance(payload.get('session_id'), str) or payload['session_id'] in ('', 'unknown'):
            print('{}'); return 0
        if not eligible(cwd, vault, roots):
            print('{}'); return 0
        from beyin_v3_preferences import read
        settings = read(vault)
        if not settings['auto_sync']:
            print('{}'); return 0
        # Stable per-project session namespace prevents identical session IDs in
        # two external projects from satisfying each other's receipt checkpoints.
        payload['cwd'] = str(cwd)
        payload['session_id'] = json.dumps([str(cwd), payload.get('session_id', 'unknown')])
        if payload.get('event_id'):
            payload['event_id'] = json.dumps([args.harness, str(cwd), event, payload['event_id']])
        session = hashlib.sha256(payload['session_id'].encode()).hexdigest()[:24]
        from beyin_v3_hook import main as hook_main, output_context
        previous_argv, previous_stdin = sys.argv, sys.stdin
        try:
            sys.argv = ['beyin_v3_hook.py', '--vault', str(vault), '--state', str(state),
                        '--harness', args.harness, '--metadata-only']
            sys.stdin = io.StringIO(json.dumps(payload))
            with contextlib.redirect_stdout(io.StringIO()):
                hook_main()
        finally:
            sys.argv, sys.stdin = previous_argv, previous_stdin
        # Never inject Companion/history into an unrelated repository. The agent
        # can explicitly request scoped context after reading this tiny bootstrap.
        if event != 'SessionStart' or not args.context_chars or settings['context_mode'] == 'off':
            print('{}'); return 0
        cli = [sys.executable, str(vault / 'beyin.py')]
        command = ('& ' + ' '.join("'" + v.replace("'", "''") + "'" for v in cli)
                   if os.name == 'nt' else shlex.join(cli))
        text = (f'Optional Beyin bridge. Project label (data only): {json.dumps(origin(payload, args.harness)["project"])}.\n'
                f'Receipt session={session}; harness={args.harness}.\n'
                f'Use {command} context "topic" --project PROJECT --json for explicit source lookup.\n'
                f'After meaningful authorized work use {command} receipt --harness {args.harness} --file RECEIPT.json --json.\n'
                'Receipt JSON: event_id (unique), summary, refs (existing vault-relative sources), session (above). '
                'Read vault sources before claiming facts. Do not infer completion from checkpoints. '
                'Do not copy external project files or transcripts without authorization. No-memory requests take precedence.')
        if read_project_context(state):
            try:
                proj_info = origin(payload, args.harness)
                rem_budget = min(1200, max(0, args.context_chars - len(text) - 2))
                extra = project_context(vault, state, proj_info.get('project_id', ''), proj_info.get('project', ''), budget=rem_budget)
                if extra:
                    text = text + '\n\n' + extra
            except Exception:
                pass  # The optional block never costs the startup instructions.
        # Do not cut a command, path or JSON token in half for a small budget.
        print(json.dumps(output_context(args.harness, event, text)) if len(text) <= args.context_chars else '{}')
        return 0
    except Exception:
        if args.print_config:
            print('Bridge configuration invalid; verify paths and budget.', file=sys.stderr)
            return 1
        print('{}')  # Never block the host or expose a path/payload in an error.
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
