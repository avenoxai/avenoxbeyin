"""Opt-in local secret redaction for user-authored V3 write commands.

Known patterns and safe custom literals are scanned in raw and Cf-normalized
text (including code blocks), redacting the union of original spans. This is not
exhaustive for combined boundary/interior obfuscations: normalization can remove
a boundary while the raw pattern sees only part of an interrupted token.
Arbitrary base64 is not decoded: a zero-width character must not hide a secret,
and stripping format characters must not turn a short or placeholder literal
into a pattern that redacts ordinary text. The existing Basic auth check stays.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import base64
import binascii
import re
import unicodedata
import sqlite3


# Match whole assignment names, including ASCII environment prefixes such as
# TYPESAFE_ or DB_. Keep the outer boundary and require the secret name at the
# end: API_KEY_COUNT and tokenizer settings are not credential assignments.
# Hugging Face special token attributes are model vocabulary, not credentials:
# tokenizer.pad_token = tokenizer.eos_token must not mask a note or block Jev.
# Only the exact attribute names are skipped; SPLUNK_TOKEN still masks.
# SECRET_KEY, AWS_SECRET_ACCESS_KEY and PRIVATE_KEY are among the most common
# .env credentials; PUBLIC_KEY and *_KEY_PATH names stay unmasked.
_SECRET_NAME = (r"(?!(?:pad|eos|bos|unk|sep|cls|mask)_token(?a:(?![a-z0-9_])))"
                r"(?a:(?:[a-z_][a-z0-9_]*_|_)?)"
                r"(?:api[_ -]?key|access[_ -]?token|token|secret(?:[_ -]?access)?[_ -]?key"
                r"|private[_ -]?key|secret|password|passwd)")
# The name boundary is ASCII like the prefix. Unicode IGNORECASE lets [a-z] match
# the Turkish İ and ı, which hid the whole value of KİŞİSEL_API_KEY=... while
# MÜŞTERI_API_KEY=... was masked.
_NAME_START = r"(?a:(?<![a-z0-9_]))"
# A value is masked whole or not at all: a mask that reports success but leaves
# the tail of the value in the note is the worst outcome. _backtick_value builds
# one branch per opening run of backticks.
# A span holding only an upper-case snake_case name documents a variable
# ("API key: `OPENAI_API_KEY` okunur"), so it stays text; the check is
# case-sensitive under the pattern's (?i).
_LINE_END = r"(?=[ \t]*(?:\r?\n|\Z))"
# What may follow a closing backtick run and stay outside the mask: up to three
# punctuation marks before a space or the end ("`value`.", "`value`, sonra").
# Typographic marks count too: macOS replaces quotes and dots as the user types
# ("“`NAME=value`” dedi", "`NAME=value`… sonra").
_AFTER_VALUE = (r"""[.,;:!?)\]}'"*_~|\u2019\u201d\u00bb\u203a\u2026\u2014\u2013]{0,3}"""
                r"""(?:\s|\Z)""")
# A Turkish suffix after inline code ("`NAME=value`'yi ayarla", "`...`’nin",
# "`...`'lerindekilerden") also follows a closing run. It is lower case and
# bounded, so it cannot take with it a long piece of a value that holds a quote
# after a backtick, and the lookahead stays linear.
_TURKISH_SUFFIX = r"['\u2019](?-i:[a-zçğıöşü]){1,20}(?=" + _AFTER_VALUE + ")"
# A quoted value ends at its closing quote, as in 3.17.0.
_QUOTED_VALUE = r"""\\{0,8}["\']\S{8,}\\{0,8}["\']"""


