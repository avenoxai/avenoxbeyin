"""Report in-vault links in instruction and skill files that resolve to nothing.

Reads files only: no model call, no network, no write. Code (fenced blocks and inline spans) is
skipped so examples in skills never count, and a link that leaves the vault is not ours to judge.
"""
import os
from pathlib import Path
import re
import unicodedata
from urllib.parse import unquote

# A fence closes only on a run of the same character at least as long; an unclosed one runs to the end.
FENCE = re.compile(r'(?ms)^[ \t]*(?P<run>(?P<ch>[`~])(?P=ch){2,})[^\n]*\n.*?(?:^[ \t]*(?P=run)(?P=ch)*[ \t]*$|\Z)')
INLINE = re.compile(r'(`+)[^\n]*?\1')
COMMENT = re.compile(r'(?s)<!--.*?-->|%%.*?%%')
WIKI = re.compile(r'!?\[\[([^\]\n]+)\]\]')
LINK = re.compile(r'!?\[[^\]\n]*\]\((<[^>\n]+>|(?:[^()\s]|\([^()\s]*\))+)(?:\s+(?:"[^"\n]*"|\'[^\'\n]*\'))?\)')
SCHEME = re.compile(r'^[A-Za-z][A-Za-z0-9+.-]*:')
DRIVE = re.compile(r'^(?:[A-Za-z]:[\\/]|\\\\)')  # a drive or a UNC share, not a one-letter URI
PLACEHOLDER = set('<>{}*$')
LIMIT = 20
TARGET_LIMIT = 200  # a runaway match never floods the doctor output


def instruction_files(vault):
    """AGENTS.md, CLAUDE.md, the companion Kurallar.md and every Markdown file in the skill roots."""
    files = [vault / 'AGENTS.md', vault / 'CLAUDE.md']
    try:
        import beyin_v3_companion
        folder = beyin_v3_companion.directory(vault)
        if folder is not None:
            files.append(folder / 'Kurallar.md')
    except Exception:
        pass  # a missing companion never hides the other files
    for root in ('.agents/skills', '.claude/skills', '.kiro/skills'):
        if (vault / root).is_dir():
            files += sorted((vault / root).rglob('*.md'))
    unique = {}
    for path in files:
        if path.is_file() and not _mirror(vault, path):
            unique.setdefault(os.path.realpath(path), path)  # CLAUDE.md -> AGENTS.md is read once
    return list(unique.values())


def _mirror(vault, path):
    """Identical client mirrors are reported once, under .agents."""
    for root in ('.claude/skills', '.kiro/skills'):
        try:
            twin = vault / '.agents/skills' / path.relative_to(vault / root)
            return twin.is_file() and twin.read_bytes() == path.read_bytes()
        except ValueError:
            continue
        except OSError:
            return False
    return False


def _key(text):
    """Obsidian matches links case-insensitively and in NFC; macOS may store names in NFD."""
    return unicodedata.normalize('NFC', text).casefold()


def _note_index(vault):
    """Every trailing path of every file and folder outside hidden folders, with and without .md.

    Obsidian resolves [[name]], [[sub/name]] and [[root/sub/name]] by path suffix, case-insensitively,
    so one set answers bare names, partial paths and full paths on every file system alike.
    """
    keys = set()
    for directory, folders, files in os.walk(vault):
        folders[:] = [name for name in folders if not name.startswith('.')]
        parts = Path(directory).relative_to(vault).parts
        for name in folders + files:
            full = parts + (name,)
            for start in range(len(full)):
                tail = _key('/'.join(full[start:]))
                keys.add(tail)
                if tail.endswith('.md'):
                    keys.add(tail[:-3])
    return keys


def _blank_code(text):
    """Blank code while keeping every newline, so reported line numbers stay true."""
    def lines(match):
        return '\n' * match.group(0).count('\n')
    text = INLINE.sub(lambda match: ' ' * len(match.group(0)), FENCE.sub(lines, text))
    return COMMENT.sub(lines, text)


def _inside(vault, path):
    try:
        path.resolve().relative_to(vault)
        return True
    except (ValueError, OSError):
        return False


def _exists(path):
    """Python 3.11 and Windows raise for over-long or invalid names instead of answering False."""
    try:
        return path.exists() or (path.suffix.lower() != '.md' and path.with_name(path.name + '.md').exists())
    except (OSError, ValueError):
        return False


