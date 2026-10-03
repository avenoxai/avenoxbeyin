"""User-owned companion bootstrap and compact, source-verified session context."""
import json
from pathlib import Path
import re
import sys
import unicodedata

NAMES = ('Core.md', 'Soul.md', 'Kurallar.md', 'Last-Session.md', 'Threads.md', 'Journal.md')
FLOORS = {'Kurallar.md': .4, 'Last-Session.md': .2}
# Hygiene limits in characters for the two handoff files that are meant to be rewritten,
# not appended to (#96). Rules, identity and the Journal accumulate by design and are only
# measured. 0 turns a limit off; beyin.py preferences changes them.
LIMITS = {'Last-Session.md': 3000, 'Threads.md': 8000}
LIMIT_RANGE = (1000, 200000)
DEFAULT_DIRECTORY = '🔮 850-Companion'
STARTERS = {
    'Core.md': '# Düşünme ortağı\n\nKullanıcının düşünme ortağı ve ikinci beyniyim. Kimliğimi ve çalışma biçimimi birlikte belirleriz.\n\n## Kullanıcı ve ortak çalışma biçimi\nHenüz kişiselleştirilmedi. Kullanıcının adı, tercih ettiği hitap, çalışma alanı ve beklentilerini konuşarak öğren. Bilinmeyen geçmişi uydurma.\n\n## Kalıcı tercihler\nKullanıcının açıkça belirttiği tercihleri ve dayandıkları kaynağı burada tut.\n',
    'Kurallar.md': '# Kullanıcının düzeltmeleri\n\nHenüz kaydedilmiş bir düzeltme yok. Açık kullanıcı düzeltmelerini tarih ve kapsamıyla, her kuralı bir iki satırda kaydet; uzun gerekçe ve olayın anlatımı ayrı bir nota gider. Geçici istekleri kalıcı kurala dönüştürme.\n',
    'Last-Session.md': '# Son oturum\n\nHenüz bir çalışma sonucu kaydedilmedi. Anlamlı çalışma sonunda sonuç, gerekçe, açık kalan adım ve kaynak bağlantılarını `## YYYY-MM-DD HH:MM · <etiket> · <session_id[:8]>` başlığıyla buraya yaz. Paralel oturumlarda yalnız kendi kartını düzenle, başka oturumların kartlarını ezme.\n',
    'Threads.md': '# Threads\n\n## Active Threads\nHenüz açık bir konu kaydedilmedi.\n\n## Closed Threads\n',
    'Journal.md': '# Journal\n\nOrtak çalışmadan doğan gözlemler, öğrenimler ve açık sorular. Çıkarımları kesin kullanıcı bilgisi olarak sunma.\n',
}


def directory(vault):
    """Reuse one existing local identity directory; never guess between identities."""
    vault = Path(vault).resolve()
    candidates = []
    named = []
    for path in vault.iterdir():
        if (path.name.startswith('.') or path.is_symlink() or not path.is_dir() or
                re.search(r'(?i)(archive|arşiv|arsiv)', path.name)):
            continue
        if path.name == DEFAULT_DIRECTORY or path.name.casefold().endswith(('companion', 'echo')):
            named.append(path)
        elif any((path / name).is_file() for name in ('Core.md', 'Soul.md')):
            candidates.append(path)
    candidates = named or candidates
    if len(candidates) > 1:
        return None
    return candidates[0] if candidates else vault / DEFAULT_DIRECTORY