def _unquoted_value(start="", head_runs=False, suffix=True):
    """A value that no quote or backtick opens, kept inside one non-space token.

    A comma or a semicolon ends it, as in 3.17.0: "token=Xy7kL9am,mode=fast"
    and "Password=Xy7kL9am;Database=foo" keep the next setting. A value that
    holds one of them has to be quoted. A backtick run that a value character
    follows belongs to the value ("Xy7#kL9a`mN2$pQ4!"); a run that a space, the
    end, up to three punctuation marks before them or a Turkish suffix follow
    closes the inline-code span around the assignment and stays outside
    ("- `NAME=value`", "`NAME=value`'yi ayarla"). The run is one step that
    needs a value character after it, so a long run cannot make the scan
    quadratic.

    An assignment value starts with eight characters that are not backticks,
    as in 3.17.0, so a JSON-escaped fence after a label ("Token:\\n```js")
    stays text. A setx value counted backticks among its first eight in 3.17.0
    (head_runs) and keeps doing so.
    Without suffix a Turkish suffix after a backtick run is part of the value,
    as 3.17.0 read a setx value: a suffix may be up to 20 letters of the value.
    """
    character = r"[^\x60\s,;]"
    inner_run = (r"\x60+(?=" + character + r")(?!" + _AFTER_VALUE
                 + ("|" + _TURKISH_SUFFIX if suffix else "") + ")")
    step = "(?:" + character + "|" + inner_run + ")"
    head = step + "{7,}" if head_runs else character + "{7,}" + step + "*"
    return start + r"[^\"'\x60\s,;]" + head


def _backtick_value(opener, close, before_line_end, short_run=None):
    """A value opened by a run of backticks, kept inside one non-space token.

    Markdown writes a value that holds backticks with a longer opening run
    (``Xy7`mN2``, ```Xy7``mN2```), so a run shorter than the opener is part of
    the value (short_run; a single backtick opener holds none). A run at least
    as long as the opener closes it; when only backticks and up to three
    punctuation marks follow before a space, they stay outside ("`value`."),
    otherwise the rest of the token goes with the value ("`Xy7kL9am`mN2" or
    "`value`'yi"), so no tail is left visible. A span that closes before eight
    characters is short inline code and stays text ("`kisa`mN2pQ4zR8"); the
    scan does not read on through its closing run.
    One or two backticks may also end at the line end, as an inline span may be
    unclosed or wrap; three or more open a fenced block whose info string ends
    the line ("API key:\\n```javascript"), so that run needs a closing run or
    at least a backtick at the line end, which an info string cannot hold.
    Jev scans the JSON form of a note too, where a line break is the two
    characters \\n. The value never holds \\n, \\r or \\t, so a short span
    before a line break ("token: `kisa`\\ndevam") or a fence opened on the
    label line stays text there as it does in the note. A value written with a
    literal \\n is left as written. An unclosed value does not end in a quote
    or a closing bracket, so the end of the JSON string ('`hunter2"}') does not
    lengthen a short span into a value.
    A span with a space inside stays prose (``value` rest``); nothing masks
    only its first word.
    """
    character = r"(?!\\[nrt])[^\x60\s]"
    step = "(?:" + character + ("" if short_run is None
                                else "|" + short_run + r"(?=[^\x60\s])") + ")"
    ending = close + r"(?:(?=\x60*" + _AFTER_VALUE + r")|\S*)"
    ending += "|" + r"""(?<!["\]}])""" + before_line_end + _LINE_END
    return opener + character + step + "{7,}?(?:" + ending + ")"


# The fence run is capped so a chain of ever shorter openers in one token
# cannot make the scan quadratic; a longer run is left as written.
def _backtick_span():
    return (r"""(?!(?-i:\x60+[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\x60))(?:"""
            + _backtick_value(r"\x60", r"\x60", r"\x60*") + "|"
            + _backtick_value(r"\x60\x60", r"\x60\x60", r"\x60*", r"\x60") + "|"
            + _backtick_value(r"(?P<fence>\x60{3,16})", r"(?P=fence)", r"\x60+",
                              r"(?!(?P=fence))\x60+") + ")")


# The same name with = and no value, or an empty quoted value, documents the
# variable too ("Token: `OPENAI_API_KEY=` gerekli"); 3.17.0 left it as written.
# It stays text only when its closing run ends the token (a space, the end,
# trailing marks, a Turkish suffix or a JSON escape follow), so a value glued
# after the run ("`NAME=`Xy7kL9amN2pQ") is still masked with the span.
_EMPTY_ASSIGNMENT = (r"""(?!(?-i:\x60+[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+="""
                     r"""(?:\\{0,8}"\\{0,8}"|\\{0,8}'\\{0,8}')?\x60+)(?="""
                     + _AFTER_VALUE + "|" + _TURKISH_SUFFIX + r"|\\[nrt]))")
