"""Plain-language Turkish daily summary for `recap --human [--days N] [--write]` (#970).

Model-free and read-only by default; `--write` updates only the bounded
`<!-- beyin-recap:start -->` ... `<!-- beyin-recap:end -->` section in
`daily/YYYY-MM-DD.md`, preserving all surrounding user content.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import time
import unicodedata


RECAP_START = '<!-- beyin-recap:start -->'
RECAP_END = '<!-- beyin-recap:end -->'

MAX_READ_BYTES = 4 * 1024 * 1024
MAX_THREADS_BYTES = 2 * 1024 * 1024
MAX_OPEN_TASKS_SHOWN = 15
MAX_OVERDUE_TASKS_SHOWN = 15
MAX_UNREADABLE_SHOWN = 15

# Bidirectional overrides can reorder terminal display; Cc covers ESC/BEL/etc.
_BIDI_CONTROLS = frozenset('\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069')

_TR_ASCII = str.maketrans({
    'ğ': 'g', 'Ğ': 'G', 'ü': 'u', 'Ü': 'U', 'ş': 's', 'Ş': 'S',
    'ı': 'i', 'İ': 'I', 'ö': 'o', 'Ö': 'O', 'ç': 'c', 'Ç': 'C',
    '—': '-', '–': '-', '“': '"', '”': '"', '’': "'",
})

_NEXT_LINE = re.compile(
    r'(?i)^[ \t]*(?:[-*+][ \t]+)?(?:\*\*)?'
    r'(?:next(?:[ \t]+step)?|sonraki(?:[ \t]+ad[ıi]m)?|s[ıi]radaki(?:[ \t]+ad[ıi]m)?)'
    r'(?:[ \t]*\([^\n]*?\))?[ \t]*:(?:\*\*)?[ \t]*(.+?)[ \t]*$'
)

_PLACEHOLDER_NEXT = frozenset({'', '-', '—', 'none', '(none)', 'yok', '(yok)', 'n/a', 'tbd'})

_CLOSED_STATUSES = frozenset({
    'done', 'cancelled', 'canceled', 'kapandi', 'completed',
    'tamamlandi', 'iptal', 'closed', 'bitti', 'cozuldu', 'resolved',
    'archived', 'arsivlendi',
})

_STATUS_TR = {
    'waiting': 'beklemede',
    'blocked': 'engelli',
    'inbox': 'henüz planlanmadı',
}

_DAY_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
_FENCE_RE = re.compile(r'^[ \t]{0,3}(`{3,}|~{3,})')


def plain_text(value) -> str:
    """One safe line from stored text: newlines fold, control and bidi characters become '?'."""
    text = str(value if value is not None else '').replace('\r\n', '\n').strip('\r\n')
    return ''.join(
        ' / ' if ch in '\n\r\u2028\u2029\x85' else
        ' ' if ch == '\t' else
        '?' if unicodedata.category(ch) == 'Cc' or ch in _BIDI_CONTROLS else ch
        for ch in text
    ).strip()


def write_human_output(text: str, stream=None) -> None:
    """Write human text safely even if stdout uses a legacy non-UTF-8 encoding."""
    target = sys.stdout if stream is None else stream
    try:
        print(text, file=target)
    except UnicodeEncodeError:
        encoding = getattr(target, 'encoding', None) or 'ascii'
        fallback = text.translate(_TR_ASCII)
        safe = fallback.encode(encoding, errors='replace').decode(encoding, errors='replace')
        print(safe, file=target)


def _is_link_or_reparse(info) -> bool:
    """True for symlinks, Windows directory junctions, or any other reparse point."""
    return (
        stat.S_ISLNK(info.st_mode)
        or bool(getattr(info, 'st_reparse_tag', 0))
        or bool(getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0))
    )


def _safe_regular_file(path: Path, vault: Path, *, max_bytes: int = MAX_READ_BYTES) -> bool:
    """Verify every component from vault to path has no symlink/reparse and target is a bounded file."""
    try:
        vault_root = vault.resolve()
        rel = path.relative_to(vault_root)
    except (OSError, ValueError):
        return False
    current = vault_root
    for part in rel.parts:
        current = current / part
        try:
            info = os.lstat(current)
        except OSError:
            return False
        if _is_link_or_reparse(info):
            return False
    try:
        final_info = os.lstat(path)
        if _is_link_or_reparse(final_info) or not stat.S_ISREG(final_info.st_mode):
            return False
        if final_info.st_size > max_bytes:
            return False
        return path.resolve().is_relative_to(vault_root)
    except (OSError, ValueError):
        return False


def _ensure_period(sentence: str) -> str:
    text = sentence.rstrip()
    if not text:
        return ''
    if text[-1] in '.!?':
        return text
    return text + '.'


def _group_receipt_sessions(db, receipt_result: dict) -> list[dict]:
    """Group receipt items by session (one entry per session; sessionless receipts stay distinct)."""
    items = receipt_result.get('items') or []
    if not items:
        return []

    meta_by_source = {}
    try:
        for ident, payload in db.execute('SELECT id, payload FROM receipts'):
            try:
                event = json.loads(payload)
            except (TypeError, ValueError):
                continue
            source = 'receipts/' + hashlib.sha256(str(ident).encode('utf-8')).hexdigest() + '.md'
            session = event.get('session')
            meta_by_source[source] = {
                'session': session if isinstance(session, str) and session.strip() else None,
                'harness': event.get('harness'),
            }
    except Exception:
        meta_by_source = {}

    groups = []
    index_by_key = {}
    for item in items:
        source = item.get('source', '')
        meta = meta_by_source.get(source, {})
        session_id = meta.get('session')
        key = ('session', session_id) if session_id else ('item', source or item.get('created_at', ''))
        idx = index_by_key.get(key)
        if idx is None:
            index_by_key[key] = len(groups)
            groups.append({
                'day': item.get('day') or str(item.get('created_at', ''))[:10],
                'items': [item],
            })
        else:
            groups[idx]['items'].append(item)

    sessions = []
    for group in groups:
        # receipt_result['items'] is newest-first; reverse within a session so summaries read chronologically.
        ordered = list(reversed(group['items']))
        summaries = []
        seen_summaries = set()
        visible_refs = []
        seen_refs = set()
        links = []
        seen_links = set()
        private_refs = 0
        missing_refs = 0
        withheld_count = 0

        for item in ordered:
            if item.get('summary_withheld'):
                withheld_count += 1
            else:
                text = plain_text(item.get('summary', ''))
                if text and text not in seen_summaries:
                    seen_summaries.add(text)
                    summaries.append(text)
            for ref in item.get('refs') or []:
                cleaned = plain_text(ref)
                if cleaned and cleaned not in seen_refs:
                    seen_refs.add(cleaned)
                    visible_refs.append(cleaned)
            for link in item.get('links') or []:
                cleaned = plain_text(link)
                if cleaned and cleaned not in seen_links:
                    seen_links.add(cleaned)
                    links.append(cleaned)
            withheld = item.get('refs_withheld') or {}
            private_refs += int(withheld.get('private') or 0)
            missing_refs += int(withheld.get('missing') or 0)

        sessions.append({
            'day': plain_text(group['day']),
            'summaries': summaries,
            'all_withheld': bool(withheld_count and not summaries),
            'withheld_count': withheld_count,
            'refs': visible_refs,
            'links_count': len(links),
            'private_refs': private_refs,
            'missing_refs': missing_refs,
        })
    return sessions


def _collect_thread_nexts(vault: Path, state: Path | None) -> list[dict]:
    """Read active Threads.md Next lines fail-open, obeying privacy, symlink, and fence guards."""
    try:
        from beyin_v3_triage import _scan_allowed, _scan_keys, _thread_topics, _threads_source
        from beyin_v3_sync import parse
    except Exception:
        return []

    try:
        source, reason = _threads_source(vault, state)
        if reason or source is None or not source.exists():
            return []
        if not _safe_regular_file(source, vault, max_bytes=MAX_THREADS_BYTES):
            return []
        if not _scan_allowed(source, vault, _scan_keys(vault, state)):
            return []
        text = source.read_text(encoding='utf-8').lstrip('\ufeff')
        # Re-check full frontmatter so private/untrusted Threads.md never leaks.
        try:
            meta, _ = parse(text)
        except Exception:
            meta = {}
        if isinstance(meta, dict):
            vis = str(meta.get('visibility') or '').strip().lower()
            if vis and vis not in ('public', 'internal'):
                return []
            if str(meta.get('trust') or '').strip().lower() == 'untrusted' or meta.get('trusted') is False:
                return []
    except (OSError, ValueError, UnicodeError):
        return []

    found = []
    seen = set()
    try:
        for raw_title, body in _thread_topics(text):
            topic = re.sub(r'(?i)^(?:thread|konu):\s*', '', str(raw_title or '')).strip() or str(raw_title or '').strip()
            topic = plain_text(topic)
            for line in body.splitlines():
                match = _NEXT_LINE.match(line)
                if not match:
                    continue
                step = match.group(1).strip()
                if step.startswith('**') and step.endswith('**') and len(step) > 4:
                    step = step[2:-2].strip()
                step = plain_text(step)
                if step.casefold() in _PLACEHOLDER_NEXT:
                    continue
                key = (topic.casefold(), step.casefold())
                if key not in seen:
                    seen.add(key)
                    found.append({'topic': topic, 'next': step})
                break
    except Exception:
        return found
    return found


def _parse_iso_day(raw) -> date | None:
    if not isinstance(raw, str):
        return None
    val = raw.strip()
    if len(val) < 10 or not _DAY_RE.match(val[:10]):
        return None
    try:
        return date.fromisoformat(val[:10])
    except ValueError:
        return None


def _collect_tasks(vault: Path, state: Path | None, store, db, local_today: date) -> tuple[list[dict], list[dict]]:
    """Collect visible, trusted, open tasks split into non-overdue and overdue lists."""
    try:
        from beyin_v3 import _status_word, _trusted_record
        from beyin_v3_hygiene import fold, sensitive_excluded
        from beyin_v3_index import dir_key, exclusions
        from beyin_v3_triage import _unfenced_thread_lines
    except Exception:
        return [], []

    try:
        excluded = exclusions(state, vault)[0] if state is not None else frozenset()
    except Exception:
        excluded = frozenset()

    try:
        superseded = store._superseded_ids()
    except Exception:
        superseded = set()

    open_tasks = []
    overdue_tasks = []
    seen_sources = set()

    try:
        rows = db.execute('SELECT payload FROM records ORDER BY id').fetchall()
    except Exception:
        return [], []

    for (payload,) in rows:
        try:
            record = json.loads(payload)
        except (TypeError, ValueError):
            continue
        if not isinstance(record, dict):
            continue
        if record.get('id') in superseded:
            continue
        if not _trusted_record(record):
            continue
        visibility = record.get('visibility')
        if visibility not in (None, 'public', 'internal'):
            continue

        source = str(record.get('source') or '')
        if not source or source in seen_sources:
            continue
        parts = Path(source).parts[:-1]
        if any(sensitive_excluded(part) or dir_key(part) in excluded for part in parts):
            continue

        is_task = record.get('kind') == 'task'
        if not is_task and parts:
            top_folded = fold(parts[0])
            is_task = 'task' in top_folded or 'gorev' in top_folded
        if not is_task:
            continue

        status = _status_word(record) or 'active'
        body_text = str(record.get('text') or '')
        if status not in _CLOSED_STATUSES and body_text:
            for line in _unfenced_thread_lines(body_text.splitlines()):
                m = re.match(r'(?i)^status:\s*([a-zA-Z_-]+)', line.strip())
                if m and m.group(1).lower() in _CLOSED_STATUSES:
                    status = m.group(1).lower()
                    break
        if status in _CLOSED_STATUSES:
            continue

        seen_sources.add(source)
        title = str(record.get('title') or '').strip()
        if not title and body_text:
            for line in _unfenced_thread_lines(body_text.splitlines()):
                sline = line.strip()
                if sline.startswith('#'):
                    title = sline.lstrip('#').strip()
                    break
        if not title:
            title = Path(source).stem
        title = plain_text(title)

        due_date = _parse_iso_day(record.get('due_at')) or _parse_iso_day(record.get('due'))
        if due_date is not None and due_date < local_today:
            overdue_tasks.append({
                'title': title,
                'source': plain_text(source),
                'due_date': due_date.isoformat(),
                'days_overdue': (local_today - due_date).days,
                'status': status,
            })
        else:
            open_tasks.append({
                'title': title,
                'source': plain_text(source),
                'due_date': due_date.isoformat() if due_date is not None else None,
                'due_today': due_date == local_today,
                'status': status,
            })

    overdue_tasks.sort(key=lambda item: (item['due_date'], item['title'].casefold()))
    open_tasks.sort(key=lambda item: (item['due_date'] is None, item['due_date'] or '', item['title'].casefold()))
    return open_tasks, overdue_tasks


def _collect_inbox(vault: Path, state: Path | None, now_ts: float) -> dict:
    """Collect unprocessed inbox note counts fail-open."""
    try:
        from beyin_v3_triage import scan_inbox
        report = scan_inbox(vault, now_ts, state=state)
    except Exception:
        return {'count': 0, 'folders': [], 'scan_error': False}

    folders = []
    scan_error = report.get('status') in ('unavailable', 'partial')
    for row in report.get('folders') or []:
        if row.get('error'):
            scan_error = True
        notes = int(row.get('notes') or 0)
        if notes > 0:
            folders.append({
                'folder': plain_text(row.get('folder', '')),
                'notes': notes,
                'oldest_days': row.get('oldest_days'),
            })
    total = sum(f['notes'] for f in folders)
    return {'count': total, 'folders': folders, 'scan_error': scan_error}


def _classify_warning_kind(warning: dict) -> str | None:
    kind = warning.get('kind')
    if kind == 'missing_receipt_source':
        return None
    if kind == 'receipt_view':
        return 'receipt_view'
    reason = str(warning.get('reason') or '').lower()
    if 'conflict markers' in reason:
        return 'conflict_markers'
    if 'file sync' in reason:
        return 'sync_copy'
    if any(tok in reason for tok in ('utf-8', 'utf8', 'decode', 'surrogate', 'cesu-8')):
        return 'encoding'
    if any(tok in reason for tok in ('frontmatter', 'yaml', 'json', 'revision', 'metadata', 'mapping')):
        return 'frontmatter'
    return 'unreadable'


def _collect_unreadable_notes(sync_result: dict | None) -> list[dict]:
    """Turn sync warnings into deduplicated, privacy-safe unreadable note entries."""
    if not isinstance(sync_result, dict):
        return []
    try:
        from beyin_v3_hygiene import sensitive_excluded
    except Exception:
        sensitive_excluded = lambda _name: False  # noqa: E731

    items = []
    seen = set()
    for warning in sync_result.get('warnings') or []:
        if not isinstance(warning, dict):
            continue
        source = warning.get('source')
        if not isinstance(source, str) or not source.strip():
            continue
        if any(sensitive_excluded(part) for part in Path(source).parts[:-1]):
            continue
        kind = _classify_warning_kind(warning)
        if kind is None:
            continue
        cleaned = plain_text(source)
        if cleaned in seen:
            continue
        seen.add(cleaned)
        items.append({'source': cleaned, 'kind': kind})
    return items


def _collect_over_limit_files(vault: Path, state: Path | None, store) -> list[dict]:
    """Collect over-limit companion files and clipped Kurallar.md files fail-open."""
    items = []
    seen = set()
    try:
        import beyin_v3_companion as companion
        report = companion.hygiene(vault, state, include_projects=True)
        for name, entry in (report.get('files') or {}).items():
            if isinstance(entry, dict) and entry.get('over_limit'):
                label = plain_text(name)
                if label not in seen:
                    seen.add(label)
                    items.append({
                        'path': label,
                        'chars': int(entry.get('chars') or 0),
                        'limit': int(entry.get('limit') or 0),
                        'kind': 'companion',
                    })
        for proj in report.get('projects') or []:
            proj_dir = proj.get('directory') or ''
            for name, entry in (proj.get('files') or {}).items():
                if isinstance(entry, dict) and entry.get('over_limit'):
                    label = plain_text(f'{proj_dir}/{name}' if proj_dir else name)
                    if label not in seen:
                        seen.add(label)
                        items.append({
                            'path': label,
                            'chars': int(entry.get('chars') or 0),
                            'limit': int(entry.get('limit') or 0),
                            'kind': 'companion',
                        })
    except Exception:
        pass

    # Check clipped Kurallar.md / project Kurallar.md (same measurement as doctor).
    try:
        if state is not None and store is not None:
            import beyin_v3_companion as companion
            import beyin_v3_preferences as preferences
            prefs = preferences.load(state)
            if prefs.get('context_mode') != 'off':
                budget = companion.client_budget(
                    'claude', companion.opening_budget(state, prefs.get('context_chars', 8000))
                )
                rules = companion.rules_opening(store, budget)
                if rules and rules.get('clipped') and not rules.get('stale'):
                    label = plain_text(rules.get('source') or 'Kurallar.md')
                    if label not in seen:
                        seen.add(label)
                        items.append({
                            'path': label,
                            'chars': int(rules.get('chars') or 0),
                            'limit': int(rules.get('share') or budget),
                            'kind': 'rules',
                        })
                for proj in companion.project_rules_opening(store, budget) or []:
                    if proj.get('clipped') and not proj.get('stale'):
                        label = plain_text(proj.get('source') or proj.get('project') or 'Kurallar.md')
                        if label not in seen:
                            seen.add(label)
                            items.append({
                                'path': label,
                                'chars': int(proj.get('chars') or 0),
                                'limit': int(proj.get('share') or budget),
                                'kind': 'rules',
                            })
    except Exception:
        pass

    return items


def collect_daily_summary(
    vault: Path,
    state: Path | None,
    store,
    db,
    receipt_result: dict,
    sync_result: dict | None,
    *,
    days: int = 1,
    today: date | None = None,
    now: float | None = None,
) -> dict:
    """Gather all data for the plain-language daily summary without model calls."""
    vault = Path(vault).resolve()
    local_today = today or datetime.now(timezone.utc).astimezone().date()
    now_ts = time.time() if now is None else now

    sessions = _group_receipt_sessions(db, receipt_result)
    thread_nexts = _collect_thread_nexts(vault, state)
    open_tasks, overdue_tasks = _collect_tasks(vault, state, store, db, local_today)
    inbox = _collect_inbox(vault, state, now_ts)
    unreadable_notes = _collect_unreadable_notes(sync_result)
    over_limit_files = _collect_over_limit_files(vault, state, store)

    return {
        'days': int(days),
        'date': local_today.isoformat(),
        'sessions': sessions,
        'total_receipts': int(receipt_result.get('total', len(receipt_result.get('items') or []))),
        'shown_receipts': int(receipt_result.get('shown', len(receipt_result.get('items') or []))),
        'truncated_receipts': bool(receipt_result.get('truncated', False)),
        'undated_omitted': int(receipt_result.get('undated_omitted', 0)),
        'missing_source_omitted': int(receipt_result.get('missing_source_omitted', 0)),
        'thread_nexts': thread_nexts,
        'open_tasks': open_tasks,
        'overdue_tasks': overdue_tasks,
        'inbox': inbox,
        'unreadable_notes': unreadable_notes,
        'over_limit_files': over_limit_files,
    }


def _format_session_line(session: dict, *, multi_day: bool) -> str:
    if session.get('all_withheld'):
        summary_text = 'Özel bir kaynakla ilişkili oturum özeti gizlilik nedeniyle gösterilmedi'
    else:
        summary_text = '; '.join(session.get('summaries') or [])
        if session.get('withheld_count'):
            summary_text = f'{summary_text}; ayrıca özel bir kayıt gizlendi' if summary_text else 'Özel bir kayıt gizlendi'

    evidence_parts = []
    refs = session.get('refs') or []
    if refs:
        shown = ', '.join(refs[:3])
        if len(refs) > 3:
            shown += f' ve {len(refs) - 3} kaynak daha'
        evidence_parts.append(f'dayanak: {shown}')
    missing = int(session.get('missing_refs') or 0)
    if missing > 0:
        evidence_parts.append(f'{missing} kaynak dosyası bulunamıyor')
    private = int(session.get('private_refs') or 0)
    if private > 0:
        evidence_parts.append(f'{private} özel kaynak gizlendi')
    links_count = int(session.get('links_count') or 0)
    if links_count > 0:
        evidence_parts.append(f'{links_count} dış bağlantı')
    if not evidence_parts:
        evidence_parts.append('ek kaynak bağlantısı belirtilmedi')

    evidence_text = '; '.join(evidence_parts)
    prefix = f"{session['day']} tarihinde: " if multi_day and session.get('day') else ''
    body = f'{prefix}{summary_text}'
    if body and body[-1] in '.!?':
        return f'- {body} ({evidence_text[0].upper()}{evidence_text[1:]}.)'
    return f'- {body} ({evidence_text}).'


def _format_unreadable_line(item: dict) -> str:
    source = plain_text(item.get('source', ''))
    kind = item.get('kind', 'unreadable')
    if kind == 'conflict_markers':
        return f'- "{source}" dosyasında çözülmemiş birleştirme çakışması var; dosyayı açıp çakışma işaretlerini temizleyin.'
    if kind == 'encoding':
        return f'- "{source}" dosyası bozuk metin kodlaması nedeniyle okunamadı; dosyayı UTF-8 olarak yeniden kaydedin.'
    if kind == 'frontmatter':
        return f'- "{source}" notunun üst bilgi bölümü okunamadı; dosyayı açıp başındaki "---" alanını düzeltin.'
    if kind == 'sync_copy':
        return f'- "{source}" dosyası eşitleme kopyası gibi görünüyor; asıl notla karşılaştırıp fazlalık olanı silin.'
    if kind == 'receipt_view':
        return f'- "{source}" özet görünümünde elle yapılmış değişiklikler korundu; dosyayı kontrol edip gerekirse yenileyin.'
    return f'- "{source}" notu okunamadı; dosyayı açıp biçimini ve içeriğini kontrol edin.'


def _format_over_limit_line(item: dict) -> str:
    path = plain_text(item.get('path', ''))
    chars = int(item.get('chars') or 0)
    limit = int(item.get('limit') or 0)
    if item.get('kind') == 'rules':
        return (
            f'- "{path}" kural dosyası açılış sınırını aşıyor ({chars} karakter, sınır {limit} karakter); '
            'uzun açıklamaları ayrı bir nota taşıyıp kuralları kısaltın.'
        )
    return (
        f'- "{path}" dosyası önerilen uzunluğu aşıyor ({chars} karakter, sınır {limit} karakter); '
        'eski kayıtları silmeden arşive taşımak için ajanınızdan dosyayı sıkıştırmasını isteyin.'
    )


def format_human(result: dict, *, heading_level: int = 2, include_write_note: bool = True) -> str:
    """Render the plain-language Turkish daily summary with three clear headings."""
    summary = result.get('daily_summary') if isinstance(result.get('daily_summary'), dict) else result
    days = int(summary.get('days') or 1)
    h = '#' * heading_level

    # 1. Bugün yapılanlar / Son N günde yapılanlar
    done_heading = f'{h} Bugün yapılanlar' if days == 1 else f'{h} Son {days} günde yapılanlar'
    done_lines = [done_heading]
    sessions = summary.get('sessions') or []
    if sessions:
        for session in sessions:
            done_lines.append(_format_session_line(session, multi_day=days > 1))
    else:
        if days == 1:
            done_lines.append('Bugün kaydedilmiş bir oturum özeti bulunmuyor.')
        else:
            done_lines.append(f'Son {days} günde kaydedilmiş bir oturum özeti bulunmuyor.')

    if summary.get('truncated_receipts'):
        shown = int(summary.get('shown_receipts') or len(sessions))
        total = int(summary.get('total_receipts') or shown)
        remaining = max(0, total - shown)
        if remaining > 0:
            done_lines.append(f'En güncel {shown} kayıt gösterildi; toplam {total} kayıttan {remaining} kayıt daha bulunuyor.')
    undated = int(summary.get('undated_omitted') or 0)
    if undated > 0:
        done_lines.append(f'Tarihi okunamayan {undated} eski kayıt bu özete dahil edilmedi.')
    missing_sources = int(summary.get('missing_source_omitted') or 0)
    if missing_sources > 0:
        done_lines.append(f'Dosyası artık bulunmayan {missing_sources} eski kayıt bu listede yer almadı.')

    # 2. Bekleyenler
    pending_lines = [f'{h} Bekleyenler']
    pending_count = 0

    for item in summary.get('thread_nexts') or []:
        topic = plain_text(item.get('topic', ''))
        step = plain_text(item.get('next', ''))
        if topic and step:
            pending_lines.append(_ensure_period(f'- Açık konu "{topic}" için sıradaki adım: {step}'))
            pending_count += 1

    open_tasks = summary.get('open_tasks') or []
    for task in open_tasks[:MAX_OPEN_TASKS_SHOWN]:
        title = plain_text(task.get('title', ''))
        st_label = _STATUS_TR.get(task.get('status', ''), '')
        st_part = f' ({st_label})' if st_label else ''
        if task.get('due_today'):
            due_part = ' (son tarih: bugün)'
        elif task.get('due_date'):
            due_part = f" (son tarih: {plain_text(task['due_date'])})"
        else:
            due_part = ''
        pending_lines.append(f'- Açık görev: "{title}"{st_part}{due_part}.')
        pending_count += 1
    if len(open_tasks) > MAX_OPEN_TASKS_SHOWN:
        extra = len(open_tasks) - MAX_OPEN_TASKS_SHOWN
        pending_lines.append(f'- Ayrıca {extra} açık görev daha bulunuyor.')

    inbox = summary.get('inbox') or {}
    inbox_count = int(inbox.get('count') or 0)
    inbox_folders = inbox.get('folders') or []
    if inbox_count > 0:
        if len(inbox_folders) == 1:
            row = inbox_folders[0]
            folder_name = plain_text(row.get('folder', 'gelen kutusu'))
            oldest = row.get('oldest_days')
            age_part = f' (en eskisi {int(oldest)} gün önce eklendi)' if isinstance(oldest, int) and oldest > 0 else ''
            pending_lines.append(f'- Gelen kutusunda ({folder_name}) işlenmeyi bekleyen {inbox_count} not var{age_part}.')
        elif len(inbox_folders) > 1:
            parts = ', '.join(f"{plain_text(r.get('folder', ''))}: {int(r.get('notes', 0))} not" for r in inbox_folders[:5])
            pending_lines.append(f'- Gelen kutusunda toplam {inbox_count} işlenmemiş not bekliyor ({parts}).')
        else:
            pending_lines.append(f'- Gelen kutusunda işlenmeyi bekleyen {inbox_count} not var.')
        pending_count += 1

    overdue_tasks = summary.get('overdue_tasks') or []
    if pending_count == 0:
        if overdue_tasks:
            pending_lines.append('Bekleyen başka bir konu, görev ya da gelen kutusu notu görünmüyor; geciken görevler aşağıda listelendi.')
        else:
            pending_lines.append('Şu an bekleyen bir açık konu, görev ya da gelen kutusu notu görünmüyor.')

    # 3. Sizden beklenen
    attention_lines = [f'{h} Sizden beklenen']
    attention_count = 0

    unreadable = summary.get('unreadable_notes') or []
    for item in unreadable[:MAX_UNREADABLE_SHOWN]:
        attention_lines.append(_format_unreadable_line(item))
        attention_count += 1
    if len(unreadable) > MAX_UNREADABLE_SHOWN:
        extra = len(unreadable) - MAX_UNREADABLE_SHOWN
        attention_lines.append(f'- Okunamayan {extra} not daha var; dosyaların biçimini kontrol edin.')

    for item in summary.get('over_limit_files') or []:
        attention_lines.append(_format_over_limit_line(item))
        attention_count += 1

    for task in overdue_tasks[:MAX_OVERDUE_TASKS_SHOWN]:
        title = plain_text(task.get('title', ''))
        due_date = plain_text(task.get('due_date', ''))
        days_overdue = int(task.get('days_overdue') or 0)
        attention_lines.append(
            f'- "{title}" görevinin son tarihi {due_date} idi ({days_overdue} gün geçti); '
            'görevi tamamlayın ya da yeni bir son tarih belirleyin.'
        )
        attention_count += 1
    if len(overdue_tasks) > MAX_OVERDUE_TASKS_SHOWN:
        extra = len(overdue_tasks) - MAX_OVERDUE_TASKS_SHOWN
        attention_lines.append(f'- Son tarihi geçmiş {extra} görev daha var; tarihlerini gözden geçirin.')

    if inbox.get('scan_error'):
        attention_lines.append('- Gelen kutusu klasörlerinden biri okunamadı; klasör izinlerini kontrol edip özeti yeniden çalıştırın.')
        attention_count += 1

    if attention_count == 0:
        attention_lines.append('Şu an müdahale etmeniz gereken bir sorun görünmüyor.')

    sections = ['\n'.join(done_lines), '\n'.join(pending_lines), '\n'.join(attention_lines)]
    written = result.get('written_daily') if include_write_note and isinstance(result, dict) else None
    if isinstance(written, dict) and written.get('path'):
        wpath = plain_text(written['path'])
        if written.get('action') == 'updated':
            sections.append(f'Özet {wpath} dosyasındaki "Günün özeti" bölümünde güncellendi.')
        else:
            sections.append(f'Özet {wpath} dosyasına yazıldı.')
    return '\n\n'.join(sections)


def _find_marker_span(lines: list[str]) -> tuple[int, int] | None:
    """Locate a single valid pair of standalone marker lines outside fenced code blocks."""
    starts = []
    ends = []
    fence = None
    for idx, line in enumerate(lines):
        stripped = line.rstrip('\r\n')
        if fence:
            if re.fullmatch(r'[ \t]{0,3}' + re.escape(fence[0]) + '{' + str(len(fence)) + r',}[ \t]*', stripped):
                fence = None
            continue
        opened = _FENCE_RE.match(stripped)
        if opened and not (opened.group(1)[0] == '`' and '`' in stripped[opened.end():]):
            fence = opened.group(1)
            continue
        trimmed = stripped.strip()
        if trimmed == RECAP_START:
            starts.append(idx)
        elif trimmed == RECAP_END:
            ends.append(idx)

    if not starts and not ends:
        return None
    if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
        raise ValueError('Malformed beyin-recap markers in daily note; refusing to modify user content')
    return starts[0], ends[0]


def _atomic_replace(source: Path, destination: Path) -> None:
    """os.replace with brief Windows sharing-violation retry."""
    for attempt in range(5):
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if sys.platform != 'win32' or attempt == 4:
                raise
            time.sleep(0.05 * (attempt + 1))


def write_daily_summary(vault: Path, body_text: str, *, today: date | None = None) -> dict:
    """Atomically write or update the bounded 'Günün özeti' section in daily/YYYY-MM-DD.md.

    Fails closed on symlinks, Windows junctions/reparse points, managed receipt-index
    views, malformed markers, or non-regular files. Never writes inside daily/v3/.
    """
    vault_root = Path(vault).resolve()
    if not vault_root.is_dir():
        raise ValueError('vault must be an existing directory')

    local_today = today or datetime.now(timezone.utc).astimezone().date()
    day_str = local_today.isoformat()
    if not _DAY_RE.fullmatch(day_str):
        raise ValueError(f'invalid daily summary date: {day_str!r}')

    daily_dir = vault_root / 'daily'
    try:
        dir_info = os.lstat(daily_dir)
    except FileNotFoundError:
        dir_info = None
    if dir_info is not None:
        if _is_link_or_reparse(dir_info):
            raise ValueError('Refusing to write through symlink or junction: daily')
        if not stat.S_ISDIR(dir_info.st_mode):
            raise ValueError('daily path exists and is not a directory')
    else:
        daily_dir.mkdir(exist_ok=True)
        dir_info = os.lstat(daily_dir)
        if _is_link_or_reparse(dir_info) or not stat.S_ISDIR(dir_info.st_mode):
            raise ValueError('Refusing to write through symlink or junction: daily')

    if not daily_dir.resolve().is_relative_to(vault_root):
        raise ValueError('daily directory resolves outside vault')

    target = daily_dir / f'{day_str}.md'
    relative = f'daily/{day_str}.md'
    if relative.startswith('daily/v3/'):
        raise ValueError('Refusing to write inside managed daily/v3 directory')

    try:
        target_info = os.lstat(target)
    except FileNotFoundError:
        target_info = None

    existed = target_info is not None
    if target_info is not None:
        if _is_link_or_reparse(target_info):
            raise ValueError(f'Refusing to write through symlink or junction: {relative}')
        if not stat.S_ISREG(target_info.st_mode):
            raise ValueError(f'Target is not a regular file: {relative}')
        if target_info.st_size > MAX_READ_BYTES:
            raise ValueError(f'Daily note exceeds size limit: {relative}')
        if not target.resolve().is_relative_to(vault_root):
            raise ValueError(f'Target resolves outside vault: {relative}')

    block_lines = [RECAP_START, '## Günün özeti', '', *body_text.strip().splitlines(), RECAP_END]

    if existed:
        raw_bytes = target.read_bytes()
        try:
            existing = raw_bytes.decode('utf-8')
        except UnicodeDecodeError as exc:
            raise ValueError(f'Existing daily note is not valid UTF-8: {relative}') from exc

        bom = '\ufeff' if existing.startswith('\ufeff') else ''
        content = existing[1:] if bom else existing

        # Never overwrite a managed flat receipt-index view.
        try:
            from beyin_v3_sync import parse
            meta, _ = parse(content)
        except Exception:
            meta = None
        if isinstance(meta, dict) and meta.get('generated') is True and meta.get('kind') == 'receipt-index':
            raise ValueError(f'{relative} is a managed receipt-index view; refusing to overwrite')

        nl = '\r\n' if '\r\n' in content else '\n'
        managed_block = nl.join(block_lines)
        raw_lines = content.splitlines(keepends=True)
        span = _find_marker_span(raw_lines)
        if span is not None:
            start_idx, end_idx = span
            end_has_nl = raw_lines[end_idx].endswith(('\r\n', '\n'))
            replacement = managed_block + (nl if end_has_nl else '')
            updated_content = ''.join(raw_lines[:start_idx]) + replacement + ''.join(raw_lines[end_idx + 1:])
        else:
            if not content:
                updated_content = managed_block + nl
            elif content.endswith(nl + nl):
                updated_content = content + managed_block + nl
            elif content.endswith(nl):
                updated_content = content + nl + managed_block + nl
            else:
                updated_content = content + nl + nl + managed_block + nl
        final_text = bom + updated_content
    else:
        managed_block = '\n'.join(block_lines)
        final_text = f'# {day_str}\n\n{managed_block}\n'

    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode='wb', dir=daily_dir, prefix='.beyin-recap-', suffix='.tmp', delete=False
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(final_text.encode('utf-8'))
            handle.flush()
            os.fsync(handle.fileno())
        # Defense in depth: re-verify target before replace.
        try:
            post_info = os.lstat(target)
        except FileNotFoundError:
            post_info = None
        if post_info is not None and (_is_link_or_reparse(post_info) or not stat.S_ISREG(post_info.st_mode)):
            raise ValueError(f'Refusing to write through symlink or junction: {relative}')
        _atomic_replace(temp_path, target)
        temp_path = None
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except OSError:
                pass

    return {'path': relative, 'action': 'updated' if existed else 'created'}