def initialize(vault, state):
    """Create missing starter notes once. These are user data, never package-owned.

    This is deliberately separate from managed rollback: learning written after
    installation must survive updates and uninstall. Exclusive creation preserves
    existing notes and makes interrupted/repeated bootstrap safe to retry.
    """
    state = Path(state)
    marker = state / 'companion-bootstrap.json'
    if marker.exists():
        return {'status': 'existing'}
    target = directory(vault)
    if target is None or target.is_symlink():
        return {'status': 'needs_attention', 'reason': 'Choose the existing companion directory; no notes created.'}
    target.mkdir(parents=True, exist_ok=True)
    if not target.resolve().is_relative_to(Path(vault).resolve()):
        raise ValueError('Companion directory escapes vault')
    created = []
    for name, text in STARTERS.items():
        path = target / name
        # A pre-existing Soul already carries identity; do not create a competing Core.
        if name == 'Core.md' and (target / 'Soul.md').exists():
            continue
        try:
            with path.open('x', encoding='utf-8') as output:
                output.write(text)
            created.append(name)
        except FileExistsError:
            pass
    from beyin_v3_sync import atomic
    state.mkdir(parents=True, exist_ok=True)
    atomic(marker, json.dumps({'schema': 1, 'directory': target.relative_to(Path(vault).resolve()).as_posix()}))
    return {'status': 'initialized', 'created': created}


def check_limits(value):
    if not isinstance(value, dict) or set(value) - set(LIMITS) - {'schema'} or value.get('schema', 1) != 1:
        raise ValueError('companion limits accept only ' + ', '.join(LIMITS))
    for name in LIMITS:
        number = value.get(name, LIMITS[name])
        if type(number) is not int or not (number == 0 or LIMIT_RANGE[0] <= number <= LIMIT_RANGE[1]):
            raise ValueError(f'{name} limit must be 0 (off) or an integer between {LIMIT_RANGE[0]} and {LIMIT_RANGE[1]}')
    return {name: value.get(name, LIMITS[name]) for name in LIMITS}


def read_limits(state):
    """(limits, valid). Machine-local, beside the runtime state, never in .beyin-preferences.json:
    an older release rejects unknown preference fields, so a rollback would break its hooks.
    A damaged file falls back to the defaults here and is never silently rewritten."""
    path = Path(state) / 'companion-limits.json'
    if not path.exists() and not path.is_symlink():
        return dict(LIMITS), True
    try:
        if path.is_symlink():
            raise ValueError('symlink')
        return check_limits(json.loads(path.read_text(encoding='utf-8'))), True
    except (ValueError, OSError):
        return dict(LIMITS), False


def save_limits(state, changes):
    current, valid = read_limits(state)
    if not valid:
        raise ValueError('companion-limits.json in the runtime state is invalid; fix or remove it first')
    result = check_limits(dict(current, **changes))
    from beyin_v3_sync import atomic
    atomic(Path(state) / 'companion-limits.json', json.dumps(dict(schema=1, **result), ensure_ascii=False, indent=2) + '\n')
    return result


# Budget for the companion opening context (SessionStart and continuity questions), #140.
# The handoff limits alone (3000 + 8000) nearly fill the 12000 context_chars ceiling, so the
# opening may use a larger, separate budget. Machine-local in <state>/companion-context.json,
# never a .beyin-preferences.json field or range: an older release validates that file and a
# rollback would otherwise break every hook, doctor and even `preferences` itself.
CONTEXT_FILE = 'companion-context.json'
CONTEXT_RANGE = (1000, 24000)


def check_context(value):
    if not isinstance(value, dict) or set(value) - {'schema', 'context_chars'} or value.get('schema', 1) != 1:
        raise ValueError('companion context accepts only context_chars')
    number = value.get('context_chars', 0)
    if type(number) is not int or not (number == 0 or CONTEXT_RANGE[0] <= number <= CONTEXT_RANGE[1]):
        raise ValueError('companion context_chars must be 0 (use context_chars) or an integer between '
                         f'{CONTEXT_RANGE[0]} and {CONTEXT_RANGE[1]}')
    return {'context_chars': number}


def read_context(state):
    """(settings, valid). A missing file means 0: the opening uses context_chars. A damaged
    file falls back to 0 as well and is never silently rewritten."""
    path = Path(state) / CONTEXT_FILE
    if not path.exists() and not path.is_symlink():
        return {'context_chars': 0}, True
    try:
        if path.is_symlink():
            raise ValueError('symlink')
        return check_context(json.loads(path.read_text(encoding='utf-8'))), True
    except (ValueError, OSError):
        return {'context_chars': 0}, False


