"""Vault Markdown edits made through the Claude Code shell tool, seen at Stop (#212).

PostToolUse hooks match only file-editing tools, so `sed -i`, a heredoc or an inline Python
write left the receipt reminder silent. Matching Bash itself would start a hook process for
every shell command, and the command text alone cannot prove a write. Stop instead reads the
bounded tail of the session's own transcript and stops at the turn's prompt, so a command is
attributed to the session that ran it. A command counts only when a vault Markdown file it
names (a plain or quoted path, or a glob in the last path component) was last modified while
that command ran: between its call and its result record. Nothing is counted from the command
text alone, and a later write by someone else to a file the command only read falls outside
that window. A variable path, a script that computes its paths, a background write, a rename
or a deletion stays unseen, as before.

Bounded: at most TAIL_BYTES of the transcript, MAX_COMMANDS commands, MAX_STATS file stats,
MAX_LISTED directory entries per glob; no vault walk. Nothing from the transcript is kept or
logged; the caller stores only a timestamp. Any unexpected input answers None (fail open).
"""
import json
import os
from pathlib import Path
import re
import stat
import unicodedata
from datetime import datetime

TAIL_BYTES = 2 * 1024 * 1024
MAX_COMMANDS = 200
MAX_COMMAND_CHARS = 64 * 1024
MAX_TOKENS = 64
MAX_STATS = 256
MAX_LISTED = 512
# A command's writes fall between its call record and its result record; file systems with
# whole or two-second timestamps (HFS+, FAT) and coarse kernel clocks can date them slightly off.
SLACK = 2.0
SHELL_TOOLS = ('Bash', 'PowerShell')
_USER = re.compile(rb'"type"\s*:\s*"user"')
_SHELL = re.compile(rb'"name"\s*:\s*"(?:Bash|PowerShell)"')
_RESULT_ID = re.compile(rb'"tool_use_id"\s*:\s*"([^"\\]{1,200})"')
_QUOTED = re.compile(r'"((?:[^"\\\n]|\\.)*)"|\'([^\'\n]*)\'')
_POSIX_TOKEN = re.compile(r'(?:\\.|[^\s\'"`;|&<>(),=\\])+')
_POWERSHELL_TOKEN = re.compile(r'[^\s\'"`;|&<>(),=]+')
_ESCAPE = re.compile(r'\\(.)')
_GLOB = re.compile(r'[*?\[]')
_FLAT_DAILY = re.compile(r'daily/[0-9]{4}-[0-9]{2}-[0-9]{2}\.md')
# A directory change inside the same command: its relative names are tried from there as well.
_CHANGE_DIR = re.compile(r'(?:^|[;&|(\n])\s*(?:cd|pushd|Set-Location|sl)\s+("[^"\n]+"|\'[^\'\n]+\'|[^\s;&|()<>]+)', re.I)
MAX_DIRS = 4


def _stamp(value):
    """Epoch seconds of an ISO-8601 transcript stamp with an explicit zone, else None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value[:-1] + '+00:00' if value.endswith('Z') else value)
    except ValueError:
        return None
    return moment.timestamp() if moment.tzinfo is not None else None


def _tail(raw):
    """The complete lines in the last TAIL_BYTES of a regular, non-link .jsonl file."""
    if not isinstance(raw, str) or not raw.strip() or '\0' in raw:
        return None
    path = Path(raw)
    if not path.is_absolute() or path.suffix.lower() != '.jsonl' or path.is_symlink():
        return None
    with path.open('rb') as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            return None
        start = max(0, info.st_size - TAIL_BYTES)
        handle.seek(start)
        data = handle.read(TAIL_BYTES)
    lines = data.split(b'\n')
    return lines[1:] if start else lines  # the first line of a cut tail is partial


def _is_prompt(record):
    """A turn's opening prompt, by the record shapes scripts/evaluate_v3_usage.py reads."""
    if record.get('isSidechain') is True:
        return False  # a subagent's task prompt, inside the parent turn
    if record.get('isMeta') and 'origin' not in record:
        return False
    message = record.get('message')
    content = message.get('content') if isinstance(message, dict) else None
    if isinstance(content, str):
        return True
    return isinstance(content, list) and not any(
        isinstance(block, dict) and block.get('type') == 'tool_result' for block in content)