_SECRET_VALUE = ("(?:" + _QUOTED_VALUE + "|" + _EMPTY_ASSIGNMENT + _backtick_span()
                 + "|" + _unquoted_value() + ")")

# The setx value of 3.17.0 may start with a backtick and counts backticks among
# its first eight characters. A setx value that the span and unquoted rules
# above do not read (an unclosed span before more text, "setx DB_PASSWORD
# `$Xy7kL9am2 /M"; a span that closes before eight characters,
# "`kisa`mN2pQ4zR8"; a run longer than the fence cap; a value split inside its
# first eight characters) was masked by 3.17.0, so it is masked here too, to
# the end of the token: a comma or a semicolon in it leaves no tail, and only
# up to three punctuation marks before the space stay outside, as after a
# span. Text without a backtick never reaches this rule, since the unquoted
# rule above reads such a value exactly as 3.17.0 did.
_SETX_FALLBACK = (r"""(?=[^\s,;]*\d)[^"\'\s,;](?=[^\s,;]{7})\S*?(?="""
                  + _AFTER_VALUE + ")")


BUILTIN_PATTERNS = (
    re.compile(r"(?i)(?<=[?&])" + _SECRET_NAME + r"=[^&\s]{8,}"),
    # A quoted name may close a subscript: os.environ["NAME"] = "value". The
    # quote is required so a Markdown reference link [token]: url stays text.
    re.compile(r"""(?i)""" + _NAME_START + r"""(?:\\{0,8}["\']?""" + _SECRET_NAME +
               r"""(?:\\{0,8}["\']\]|\\{0,8}["\']?))\s*[:=]\s*""" + _SECRET_VALUE),
    # Commands that separate the name from the value without = or a colon:
    # Windows setx and [Environment]::SetEnvironmentVariable, fish set -x. A
    # space also separates prose words, so an unquoted value needs a digit:
    # "setx TYPESAFE_API_KEY komutunu kullan" stays text.
    re.compile(r"""(?i)(?a:(?<![a-z0-9_-]))(?:(?:setx(?:[ \t]+/m)?|set[ \t]+-[a-z]{0,3}x[a-z]{0,3})"""
               r"""[ \t]+["\']?""" + _SECRET_NAME + r"""["\']?[ \t]+|setenvironmentvariable\([ \t]*["\']"""
               + _SECRET_NAME + r"""["\'][ \t]*,[ \t]*)"""
               + "(?:" + _QUOTED_VALUE + r"|(?=\S*\d)" + _backtick_span() + "|"
               + _unquoted_value(start=r"(?=[^\s,;]*\d)", head_runs=True, suffix=False)
               + "|" + _SETX_FALLBACK + ")"),
    re.compile(r"(?<=://)[^/\s?#:]*:[^/\s?#]*(?=@[^/\s?#@]+)"),
    re.compile(r"\bgh(?:p|o|u|s|r)_[A-Za-z0-9]{20,255}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,255}\b"),
    # Hugging Face access token: hf_ plus exactly 34 letters or digits. A fixed length
    # keeps identifiers such as hf_TransformersAutoModelLoader out.
    re.compile(r"\bhf_[A-Za-z0-9]{34}(?![A-Za-z0-9])"),
    # The key alphabet includes - and _, so only a key character may not follow; a period
    # or dash before the key, or a sentence-ending period after it, still redacts.
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,255}(?![A-Za-z0-9_-])"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    # Complete blocks may appear as escaped strings or with internal whitespace.
    # Stop at the next PEM boundary, so missing trailers cannot cause repeated rescans.
    re.compile(r"-----BEGIN (?P<keytype>[A-Z0-9 ]*PRIVATE KEY)-----"
               r"(?:(?!-----(?:BEGIN|END) )[\s\S])*"
               r"-----END (?P=keytype)-----"),
    # Match PEM body lines locally, independent of whether the trailer is present.
    # A second header or a prose line cannot pull the scan into the next block.
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"
               r"(?:[ \t]*\r?\n[ \t]*(?:Proc-Type:|DEK-Info:)[^\r\n]*){0,2}"
               r"(?:[ \t]*\r?\n(?=[ \t]*\r?\n))?"
               r"(?:[ \t]*\r?\n[ \t]*[A-Za-z0-9+/]{4,}={0,2}(?=[ \t]*(?:\r?\n|\Z)))*"
               r"(?:[ \t]*\r?\n[ \t]*-----END [A-Z0-9 ]*PRIVATE KEY-----)?"),
    re.compile(r"(?i)\bauthorization[ \t]*:[ \t]*token[ \t]+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{20,}"),
    # Provider formats with a fixed, distinctive prefix. Stripe keys use underscores,
    # so the dashed sk- pattern above never sees them (#151).
    re.compile(r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{16,255}\b"),
    re.compile(r"\bxox[abeoprs]-[0-9]+-[A-Za-z0-9-]{8,255}\b"),
    re.compile(r"\bxapp-[0-9]+-[A-Za-z0-9-]{8,255}\b"),
    re.compile(r"(?<![A-Za-z0-9_-])AIza[0-9A-Za-z_-]{35}(?![A-Za-z0-9_-])"),
    re.compile(r"\bnpm_[A-Za-z0-9]{36}(?![A-Za-z0-9])"),
    re.compile(r"(?<![A-Za-z0-9_.-])SG\.[A-Za-z0-9_-]{22}\.[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-]|\.[A-Za-z0-9_-])"),
    # A JWT header is base64url JSON, so it always opens with eyJ; three dot-separated
    # base64url segments are required (five for an encrypted JWE). Plain base64
    # (images, blobs) has no dots.
    re.compile(r"(?<![A-Za-z0-9_.-])eyJ[A-Za-z0-9_-]{10,}(?:\.[A-Za-z0-9_-]{10,}){2}(?:\.[A-Za-z0-9_-]+){0,2}(?![A-Za-z0-9_-])"),
    # Discord bot token: base64 of a numeric user id (so it starts with M, N or O),
    # a 6 character timestamp and an HMAC of at least 27 characters. Like SendGrid it
    # must stand alone: a segment of a longer dotted name is not a token.
    re.compile(r"(?<![A-Za-z0-9_.-])[MNO][A-Za-z0-9_-]{22,27}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,40}(?![A-Za-z0-9_-]|\.[A-Za-z0-9_-])"),
)