def _skill_root(vault, source):
    """Skill references are written relative to the skill folder, whichever file they sit in."""
    for folder in source.parents:
        if (folder / 'SKILL.md').is_file():
            return folder
        if folder == vault:
            return None
    return None


def _live(vault, source, target, index):
    """True when the target resolves, None when it leaves the vault, False when it is dead.

    A relative path is tried from the file's folder, the vault root and the skill folder, then by
    Obsidian's case-insensitive path-suffix lookup, so the answer is the same on macOS, Linux and
    Windows. An absolute path names one place instead of a folder to search (see _target_paths).
    """
    paths = _target_paths(vault, source, target)
    if paths is None:
        return None  # the link leaves the vault
    inside = False
    for path in paths:
        if not _inside(vault, path):
            continue
        inside = True
        relative = Path(os.path.relpath(os.path.normpath(path), vault)).as_posix()
        if _exists(path) or _key(relative) in index:
            return True
    return False if inside else None


def _target_paths(vault, source, target):
    """Every path a link may name, in the order it is tried, or None when the link leaves the vault.

    A relative path is searched from the file's folder, the vault root and the skill folder. An
    absolute path names one place instead of a folder to search, so it is read as written: a link that
    names a file in the vault by full path is judged like any other, and a path that exists outside
    the vault leaves it, which is not ours to judge. The other platform's spelling cannot be opened
    here and says nothing about the machine that wrote the link, so it is skipped the same way. A
    leading slash also spells a vault-root link in Obsidian, so that reading is kept.
    """
    if not _absolute(target):
        bases = (source.parent, vault, _skill_root(vault, source))
        return [base / target for base in bases if base is not None]
    written = Path(target) if _openable(target) else None
    if written is not None and not _inside(vault, written) and _exists(written):
        return None
    paths = [written] if written is not None else []
    if target.startswith('/'):
        paths.append(vault / target.lstrip('/'))
    return paths


def _absolute(target):
    """True when the target names one place instead of a folder to search.

    The shape decides rather than Path.is_absolute, so `/x`, `C:\\x`, `C:/x` and `\\\\host\\share` read
    the same on every platform.
    """
    return target.startswith('/') or _windows_path(target)


def _windows_path(target):
    """A drive, UNC or root-of-drive path, whether or not this platform can open it."""
    return target.startswith('\\') or DRIVE.match(target) is not None


def _openable(target):
    """True when this file system can read the spelling of an absolute path.

    Another platform's spelling is one odd local file name here, and a share on another machine is
    that machine's disk, so neither is opened: the check never leaves this computer.
    """
    return not target.startswith('\\\\') and (os.name == 'nt' or not _windows_path(target))


def _wiki_dead(vault, source, target, index):
    target = target.split('|', 1)[0].rstrip('\\').split('#', 1)[0].split('^', 1)[0].strip()
    if not target or PLACEHOLDER & set(target):
        return None
    return target if _live(vault, source, target, index) is False else None


def _link_dead(vault, source, target, index):
    target = unquote(target.strip('<>')).split('#', 1)[0].split('?', 1)[0].strip()
    if not target or target.startswith('//') or PLACEHOLDER & set(target):
        return None
    if SCHEME.match(target) and not _absolute(target):  # `C:\\x` is a drive, not a one-letter URI
        return None
    return target if _live(vault, source, target, index) is False else None


def check(vault):
    vault = Path(vault).resolve()
    files = instruction_files(vault)
    dead, index = [], None
    for source in files:
        try:
            text = _blank_code(source.read_text(encoding='utf-8-sig'))
        except (OSError, UnicodeError):
            continue
        for number, line in enumerate(text.splitlines(), 1):
            wikis, links = list(WIKI.finditer(line)), list(LINK.finditer(line))
            if (wikis or links) and index is None:
                index = _note_index(vault)
            found = [_wiki_dead(vault, source, match.group(1), index) for match in wikis]
            found += [_link_dead(vault, source, match.group(1), index) for match in links]
            dead += [{'file': source.relative_to(vault).as_posix(), 'line': number, 'target': target[:TARGET_LIMIT]}
                     for target in found if target]
    return {'checked_files': len(files), 'dead_count': len(dead), 'dead': dead[:LIMIT],
            'truncated': len(dead) > LIMIT}