def turn_commands(lines):
    """[(issued, finished, command, cwd, powershell)] of the current turn, oldest first.

    Read backward from the end to the turn's prompt; without a prompt in the tail every
    record in it belongs to the current turn. Only lines that can be a prompt or a shell
    call are decoded, then the tool_result lines that answer one of those calls. A call
    without its result in the tail (still running, cut off) is left out.
    """
    calls, start = [], 0
    for index in range(len(lines) - 1, -1, -1):
        line = lines[index]
        user = _USER.search(line) is not None and b'"tool_result"' not in line
        shell = b'"tool_use"' in line and _SHELL.search(line) is not None
        if not user and not shell:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        if record.get('type') == 'user':
            if _is_prompt(record):
                start = index + 1
                break
            continue
        if record.get('type') != 'assistant':
            continue
        moment = _stamp(record.get('timestamp'))
        message = record.get('message')
        content = message.get('content') if isinstance(message, dict) else None
        if moment is None or not isinstance(content, list):
            continue
        cwd = record.get('cwd') if isinstance(record.get('cwd'), str) else None
        for block in reversed(content):
            if not isinstance(block, dict) or block.get('type') != 'tool_use' or block.get('name') not in SHELL_TOOLS:
                continue
            data = block.get('input')
            command = data.get('command') if isinstance(data, dict) else None
            if isinstance(command, str) and command.strip() and isinstance(block.get('id'), str) and block['id']:
                calls.append((block['id'], moment, command[:MAX_COMMAND_CHARS], cwd, block.get('name') == 'PowerShell'))
        if len(calls) >= MAX_COMMANDS:
            break
    if not calls:
        return []
    pending, finished = {call[0] for call in calls}, {}
    for line in lines[start:]:
        if b'"tool_result"' not in line:
            continue
        ids = {match.group(1).decode('utf-8', 'replace') for match in _RESULT_ID.finditer(line)} & pending
        if not ids:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict) or record.get('type') != 'user':
            continue
        message = record.get('message')
        content = message.get('content') if isinstance(message, dict) else None
        moment = _stamp(record.get('timestamp'))
        if moment is None or not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get('type') == 'tool_result' and block.get('tool_use_id') in ids:
                finished.setdefault(block['tool_use_id'], moment)
    found = [(moment, finished[tool], command, cwd, powershell)
             for tool, moment, command, cwd, powershell in calls if tool in finished]
    found.reverse()
    return found


def markdown_tokens(command, powershell=False):
    """Path-like `.md` words of a command: quoted strings first, then the unquoted words.

    Variables, command substitutions and URLs are skipped; their path is not in the text.
    """
    found = []

    def add(text):
        text = text.strip()
        if (text and len(text) <= 4096 and text.casefold().endswith('.md') and '://' not in text
                and not any(mark in text for mark in ('$', '`', '\0', '\n', '{', '}'))):
            if text not in found and len(found) < MAX_TOKENS:
                found.append(text)

    for match in _QUOTED.finditer(command):
        if match.group(1) is not None:
            add(match.group(1) if powershell else _ESCAPE.sub(r'\1', match.group(1)))
        else:
            add(match.group(2))
    rest = _QUOTED.sub(' ', command)
    for match in (_POWERSHELL_TOKEN if powershell else _POSIX_TOKEN).finditer(rest):
        add(match.group(0) if powershell else _ESCAPE.sub(r'\1', match.group(0)))
    return found