def _custom_literals(state):
    """Load bounded literal values, not executable regular expressions."""
    path = Path(state) / 'secret-patterns.txt'
    if not path.exists():
        return []
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 64 * 1024:
        raise ValueError('secret-patterns.txt must be a regular file no larger than 64 KiB')
    values = []
    for number, raw in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
        value = raw.strip()
        if not value or value.startswith('#'):
            continue
        if len(value) < 4 or len(value) > 1000 or value == '[REDACTED]':
            raise ValueError(f'invalid secret literal at line {number}')
        values.append(value)
        if len(values) > 100:
            raise ValueError('secret-patterns.txt supports at most 100 literals')
    return values


_BASIC_AUTH = re.compile(r"(?i)\bbasic[ \t]+([A-Za-z0-9+/]+={0,2})(?![A-Za-z0-9+/=])")


def _without_format(text):
    return ''.join(character for character in text if unicodedata.category(character) != 'Cf')


def _sub_normalized(pattern, text, positions):
    parts, mapped, spans = [], [], []
    end, count = 0, 0
    for match in pattern.finditer(text):
        value = '[REDACTED]'
        parts.append(text[end:match.start()])
        mapped.extend(positions[end:match.start()])
        parts.append(value)
        mapped.extend([positions[match.start()]] * len(value))
        spans.append((positions[match.start()], positions[match.end() - 1] + 1, value))
        end = match.end()
        count += 1
    parts.append(text[end:])
    mapped.extend(positions[end:])
    return ''.join(parts), mapped, spans, count