def save_context(state, context_chars):
    current, valid = read_context(state)
    if not valid:
        raise ValueError(CONTEXT_FILE + ' in the runtime state is invalid; fix or remove it first')
    result = check_context(dict(current, context_chars=context_chars))
    from beyin_v3_sync import atomic
    atomic(Path(state) / CONTEXT_FILE, json.dumps(dict(schema=1, **result), ensure_ascii=False, indent=2) + '\n')
    return result


def opening_budget(state, context_chars):
    """Characters for the companion opening context; context_chars unless set separately."""
    return read_context(state)[0]['context_chars'] or context_chars


# A client that moves a long hook additionalContext to a file shows the model only part of it,
# so the automatic context stays under that client's line. Each entry is (limit, measure):
# - Claude Code: over 10,000 characters (JavaScript string length, so UTF-16 units) it shows
#   only a ~2,000 character preview; no setting raises it (#175).
# - Codex: over 2,500 approximate tokens, ceil(UTF-8 bytes / 4), so 10,000 bytes, it shows a
#   head and tail preview with the middle cut out (codex-rs/hooks/src/output_spill.rs). Its own
#   per-hook additionalContextLimit lives in .codex/hooks.json, and editing that file drops the
#   user's hook trust, so the hook keeps its text under the default instead.
# Clients without a known cap here keep their budget.
CLIENT_TEXT_LIMITS = {'claude': (10000, 'utf-16'), 'codex': (10000, 'utf-8')}
CLIENT_HEADROOM = 500


def client_size(harness, text):
    """Length of text in the client's own measure; None for a client without a known cap."""
    limit = CLIENT_TEXT_LIMITS.get(harness)
    if not limit:
        return None
    return len(text.encode('utf-16-le')) // 2 if limit[1] == 'utf-16' else len(text.encode('utf-8'))


def client_budget(harness, chars):
    """The part of a character budget the client shows in full. For a byte-measured client the
    headroom also absorbs the multi-byte letters of an ordinary Turkish text."""
    limit = CLIENT_TEXT_LIMITS.get(harness)
    return min(chars, limit[0] - CLIENT_HEADROOM) if limit else chars


def client_rebudget(harness, budget, text):
    """A smaller character budget for text that was rendered with budget but is over the client's
    own measure (a Turkish letter is two UTF-8 bytes for Codex), so a second render keeps every
    section instead of losing the tail; None when it already fits."""
    limit = CLIENT_TEXT_LIMITS.get(harness)
    size = client_size(harness, text) if limit else None
    if size is None or size <= limit[0]:
        return None
    return budget * limit[0] // size