class _Probe:
    """Stat budget and vault containment for one Stop."""

    def __init__(self, vault, state):
        self.root = Path(vault).resolve()
        self.key = self._key(self.root)
        self.state = state
        self.stats = 0
        self.views = None

    @staticmethod
    def _key(path):
        return unicodedata.normalize('NFC', str(path)).replace('\\', '/').casefold().rstrip('/')

    def relative(self, path):
        """The vault-relative Markdown source this path names, or None (outside, link, dot folder)."""
        try:
            if path.is_symlink():
                return None
            resolved = path.resolve()
        except (OSError, RuntimeError, ValueError):
            return None
        key = self._key(resolved)
        if not key.startswith(self.key + '/'):
            return None
        parts = resolved.parts[len(self.root.parts):]
        if not parts or any(part.startswith('.') for part in parts) or not parts[-1].casefold().endswith('.md'):
            return None
        return '/'.join(unicodedata.normalize('NFC', part) for part in parts)

    def generated(self, relative):
        """Receipt views the sync rewrites for any session's receipt are not this session's edit."""
        from beyin_v3_projections import is_receipt_view_source
        if is_receipt_view_source(relative):
            return True
        if not _FLAT_DAILY.fullmatch(relative):
            return False
        if self.views is None:
            self.views = self._view_rows()
        return self.views is True or relative in self.views

    def _view_rows(self):
        import sqlite3
        from beyin_v3_projections import receipt_view_rows
        if self.state is None:
            return True
        database = Path(self.state) / 'memory.sqlite3'
        try:
            if database.is_symlink() or not database.is_file():
                return set()
            db = sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True, timeout=1)
            try:
                return {path for path, _ in receipt_view_rows(db)}
            finally:
                db.close()
        except (sqlite3.Error, OSError, ValueError):
            return True  # unknown ownership: a dated daily note is treated as a view

    def changed(self, path, since, until):
        """(relative, mtime) of a vault Markdown file last modified within [since, until], else None."""
        if self.stats >= MAX_STATS:
            return None
        self.stats += 1
        relative = self.relative(path)
        if relative is None:
            return None
        try:
            info = os.stat(path)
        except (OSError, ValueError):
            return None
        if not stat.S_ISREG(info.st_mode) or not since <= info.st_mtime <= until or self.generated(relative):
            return None
        return relative, info.st_mtime

    def expand(self, path):
        """A path, or the names its last component matches as a glob in one listed directory."""
        if not _GLOB.search(path.name):
            return [path]
        if self.stats >= MAX_STATS or _GLOB.search(str(path.parent)) or self.relative_dir(path.parent) is None:
            return []
        self.stats += 1
        import fnmatch
        matches = []
        try:
            with os.scandir(path.parent) as entries:
                for count, entry in enumerate(entries):
                    if count >= MAX_LISTED or len(matches) >= MAX_STATS:
                        break
                    if fnmatch.fnmatchcase(entry.name, path.name):
                        matches.append(path.parent / entry.name)
        except (OSError, ValueError):
            return []
        return sorted(matches)

    def relative_dir(self, path):
        try:
            if path.is_symlink():
                return None
            key = self._key(path.resolve())
        except (OSError, RuntimeError, ValueError):
            return None
        return key if key == self.key or key.startswith(self.key + '/') else None


def _paths(token, bases):
    variants = [token]
    for form in ('NFC', 'NFD'):
        other = unicodedata.normalize(form, token)
        if other not in variants:
            variants.append(other)
    for text in variants:
        path = Path(os.path.expanduser(text) if text.startswith('~') else text)
        if path.is_absolute():
            yield path
        else:
            for base in bases:
                yield base / path


def shell_edit(payload, vault, state=None):
    """{'at': stamp, 'knowledge': bool} for the first shell command of this turn that changed a
    vault Markdown file it names, or None. `at` is the command's own call stamp."""
    try:
        lines = _tail(payload.get('transcript_path'))
        if not lines:
            return None
        commands = turn_commands(lines)
        if not commands:
            return None
        probe = _Probe(vault, state)
        cwd = payload.get('cwd')
        first, knowledge = None, False
        for moment, done, command, record_cwd, powershell in commands:
            bases = []
            for base in (record_cwd, cwd, str(probe.root)):
                if isinstance(base, str) and base.strip() and Path(base).is_absolute() and Path(base) not in bases:
                    bases.append(Path(base))
            limit = len(bases) + MAX_DIRS
            for match in _CHANGE_DIR.finditer(command):
                folder = match.group(1).strip('\'"')
                if not folder or '$' in folder or '`' in folder or folder.startswith('~'):
                    continue
                for base in list(bases):
                    if len(bases) < limit and base / folder not in bases:
                        bases.append(base / folder)
            hits = []
            seen = set()
            for token in markdown_tokens(command, powershell=powershell):
                for path in _paths(token, bases):
                    for concrete in probe.expand(path):
                        if concrete in seen:
                            continue
                        seen.add(concrete)
                        hit = probe.changed(concrete, moment - SLACK, done + SLACK)
                        if hit is not None:
                            hits.append(hit[0])
            if hits:
                if first is None:
                    first = moment
                if not knowledge:
                    from beyin_v3_projections import is_distilled_note
                    knowledge = any(is_distilled_note(relative) for relative in hits)
            if probe.stats >= MAX_STATS or (first is not None and knowledge):
                break
        return None if first is None else {'at': first, 'knowledge': knowledge}
    except Exception:
        return None