def _restore_original(text, spans):
    merged = []
    for start, stop, value in sorted(spans):
        if merged and start < merged[-1][1]:
            previous = merged[-1]
            merged[-1] = (previous[0], max(stop, previous[1]), previous[2])
        else:
            merged.append((start, stop, value))
    parts, end = [], 0
    for start, stop, value in merged:
        parts.extend((text[end:start], value))
        end = stop
    parts.append(text[end:])
    return ''.join(parts), len(merged)


def _redact(text, patterns):
    # ASCII cannot contain Cf characters; preserve the original substitution path.
    if text.isascii() or not any(unicodedata.category(character) == 'Cf' for character in text):
        result, count = text, 0
        for pattern in patterns:
            result, matches = pattern.subn('[REDACTED]', result)
            count += matches
        return result, count
    positions = [index for index, character in enumerate(text)
                 if unicodedata.category(character) != 'Cf']
    normalized = ''.join(text[index] for index in positions)
    spans = []
    for pattern in patterns:
        normalized, positions, found, _ = _sub_normalized(pattern, normalized, positions)
        spans.extend(found)
    # Normalization can remove an existing regex boundary (x + Cf + sk-...).
    # Preserve raw matches too; union their original spans before replacing so a
    # raw partial match cannot prevent normalization from masking the whole key.
    raw, raw_positions = text, range(len(text))
    for pattern in patterns:
        raw, raw_positions, found, _ = _sub_normalized(pattern, raw, raw_positions)
        spans.extend(found)
    return _restore_original(text, spans)


def redact(text, state):
    if not isinstance(text, str):
        raise ValueError('secret filter input must be text')
    result, count = _redact(text, BUILTIN_PATTERNS)
    def redact_basic(match):
        nonlocal count
        try:
            decoded = base64.b64decode(match[1], validate=True)
        except (ValueError, binascii.Error):
            return match[0]
        if b':' not in decoded:
            return match[0]
        count += 1
        return '[REDACTED]'

    result = _BASIC_AUTH.sub(redact_basic, result)
    for literal in _custom_literals(state):
        normalized = _without_format(literal)
        if (len(normalized) >= 4 and '[REDACTED]' not in normalized
                and normalized not in '[REDACTED]'):
            result, matches = _redact(result, (re.compile(re.escape(normalized)),))
        else:
            # Unsafe normalized values must retain only their original exact match.
            matches = result.count(literal)
            if matches:
                result = result.replace(literal, '[REDACTED]')
        count += matches
    return result, count


def record(state, count):
    """Persist only aggregate counts outside the vault; never persist matches."""
    if type(count) is not int or count < 0:
        raise ValueError('redaction count must be a nonnegative integer')
    state = Path(state)
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = state / 'secret-filter.sqlite3'
    if path.is_symlink():
        raise ValueError('secret filter health database must not be a symlink')
    with closing(sqlite3.connect(path, timeout=10)) as db, db:
        db.execute('CREATE TABLE IF NOT EXISTS health (id INTEGER PRIMARY KEY CHECK(id=1), total INTEGER NOT NULL, last_count INTEGER NOT NULL, last_at TEXT)')
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT total FROM health WHERE id=1').fetchone()
        total = (row[0] if row else 0) + count
        db.execute('INSERT OR REPLACE INTO health VALUES (1,?,?,?)',
                   (total, count, datetime.now(timezone.utc).isoformat()))
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return {'total': total, 'last_count': count}


def health(state):
    path = Path(state) / 'secret-filter.sqlite3'
    if not path.exists():
        return {'total': 0, 'last_count': 0, 'last_at': None}
    if path.is_symlink() or not path.is_file():
        raise ValueError('secret filter health database must be a regular file')
    with closing(sqlite3.connect(path, timeout=2)) as db:
        row = db.execute('SELECT total,last_count,last_at FROM health WHERE id=1').fetchone()
    return ({'total': row[0], 'last_count': row[1], 'last_at': row[2]}
            if row else {'total': 0, 'last_count': 0, 'last_at': None})