def fit_client(harness, text):
    """Last guard after client_budget: an astral character (the companion folder's emoji) counts
    twice in UTF-16 and four times in UTF-8, so trim the tail until the client's own measure fits."""
    limit = CLIENT_TEXT_LIMITS.get(harness)
    over = client_size(harness, text) - limit[0] if limit else 0
    while over > 0:
        # A character is one to four units: cut at least one, at most what is over.
        text = text[:len(text) - max(1, over // (2 if limit[1] == 'utf-16' else 4))]
        over = client_size(harness, text) - limit[0]
    return text


def size(path):
    """Unicode characters as stored: not bytes and not UTF-16 units, so a Turkish or
    emoji-rich file is not reported larger than it is. Line endings are not translated
    (CRLF counts as two), the same measure companion-compact fits a file to. Streams;
    bounded memory."""
    count = 0
    with Path(path).open(encoding='utf-8', errors='replace', newline='') as source:
        for chunk in iter(lambda: source.read(1 << 16), ''):
            count += len(chunk)
    return count


def hygiene(vault, state, names=NAMES, target=None):
    """Read-only size report of the companion sources; limited files say whether they are over."""
    vault = Path(vault).resolve()
    target = directory(vault) if target is None else target
    configured, valid = read_limits(state)
    report = {'status': 'ok', 'limits': configured, 'limits_file': 'ok' if valid else 'invalid_defaults_used',
              'files': {}, 'over_limit': []}
    if target is None:
        return dict(report, status='ambiguous_directory')
    report['directory'] = target.relative_to(vault).as_posix()
    for name in names:
        path = target / name
        if path.is_symlink() or not path.is_file():
            continue
        entry = {'chars': size(path)}
        if name in LIMITS:
            entry['limit'] = configured[name]
            entry['over_limit'] = bool(configured[name]) and entry['chars'] > configured[name]
            if entry['over_limit']:
                report['over_limit'].append(name)
        report['files'][name] = entry
    if report['over_limit']:
        report['status'] = 'over_limit'
    return report


def hygiene_notice(report):
    """One line at the top of the session context, only when a handoff file is over its limit."""
    over = [f'{name} is {report["files"][name]["chars"]} chars (limit {report["files"][name]["limit"]})'
            for name in report.get('over_limit', [])]
    if not over:
        return ''
    python = 'py -3' if sys.platform == 'win32' else 'python3'
    return ('Memory hygiene: ' + '; '.join(over) + f'. Before other memory writes run: {python} beyin.py companion-compact '
            '(moves old entries verbatim to a private archive, deletes nothing). Do not read the whole file; '
            'afterwards rewrite in place, never append.\n')


# Turkish letters that NFKD does not decompose; the rest (ş, ü, ç, â, ...) lose their marks there.
FOLD = str.maketrans('ıİ', 'iI')
# "We are back, what were we doing" in its common first-person forms, matched on folded
# text so a keyboard without Turkish letters (donduk, yapmistik) reads the same (#151).
RETURNING = re.compile(r'\b(?:ne(?:ler)? (?:yaptik|yapmistik|yapiyorduk)|nere?deydik|kaldigimiz (?:yer|konu)'
                       r'|son durum(?:umuz)? (?:ne|nedir|neydi)\b|ne olmustu|(?:tatil|izin)den don(?:dum|duk)\b'
                       r'|kisilig|what (?:did|have) we (?:do|done|work(?:ed)? on)|what were we (?:doing|working on)'
                       r'|where were we|catch me up)')


def fold(text):
    """Lowercase ASCII-ish form for matching: ı/İ become i, combining marks drop."""
    decomposed = unicodedata.normalize('NFKD', text.translate(FOLD))
    return ''.join(c for c in decomposed if not unicodedata.combining(c)).lower()


def relevant(query):
    # 'nerede kal' only as a first-person continuity question (#145): kargo nerede kaldı is not.
    # (?!l[ae]r) keeps kaldıkları/kaldiklarini (theirs) out; re.I already folds ı/i/I/İ, only
    # ş/s and ğ/g need ASCII spellings.
    return bool(re.search(r'(?i)(son (oturum|konuş)|geçen (sefer|oturum|konuş)|ner(?:e)?de kal(?:dık|dıydık|mıştık|dığım|dım|dıydım|mıştım|mışız|mışım|dıysak|dıysam|dik|diydik|mistik|digim|dim|diydim|mistim|misiz|misim|diysak|diysam)(?!l[ae]r)|ne (yaptık|yapmıştık)|beni (tanı|hatırla)|kişili|tercihlerim|sen kimsin|kim olduğunu|last (session|time)|previous session|where (did we|we) leave|remember me|personality|my (preferences|name)|who (am i|are you))', query)) or bool(RETURNING.search(fold(query)))


def stamp(header):
    """Sortable (date, time) of a Journal header; an untimed entry sorts before timed ones."""
    date = re.search(r'\d{4}-\d{2}-\d{2}', header)
    clock = re.search(r'(?<!\d)([01]?\d|2[0-3]):[0-5]\d(?!\d)', header)
    return (date[0], clock[0].rjust(5, '0') if clock else '') if date else None


def excerpt(name, text):
    if name == 'Threads.md':
        # Same active headings compaction recognizes (beyin_v3_compact.ACTIVE), so a
        # '## Açık konular' file does not inject its closed threads too (#153).
        match = re.search(r'(?im)^## (?:(?:Active|Open)(?: Threads)?|Aktif[^\n]*|A[çc][ıi]k(?:[ \t][^\n]*)?)\s*$', text)
        if match:
            body = text[match.start():]
            closed = re.search(r'(?im)^## (?:Closed|Kapan|Kapalı)', body)
            return body[:closed.start()] if closed else body
    if name == 'Journal.md':
        entries = list(re.finditer(r'(?m)^## ([^\n]+)', text))
        dated = [(moment, i) for i, item in enumerate(entries) if (moment := stamp(item[1]))]
        if entries:
            # Equal timestamps follow the file's own direction: a newest-first journal keeps
            # the latest entry at the top, an append-ordered one at the bottom.
            # Only distinct timestamps show a direction; when every dated entry shares one,
            # the bottom goes first, as for an all-undated journal (#163).
            moments = [moment for moment, _ in dated]
            rising = any(a < b for a, b in zip(moments, moments[1:]))
            newest_first = not rising and any(a > b for a, b in zip(moments, moments[1:]))
            index = max(dated, key=lambda item: (item[0], -item[1] if newest_first else item[1]))[1] if dated else len(entries) - 1
            # An undated entry at the end the newest writing lands on is the latest thought,
            # so it wins over dated ones instead of dropping out of the selection (#134).
            # Without two distinct dates the direction is unknown; the bottom goes first,
            # as in an all-undated journal.
            undated = set(range(len(entries))) - {i for _, i in dated}
            if len({moment for moment, _ in dated}) > 1:
                edges = (0,) if newest_first else (len(entries) - 1,)
            else:
                edges = (len(entries) - 1, 0)
            index = next((edge for edge in edges if edge in undated), index)
            return text[entries[index].start():entries[index + 1].start() if index + 1 < len(entries) else len(text)]
    if name == 'Last-Session.md':
        previous = re.search(r'(?im)^## (?:Previous|Önceki)', text)
        if previous:
            return text[:previous.start()]
    if name == 'Kurallar.md':
        return without_v2_window(text)
    return text


# The V2 seed of Kurallar.md says its "first 60 lines" are injected. V3 shows the start and
# end of the file within a character budget (#45), so that text sends the agent the wrong
# model: rules were moved to the top "out of the 60-line window" (#177). The opening context
# names the omission instead; the user's file is never changed.
V2_WINDOW = re.compile(r'(?i)\bilk 60 sat[ıi]r')
V2_WINDOW_NOTE = ('[V2 template note about "the first 60 lines" omitted: V3 shows the start and end of this '
                  'file within a character budget; read source]')


def _level(paragraph):
    """Heading level of a one-line heading paragraph, else 0."""
    match = re.fullmatch(r'(#{1,6}) [^\n]*\n?', paragraph)
    return len(match[1]) if match else 0


def without_v2_window(text):
    pieces = re.split(r'(\n(?:[ \t]*\n)+)', text)
    paragraphs, gaps = pieces[0::2], [''] + pieces[1::2]
    drop = {i for i, item in enumerate(paragraphs)
            if V2_WINDOW.search(item) and '**kural' not in item.casefold()}
    if not drop:
        return text
    for i in sorted(drop):
        # A section heading left with nothing under it (`## Nasıl büyür`) goes with its paragraph.
        after = next((j for j in range(i + 1, len(paragraphs)) if j not in drop), None)
        if i and _level(paragraphs[i - 1]) and (after is None or 0 < _level(paragraphs[after].split('\n', 1)[0]) <= _level(paragraphs[i - 1])):
            drop.add(i - 1)
    out, noted = '', False
    for i, item in enumerate(paragraphs):
        if i in drop:
            if not noted:
                out += gaps[i] + V2_WINDOW_NOTE
                noted = True
            continue
        out += gaps[i] + item
    return out.lstrip('\n') if not text.startswith('\n') else out


# A rule and the story of why it exists share one list item in the V2 format
# (`- **kural:** ... **neden:** ...`). When the rules do not fit their share of the opening,
# the reasons go first so more rules arrive whole (#177); the rule sentences stay verbatim.
REASON = re.compile(r'(?:\n[ \t]*|[ \t]*)\*\*(?:neden|gerekçe|gerekce|why|reason)(?::\*\*|\*\*:)'
                    r'.*?(?=\n[ \t]*(?:[-*+][ \t]|\d+[.)][ \t]|#)|\n[ \t]*\n|\n?\Z)', re.S | re.I)


def without_reasons(text):
    stripped, count = REASON.subn('', text)
    if not count:
        return text
    return stripped.rstrip('\n') + f'\n[{count} rule reasons (**neden:**) omitted to fit the opening; read source]\n'


RULE = re.compile(r'^(?:[-*+]|\d+[.)])[ \t]', re.M)


def ends(text, budget):
    """Opening plus closing lines of a rule set, with the omitted rules named.

    The first marker is sized with the widest form the final one can take, so the
    final one, naming only what was really dropped, can never be longer and the
    result stays inside budget. Text with no list items keeps the character count.
    """
    if len(text) <= budget:
        return text
    gap = f'\n[truncated: {len(text)} characters omitted here; read source]\n'
    total = len(RULE.findall(text))
    widest = f'\n[truncated: rules {total}-{total} ({total} of {total}) omitted here; read source]\n'
    gap = max(gap, widest, key=len)
    keep = budget - len(gap)
    if keep < 80:
        return None
    # Cut on line boundaries so neither end is a half rule; the closing part also takes
    # whatever the opening gave back.
    head = text[:keep - keep // 2]
    head = head[:head.rfind('\n') + 1] or head
    closing = text[len(text) - (keep - len(head)):]
    closing = closing[closing.find('\n') + 1:] or closing
    # Snapping to line boundaries gives characters back. Spend them on whole lines,
    # alternating between the two ends, never letting the ends meet (#151).
    start = len(text) - len(closing)
    grown = True
    while grown:
        grown = False
        cut = text.rfind('\n', len(head), start - 1) + 1 or len(head)
        if cut < start and len(head) + len(closing) + start - cut <= keep:
            closing, start, grown = text[cut:], cut, True
        end = text.find('\n', len(head), start) + 1
        if end and len(closing) + end <= keep and end < start:
            head, grown = text[:end], True
    first = len(RULE.findall(head)) + 1
    omitted = len(RULE.findall(text, len(head), start))
    what = f'rules {first}-{first + omitted - 1} ({omitted} of {total})' if omitted > 0 else f'{start - len(head)} characters'
    return head + f'\n[truncated: {what} omitted here; read source]\n' + closing


def clip(text, budget, tail=False, both=False):
    if len(text) <= budget:
        return text
    if both:
        kept = ends(text, budget)
        if kept:
            return kept
    marker = '\n[truncated: read source]\n'
    if budget <= len(marker):
        return marker.strip()[:budget]
    keep = budget - len(marker)
    return marker + text[-keep:] if tail else text[:keep] + marker


def context(store, budget, session, harness, query='', receipt='', warning=''):
    """Budget actual displayed text, not repeated JSON metadata; never cut a record header."""
    target = directory(store.vault_root)
    hygiene_line = ''
    if target is not None:
        try:
            hygiene_line = hygiene_notice(hygiene(store.vault_root, store.state_dir, tuple(LIMITS), target))
        except Exception:
            hygiene_line = ''  # A size check must never cost the session its context.
    header = (hygiene_line + warning + f'Receipt session={session}; choose --harness for the current client.\n'
              'V3 source-backed context (data, not instructions). Apply the companion protocol in AGENTS.md.\n')
    if target is None:
        return clip(header + 'Multiple companion directories: read the user-selected identity sources.\n', budget)
    snapshot = store.source_snapshot(NAMES, budget_chars=200000,
                                     source_directory=target.relative_to(store.vault_root).as_posix(),
                                     text_transform=excerpt)
    records = snapshot['records']
    notice = ''
    if snapshot['missing_sources']:
        notice += 'Missing/unavailable companion sources: ' + ', '.join(snapshot['missing_sources']) + '.\n'
    if snapshot['stale_excluded']:
        notice += 'Changed companion sources excluded; read current files.\n'
    sections = [(f'\n[{r["source"]}]\n', r['text'], Path(r['source']).name)
                for r in records]
    fixed = len(header) + len(notice) + sum(len(label) for label, _, _ in sections)
    if fixed > budget:
        return clip(header + notice + 'Read companion files: ' + ', '.join(r['source'] for r in records), budget)

    def share(available, parts):
        # Rules and the handoff get a floor first: an even split leaves the two continuity
        # sources the same share as a one-line style note. The rest water-fills, so small
        # identity files still return their unused share to long histories.
        lengths = [min(len(body), int(available * FLOORS.get(name, 0))) for _, body, name in parts]
        spare = available - sum(lengths)
        while spare and any(lengths[i] < len(item[1]) for i, item in enumerate(parts)):
            for i, (_, body, _) in enumerate(parts):
                if spare and lengths[i] < len(body):
                    lengths[i] += 1
                    spare -= 1
        return lengths

    def companion(available):
        parts = sections
        lengths = share(available, parts)
        if any(name == 'Kurallar.md' and lengths[i] < len(body) for i, (_, body, name) in enumerate(parts)):
            # Rules that do not fit drop their reasons before any rule is cut (#177).
            parts = [(label, without_reasons(body) if name == 'Kurallar.md' else body, name)
                     for label, body, name in parts]
            lengths = share(available, parts)
        rendered = header + notice
        for i, (label, body, name) in enumerate(parts):
            rendered += label + clip(body, lengths[i], both=name == 'Kurallar.md',
                                     tail=name == 'Kurallar.md' or (name == 'Journal.md' and not re.search(r'(?m)^## ', body)))
        return rendered

    needed = sum(len(body) for _, body, _ in sections)
    available = max(0, int(budget * .83) - fixed)
    companion_clipped = needed > available
    text = companion(available)
    extra = ''
    index = store.source_snapshot(['index.md'], source_directory='knowledge', budget_chars=4000)
    if index['records']:
        label = '\n[Knowledge map: knowledge/index.md]\n'
        room = budget - len(text)
        allowance = min(600, room // 3)
        if not companion_clipped:
            # The map grows with spare room (#146), but not while companion sources are clipped:
            # there each extra character would come out of what #140/#143 give back to them.
            allowance = min(1500, max(allowance, room // 4))
        if allowance > len(label) + 40:
            extra += label + clip(index['records'][0]['text'], allowance - len(label))
    remaining = budget - len(text) - len(extra)
    # At SessionStart without a query, an unqueried snapshot must never starve clipped companion
    # sources: an ambient note yields its budget so active threads and rules stay whole.
    retrieval_share = max(0, budget - int(budget * .83) - len(extra))
    if query:
        retrieval_budget = min(remaining, retrieval_share) if companion_clipped else remaining
        ranked = store.context_for(harness, query, budget_chars=max(0, retrieval_budget - 100))
    elif not companion_clipped and remaining > 200:
        ranked = store.snapshot_context(budget_chars=min(1500, max(0, remaining - 100)))
    else:
        ranked = {'records': []}
    used = {r['source'] for r in records}
    retrieval_used = 0
    retrieval_cap = retrieval_share if companion_clipped else remaining
    for record in ranked.get('records', []):
        if record['source'] in used:
            continue
        label = f'\n[Related source: {record["source"]}]\n'
        room = min(budget - len(text) - len(extra) - len(label), retrieval_cap - retrieval_used - len(label))
        if room > 40:
            addition = label + clip(record['text'], room)
            extra += addition
            retrieval_used += len(addition)
    # The receipt comes with its whole header, which names the source and calls it a historical
    # claim (#147), plus some body; a narrower room goes back to the companion sources instead.
    receipt_head = receipt.find('):\n') + 3 if receipt else 0
    if receipt and len(text) + len(extra) + receipt_head + 80 < budget:
        extra += clip(receipt, budget - len(text) - len(extra))
    # Retrieval takes its share first; every character it did not use goes back to the
    # clipped companion sources instead of being dropped.
    regained = companion(budget - fixed - len(extra))
    if len(text) < len(regained) <= budget - len(extra):
        text = regained
    return text + extra
