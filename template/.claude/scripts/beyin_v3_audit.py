"""Advisory memory auditor: evaluates notes for near-duplicates, staleness, and status contradictions (#372).

Local-first, deterministic, and strictly read-only:
- Compares candidate notes across knowledge, projects, and notes directories.
- Measures lexical overlap (containment and Jaccard) using V3 stemmer and stopwords.
- Identifies status/outcome contradictions across time (antonym pairs).
- Generates actionable, non-destructive advice (superseding, consolidation, linking).
- Respects privacy (visibility: private) and untrusted boundaries.
- Never deletes, merges, or modifies any user notes.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import time
import unicodedata

_MODULE_DIR = str(Path(__file__).resolve().parent)
if _MODULE_DIR not in sys.path:
    sys.path.insert(0, _MODULE_DIR)

# No local fallback: a weaker copy of the kasa-word list would silently widen what the
# report may print. The runtime modules always ship next to this file.
from beyin_v3 import (
    _tokens,
    _token_counts,
    _stem,
    _status_word,
    _trusted_record,
    RETIRED_STATUSES,
    resolve_supersedes,
    STOPWORDS,
    _strict_terms,
    sensitive_source,
    sensitive_excluded,
    sensitive_fold as fold,
)
from beyin_v3_sync import _hash, parse as _parse_note


# Status antonym pairs for contradiction detection (Turkish & English)
STATUS_ANTIPAIRS = [
    # Dogrulanmis / Dogrulanmamis (Verification state)
    (
        {"dogrulandi", "dogrulanmis", "verified", "passed", "kanitlandi", "onaylandi", "gecerli"},
        {"dogrulanmadi", "dogrulanmamis", "unverified", "failed", "kanitlanmadi", "reddedildi", "gecersiz"},
    ),
    # Calisiyor / Calismiyor (Operational state). "acik", "kapali", "canli" and "devam" name no
    # state in most Turkish prose ("acik kaynak", "canli yayin", "devam etti"); in a measured
    # 855-note vault they produced 10 of 13 contradictions between live notes.
    (
        {"calisiyor", "working", "aktif", "active"},
        {"calismiyor", "broken", "pasif", "inactive", "calismadi"},
    ),
    # Cozuldu / Cozulmedi (Completion state)
    (
        {"cozuldu", "tamamlandi", "bitti", "resolved", "completed", "done"},
        {"cozulmedi", "bekliyor", "unresolved", "pending"},
    ),
]


def _stems(words):
    """Notes are compared as stems ("doğrulandı" -> "dogruland"), so the table is stemmed the
    same way; each stem keeps its table spelling for the report."""
    table = {}
    for word in sorted(words):
        stems = _tokens(word)
        if len(stems) == 1:
            table.setdefault(next(iter(stems)), word)
    return table


_STATUS_STEMS = [(_stems(positive), _stems(negative)) for positive, negative in STATUS_ANTIPAIRS]

SYSTEM_EXCLUDED_DIRS = {
    '.obsidian', '.git', '.claude', '.agents', '.kiro', 'node_modules', '.venv', 'venv', '__pycache__',
    'templates', 'sablon', 'sablonlar', 'archive', 'archives', 'arsiv', 'arsivler', 'raw', 'receipts', 'daily'
}

SYSTEM_EXCLUDED_FILES = {
    'claude.md', 'agents.md', 'readme.md', 'core.md', 'soul.md', 'kurallar.md', 'last-session.md',
    'threads.md', 'journal.md', 'beyin.py'
}


def parse_frontmatter_and_body(text: str) -> tuple[dict, str]:
    """Frontmatter through the sync engine's parser, so a BOM, YAML booleans and Obsidian
    lists read exactly as they do for context. Raises ValueError for frontmatter the
    engine would not index either."""
    meta, body = _parse_note(text)
    return meta, body.strip()


def _auditable(path: Path, rel_path: str) -> tuple[dict, str] | None:
    """(metadata, body) when a note may appear in the report, else None.

    The context boundary, not a looser copy of it: no symlink (the folder check would name
    one file and the read would open another), no kasa-class folder, frontmatter the engine
    can parse, visibility public or internal (missing means internal) and a trusted record.
    """
    if path.is_symlink() or sensitive_source(rel_path):
        return None
    try:
        meta, body = parse_frontmatter_and_body(path.read_text(encoding='utf-8', errors='replace'))
    except (OSError, ValueError):
        return None
    if meta.get('visibility', 'internal') not in ('public', 'internal') or not _trusted_record(meta):
        return None
    if meta.get('kind') == 'receipt' or meta.get('generated') is True or _status_word(meta) in RETIRED_STATUSES:
        return None
    return meta, body


def _candidate(path: Path, rel_path: str, meta: dict, body: str) -> dict:
    title = str(meta.get('title') or '').strip()
    if not title:
        for line in body.splitlines():
            if line.startswith('# '):
                title = line[2:].strip()
                break
    if not title:
        title = path.stem
    created_val = meta.get('created') or meta.get('created_at') or meta.get('date')
    created_ts = parse_date_to_timestamp(created_val)
    modified_val = meta.get('modified') or meta.get('updated_at') or meta.get('modified_at')
    updated_ts = parse_date_to_timestamp(modified_val)
    if updated_ts is None:
        # Sync and checkout can rewrite mtime; only a note's own dates establish chronology.
        updated_ts = created_ts if created_ts is not None else 0.0
    if created_ts is None:
        created_ts = updated_ts
    body_tokens = _strict_terms(_tokens(body) - STOPWORDS)
    title_tokens = _strict_terms(_tokens(title) - STOPWORDS)
    supersedes = meta.get('supersedes', [])
    if isinstance(supersedes, str):
        supersedes = [supersedes]
    elif not isinstance(supersedes, list):
        supersedes = []
    note_id = meta.get('id')
    return {
        # The id sync gives the record, so `supersedes: [<id>]` from note-create resolves here too.
        'id': note_id if isinstance(note_id, str) and note_id else 'md-' + _hash(rel_path)[:24],
        'path': rel_path,
        'full_path': path,
        'title': title,
        'tokens': body_tokens | title_tokens,
        'status': str(meta.get('status', '')).lower().strip(),
        'created_ts': created_ts,
        'updated_ts': updated_ts,
        'words_count': len(re.findall(r'\w+', body)),
        'supersedes': [str(s).strip() for s in supersedes if s],
    }


def parse_date_to_timestamp(val: str | None) -> float | None:
    if not val or not isinstance(val, str):
        return None
    val_str = val.strip()
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(val_str[:19].replace('T', ' '), fmt[:len(val_str[:19].replace('T', ' '))])
            return dt.replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            pass
    return None


def _index_excluded_predicate(state: Path | None, vault: Path):
    if state is None:
        return lambda name: False
    try:
        from beyin_v3_index import dir_key, exclusions
        keys = exclusions(state, vault)[0]
    except Exception:
        keys = frozenset()
    if not keys:
        return lambda name: False
    return lambda name: dir_key(name) in keys


def scan_audit_candidates(vault: Path, state: Path | None = None) -> list[dict]:
    """Scan auditable markdown notes from the vault while enforcing safety and boundary rules."""
    vault = Path(vault).resolve()
    candidates = []
    is_index_excluded = _index_excluded_predicate(state, vault)

    for root_str, dirs, files in os.walk(vault):
        root_path = Path(root_str)
        # Prune system/excluded directories
        pruned_dirs = []
        for d in dirs:
            dfold = fold(d)
            if d.startswith('.') or dfold in SYSTEM_EXCLUDED_DIRS or sensitive_excluded(d) or is_index_excluded(d):
                pruned_dirs.append(d)
        for d in pruned_dirs:
            dirs.remove(d)

        for f in files:
            if not f.endswith('.md'):
                continue
            if fold(f) in SYSTEM_EXCLUDED_FILES:
                continue

            full_path = root_path / f
            try:
                rel_path = full_path.relative_to(vault).as_posix()
            except ValueError:
                rel_path = full_path.as_posix()

            checked = _auditable(full_path, rel_path)
            if checked is None:
                continue
            candidates.append(_candidate(full_path, rel_path, *checked))

    return candidates


def _detect_status_conflict(older: dict, newer: dict, allow_body: bool = True) -> dict | None:
    """Detect status or outcome contradiction between an older note and a newer note."""
    older_status_tokens = _tokens(older.get('status', ''))
    newer_status_tokens = _tokens(newer.get('status', ''))

    # Check explicit frontmatter status first (authoritative declaration)
    for positive, negative in _STATUS_STEMS:
        old_pos, old_neg = older_status_tokens & positive.keys(), older_status_tokens & negative.keys()
        new_pos, new_neg = newer_status_tokens & positive.keys(), newer_status_tokens & negative.keys()
        if old_pos and new_neg and not old_neg and not new_pos:
            return {
                'older_term': positive[min(old_pos)],
                'newer_term': negative[min(new_neg)],
                'direction': 'positive_to_negative',
            }
        if old_neg and new_pos and not old_pos and not new_neg:
            return {
                'older_term': negative[min(old_neg)],
                'newer_term': positive[min(new_pos)],
                'direction': 'negative_to_positive',
            }

    if not allow_body:
        return None

    older_tokens = older['tokens'] | older_status_tokens
    newer_tokens = newer['tokens'] | newer_status_tokens

    for positive, negative in _STATUS_STEMS:
        old_pos, old_neg = older_tokens & positive.keys(), older_tokens & negative.keys()
        new_pos, new_neg = newer_tokens & positive.keys(), newer_tokens & negative.keys()
        # Only a flip counts: a note that names both sides ("12 passed, 0 failed") states no
        # single status, so it neither starts nor ends a contradiction.
        if old_pos and new_neg and not old_neg and not new_pos:
            return {
                'older_term': positive[min(old_pos)],
                'newer_term': negative[min(new_neg)],
                'direction': 'positive_to_negative',
            }
        if old_neg and new_pos and not old_pos and not new_neg:
            return {
                'older_term': negative[min(old_neg)],
                'newer_term': positive[min(new_pos)],
                'direction': 'negative_to_positive',
            }

    return None


def _evaluate_pair(a: dict, b: dict, threshold: float, min_shared: int) -> dict | None:
    """Evaluate two candidate notes for lexical overlap, status contradiction, and context bloat."""
    tokens_a = a['tokens']
    tokens_b = b['tokens']
    if not tokens_a or not tokens_b:
        return None

    shared = tokens_a & tokens_b
    shared_count = len(shared)
    if shared_count < min_shared:
        return None

    min_vocab = min(len(tokens_a), len(tokens_b))
    max_vocab = max(len(tokens_a), len(tokens_b))
    if min_vocab == 0:
        return None

    overlap = shared_count / min_vocab
    if overlap < threshold:
        return None

    union_count = len(tokens_a | tokens_b)
    jaccard = (shared_count / union_count) if union_count else 0.0
    vocab_ratio = max_vocab / min_vocab

    # An asymmetric pair (e.g. giant note containing words of a tiny note)
    # is neither a true near duplicate nor a valid context for body-level status conflict (#466).
    symmetric_pair = vocab_ratio <= 3.0 and jaccard >= 0.20

    # Determine temporal order
    time_a = a.get('updated_ts') or a.get('created_ts') or 0.0
    time_b = b.get('updated_ts') or b.get('created_ts') or 0.0
    if time_a < time_b:
        older, newer = a, b
    elif time_b < time_a:
        older, newer = b, a
    else:
        # Tie-break deterministically by path
        if a['path'] <= b['path']:
            older, newer = a, b
        else:
            older, newer = b, a

    # If newer note explicitly supersedes older note, it is already resolved
    older_stem = Path(older['path']).stem
    if any(ref in (older['path'], older_stem) for ref in newer.get('supersedes', [])):
        return None

    # A path tie-break keeps output stable but cannot establish a change over time.
    chronology_known = time_a > 0 and time_b > 0 and time_a != time_b
    conflict = _detect_status_conflict(older, newer, allow_body=symmetric_pair) if chronology_known else None
    novel_newer = sorted(newer['tokens'] - older['tokens'])
    novel_sample = novel_newer[:6]

    # Context bloat heuristic
    context_bloat = older['words_count'] >= 500 and newer['words_count'] >= 500 and overlap >= 0.70

    if conflict:
        finding_type = 'status_contradiction'
        severity = 'warning'
        recom = (
            f"Eski kayittaki durum ('{conflict['older_term']}') yeni kayitta ('{conflict['newer_term']}') "
            f"olarak degismis gorunuyor. Eski kaydi tarihsel (supersedes veya status: archived) olarak "
            f"isaretlemeyi ya da guncel durumu tek kaynakta toplamayi degerlendir."
        )
    elif overlap >= 0.80 and jaccard >= 0.30 and vocab_ratio <= 3.0:
        finding_type = 'near_duplicate'
        severity = 'warning' if overlap >= 0.85 else 'info'
        pct = int(round(overlap * 100))
        recom = (
            f"Iki kayit buyuk olcude ortusuyor (%{pct}). Ayri not acmak yerine '{older['path']}' "
            f"kaydini guncellemek veya yeni bulgulari mevcut nota eklemek baglam yukunu azaltabilir."
        )
    elif jaccard >= 0.20 and vocab_ratio <= 4.0:
        finding_type = 'substantial_overlap'
        severity = 'info'
        pct = int(round(overlap * 100))
        novel_str = ', '.join(f"'{w}'" for w in novel_sample[:4]) if novel_sample else 'ek kavramlar'
        recom = (
            f"Notlar ortak bir konuyu paylasiyor (%{pct} ortusme). Yeni not {novel_str} gibi ek kavramlar "
            f"iceriyor; bagimsiz bir dogrulama veya ayri bir deneme amaci tasimiyorsa iki notu birbirine "
            f"baglamayi ([[link]]) veya tek kaynakta birlestirmeyi degerlendir."
        )
    else:
        return None

    finding = {
        'type': finding_type,
        'severity': severity,
        'older_note': older['path'],
        'newer_note': newer['path'],
        'overlap': round(overlap, 3),
        'jaccard': round(jaccard, 3),
        'shared_terms_count': shared_count,
        'shared_terms_sample': sorted(shared)[:8],
        'novel_terms_newer_sample': novel_sample,
        'words_older': older['words_count'],
        'words_newer': newer['words_count'],
        'recommendation': recom,
    }
    if conflict:
        finding['conflict'] = conflict
    if context_bloat:
        finding['context_bloat'] = True

    return finding


def audit_memory(
    vault: Path | str,
    state: Path | str | None = None,
    target_file: str | Path | None = None,
    threshold: float = 0.60,
    min_shared: int = 5,
    limit: int = 20,
) -> dict:
    """Audit vault memory for near-duplicates, staleness, and contradictions."""
    vault = Path(vault).resolve()
    state = Path(state).resolve() if state else None

    if not 0.0 <= threshold <= 1.0:
        raise ValueError("threshold must be between 0.0 and 1.0")
    if min_shared < 1:
        raise ValueError("min_shared must be at least 1")
    if limit < 0:
        raise ValueError("limit must be nonnegative")

    candidates = scan_audit_candidates(vault, state=state)
    target_rel = None
    target_candidate = None

    if target_file:
        given = Path(target_file)
        if not given.is_absolute():
            given = vault / given
        target_path = given.resolve()

        if not target_path.is_file():
            raise FileNotFoundError(f"Target file not found: {target_file}")

        try:
            target_rel = target_path.relative_to(vault).as_posix()
        except ValueError:
            raise ValueError("audit target must be a note inside the vault") from None

        # Find or construct target candidate
        for c in candidates:
            if c['path'] == target_rel:
                target_candidate = c
                break

        if target_candidate is None:
            # A folder the scan skips by name (daily/, archive/) may still be audited on request,
            # but never a note the context itself would not show.
            checked = _auditable(given, target_rel)
            if checked is None:
                raise ValueError("audit target is private, untrusted, retired, generated, a symlink or unreadable")
            target_candidate = _candidate(target_path, target_rel, *checked)

    # A note another trusted note supersedes is history for every read route. The core resolver
    # matches record ids, vault paths with or without .md, [[links]] and unique file names.
    pool = candidates + ([target_candidate] if target_candidate is not None and target_candidate not in candidates else [])
    retired = resolve_supersedes([{'id': c['id'], 'source': c['path'], 'supersedes': c['supersedes']} for c in pool])[0]
    candidates = [c for c in candidates if c['id'] not in retired]
    if target_candidate is not None and target_candidate['id'] in retired:
        raise ValueError("audit target is retired by supersedes")

    findings = []
    pairs_compared = 0

    if target_candidate:
        for c in candidates:
            if c['path'] == target_candidate['path']:
                continue
            pairs_compared += 1
            f = _evaluate_pair(target_candidate, c, threshold=threshold, min_shared=min_shared)
            if f:
                findings.append(f)
    else:
        # Inverted index for fast candidate selection
        token_to_cand = {}
        for idx, c in enumerate(candidates):
            for t in c['tokens']:
                token_to_cand.setdefault(t, []).append(idx)

        evaluated_pairs = set()
        for i, c in enumerate(candidates):
            shared_with = Counter()
            for t in c['tokens']:
                for j in token_to_cand.get(t, []):
                    if j > i:
                        shared_with[j] += 1
            for j, count in shared_with.items():
                if count >= min_shared:
                    pair_key = (i, j)
                    if pair_key not in evaluated_pairs:
                        evaluated_pairs.add(pair_key)
                        pairs_compared += 1
                        f = _evaluate_pair(candidates[i], candidates[j], threshold=threshold, min_shared=min_shared)
                        if f:
                            findings.append(f)

    # Sort findings: status contradictions first, then highest overlap
    type_priority = {'status_contradiction': 0, 'near_duplicate': 1, 'substantial_overlap': 2}
    findings.sort(key=lambda item: (type_priority.get(item['type'], 3), -item['overlap']))
    findings = findings[:limit]

    return {
        'schema': 1,
        'vault': str(vault),
        'target': target_rel,
        'notes_scanned': len(candidates),
        'pairs_compared': pairs_compared,
        'threshold': threshold,
        'min_shared': min_shared,
        'findings': findings,
    }


def format_audit_human(report: dict) -> str:
    """Format auditor report as clean ASCII Turkish terminal output."""
    lines = ['=== Ikinci Beyin Hafiza Denetim Raporu (Auditor) ===\n']
    target = report.get('target')
    notes_scanned = report.get('notes_scanned', 0)
    pairs_compared = report.get('pairs_compared', 0)
    findings = report.get('findings', [])

    if target:
        lines.append(f"Hedef Not: {target}")
        lines.append(f"Karsilastirilan Not: {notes_scanned} | Bulgu: {len(findings)}\n")
    else:
        lines.append(f"Taranan Not: {notes_scanned} | Aday Karsilastirma: {pairs_compared} | Bulgu: {len(findings)}\n")

    if not findings:
        lines.append("Belirgin bir tekrar, celiski veya guncellik sorunu tespit edilmedi; hafiza durumu dengeli.")
        return '\n'.join(lines)

    contradictions = [f for f in findings if f.get('type') == 'status_contradiction']
    duplicates = [f for f in findings if f.get('type') == 'near_duplicate']
    overlaps = [f for f in findings if f.get('type') == 'substantial_overlap']

    if contradictions:
        lines.append("[Celiski ve Guncellik Sinyalleri]")
        for c in contradictions:
            lines.append(f"  - {c['older_note']} <-> {c['newer_note']}")
            pct = int(round(c.get('overlap', 0) * 100))
            lines.append(f"    Ortusme: %{pct} ({c.get('shared_terms_count', 0)} ortak terim)")
            conf = c.get('conflict', {})
            if conf:
                lines.append(f"    Eski durum: \"{conf.get('older_term')}\" -> Yeni durum: \"{conf.get('newer_term')}\"")
            lines.append(f"    Tavsiye: {c.get('recommendation', '')}")
            lines.append("")

    if duplicates:
        lines.append("[Yuksek Tekrar ve Ortusme]")
        for d in duplicates:
            lines.append(f"  - {d['older_note']} <-> {d['newer_note']}")
            pct = int(round(d.get('overlap', 0) * 100))
            lines.append(f"    Ortusme: %{pct} ({d.get('shared_terms_count', 0)} ortak terim)")
            nov = d.get('novel_terms_newer_sample', [])
            if nov:
                lines.append(f"    Yeni terimler: {', '.join(nov[:5])}")
            if d.get('context_bloat'):
                lines.append(f"    Baglam boyutu: {d.get('words_older', 0)} ve {d.get('words_newer', 0)} kelime")
            lines.append(f"    Tavsiye: {d.get('recommendation', '')}")
            lines.append("")

    if overlaps:
        lines.append("[Kismi Ortusme ve Iliskilendirme]")
        for o in overlaps:
            lines.append(f"  - {o['older_note']} <-> {o['newer_note']}")
            pct = int(round(o.get('overlap', 0) * 100))
            lines.append(f"    Ortusme: %{pct} ({o.get('shared_terms_count', 0)} ortak terim)")
            nov = o.get('novel_terms_newer_sample', [])
            if nov:
                lines.append(f"    Ek kavramlar: {', '.join(nov[:5])}")
            lines.append(f"    Tavsiye: {o.get('recommendation', '')}")
            lines.append("")

    lines.append("[Denetci Siniri]")
    lines.append("  Benzerlik tek basina gereksizlik degildir; bagimsiz dogrulamalar ve kanitlar")
    lines.append("  korunmaya deger olabilir. Denetci yalniz tavsiye verir; hicbir notu silmez.")
    return '\n'.join(lines)
