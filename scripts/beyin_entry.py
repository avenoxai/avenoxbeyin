#!/usr/bin/env python3
"""Vault-local entry point; configuration contains no credentials."""
import importlib.util
import json
from pathlib import Path
import sys
import unicodedata
sys.dont_write_bytecode = True



JEV_MODES = {'off': 'kapali', 'shadow': 'golge', 'on': 'acik'}
_DOCTOR_JEV_MODES = {'off': 'kapalı', 'shadow': 'gölge', 'on': 'açık'}
_TURKISH_SAMPLE = 'çÇğĞıİöÖşŞüÜ'
_TURKISH_ASCII = str.maketrans({
    'ç': 'c', 'Ç': 'C',
    'ğ': 'g', 'Ğ': 'G',
    'ı': 'i', 'İ': 'I',
    'ö': 'o', 'Ö': 'O',
    'ş': 's', 'Ş': 'S',
    'ü': 'u', 'Ü': 'U',
    # Circumflex vowels of older spelling (Kâğıt, hâlâ) show up in note names and text.
    'â': 'a', 'Â': 'A', 'î': 'i', 'Î': 'I', 'û': 'u', 'Û': 'U',
})


def jev_mode(mode, provider):
    """Laya is shadow-only: a saved or hand-edited `on` still runs as shadow."""
    if provider == 'laya' and mode in ('shadow', 'on'):
        return 'golge (yalniz)'
    return JEV_MODES.get(mode, 'bilinmiyor')


def _doctor_jev_mode(mode, provider):
    """Plain Turkish Jev mode label for doctor --human."""
    if provider == 'laya' and mode in ('shadow', 'on'):
        return 'gölge (yalnız)'
    return _DOCTOR_JEV_MODES.get(mode, 'bilinmiyor')


def update_lines(result):
    status = result.get('status', 'unknown')
    command = 'py -3' if sys.platform == 'win32' else 'python3'
    if status == 'available':
        lines = ['Yeni sürüm: ' + str(result['version']),
                 'Sürüm notları: ' + str(result['release_url']),
                 'Güncellemek için: ' + command + ' beyin.py update']
    else:
        messages = {'disabled': 'Sürüm bildirimleri kapalı (bir şey yapmanıza gerek yok).',
                    'unknown': 'Sürüm kontrolü henüz yapılmadı (bir şey yapmanıza gerek yok).',
                    'recovery_required': 'Tamamlanmamış güncelleme veya geri alma var. Kurtarmak için: ' + command + ' beyin.py recover',
                    'unavailable': 'Sürüm kontrolü yapılamadı; güncellik doğrulanmadı.',
                    'up_to_date': 'Beyin güncel: ' + str(result.get('current_version', '?')),
                    'ahead': 'Kurulu sürüm resmi kararlı sürümden ileride.'}
        lines = [messages.get(status, 'Sürüm bilgisi alınamadı.')]
    if result.get('checked_at'):
        from datetime import datetime, timezone
        lines.append('Son kontrol: ' + datetime.fromtimestamp(result['checked_at'], timezone.utc).isoformat())
    return lines


def jev_lines(result):
    """Shared by the jev command and the doctor summary; reads only reported fields."""
    lines = []
    laya = result.get('provider') == 'laya'
    if result.get('automatic_model_calls'):
        lines.append('Otomatik baglam acik: her turda istemin ve en fazla 8 aday ic/kamu notunun basligi ile'
                     ' ilk 600 karakteri ' + ('yerel Laya sunucusuna' if laya else 'saglayiciya') +
                     ' gider. Ozel notlar gonderilmez.')
        if result.get('context_mode_notice'):
            lines.append("Uyari: otomatik baglam yalniz 'context_mode: turn' ile calisir; su anki ayar '" +
                         ascii_text(result.get('context_mode', '?')) + "' (etkinlestirmek icin: beyin.py preferences --context-mode turn).")
        if laya and not result.get('auto_context_applied'):
            lines.append('Laya yalniz golge modda calisir: otomatik baglam puanlari yalniz kaydedilir, baglami degistirmez.')
    if laya:
        if result.get('mode_refused') == 'laya_shadow_only':
            lines.append('jev.json acik mod istiyor ama Laya yalniz golge modda calisir; sonuclar degismez.'
                         ' Acik mod icin: jev on --provider typesafe.')
        if result.get('mode', 'off') != 'off':
            lines.append('Laya yerel sunucusu: ' + ascii_text(result.get('laya', {}).get('base_url')) +
                         '; LAYA_HOST=127.0.0.1 ile baslat. Puanlar olcum icin kaydedilir, sonuclari degistirmez.')
    elif result.get('mode', 'off') != 'off' and not result.get('key_present'):
        lines.append('TYPESAFE_API_KEY yok: cagrilar yerel sonuca duser.')
    server = result.get('server')
    if isinstance(server, dict) and server.get('checked'):
        if server.get('reachable') and server.get('pinned_model_loaded'):
            lines.append('Laya sunucusu hazir (' + ascii_text(server.get('device')) + ').')
        elif server.get('reachable'):
            lines.append('Laya sunucusu cevap veriyor ama sabitlenen model yuklu degil.')
        else:
            lines.append('Laya sunucusuna ulasilamadi: cagrilar yerel sonuca duser.')
    elif isinstance(server, dict) and server.get('reason') == 'not_applicable':
        lines.append('--check yalniz laya saglayicisinda sunucuyu yoklar.')
    return lines


def ascii_text(value):
    """Human lines stay ASCII even if a field ever carries something else."""
    return str(value if value is not None else '?').encode('ascii', 'replace').decode('ascii')


def turkish_to_ascii(value):
    """Transliterate Turkish diacritics to ASCII equivalents without introducing '?'.

    NFC first: a decomposed name (s + U+0327, as macOS file names often arrive) would keep
    its combining mark and print as 's?'.
    """
    return unicodedata.normalize('NFC', str(value if value is not None else '?')).translate(_TURKISH_ASCII)


def stream_supports_turkish(stream=None):
    """Return True when stream can encode Turkish diacritics without replacement."""
    target = sys.stdout if stream is None else stream
    encoding = getattr(target, 'encoding', None)
    if not encoding:
        return True
    try:
        _TURKISH_SAMPLE.encode(encoding)
        return True
    except (LookupError, UnicodeEncodeError):
        return False


def safe_stream_text(text, stream=None):
    """Adapt text for stream encoding: keep UTF-8 Turkish when supported, else transliterate safely."""
    target = sys.stdout if stream is None else stream
    encoding = getattr(target, 'encoding', None)
    if not encoding:
        return text
    adapted = text if stream_supports_turkish(target) else turkish_to_ascii(text)
    try:
        adapted.encode(encoding)
        return adapted
    except LookupError:
        return adapted.encode('ascii', 'replace').decode('ascii')
    except UnicodeEncodeError:  # composing first keeps decomposed Turkish letters on cp1254/cp857
        return unicodedata.normalize('NFC', adapted).encode(encoding, 'replace').decode(encoding)


def print_human(text, stream=None):
    """Write human output safely for stream encoding without raising UnicodeEncodeError."""
    target = sys.stdout if stream is None else stream
    print(safe_stream_text(text, target), file=target)


# Bidirectional overrides can reorder what a terminal shows (Trojan Source); category Cc covers ESC/BEL.
_BIDI_CONTROLS = frozenset('\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069')


def plain_text(value):
    """One terminal line from stored text: newlines fold, control and bidi characters become '?'."""
    text = str(value if value is not None else '?').replace('\r\n', '\n')
    return ''.join(' / ' if ch in '\n\r\u2028\u2029\x85' else ' ' if ch == '\t' else
                   '?' if unicodedata.category(ch) == 'Cc' or ch in _BIDI_CONTROLS else ch for ch in text)


def state_location_lines(location):
    """Doctor lines for the state_location warnings (#113); information only, never a status."""
    if not isinstance(location, dict):
        return []
    codes = {str(warning).split(':', 1)[0] for warning in location.get('warnings') or []}
    prefix = 'State konumu (bilgi): '  # docs/v3/UPDATE.md step 7 tells users to look for this prefix
    lines = []
    if 'state_split' in codes:
        lines.append(prefix + 'bu kasa için paket yönlendirmesinin iki tarafında da kurulu bir state klasörü var (' +
                     ', '.join(plain_text(root) for root in location.get('sibling_state_roots') or []) +
                     '). Oturumun hangi yarıyı okuduğu paket içinde çalışıp çalışmamasına bağlı;'
                     ' güncel olanı seçip taşıyın: https://github.com/avenoxai/avenoxbeyin/blob/main/docs/v3/UPDATE.md')
    if 'pinned_in_package_container' in codes:
        lines.append(prefix + 'sabitlenmiş state klasörü MSIX paket kapsayıcısının (Packages\\...\\LocalCache) içinde;'
                     ' paket sıfırlanır ya da başka kimlikle kurulursa bu yol kaybolur. Taşıma: https://github.com/avenoxai/avenoxbeyin/blob/main/docs/v3/UPDATE.md')
    if 'pin_resolves_elsewhere' in codes:
        lines.append(prefix + 'sabitlenmiş yol bu süreçte başka bir dizine çözümleniyor (' +
                     plain_text(location.get('pinned_resolves_here_to')) +
                     '). doctor komutunu paket içinden ve dışından çalıştırıp karşılaştırın.')
    if 'pinned_state_not_absolute' in codes:
        lines.append(prefix + '.beyin-runtime.json içindeki state yolu bu makinede mutlak bir yol değil; başka bir'
                     ' işletim sisteminden eşitlenmiş olabilir. beyin.py bu makinenin varsayılan state klasörünü'
                     ' kullanıyor. Kurulum dosyaları makineye özeldir, eşitlemeyin: https://github.com/avenoxai/avenoxbeyin/blob/main/docs/v3/MULTI-MACHINE.md')
    if 'pinned_state_empty' in codes:
        lines.append(prefix + 'sabitlenmiş state klasöründe kurulum bulunamadı. Klasör taşındıysa kurucuyu yeni'
                     ' --state ile yeniden çalıştırın.')
    if 'effective_state_differs' in codes:
        lines.append(prefix + 'bu çalışma sabitlenmiş konum yerine başka bir state klasörünü okuyor.')
    return lines


def receipt_line_endings_lines(report):
    """Doctor line for the receipt line-ending warning (#205); information only, never a status."""
    if isinstance(report, dict) and report.get('status') == 'warning':
        return ['Oturum kaydı satır sonu (bilgi): core.autocrlf=true ve receipts/ sabitlenmemiş; başka makineden gelen'
                ' oturum kayıtları CRLF ile çıkıp çakışma hatasına yol açabilir.'
                ' .gitattributes dosyasına "receipts/** -text" ekleyin: https://github.com/avenoxai/avenoxbeyin/blob/main/docs/v3/MULTI-MACHINE.md']
    return []


def sync_copy_lines(result):
    """Sync lines for file-sync copies (#205): a stamped copy is out of the index, a numbered one is only listed."""
    lines = []
    stamped = [w for w in result.get('warnings') or [] if isinstance(w, dict) and str(w.get('reason', '')).startswith('file sync')]
    for warning in stamped:
        lines.append('Senkron cakisma kopyasi indekse alinmadi: ' + plain_text(warning.get('source')) +
                     '. Asil notla karsilastir, dogru tarafi tut, digerini sil.')
    for candidate in result.get('sync_copy_candidates') or []:
        same = candidate.get('same_content')
        lines.append('Olasi senkron kopyasi: ' + plain_text(candidate.get('source')) + ' / ' + plain_text(candidate.get('original')) +
                     {True: ' (icerik ayni)', False: ' (icerik farkli)'}.get(same, '') +
                     '. Ikisi de indekste; gercek bir notsa adini degistir, kopyaysa birini sil.')
    extra = (result.get('sync_copy_candidate_count') or 0) - len(result.get('sync_copy_candidates') or [])
    if extra > 0:
        lines.append('+' + str(extra) + ' olasi kopya daha; ayrinti icin --json.')
    return lines


def supersedes_lines(issues, count):
    """Present existing sync/doctor findings only; do not guess or remember a target."""
    issues = issues or []
    count = max(count or 0, len(issues))
    if not count:
        return []

    def field(value):
        text = plain_text(value)
        return text[:157] + '...' if len(text) > 160 else text

    reasons = {'ambiguous supersedes reference': 'birden fazla notla eşleşiyor',
               'unresolved supersedes reference': 'hedef bulunamadı',
               'supersedes reference points to the note itself': 'notun kendisine işaret ediyor',
               'supersedes reference is part of a cycle': 'döngünün parçası'}
    lines = ['Supersedes uyarısı: ' + str(count) + ' referans şu anda hiçbir notu emekli etmiyor; '
             'daha önce emekli edilen notlar yeniden bağlama girebilir.']
    for issue in issues[:3]:
        reason = issue.get('reason')
        lines.append('  - ' + field(issue.get('source')) + ' -> ' + field(issue.get('value')) +
                     ' (' + field(reasons.get(reason, reason)) + ')')
    if count > min(3, len(issues)):
        lines.append('+' + str(count - min(3, len(issues))) + ' referans daha.')
    lines.append('Hedefi ve yönü kontrol edin; doğru notu açık id veya tam kasa yoluyla belirtin. Ayrıntı: --json.')
    return lines


def index_exclusion_lines(report, pruned=False):
    """Lines for the user's index exclusions (state/index.json); shared by doctor, sync and preferences."""
    if not isinstance(report, dict):
        return []
    if report.get('status') == 'unavailable':
        return ['İndeks kapsamı denetlenemedi (' + plain_text(report.get('error', 'unknown')) +
                '); indeks dışlama ayarları ve klasör ipuçları doğrulanamadı.']
    lines = []
    if report.get('excluded_dirs'):
        lines.append('İndeks dışı klasör adları (sizin listeniz, her derinlikte): ' +
                     ', '.join(plain_text(name) for name in report['excluded_dirs']) +
                     (' (bu taramada ' + str(report.get('pruned', 0)) + ' klasör atlandı)' if pruned else '') + '.')
    if report.get('error'):
        lines.append('index.json geçersiz (' + plain_text(report['error']) +
                     '); klasör hariç tutma listesi uygulanmıyor, her şey indeksleniyor.')
    return lines


def megabytes(value):
    return '%.1f' % ((value or 0) / 1048576)


def rewrite_note(entry):
    """#168: the rewrite instruction, said only once the full text is safe in the archive."""
    if entry.get('backup') in ('written', 'exists'):
        return ' (tam metin arsive yedeklendi: ' + str(entry.get('archive')) + '); dosyayi sinir icinde yeniden yaz.'
    if entry.get('backup') == 'planned':
        return '; once --dry-run olmadan calistir, tam metin arsive yedeklenir; sonra dosyayi sinir icinde yeniden yaz.'
    return '; dosyayi sinir icinde yeniden yaz.'


def pinned_overflow(count, source=None):
    """Doctor line for <!-- beyin:pin --> rules past their cap of half the rule room (#890)."""
    where = f' ({source})' if source else ''
    return (f'Sabitlenmiş (beyin:pin) {count} kural açılış payına sığmıyor{where} (bilgi): sabitlenenler '
            'kural payının en fazla yarısını kullanır. İşareti en önemli birkaç maddeye indirin.')


def doctor_attention_count(result):
    """Count distinct actionable items needing user attention in a doctor report.

    Informational checks (unused clients, skipped Git check, backup advisory,
    knowledge freshness counts, receipt coverage, boundary hints) do not count.
    Every counted item prints its own line in the doctor --human report; a check
    that prints nothing (an 'unavailable' optional report) is not counted. The
    reverse holds for problem lines (a fault, a queued change waiting for a step, a
    feature that cannot run, a scheduled check that found something), so the summary
    never says "nothing to do" above one; '(bilgi)' advice and an optional new release
    stay uncounted.
    """
    if not isinstance(result, dict):
        return 0
    count = 0
    health = result.get('hook-health.json') if isinstance(result.get('hook-health.json'), dict) else {}
    sync_info = health.get('sync') if isinstance(health.get('sync'), dict) else {}
    if sync_info.get('status') in ('conflict', 'degraded', 'error'):
        count += 1
    if result.get('hook-error.json'):
        count += 1
    if (result.get('pending_events') or 0) > 0:
        count += 1
    if isinstance(result.get('hook_paths'), dict) and result['hook_paths'].get('status') == 'stale':
        count += 1
    if isinstance(result.get('global_bridge_paths'), dict) and result['global_bridge_paths'].get('status') == 'stale':
        count += 1
    if isinstance(result.get('omp_global_hook'), dict) and result['omp_global_hook'].get('stale'):
        count += 1
    if result.get('skill_conflicts'):
        count += 1
    if result.get('instruction_conflicts'):
        count += 1
    if result.get('receipt_view_conflicts'):
        count += 1
    if result.get('exclusions_error'):
        count += 1
    hygiene = result.get('companion_hygiene') if isinstance(result.get('companion_hygiene'), dict) else {}
    over_limit = _companion_hygiene_entries(hygiene)  # one printed line each
    if over_limit:
        count += len(over_limit)
    elif hygiene.get('status') == 'unavailable':
        count += 1
    rules_opening = result.get('companion_rules_opening') if isinstance(result.get('companion_rules_opening'), dict) else {}
    if rules_opening.get('status') in ('clipped', 'stale'):
        count += 1
    for proj in rules_opening.get('project_rules') or []:
        if isinstance(proj, dict) and proj.get('status') in ('clipped', 'stale'):
            count += 1
    tc = result.get('task_completion') if isinstance(result.get('task_completion'), dict) else {}
    if tc.get('strict_issue_count') or tc.get('error'):
        count += 1
    val = result.get('validity') if isinstance(result.get('validity'), dict) else {}
    if val.get('ignored_rejection_count') or val.get('error'):
        count += 1
    scopes = result.get('project_scopes') if isinstance(result.get('project_scopes'), dict) else {}
    if scopes.get('status') in ('error', 'warning'):
        count += 1
    if isinstance(result.get('updates'), dict) and result['updates'].get('status') == 'recovery_required':
        count += 1
    ctx_fill = result.get('context_fill') if isinstance(result.get('context_fill'), dict) else {}
    if ctx_fill.get('status') == 'invalid':
        count += 1
    jev = result.get('jev') if isinstance(result.get('jev'), dict) else {}
    if jev.get('config_valid') is False:
        count += 1
    if jev.get('mode', 'off') != 'off' and jev.get('context_mode_notice'):
        count += 1  # automatic context is on but cannot run in this context_mode
    kf = result.get('knowledge_freshness') if isinstance(result.get('knowledge_freshness'), dict) else {}
    if kf.get('status') == 'unavailable':
        count += 1
    references = result.get('instruction_references') if isinstance(result.get('instruction_references'), dict) else {}
    if references.get('status') == 'unavailable' or references.get('dead_count'):
        count += 1
    supersedes = result.get('supersedes') if isinstance(result.get('supersedes'), dict) else {}
    if supersedes.get('dead_count'):
        count += 1
    parallel = result.get('parallel_sessions') if isinstance(result.get('parallel_sessions'), dict) else {}
    if parallel.get('valid') is False:
        count += 1
    dedupe = result.get('context_dedupe') if isinstance(result.get('context_dedupe'), dict) else {}
    if dedupe.get('valid') is False:
        count += 1
    queue = result.get('mutation_queue') if isinstance(result.get('mutation_queue'), dict) else {}
    if queue.get('valid') is False:
        count += 1
    if _queue_needs_action(queue):
        count += 1
    devices = result.get('devices') if isinstance(result.get('devices'), dict) else {}
    if devices.get('valid') is False:
        count += 1
    index_scope = result.get('index_exclusions') if isinstance(result.get('index_exclusions'), dict) else {}
    if index_scope.get('status') == 'unavailable' or index_scope.get('error'):
        count += 1
    if state_location_lines(result.get('state_location')):
        count += 1
    rle = result.get('receipt_line_endings') if isinstance(result.get('receipt_line_endings'), dict) else {}
    if rle.get('status') == 'warning':
        count += 1
    tracked = result.get('tracked_machine_files') if isinstance(result.get('tracked_machine_files'), dict) else {}
    if tracked.get('status') in ('warning', 'unavailable'):
        count += 1
    if count == 0 and result.get('status') in ('needs_attention', 'pending'):
        count = 1
    # The last scheduled run is a record of an earlier check: its sync findings come from a
    # CLI sync this report does not repeat, and its notice sends the user here. It never lets
    # the summary say "nothing to do", but a live finding already covers what it saw.
    scheduled = result.get('scheduled_check') if isinstance(result.get('scheduled_check'), dict) else {}
    if count == 0 and scheduled.get('status') in ('needs_attention', 'error'):
        count = 1
    return count


def _queue_needs_action(queue):
    """True when the offline queue line names something the user has to run (#965 summary)."""
    if not (queue.get('enabled') or queue.get('queue_exists')):
        return False  # doctor prints no queue line
    if queue.get('status') == 'invalid':
        return True  # the queue file could not be read
    return any(isinstance(queue.get(key), int) and queue[key] > 0
               for key in ('pending_count', 'conflict_count', 'failed_count'))


# beyin_v3_scheduled.ISSUE_LABELS in doctor --human wording; an unknown kind is not shown.
_SCHEDULED_ISSUES = (('sync_conflict', 'eşitleme çakışması'), ('sync_degraded', 'eşitleme uyarısı'),
                     ('hook', 'kanca uyarısı'), ('skill_or_instruction', 'beceri/talimat çakışması'),
                     ('task', 'görev kaydı sorunu'), ('validity', 'geçerlilik sorunu'))


_HOOK_FILE_CLIENTS = {
    '.claude/settings.local.json': 'claude',
    '.codex/hooks.json': 'codex',
    '.agents/hooks.json': 'antigravity',
    '.kiro/hooks/beyin-v3.json': 'kiro',
}


def _client_detail_notes(details):
    """Collect explicit error/warning text from a lifecycle entry, if present."""
    notes = []
    for key in ('error', 'warning'):
        value = details.get(key)
        if isinstance(value, str):
            stripped = value.strip()
            if stripped:
                notes.append(plain_text(stripped))
        elif value is True:
            notes.append('hata' if key == 'error' else 'uyarı')
        elif value and not isinstance(value, bool):
            notes.append(plain_text(value))
    for key in ('errors', 'warnings'):
        items = details.get(key)
        if isinstance(items, (list, tuple)):
            for item in items:
                if isinstance(item, str):
                    stripped = item.strip()
                    if stripped:
                        notes.append(plain_text(stripped))
                elif item and not isinstance(item, bool):
                    notes.append(plain_text(item))
        elif isinstance(items, str):
            stripped = items.strip()
            if stripped:
                notes.append(plain_text(stripped))
    return list(dict.fromkeys(notes))


def _client_doctor_issues(result):
    """Map client names to active hook/config warnings reported elsewhere in doctor."""
    issues = {}

    def add(client, reason):
        if isinstance(client, str) and client:
            bucket = issues.setdefault(client, [])
            if reason not in bucket:
                bucket.append(reason)

    hook_error = result.get('hook-error.json')
    if isinstance(hook_error, dict):
        client = hook_error.get('harness') or hook_error.get('client')
        err = hook_error.get('error') or hook_error.get('message')
        if isinstance(err, str):
            err = err.strip()
        add(client, 'kanca hatası: ' + plain_text(err) if err else 'kanca hatası')

    paths = result.get('hook_paths')
    if isinstance(paths, dict) and paths.get('status') == 'stale':
        stale_rows = paths.get('stale')
        if isinstance(stale_rows, (list, tuple)):
            for row in stale_rows:
                if isinstance(row, dict):
                    client = _HOOK_FILE_CLIENTS.get(row.get('file'))
                    if client:
                        add(client, 'kanca dosyası okunamadı' if row.get('reason') == 'unreadable' else 'kanca yolu eski')

    bridges = result.get('global_bridge_paths')
    if isinstance(bridges, dict) and bridges.get('status') == 'stale':
        stale_bridges = bridges.get('stale')
        if isinstance(stale_bridges, (list, tuple)):
            for row in stale_bridges:
                if isinstance(row, dict):
                    add(row.get('harness'), 'genel köprü yolu eski')

    omp_hook = result.get('omp_global_hook')
    if isinstance(omp_hook, dict):
        omp_error = omp_hook.get('error')
        if isinstance(omp_error, str):
            omp_error = omp_error.strip()
        if omp_hook.get('stale') is True:
            add('omp', 'genel kanca kopyası eski')
        elif omp_error and not isinstance(omp_error, bool):
            add('omp', 'genel kanca denetlenemedi: ' + plain_text(omp_error))

    cov = result.get('receipt_coverage')
    hermes_missing = cov.get('hermes_without_terminal') if isinstance(cov, dict) else None
    if type(hermes_missing) is int and hermes_missing > 0:
        add('hermes', 'kapanış sinyali eksik')

    return issues


def lifecycle_lines(result):
    """Doctor lines for client lifecycle; collapse idle never-seen clients into one summary line (#962)."""
    lifecycle = result.get('lifecycle')
    if not isinstance(lifecycle, dict) or not lifecycle:
        return []
    doctor_issues = _client_doctor_issues(result)
    status_labels = {
        'observed_metadata': 'olay görüldü',
        'never_seen': 'henüz oturum gözlenmedi',
        'error': 'hata',
        'warning': 'uyarı',
        'needs_attention': 'kontrol gerekiyor',
    }
    lines = []
    unused = []
    for name, raw_details in lifecycle.items():
        label = plain_text(name)
        details = raw_details if isinstance(raw_details, dict) else {}
        client_status = details.get('status')
        if not isinstance(client_status, str) or not client_status:
            client_status = 'never_seen'
        detail_notes = _client_detail_notes(details)
        extra_notes = [note for note in doctor_issues.get(name, ()) if note not in detail_notes]
        notes = detail_notes + extra_notes
        events = details.get('events')
        has_events = isinstance(events, (list, tuple)) and any(events)
        if client_status == 'never_seen' and not notes and not has_events:
            unused.append(label)
            continue
        state_text = status_labels.get(client_status, plain_text(client_status))
        if notes:
            lines.append(label + ': ' + state_text + ' (' + '; '.join(notes) + ')')
        else:
            lines.append(label + ': ' + state_text)
    if unused:
        lines.append('Kullanılmayan istemciler: ' + ', '.join(unused) + ' (' + str(len(unused)) + ')')
    return lines


def _companion_hygiene_entries(hygiene):
    """(label, file name, size entry, is_project) for every over-limit companion file (#922).

    over_limit names root files bare and project files as '<directory>/<name>'. Each listed
    name gives exactly one entry, in that order, so the summary count and the printed lines
    agree; a name without a size reading gets an empty entry instead of being dropped.
    """
    if not isinstance(hygiene, dict):
        return []
    files = hygiene.get('files') if isinstance(hygiene.get('files'), dict) else {}
    project_files = {}
    for proj in hygiene.get('projects') or []:
        if isinstance(proj, dict) and isinstance(proj.get('directory'), str) and isinstance(proj.get('files'), dict):
            for name, entry in proj['files'].items():
                project_files[proj['directory'] + '/' + str(name)] = entry
    entries = []
    for name in dict.fromkeys(name for name in hygiene.get('over_limit') or [] if isinstance(name, str)):
        project = '/' in name
        entry = project_files.get(name) if project else files.get(name)
        entries.append((name, name.rsplit('/', 1)[-1], entry if isinstance(entry, dict) else {}, project))
    return entries


def human_result(result, command, installed_version=None):
    status = result.get('status', '')
    if command == 'scheduled-check' and 'status' in result:
        # Exit 4 (needs attention) prints here like a finished check: the check ran and found something.
        texts = {'ok': 'Zamanlanmis denetim: saglikli.',
                 'needs_attention': 'Zamanlanmis denetim: kontrol gerektiren bir sorun var; ayrinti icin beyin.py doctor.',
                 'already_running': 'Zamanlanmis denetim: baska bir denetim hala calisiyor; bu kosu atlandi.',
                 'error': 'Zamanlanmis denetim tamamlanamadi (' + ascii_text(result.get('error')) + '); beyin.py doctor ile bak.'}
        lines = [texts.get(status, 'Zamanlanmis denetim: ' + ascii_text(status))]
        notification = {'shown': 'Bildirim gonderildi.',
                        'failed': 'Bildirim gonderilemedi.',
                        'unavailable': 'Bu makinede bildirim araci yok.'}
        if result.get('notification') in notification:
            lines.append(notification[result['notification']])
        if status == 'needs_attention' and result.get('session_notice_pending') is True:
            lines.append('Sonraki ajan oturumunun acilisinda da tek satir olarak soylenecek.')
        return '\n'.join(lines)
    if result.get('error'):
        return ('Islem tamamlanamadi: ' + str(result.get('message', result['error'])) +
                ('\n' + ascii_text(result['hint']) if result.get('hint') else ''))
    if command == 'devices':
        return json.dumps(result, ensure_ascii=False, indent=2)
    if command == 'hook-reconcile':
        status = result.get('status')
        if status in ('ok', 'noop'):
            return 'Hook yollari zaten guncel; hicbir dosya degistirilmedi.'
        if status == 'not_installed':
            return 'Bu vault\'ta kurulu hook dosyasi bulunamadi.'
        lines = ['Hook mutabakati tamamlandi: ' + str(len(result.get('files', []))) + ' dosya guncellendi (' +
                 str(result.get('reconciled_count', 0)) + ' hook komutu bu makineye uyarlandi).']
        for entry in result.get('files', []):
            lines.append('- ' + entry['file'] + ' (yedek: ' + entry.get('backup', '') + ', ' +
                         str(entry.get('reconciled', 0)) + ' hook yenilendi)')
            for change in entry.get('changes', []):
                lines.append('  * ' + change['event'] + ':')
                lines.append('    Eski: ' + plain_text(change.get('old_summary', change.get('old_command', ''))[:80]))
                lines.append('    Yeni: ' + plain_text(change.get('new_summary', change.get('new_command', ''))[:80]))
        lines.append('Eski komutlar bu makinenin Python ve vault yollariyla degistirildi.')
        return '\n'.join(lines)
    if command == 'triage':
        try:
            from beyin_v3_triage import format_human
            return format_human(result)
        except Exception:
            return json.dumps(result, ensure_ascii=False, indent=2)
    if command == 'selftest':
        status = result.get('status')
        if status == 'error':
            return 'Oz-denetim tamamlanamadi: ' + plain_text(result.get('message', ''))
        if result.get('added'):
            return 'Soru eklendi: ' + plain_text(result.get('questions_path', ''))
        if status == 'no_questions':
            return 'Vault oz-denetim (selftest): soru dosyasi bulunamadi (' + plain_text(result.get('questions_path')) + '). ' + plain_text(result.get('hint', ''))
        total = result.get('total', 0)
        passed = result.get('passed', 0)
        missed = result.get('missed', [])
        ctrl_total = result.get('controls_total', 0)
        ctrl_passed = result.get('controls_passed', 0)
        regressed = result.get('regressed', [])
        lines = [
            f'Vault oz-denetim (selftest): toplam {total} soru, gecen {passed}, kacan {len(missed)}.',
            f'Kontroller: {ctrl_passed}/{ctrl_total} basarili.'
        ]
        if result.get("mode") == "context":
            lines.append(f'Bağlam bütçesi {result["budget"]} karakter. Kırpılan cevap sayısı {result["truncated"]}.')
        if regressed:
            lines.append(f'UYARI: {len(regressed)} soru onceki kosuya gore geriledi (eskiden geciyordu, simdi kacti)!')
        for m in missed[:3]:
            lines.append(f'  - Soru: "{m.get("query")}" -> Beklenen: {m.get("expected_source")}')
        return '\n'.join(lines)
    if command == 'audit':
        try:
            from beyin_v3_audit import format_audit_human
            return format_audit_human(result)
        except Exception:
            return json.dumps(result, ensure_ascii=False, indent=2)
    if command == 'jev':
        features = result.get('features', {})
        provider = result.get('provider')
        lines = ['Jev: ' + jev_mode(result.get('mode'), provider) +
                 (' (kayitli: ' + jev_mode(result.get('saved_mode'), provider) + ')'
                  if result.get('kill_switch') else ''),
                 'Ozellikler: ' + (', '.join(name + ' ' + ('acik' if value else 'kapali')
                                             for name, value in features.items()) or 'yok')]
        if result.get('provider') == 'laya':
            lines += ['Saglayici: laya (yerel, model ' + ascii_text(result.get('laya', {}).get('model')) + ')',
                      'Anahtar: gerekmez' + (' (LAYA_API_KEY var)' if result.get('key_present') else '')]
        else:
            lines.append('Anahtar: ' + ('var' if result.get('key_present') else 'yok'))
        lines += ['Acil kapatma: ' + ('acik' if result.get('kill_switch') else 'kapali'),
                  'Son 24 saat: ' + str(result.get('last_24h', {}).get('calls', 0)) + ' cagri']
        if not result.get('config_valid', True):
            lines.append('Ayar dosyasi bozuk; jev.json elle duzeltilmeli.')
        return '\n'.join(lines + jev_lines(result))
    if command == 'jev-audit':
        audit = result.get('audit', {})
        lines = ['Jev auto_context denetimi: ' + ('acik' if audit.get('enabled') else 'kapali'),
                 'Ayar dosyasi: ' + result.get('path', '?'),
                 'Log dosyasi: ' + result.get('log_path', '?'),
                 'Not puanlari: ' + ('dahil' if audit.get('include_scores') else 'haric'),
                 'Topical puani: ' + ('dahil' if audit.get('include_topical') else 'haric')]
        if result.get('changed'):
            lines.insert(0, 'Jev auto_context denetimi ' + ('etkinlestirildi' if audit.get('enabled') else 'devre disi birakildi'))
        return '\n'.join(lines)
    if command == 'preferences':
        prefs = result['preferences']
        lines = ['Otomatik kontrol: ' + ('acik' if prefs['auto_sync'] else 'kapali'),
                 'Kontrol araligi: ' + str(prefs['interval_minutes']) + ' dakika (0 = her olay)',
                 'Otomatik baglam: ' + prefs['context_mode'],
                 'Baglam ust siniri: ' + str(prefs['context_chars']) + ' karakter',
                 'Sir suzgeci: ' + ('acik' if prefs['secret_filter'] else 'kapali'),
                 'Surum bildirimi: ' + ('acik' if result.get('update_notifications', {}).get('effective') else 'kapali'),
                 'Proje oturum basi baglami: ' + ('acik' if result.get('project_context') == 'on' else 'kapali')]
        if result.get('daily_layout') in ('flat', 'v3'):
            layout = result['daily_layout']
            lines.append('Daily duzeni: ' + layout + ' (' +
                         ('daily/YYYY-MM-DD.md' if layout == 'flat' else 'daily/v3/YYYY-MM-DD.md') + ')')
        opening = (result.get('companion_context') or {}).get('context_chars')
        if opening is not None:
            lines.append('Oturum basi hafiza baglami: ' + (str(opening) + ' karakter' if opening else 'baglam ust siniri kadar'))
        if result.get('companion_context_notice'):
            lines.append(result['companion_context_notice'])
        for name, value in (result.get('companion_limits') or {}).items():
            lines.append('Hafiza dosyasi siniri, ' + name + ': ' + (str(value) + ' karakter' if value else 'kapali'))
        hygiene = result.get('hygiene') or {}
        if hygiene:
            lines.append('Hijyen sinyalleri (varsayilan kapali): kelime tavani uyarisi ' +
                         ('acik (' + str(hygiene.get('max_words')) + ' kelime)' if hygiene.get('word_cap_warning') else 'kapali') +
                         ', klasor sorulari ' + ('acik' if hygiene.get('folder_questions') else 'kapali') +
                         ', terfi raporu ' + ('acik' if hygiene.get('promotion') else 'kapali'))
        if result.get('hygiene_notice'):
            lines.append(result['hygiene_notice'])
        inbox = result.get('inbox_report') or {}
        if inbox:
            lines.append('Gelen kutusu raporu (varsayilan kapali): ' +
                         ('acik (' + str(inbox.get('max_items')) + ' not / ' + str(inbox.get('max_days')) + ' gun; ' +
                          ('klasorler: ' + ', '.join(plain_text(name) for name in inbox['folders'])
                           if inbox.get('folders') else 'klasor adindan tanima') + ')'
                          if inbox.get('enabled') else 'kapali'))
        if result.get('inbox_report_notice'):
            lines.append(result['inbox_report_notice'])
        if result.get('parallel_sessions'):
            lines.append('Paralel oturum bildirimi: ' + ('acik' if result['parallel_sessions'] == 'on' else 'kapali'))
        if result.get('parallel_sessions_notice'):
            lines.append(result['parallel_sessions_notice'])
        if result.get('context_dedupe'):
            lines.append('Oturum ici tekrar onleme: ' + ('acik' if result['context_dedupe'] == 'on' else 'kapali'))
        if result.get('context_dedupe_notice'):
            lines.append(result['context_dedupe_notice'])
        hermes_p = result.get('hermes_platforms') or {}
        if hermes_p.get('attended') or hermes_p.get('unattended'):
            h_parts = []
            if hermes_p.get('attended'):
                h_parts.append('gozetimli: ' + ', '.join(hermes_p['attended']))
            if hermes_p.get('unattended'):
                h_parts.append('gozetimsiz: ' + ', '.join(hermes_p['unattended']))
            lines.append('Hermes platform ayarlari: ' + '; '.join(h_parts))
        if result.get('hermes_platforms_notice'):
            lines.append(result['hermes_platforms_notice'])
        if result.get('excluded_components'):
            lines.append('Haric tutulan bilesenler: ' + ', '.join(result['excluded_components']))
        if result.get('exclusion_notice'):
            lines.append(result['exclusion_notice'])
        lines += index_exclusion_lines(result.get('index_exclusions'))
        if result.get('index_exclusions_notice') and not (result.get('index_exclusions') or {}).get('error'):
            lines.append(result['index_exclusions_notice'])
        lines.append('Acikken gunde en fazla bir kez GitHub surum bilgisi okunur; notlar gonderilmez.')
        lines.append('Yerel kontroller model cagirmaz. Zamanlayici kurulmaz.')
        return '\n'.join(lines)
    if command == 'recap':
        # --since/--until select local calendar days (as daily/v3 is named); the --days window stays UTC.
        local = result.get('timezone') == 'local'
        lines = ['Kaynakli etkinlik: ' + (result['from'] or 'baslangic siniri yok') + ' - ' + result['through'] +
                 (' (yerel gun; ' if local else ' (UTC; ') + 'ajan kayitlari, bagimsiz dogrulanmis olgular degil)']
        if not result['items']:
            lines.append('Bu aralikta tarihli kayit yok.')
        for item in result['items']:
            lines.append('\n' + (item.get('day') or item['created_at'][:10]) + '  ' + plain_text(item['summary']))
            lines.append('Kaynak: ' + plain_text(item['source']))
            for ref in item.get('refs', []):
                lines.append('  - ' + plain_text(ref))
            withheld = item.get('refs_withheld') or {}
            if withheld.get('private'):
                lines.append('  (' + str(withheld['private']) + ' ozel kaynak baglantisi gizlendi)')
            if withheld.get('missing'):
                lines.append('  (' + str(withheld['missing']) + ' kaynak artik yok)')
        if result['truncated']:
            lines.append('Yalniz en yeni ' + str(result['shown']) + '/' + str(result['total']) +
                         ' kayit gosterildi; --limit ile artirabilirsin.')
        if result['undated_omitted']:
            lines.append(str(result['undated_omitted']) + ' tarihsiz/eski bicimli kayit atlandi.')
        if result.get('missing_source_omitted'):
            lines.append(str(result['missing_source_omitted']) + ' kaydin makbuz dosyasi artik yok; atlandi.')
        if result.get('partial') or result.get('source_sync', {}).get('status') == 'degraded':
            lines.append('Uyari: kaynak esitlemesi kismi; ayrinti icin --json kullan.')
        if any(w.get('kind') == 'receipt_view' for w in result.get('source_sync', {}).get('warnings', [])):
            lines.append('Uyari: receipt view yerel duzenlemesi korundu; arsivleyip yenilemek icin beyin.py sync --recover-views kullan.')
        return '\n'.join(lines)
    if command == 'companion-compact':
        lines = []
        for name, entry in result.get('files', {}).items():
            if entry.get('status') in ('compacted', 'planned'):
                lines.append(name + ': ' + str(entry.get('chars')) + ' -> ' + str(entry.get('chars_after')) + ' karakter' +
                             (' olacak' if entry['status'] == 'planned' else '') + ', ' + str(entry.get('moved_chars')) +
                             ' karakter arsive ' + ('tasinacak' if entry['status'] == 'planned' else 'tasindi') + '.')
                if not entry.get('within_limit_after'):
                    lines.append(name + ' hala sinirin (' + str(entry.get('limit')) + ') ustunde' + rewrite_note(entry))
            elif entry.get('status') == 'needs_rewrite':
                lines.append(name + ': tasinacak tarihli eski kayit yok' + rewrite_note(entry))
            elif entry.get('status') == 'conflict':
                lines.append(name + ': islem sirasinda dosya degisti; hicbir sey tasinmadi, tekrar dene.')
            elif entry.get('status') == 'needs_attention':
                lines.append(name + ': ' + str(entry.get('reason', 'kontrol gerekiyor')) + '.')
            elif entry.get('status') in ('within_limit', 'limit_off'):
                lines.append(name + ': sinir icinde, degisiklik yok.')
        if result.get('status') == 'needs_attention' and not result.get('files'):
            lines.append('Sinir ayari okunamadi; hicbir sey tasinmadi.' if result.get('limits_file') == 'invalid'
                         else 'Birden fazla companion klasoru var; hicbir sey tasinmadi.')
        return '\n'.join(lines + ['Hicbir metin silinmedi; model cagrilmadi.'])
    if command == 'state-compact':
        if status == 'planned':
            return ('Eski olay kaydi: ' + str(result.get('events_prunable', 0)) + ' (yaklasik ' +
                    megabytes(result.get('event_bytes_prunable', 0)) + ' MB), eski gecici dosya: ' +
                    str(result.get('stale_temp_files', 0)) + ', suresi dolmus Jev onbellegi: ' +
                    str(result.get('expired_jev_cache', 0)) + '. Hicbir sey degismedi.')
        lines = ['Silinen eski olay kaydi: ' + str(result.get('events_removed', 0)) + ', eski gecici dosya: ' +
                 str(result.get('temp_files_removed', 0)) + ', suresi dolmus Jev onbellegi: ' +
                 str(result.get('jev_cache_removed', 0)) + '.']
        if result.get('vacuum') == 'done':
            lines.append('Yerel indeks ' + megabytes(result.get('database_bytes_before', 0)) + ' MB -> ' +
                         megabytes(result.get('database_bytes_after', 0)) + ' MB.')
        elif result.get('vacuum') == 'busy':
            lines.append('Indeks su an kullaniliyor; VACUUM yapilamadi. Acik oturumlar bitince tekrar dene.')
        else:
            lines.append('VACUUM tamamlanamadi: ' + ascii_text(result.get('vacuum')))
        return '\n'.join(lines + ['Vault dosyalarina dokunulmadi.'])
    if command == 'feedback':
        if result.get('success'):
            fb = result.get('feedback', {})
            return f"Geri bildirim kaydedildi ({result.get('target')} -> {result.get('verdict')}): ok={fb.get('ok', 0)}, wrong={fb.get('wrong', 0)}, stale={fb.get('stale', 0)}"
        return f"Geri bildirim kaydedilemedi: {result.get('message') or result.get('error', 'unknown error')}"
    if command == 'checkpoint':
        steps = result.get('steps') or {}
        parts = [f"{k}={v.get('status') if isinstance(v, dict) else v}" for k, v in steps.items()]
        summary = ', '.join(parts) if parts else 'adim yok'
        return f"Oturum kontrol noktasi ({result.get('status', 'tamamlandi')}): {summary}."

    if command == 'mutation-queue':
        if isinstance(result.get('enabled'), bool):
            return ('Cevrimdisi mutasyon kuyrugu: ' + ('acik' if result['enabled'] else 'kapali (acmak icin: beyin.py mutation-queue --enable)') +
                    '; ' + str(result.get('pending_count', 0)) + ' bekleyen, ' + str(result.get('conflict_count', 0)) +
                    ' cakisma, ' + str(result.get('failed_count', 0)) + ' basarisiz.')
        if 'replayed' in result:
            text = ('Kuyruk yeniden oynatildi: ' + str(result.get('applied', 0)) + ' uygulandi, ' +
                    str(result.get('conflicts', 0)) + ' cakisma, ' + str(result.get('failed', 0)) + ' basarisiz.')
            if result.get('source_sync', {}).get('status') in ('error', 'degraded', 'conflict'):
                text += ('\nKaynak esitlemesi dikkat gerektiriyor; tanilamak ve indeksi yenilemek icin '
                         'beyin.py sync calistir. Uygulanan kayitlari yeniden kuyruga ekleme.')
            return text
        if 'pruned' in result:
            return 'Kuyruktan ' + str(result['pruned']) + ' kayit temizlendi; ' + str(result.get('remaining', 0)) + ' kayit kaldi.'
        if result.get('mutation_id'):
            return ('Kuyruk kaydi ' + plain_text(result['mutation_id']) + ': ' + plain_text(result.get('status', '')) +
                    (' (zaten kuyrukta)' if result.get('duplicate') else '') + '.')
    if command == 'doctor':
        py_cmd = 'py -3' if sys.platform == 'win32' else 'python3'
        attention_count = doctor_attention_count(result)
        summary_line = (
            'Her şey yolunda; yapmanız gereken bir şey yok.'
            if attention_count == 0
            else f'Bakmanız gereken {attention_count} şey var.'
        )
        labels = {
            'never_seen': 'Henüz gerçek bir istemci oturumu gözlenmedi; ilk sohbetinizi açtığınızda otomatik olarak izlenir.',
            'observed_metadata': 'Oturum olayları normal şekilde gözleniyor.',
            'pending': 'İşlenmeyi bekleyen oturum olayları var; eşitlemek için: ' + py_cmd + ' beyin.py sync',
            'needs_attention': 'Kontrol gerektiren bir sorun var; ayrıntılar aşağıda listelenmiştir.',
        }
        lines = [
            summary_line,
            'Beyin ' + (installed_version or 'sürümü bilinmiyor'),
            labels.get(status, 'Sağlık kontrolü tamamlandı.'),
            'Bekleyen iş: ' + str(result.get('pending_events', 0)),
        ]
        health = result.get('hook-health.json') if isinstance(result.get('hook-health.json'), dict) else {}
        sync_info = health.get('sync') if isinstance(health.get('sync'), dict) else {}
        if sync_info.get('status') in ('conflict', 'degraded', 'error'):
            lines.append('Kaynak eşitlemesinde sorun var; durumu düzeltmek için: ' + py_cmd + ' beyin.py sync')
        if result.get('hook-error.json'):
            lines.append('Son kanca (hook) çalışmasında hata kaydedildi; ayrıntıyı görmek için ajanınıza "beyin doktor" deyin.')
        tc = result.get('task_completion') if isinstance(result.get('task_completion'), dict) else {}
        if tc.get('error'):
            lines.append('Görev tamamlama kontrolü tamamlanamadı; ayrıntı için --json çıktısına bakın.')
        elif tc.get('strict_issue_count'):
            lines.append('Görev tamamlama uyarısı: ' + str(tc['strict_issue_count']) +
                         ' görevde tamamlanma kaydı eksik; ilgili görev notlarını kontrol edin.')
        val = result.get('validity') if isinstance(result.get('validity'), dict) else {}
        if val.get('error'):
            lines.append('Not geçerlilik kontrolü tamamlanamadı; ayrıntı için --json çıktısına bakın.')
        elif val.get('ignored_rejection_count'):
            lines.append('Not geçerlilik uyarısı: ' + str(val['ignored_rejection_count']) +
                         ' reddedilmiş kayıt hala etkin notlarda duruyor; notları gözden geçirin.')
        omp_global = result.get('omp_global_hook') if isinstance(result.get('omp_global_hook'), dict) else {}
        if omp_global.get('stale'):
            lines.append('Genel OMP kanca kopyası güncel değil: ' + plain_text(omp_global.get('path')) +
                         '. Eski kopyayı güncelleyin veya kaldırın.')
        lines += lifecycle_lines(result)
        jev = result.get('jev') or {}
        if jev.get('mode', 'off') == 'off':
            lines.append('Akıllı bağlam desteği (Jev): kapalı (ek işlem gerekmiyor).')
        else:
            lines.append('Akıllı bağlam desteği (Jev): ' + _doctor_jev_mode(jev['mode'], jev.get('provider')) +
                         (', sağlayıcı: laya (' if jev.get('provider') == 'laya' else ' (') + 'otomatik bağlam: ' +
                         ('açık' if jev.get('automatic_model_calls') else 'kapalı') + '), son 24 saat ' +
                         str(jev.get('last_24h', {}).get('calls', 0)) + ' çağrı')
            if jev.get('context_mode_notice'):
                cm = plain_text(jev.get('context_mode', (result.get('preferences') or {}).get('context_mode', '?')))
                lines.append("Akıllı bağlam desteği (Jev) yalnızca 'context_mode: turn' ile çalışır; şu an '" +
                             cm + "' modunda. Etkinleştirmek için: " + py_cmd + ' beyin.py preferences --context-mode turn')
        if jev.get('config_valid') is False:
            lines.append('Akıllı bağlam ayarı (jev.json) bozuk; dosyayı düzeltin.')
        # Context-fill reminder (issue #685)
        ctx_fill = result.get('context_fill') or {}
        if ctx_fill.get('enabled'):
            lines.append('Bağlam doluluk hatırlatması: etkin (ilk eşik ' + str(ctx_fill.get('first_threshold', 150000)) +
                         ', tekrar ' + str(ctx_fill.get('repeat_threshold', 75000)) + ' token)')
        elif ctx_fill.get('enabled') is False:
            lines.append('Bağlam doluluk hatırlatması: kapalı')
        elif ctx_fill.get('status') == 'invalid':
            lines.append('Bağlam doluluk hatırlatması: ayar geçersiz (' + plain_text(ctx_fill.get('error', 'bilinmeyen hata')) +
                         '); ayar dosyasını kontrol edin.')
        if result.get('secrets_redacted'):
            lines.append('Sır süzgeci ' + str(result['secrets_redacted']) + ' gizli bilgi eşleşmesini [REDACTED] olarak yazdı.')
        cov = result.get('receipt_coverage')
        if isinstance(cov, dict) and cov.get('total', 0) > 0:
            ratio = cov.get('ratio')
            pct = int(round(ratio * 100)) if ratio is not None else 0
            d7 = cov.get('last_7d', {})
            d7_text = ''
            if d7.get('total', 0) > 0 and d7.get('ratio') is not None:
                d7_text = ', son 7 gün: %' + str(int(round(d7['ratio'] * 100)))
            # A question-only session needs no receipt; name how many of the missing ones saw no edit (#212).
            unobserved = cov.get('missing_no_observed_edit')
            unobserved_text = ('; ' + str(unobserved) + ' eksik oturumda düzenleme gözlenmedi'
                               if isinstance(unobserved, int) and unobserved > 0 else '')
            lines.append('Oturum kaydı kapsamı: %' + str(pct) + ' (' + str(cov['covered']) + '/' + str(cov['total']) + ' oturum' + d7_text + ')'
                         + unobserved_text)
        hermes_without_terminal = cov.get('hermes_without_terminal', 0) if isinstance(cov, dict) else 0
        if hermes_without_terminal > 0:
            lines.append('Hermes: ' + str(hermes_without_terminal) +
                         ' oturumda son tur için kapanış sinyali gözlenmedi (oturum kaydı kapsamı dışında).')
        kf = result.get('knowledge_freshness')
        if isinstance(kf, dict) and kf.get('status') == 'unavailable':
            lines.append('Son bilgi damıtması: ölçülemedi (' + plain_text(kf.get('error') or 'hata') + '); daha sonra tekrar deneyin.')
        elif isinstance(kf, dict):
            if kf.get('last_distilled_at') is not None:
                days = kf.get('days_ago', 0)
                time_text = 'bugün' if days == 0 else ('1 gün önce' if days == 1 else str(days) + ' gün önce')
                lines.append('Son bilgi damıtması: ' + time_text + ' (o tarihten beri ' + str(kf.get('receipts_since', 0)) + ' oturum kaydı)')
            else:
                lines.append('Son bilgi damıtması: henüz kavram notu damıtılmadı (toplam ' + str(kf.get('receipts_since', 0)) + ' oturum kaydı; çalıştıkça oluşur)')
        scheduled = result.get('scheduled_check') or {}
        if scheduled.get('status') == 'ok':
            lines.append('Zamanlanmış denetim (bilgi): son kontrol ' + str(scheduled.get('age_hours', '?')) + ' saat önce, sağlıklı.')
        elif scheduled.get('status') in ('needs_attention', 'error'):
            # Its sync findings come from a sync this report does not run: name them and the way to see them now.
            found = scheduled.get('issues') if isinstance(scheduled.get('issues'), dict) else {}
            kinds = ', '.join(label + ' (' + str(found[kind]) + ')' for kind, label in _SCHEDULED_ISSUES if kind in found)
            lines.append('Zamanlanmış denetim: son kontrol ' + str(scheduled.get('age_hours', '?')) + ' saat önce, ' +
                         ('dikkat gerekiyor' + (' (' + kinds + ')' if kinds else '') if scheduled['status'] == 'needs_attention'
                          else 'denetim tamamlanamadı') +
                         '. Güncel durumu görmek için: ' + py_cmd + ' beyin.py sync')
        for conflict in result.get('instruction_conflicts', []):
            lines.append('Talimat çelişkisi: ' + plain_text(conflict['file']) + ' içinde V2 derleyici ifadesi var ("' +
                         plain_text(conflict.get('snippet', '')) + '"). Bu metin V3 ajan damıtmasıyla çelişiyor; eski derleyici talimatlarını kaldırın.')
        if result.get('receipt_view_conflicts'):
            lines.append('Oturum özeti görünümü çakışması: ' + ', '.join(plain_text(c['source']) for c in result['receipt_view_conflicts']) +
                         '. Görünümleri arşivleyip yeniden üretmek için: ' + py_cmd + ' beyin.py sync --recover-views')
        if result.get('skill_conflicts'):
            lines.append('Beceri (skill) kopyaları ayrıştı: ' + ', '.join(plain_text(x) for x in result['skill_conflicts']) +
                         '. İki sürüm de korundu; hangisini tutacağınıza karar verin.')
        if result.get('skill_unmanaged'):
            lines.append('Beceri klasöründeki yönetilmeyen girdiler (bilgi): ' + ', '.join(plain_text(x) for x in result['skill_unmanaged']) +
                         '; bir şey yapmanıza gerek yok.')
        if result.get('excluded_components'):
            lines.append('Hariç tutulan bileşenler: ' + ', '.join(plain_text(x) for x in result['excluded_components']) + '.')
        if result.get('pending_exclusions') or result.get('exclusions_pending'):
            lines.append('Hariç tutma değişikliği bekliyor (' + ', '.join(plain_text(x) for x in result.get('pending_exclusions', [])) +
                         '): bir sonraki kurulum ya da güncellemede uygulanır.')
        if result.get('exclusions_error'):
            lines.append('.beyin-exclusions.json geçersiz; düzeltilene kadar güncelleme durur: ' + plain_text(result['exclusions_error']))
        hygiene = result.get('companion_hygiene') or {}
        if hygiene.get('status') == 'unavailable':
            lines.append('Hafıza hijyeni ölçülemedi (' + plain_text(hygiene.get('error', 'hata')) + '); daha sonra tekrar deneyin.')
        parallel_on = isinstance(result.get('parallel_sessions'), dict) and result['parallel_sessions'].get('enabled') is True
        hygiene_entries = _companion_hygiene_entries(hygiene)
        # --last-session-chars is one limit for every Last-Session.md: suggest the one size that fits them all.
        session_sizes = [entry['chars'] for _, file_name, entry, _ in hygiene_entries if file_name == 'Last-Session.md'
                         and isinstance(entry.get('chars'), int) and isinstance(entry.get('limit'), int)]
        suggested = ((max(session_sizes) + 999) // 1000) * 1000 if session_sizes else 0
        for label, file_name, entry, project in hygiene_entries:
            sized = isinstance(entry.get('chars'), int) and isinstance(entry.get('limit'), int)
            prefix = ('Hafıza hijyeni: ' + plain_text(label) +
                      (f' {entry["chars"]} karakter (sınır {entry["limit"]}). ' if sized else ' sınırı aşıyor. '))
            if parallel_on and file_name == 'Last-Session.md' and sized:
                own = ((entry['chars'] + 999) // 1000) * 1000
                if own <= 200000:  # beyin_v3_companion.LIMIT_RANGE upper bound
                    advice = (f'Bu kartlar hala gerekliyse: "{py_cmd} beyin.py preferences '
                              f'--last-session-chars {suggested if suggested <= 200000 else own}". ')
                else:
                    advice = 'Dosya desteklenen sınır tavanını aşıyor; arşivlemeden önce devir kartlarını gözden geçirin. '
                lines.append(prefix + 'Paralel oturum bildirimi açık; diğer oturumların taze kartlarını yalnız bu uyarıyı '
                             'kaldırmak için sıkıştırmayın. ' + advice +
                             ('Diğer oturumlar bittikten sonra eski kayıtları ayrı bir arşiv notuna taşıyın; '
                              'companion-compact bu proje dosyasını kapsamaz.' if project else
                              'Sıkıştırmayı diğer oturumlar bittikten sonra yapın.'))
            elif project:
                # companion-compact only shortens the main companion folder; naming it here sends the
                # user to a command that leaves this file untouched.
                lines.append(prefix + 'companion-compact yalnız ana companion klasörünü kısaltır, bu proje dosyasını '
                             'kapsamaz; ajanınızdan eski kayıtları silmeden ayrı bir arşiv notuna taşımasını isteyin.')
            else:
                lines.append(prefix + 'Ajanınıza "' + py_cmd +
                             ' beyin.py companion-compact" çalıştırmasını söyleyin; eski kayıtlar arşive taşınır, metin silinmez.')
        storage = result.get('state_storage') or {}
        if storage.get('compact_suggested'):
            lines.append('Yerel indeks (bilgi): ' + megabytes(storage['database_bytes']) + ' MB, bunun ' +
                         megabytes(storage['free_bytes']) + ' MB boş alan. Diske geri vermek için: ' +
                         py_cmd + ' beyin.py state-compact')
        rules_opening = result.get('companion_rules_opening') or {}
        if rules_opening.get('status') == 'stale':
            lines.append('Kurallar.md kaynak doğrulaması güncel değil (bilgi); açılış ölçümü için ' + py_cmd + ' beyin.py sync çalıştırın.')
        if rules_opening.get('status') == 'clipped':
            lines.append('Kurallar.md açılışta kırpılıyor: kural metni ' + str(rules_opening['chars']) +
                         ' karakter; Claude Code açılışı bu ayarlarla (' + str(rules_opening['budget']) +
                         ' karakter) en fazla ' + str(rules_opening['share']) + ' karakterini tam gösterir' +
                         (' (gerekçeler de çıkarıldı)' if rules_opening.get('reasons_omitted') else '') +
                         '. Oturuma özel bildirimler gerçek açılışta bu payı azaltabilir. '
                         'Kuralları kısa tutun; uzun gerekçeyi ve ayrıntıyı ayrı bir nota (ör. Kurallar-Detay.md) taşıyın.')
            omitted = rules_opening.get('omitted_rules')
            if omitted:
                lines.append(f'Gösterilmeyen kural sıraları {omitted["first"]}-{omitted["last"]} '
                             f'({omitted["count"]}/{omitted["total"]}).')
            if rules_opening.get('pinned_omitted'):
                lines.append(pinned_overflow(rules_opening['pinned_omitted']))
        for proj in rules_opening.get('project_rules') or []:
            source = plain_text(proj.get('source') or proj.get('project') or 'Kurallar.md')
            if proj.get('status') == 'stale':
                lines.append('Proje Kurallar.md kaynak doğrulaması güncel değil (bilgi): ' + source +
                             '; açılış ölçümü için ' + py_cmd + ' beyin.py sync çalıştırın.')
            elif proj.get('status') == 'clipped':
                lines.append('Proje Kurallar.md açılışta kırpılıyor: ' + source + ' ' +
                             str(proj['chars']) + ' karakter; Claude Code proje açılışı bu ayarlarla (' +
                             str(rules_opening.get('budget')) + ' karakter) en fazla ' + str(proj['share']) +
                             ' karakterini tam gösterir' +
                             (' (gerekçeler de çıkarıldı)' if proj.get('reasons_omitted') else '') +
                             '. Kuralları kısa tutun; eski veya uzun ayrıntıyı arşiv ya da detay notuna taşıyın.')
                p_omitted = proj.get('omitted_rules')
                if p_omitted:
                    lines.append(f'Gösterilmeyen proje kural sıraları ({source}) '
                                 f'{p_omitted["first"]}-{p_omitted["last"]} '
                                 f'({p_omitted["count"]}/{p_omitted["total"]}).')
                if proj.get('pinned_omitted'):
                    lines.append(pinned_overflow(proj['pinned_omitted'], source))
        checked, total = rules_opening.get('project_rules_checked'), rules_opening.get('project_rules_total')
        if isinstance(checked, int) and isinstance(total, int) and checked < total:
            lines.append(f'Proje Kurallar.md ölçümü ilk {checked} dosyayla sınırlı (bilgi): {total} dosyanın '
                         f'{total - checked} tanesi ölçülmedi.')
        references = result.get('instruction_references') or {}
        if references.get('status') == 'unavailable':
            lines.append('Talimat ve beceri bağlantı denetimi tamamlanamadı (bilgi): ' +
                         plain_text(references.get('error', 'bilinmeyen hata')) + '.')
        elif references.get('dead_count'):
            shown = ', '.join(plain_text(entry['file']) + ':' + str(entry['line']) + ' -> ' + plain_text(entry['target'])
                              for entry in references['dead'][:3])
            more = references['dead_count'] - min(3, len(references['dead']))
            lines.append('Talimat ve beceri dosyalarında kırık bağlantı (bilgi): ' + shown +
                         (' ve ' + str(more) + ' tane daha' if more > 0 else '') + '.')
        supersedes = result.get('supersedes') or {}
        lines += supersedes_lines(supersedes.get('dead'), supersedes.get('dead_count'))
        boundary_report = result.get('boundary') or {}
        boundary_lines = {
            'parent_obsidian_index': 'Kök sınırı (bilgi): üst klasörde de bir .obsidian var; Obsidian üst klasörü açıp bu kasayı alt klasör sayabilir.',
            'no_root_obsidian': 'Kök sınırı (bilgi): kasa kökünde .obsidian yok; Obsidian\'da tam olarak bu klasörü açın.',
        }
        for finding in boundary_report.get('findings') or []:
            code = str(finding).split(':', 1)[0]
            if code == 'kasa_excluded':
                lines.append('Kasa sınıfı klasör (bilgi): ' + ', '.join(plain_text(name) for name in boundary_report.get('sensitive_excluded') or []) +
                             '; hijyen taramaları bunları atlar, tam garanti için kaynaklarına visibility: private ekleyin.')
            elif code == 'code_inside_vault':
                lines.append('Kök sınırı (bilgi): kasa içinde kod klasörü var (' + ', '.join(plain_text(name) for name in boundary_report.get('code_dirs', [])[:5]) +
                             '); kod deposunu hafıza kasasının dışında tutun.')
            elif code == 'nested_git_repository':
                lines.append('Kök sınırı (bilgi): kasa içinde ayrı Git deposu var (' + ', '.join(plain_text(name) for name in boundary_report.get('nested_repositories', [])[:3]) + ').')
            elif code == 'backup_artifacts':
                lines.append('Kök sınırı (bilgi): kökte yedek ya da eşitleme kopyası var (' + ', '.join(plain_text(name) for name in boundary_report.get('backup_artifacts', [])[:5]) +
                             '); özel bir kopya taşıyor olabilir.')
            elif code in boundary_lines:
                lines.append(boundary_lines[code])
        review = result.get('review') or {}
        if review.get('due_count'):
            shown = ', '.join(plain_text(entry['source']) + ' (' + str(entry['days_overdue']) + ' gün)' for entry in review['due'][:3])
            more = review['due_count'] - min(3, len(review['due']))
            lines.append('Yeniden bakma tarihi gelen not (review_at, bilgi): ' + shown +
                         (' ve ' + str(more) + ' tane daha' if more > 0 else '') + '.')
        if review.get('invalid_count'):
            lines.append('Gerçek tarih olmayan review_at (bilgi): ' +
                         ', '.join(plain_text(entry['source']) for entry in review['invalid'][:3]) + '.')
        closed = result.get('closed_tasks') or {}
        if closed.get('closed_count'):
            shown = ', '.join(plain_text(entry['source']) + ' (' + str(entry['days_old']) + ' gün)' for entry in closed.get('closed')[:3])
            more = closed['closed_count'] - min(3, len(closed.get('closed') or []))
            lines.append(str(closed.get('days', 30)) + ' günden eski kapalı görev tasks/ altında duruyor (bilgi, taşıma sizin kararınız): ' + shown +
                         (' ve ' + str(more) + ' tane daha' if more > 0 else '') + '.')
        cap = result.get('word_cap') or {}
        if cap.get('over_count'):
            shown = ', '.join(plain_text(entry['file']) + ' (' + str(entry['words']) + ' kelime)' for entry in cap.get('over', [])[:3])
            more = cap['over_count'] - min(3, len(cap.get('over') or []))
            lines.append('Kelime tavanını (' + str(cap['cap']) + ') aşan not (bilgi): ' + shown +
                         (' ve ' + str(more) + ' tane daha' if more > 0 else '') + '.')
        promo = result.get('promotion') or {}
        if promo.get('hot'):
            lines.append('Sıcak klasörler (son ' + str(promo.get('window_days', 30)) + ' gün dokunma): ' +
                         ', '.join(plain_text(entry['folder']) + ' (' + str(entry['touches']) + ')' for entry in promo['hot'][:3]))
        if promo.get('cold'):
            lines.append('Soğuk klasörler (terfi kararı sizin): ' +
                         ', '.join(plain_text(entry['folder']) + ' (' + str(entry['days_quiet']) + ' gün)' for entry in promo['cold'][:3]))
        waiting = [entry for entry in (result.get('inbox') or {}).get('folders') or []
                   if entry.get('attention') or entry.get('error')]
        if waiting:
            lines.append('Gelen kutusunda bekleyen (bilgi, işleme kararı sizin): ' + ', '.join(
                plain_text(entry['folder']) + (' (okunamadı: ' + plain_text(entry['error']) + ')' if entry.get('error') else
                                               ' (' + str(entry['notes']) + ' not, en eskisi ' + str(entry['oldest_days']) + ' gün)')
                for entry in waiting[:3]) + (' ve ' + str(len(waiting) - 3) + ' klasör daha' if len(waiting) > 3 else '') + '.')
        parallel = result.get('parallel_sessions') or {}
        if parallel.get('enabled'):
            lines.append('Paralel oturum bildirimi: açık (son 45 dakikada etkin ' + str(parallel.get('active', 0)) + ' oturum işareti)')
        elif parallel.get('valid') is False:
            lines.append('parallel-sessions.json geçersiz; paralel oturum bildirimi kapalı sayılıyor, dosyayı kontrol edin.')
        dedupe = result.get('context_dedupe') or {}
        if dedupe.get('enabled'):
            lines.append('Oturum içi tekrar önleme: açık (' + str(dedupe.get('sessions', 0)) + ' oturum defteri)')
        elif dedupe.get('valid') is False:
            lines.append('context-dedupe.json geçersiz; oturum içi tekrar önleme kapalı sayılıyor, dosyayı kontrol edin.')
        usage = result.get('note_usage') or {}
        if isinstance(usage, dict) and usage.get('tracked', 0) > 0:
            totals = usage.get('feedback_totals', {})
            lines.append(
                f"Not kullanım takibi: {usage.get('tracked', 0)} kayıt, "
                f"{usage.get('dead_weight_count', 0)} hiç kullanılmayan not, "
                f"geri bildirim [faydalı: {totals.get('ok', 0)}, yanlış: {totals.get('wrong', 0)}, eskimiş: {totals.get('stale', 0)}]"
            )

        queue = result.get('mutation_queue') or {}
        if queue.get('valid') is False:
            lines.append('mutation-queue-settings.json geçersiz; çevrimdışı kuyruk kapalı sayılıyor, dosyayı kontrol edin.')
        if queue.get('enabled') or queue.get('queue_exists'):
            steps = []
            if queue.get('status') != 'invalid':
                if queue.get('pending_count'):
                    steps.append('kasaya uygulamak için: ' + py_cmd + ' beyin.py mutation-queue --replay')
                reviewed = [name for name, key in (('conflict', 'conflict_count'), ('failed', 'failed_count')) if queue.get(key)]
                if reviewed:
                    steps.append('incelemek için: ' + py_cmd + ' beyin.py mutation-queue --list' +
                                 (' --status ' + reviewed[0] if len(reviewed) == 1 else '') +
                                 '; inceledikten sonra kayıt kimliğiyle --retry ya da --dismiss')
            lines.append('Çevrimdışı işlem kuyruğu: ' + ('açık' if queue.get('enabled') else 'kapalı') +
                         (' (kuyruk dosyası okunamadı)' if queue.get('status') == 'invalid' else
                          ' (' + str(queue.get('pending_count', 0)) + ' bekleyen, ' + str(queue.get('conflict_count', 0)) +
                          ' çakışma, ' + str(queue.get('failed_count', 0)) + ' başarısız)') +
                         ''.join('; ' + step for step in steps))
        devices = result.get('devices') or {}
        if devices.get('valid') is False:
            lines.append('devices.json geçersiz; çoklu cihaz düzeni kapalı sayılıyor, dosyayı kontrol edin.')
        elif devices.get('enabled'):
            lines.append('Çoklu cihaz: ' + plain_text(devices.get('level')) +
                         (' / ' + plain_text(devices['phone_sync']) if devices.get('phone_sync') else ''))
        index_scope = result.get('index_exclusions') or {}
        lines += index_exclusion_lines(index_scope)
        hint = index_scope.get('vendored_hint') or {}
        if hint.get('notes'):
            more = len(hint.get('dirs') or []) - 3
            lines.append('İndeks kapsamı (bilgi): başka projelerin kod kopyası gibi görünen klasörlerde ' + str(hint['notes']) +
                         ' not indeksli (' + ', '.join(plain_text(entry['dir']) + ': ' + str(entry['notes'])
                                                       for entry in (hint.get('dirs') or [])[:3]) +
                         (' ve başka klasörler' if more > 0 or hint.get('truncated') else '') +
                         '). Sizin notunuz değilse indeks dışında tutun: ' + py_cmd +
                         ' beyin.py preferences ' + ' '.join('--index-exclude-dir ' + plain_text(name)
                                                            for name in hint.get('names') or []))
        arch_hint = index_scope.get('archive_hint') or {}
        if arch_hint.get('notes'):
            more = len(arch_hint.get('dirs') or []) - 3
            exclude_args = ' '.join('--index-exclude-dir ' + (('"' + plain_text(name) + '"') if ' ' in name else plain_text(name))
                                    for name in arch_hint.get('names') or [])
            lines.append('Arşiv kapsamı (bilgi): arşiv klasörlerinde emekli edilmemiş (canlı sayılan) ' + str(arch_hint['notes']) +
                         ' not indeksli (' + ', '.join(plain_text(entry['dir']) + ': ' + str(entry['notes'])
                                                       for entry in (arch_hint.get('dirs') or [])[:3]) +
                         (' ve başka klasörler' if more > 0 or arch_hint.get('truncated') else '') +
                         '). Bağlamdan uzak tutmak için notlara "status: archived" yazın veya klasörü indeks dışında tutun: ' +
                         py_cmd + ' beyin.py preferences ' + exclude_args)
        lines += state_location_lines(result.get('state_location'))
        lines += receipt_line_endings_lines(result.get('receipt_line_endings'))
        backup = result.get('backup') or {}
        if backup.get('status') == 'remote_configured':
            lines.append('Yedek (bilgi): Git uzak adresi ayarlı; güncel ve geri yüklenebilir ikinci kopya doğrulanmadı. docs/v3/BACKUP.md')
        elif backup:
            lines.append('Yedek (bilgi): Bu kasanın ikinci kopyası doğrulanamadı; başka yolla aldığınız yedek burada görünmez. Disk arızasına karşı ayrı bir kopya tutun. docs/v3/BACKUP.md')

        paths = result.get('hook_paths') or {}
        if paths.get('status') == 'stale':
            lines.append('Kanca (hook) yolları başka bir kuruluma işaret ediyor (' + str(len(paths.get('stale', []))) + ' eski yol). ' +
                         'Güncellemek için: ' + py_cmd + ' beyin.py hook-reconcile')
        tracked = result.get('tracked_machine_files') or {}
        bridges = result.get('global_bridge_paths') or {}
        if bridges.get('status') == 'stale':
            harnesses = sorted({row.get('harness') for row in bridges.get('stale', [])
                                if row.get('harness') in ('claude', 'codex', 'antigravity', 'opencode')})
            commands = ', '.join('bridge --harness ' + harness + ' ... --install' for harness in harnesses)
            lines.append('Genel köprü yolları eski kuruluma işaret ediyor (bilgi): ' + commands +
                         '. Yeni kasa içinden önceki --project-root, --event ve --context-chars seçimlerini koruyarak '
                         'yeniden kurun. Genel ayarlar değiştirilmedi.')
        if tracked.get('status') == 'warning':
            lines.append('Git makineye özel kurulum dosyalarını izliyor (bilgi): ' +
                         ', '.join(plain_text(name) for name in tracked.get('tracked', [])) +
                         '. Her makinenin kendi yedeğini koruyarak docs/v3/MULTI-MACHINE.md rehberini inceleyin; otomatik değişiklik yapılmadı.')
        elif tracked.get('status') == 'skipped':
            skip_reasons = {
                'not_git_repository': 'Bu klasör bir Git deposu değil; ek bir işlem gerekmiyor.',
                'not_worktree': 'Bu klasör bir Git çalışma alanı değil; ek bir işlem gerekmiyor.',
                'git_unavailable': 'Bu bilgisayarda Git bulunamadı; ek bir işlem gerekmiyor.',
            }
            lines.append('Git kontrolü (bilgi): ' + skip_reasons.get(
                tracked.get('reason'), 'Bu klasörde Git kontrolü uygulanmadı; ek bir işlem gerekmiyor.'))
        elif tracked.get('status') == 'unavailable':
            unavail_reasons = {
                'sparse_checkout_or_index': 'Kısmi Git çalışma alanı olduğu için kurulum dosyaları denetlenemedi; sorun yaşıyorsanız Git ayarlarını kontrol edin.',
                'git_timeout': 'Git zamanında yanıt vermediği için kurulum dosyaları denetlenemedi; daha sonra tekrar deneyin.',
            }
            lines.append('Git kontrolü (bilgi): ' + unavail_reasons.get(
                tracked.get('reason'), 'Git bilgisi okunamadığı için kurulum dosyaları denetlenemedi; sorun yaşıyorsanız Git durumunu kontrol edin.'))
        scopes = result.get('project_scopes') or {}
        if scopes.get('status') == 'error' and scopes.get('error'):
            lines.append('Proje kapsamları (.beyin-projects.json) geçersiz: ' + plain_text(scopes['error']) +
                         '. Dosyayı düzeltin.')
        elif scopes.get('status') in ('error', 'warning'):
            # Entry-level problems: name each entry instead of a bare "hata" (#419).
            details = []
            if scopes.get('missing_folders'):
                details.append('kasada bulunamayan klasörler: ' + ', '.join(plain_text(f) for f in scopes['missing_folders']))
            if scopes.get('duplicate_folders'):
                details.append('birden fazla projeye atanan klasörler: ' + ', '.join(plain_text(f) for f in scopes['duplicate_folders']))
            if scopes.get('invalid_entries'):
                details.append('geçersiz girdiler: ' + ', '.join(plain_text(f) for f in scopes['invalid_entries']))
            if scopes.get('duplicate_folders') or scopes.get('invalid_entries'):
                details.append('bu girdiler atlanıyor, adlandırdıkları kasa klasörleri hiçbir projeye açılmıyor' +
                               (' ve shared_unscoped düzeltilene kadar kapalı' if scopes.get('shared_unscoped') else ''))
            lines.append('Proje kapsamları (.beyin-projects.json) dikkat gerektiriyor: ' + '; '.join(details) +
                         '. Düzenlemek veya temizlemek için: ' + py_cmd + ' beyin.py project-scope prune')
        if attention_count > 0:
            lines.append('Ajanınıza "beyin doktor" diyerek ayrıntıyı inceletebilirsiniz.')
        return '\n'.join(lines + update_lines(result.get('updates', {})))
    if status == 'recovery_required':
        return '\n'.join(update_lines(result))
    if result.get('verification') == 'metadata_only':
        return '\n'.join(update_lines(result) + ['Yalniz surum bilgisi kontrol edildi; paket kurulumu denenmedi.'])
    if command == 'update' and status == 'plan':
        lines = [f"Güncelleme önizlemesi {result['current_version']} -> {result['version']}"]
        labels = {'added': 'Eklenecek', 'updated': 'Değişecek', 'removed': 'Kaldırılacak'}
        for name, action in result['files'].items():
            if action in labels:
                lines.append(labels[action] + ': ' + name)
        if result.get('trust_review_files'):
            lines.append('Kanca onayı gerekecek: ' + ', '.join(result['trust_review_files']))
        return '\n'.join(lines)
    if status == 'updated':
        message = 'Beyin guncellendi: ' + str(result.get('from_version', installed_version or '?')) + ' -> ' + str(result['version'])
        if result.get('after_update_steps'):
            step_lines = ['Guncelleme sonrasi adimlar:']
            for step in result['after_update_steps']:
                text = step.get('text') or step.get('summary', '')
                cmd = step.get('command')
                line = '  - ' + text
                if cmd:
                    line += f' ({cmd})'
                step_lines.append(line)
            message += '\n' + '\n'.join(step_lines)
        if result.get('removed'):
            message += '\nHaric tutulan bilesenler kaldirildi: ' + ', '.join(result['removed']) + '.'
        if result.get('preserved_excluded'):
            message += '\nHaric tutulan ancak degistirilmis dosyalar korundu: ' + ', '.join(result['preserved_excluded']) + '.'
        if result.get('orphaned_legacy_state'):
            message += ('\nYarida kesilmis ' + str(len(result['orphaned_legacy_state'])) + ' V2 flush kaydi var (durumu inflight,'
                        ' yazan surec artik calismiyor); engel sayilmadi, dosyalara dokunulmadi.'
                        ' O oturumlarin ozeti daily/ altina yazilmamis olabilir.')
        if result.get('stale_legacy_locks'):
            message += ('\nSahipsiz kalmis ' + str(len(result['stale_legacy_locks'])) + ' V2 kilit klasoru var (prompt_count.*.lock,'
                        ' sekiz gunden eski); engel sayilmadi, dokunulmadi.')
    elif status == 'available':
        message = 'Yeni surum var: ' + str(result.get('current_version', '?')) + ' -> ' + str(result['version']) + '\nGuncellemek icin: python beyin.py update'
    elif status == 'noop':
        message = 'Beyin guncel: ' + str(result.get('version', installed_version or '?'))
    elif status == 'dismissed':
        message = 'Bu surumun oturum bildirimi susturuldu: ' + result['version']
    elif status == 'uninstalled':
        message = 'Kurulum geri alindi. Kullanici notlari korundu.'
    elif status == 'rolled_back':
        message = 'Onceki surume donuldu: ' + str(result.get('version', '?'))
    elif status == 'recovered':
        message = 'Yarim kalan islem kurtarildi. Surum: ' + str(result.get('version', '?'))
    elif command == 'context':
        records = result.get('records', [])
        message = '\n\n'.join(str(r.get('source', '')) + '\n' + str(r.get('text', '')) for r in records) or 'Eslesen kaynak bulunamadi.'
    elif command == 'project-scope':
        action = result.get('action', 'list')
        if action == 'list':
            if result.get('status') == 'absent':
                return 'Proje kapsamlari (.beyin-projects.json) bulunamadi.'
            if result.get('error'):
                return 'Proje kapsamlari (.beyin-projects.json) gecersiz: ' + plain_text(result['error'])
            lines = ['Proje kapsamlari (.beyin-projects.json):']
            folders = result.get('folders', {})
            missing = set(result.get('missing_folders', []))
            for f, p in sorted(folders.items()):
                extra = ' (kasada bulunamadi)' if f in missing else ''
                lines.append('  ' + plain_text(f) + ' -> ' + plain_text(p) + extra)
            for f in result.get('missing_folders', []):
                if f not in folders:
                    lines.append('  ' + plain_text(f) + ' (kasada bulunamadi)')
            # Skipped entries (#419): named here, closed to every project until fixed.
            for entry in result.get('invalid_entries', []) + result.get('duplicate_folders', []):
                lines.append('  ' + plain_text(entry) + ' (atlandi)')
            shared = result.get('shared_unscoped')
            if shared and result.get('shared_unscoped_effective') is False:
                lines.append('  shared_unscoped: kapali (atlanan girdiler duzeltilene kadar)')
            else:
                lines.append('  shared_unscoped: ' + ('acik' if shared else 'kapali'))
            return '\n'.join(lines)
        elif action == 'add':
            # add is the only command that changes shared_unscoped (#607): show the saved value.
            return ('Klasor proje kapsamina eklendi: ' + plain_text(result.get('folder', '')) + ' -> ' +
                    plain_text(result.get('project', '')) +
                    '\n  shared_unscoped: ' + ('acik' if result.get('shared_unscoped') else 'kapali'))
        elif action == 'remove':
            return 'Klasor proje kapsamindan cikarildi: ' + plain_text(result.get('folder', ''))
        elif action == 'prune':
            pruned = result.get('pruned', [])
            if not pruned:
                return 'Temizlenecek eksik klasor bulunmadi; tum klasorler kasada mevcut.'
            return 'Kasada bulunmayan ' + str(len(pruned)) + ' klasor kapsamdan cikarildi: ' + ', '.join(plain_text(f) for f in pruned)
    elif command == 'bridge':
        status = result.get('status')
        harness = result.get('harness', 'bilinmiyor')
        if status == 'installed':
            lines = ['Global kopru kuruldu (' + harness + '):']
            if result.get('hook_file'):
                lines.append('  Hook ayari: ' + plain_text(result['hook_file']))
            if result.get('gemini_md'):
                lines.append('  Global yonerge: ' + plain_text(result['gemini_md']))
            if result.get('project_roots'):
                lines.append('  Kapsanan proje kokleri: ' + ', '.join(plain_text(r) for r in result['project_roots']))
            if result.get('replaced_owner'):
                lines.append('Baska bir kasaya ait kopru ezildi (' + plain_text(result['replaced_owner']) + '); .bak yedegi alindi.')
            else:
                lines.append('Mevcut kullanici ayarlari korundu; .bak yedegi alindi.')
            return '\n'.join(lines)
        elif status == 'uninstalled':
            lines = ['Global kopru kaldirildi (' + harness + '):']
            if result.get('hook_file'):
                lines.append('  Hook ayari: ' + plain_text(result['hook_file']))
            lines.append('  Kaldirilan kopru girdisi sayisi: ' + str(result.get('removed_count', 0)))
            lines.append('Kullaniciya ait diger ayarlar korundu.')
            return '\n'.join(lines)
    else:
        message = 'Islem sonucu: ' + str(status or 'tamamlandi')
        if command in ('sync', 'recover-views'):
            if result.get('recovered_views'):
                recovered_lines = ['Cakisan receipt gorunumleri arsivlendi ve yeniden uretildi:']
                for item in result['recovered_views']:
                    recovered_lines.append('  - ' + plain_text(item['source']) + ' -> ' + plain_text(item['archive']))
                message = '\n'.join([message] + recovered_lines)
            message = '\n'.join([message] + index_exclusion_lines(result.get('index_exclusions'), pruned=True) +
                                (sync_copy_lines(result) if command == 'sync' else []) +
                                supersedes_lines(result.get('supersedes_issues'), result.get('supersedes_issue_count')))
    if result.get('trust_review_required'):
        message += '\n' + trust_review_line(result.get('trust_review_files'))
    if result.get('view_conflicts'):
        message += '\nReceipt gorunumleri korundu: ' + ', '.join(plain_text(c['source']) for c in result['view_conflicts']) + '. Kurtarma: beyin.py sync --recover-views'
    return message


# Hook definition file -> the step its client needs before the new hooks run (SETUP-V3.md step 5).
TRUST_REVIEW = {
    '.codex/hooks.json': 'Codex: /hooks guven incelemesi',
    '.kiro/hooks/beyin-v3.json': 'Kiro: Agent Hooks paneli veya CLI /hooks incelemesi',
    '.agents/hooks.json': 'Antigravity: vault klasorune guven',
    '.claude/settings.local.json': 'Claude Code: ek inceleme yok',
}


def trust_review_line(files):
    """Name only the clients whose hook definitions changed; an older updater gives no list."""
    changed = [name for name in TRUST_REVIEW if name in (files or ())]
    if not changed:
        return 'Hook tanimi degisti: kullandigin istemcide guven incelemesini tamamla ve yeni oturum ac.'
    return ('Hook tanimi degisti (' + ', '.join(changed) + '). ' +
            '; '.join(TRUST_REVIEW[name] for name in changed) + '. Ardindan yeni oturum ac.')


def print_error(error):
    """--json error on stderr with real characters as UTF-8 (#587), like the CLI's print_error.
    Kept here because a broken .beyin-runtime.json fails before the CLI module is loaded."""
    if hasattr(sys.stderr, 'reconfigure'):
        try:
            sys.stderr.reconfigure(encoding='utf-8')
        except (OSError, ValueError):
            pass
    # Control characters (DEL, C1), bidi and other format controls, line separators and lone
    # surrogates stay escaped.
    text = ''.join(json.dumps(ch)[1:-1] if unicodedata.category(ch) in ('Cc', 'Cf', 'Cs', 'Zl', 'Zp') else ch
                   for ch in json.dumps(error, ensure_ascii=False))
    try:
        print(text, file=sys.stderr)
    except UnicodeEncodeError:
        print(json.dumps(error, ensure_ascii=True), file=sys.stderr)


def load_cli(directory):
    """The installed CLI module, loaded from its file like every other entry-point command."""
    spec = importlib.util.spec_from_file_location('beyin_installed_cli', directory / 'beyin_v3_cli.py')
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    return cli


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    force_json = '--json' in argv or '--print-config' in argv
    human = ('--human' in argv or sys.stdout.isatty()) and not force_json
    argv = [arg for arg in argv if arg not in ('--human', '--json')]
    if human:
        # A piped or redirected Windows console uses a legacy code page (cp1252 has no s-cedilla);
        # write redirected human output as UTF-8 so stored text and paths survive intact.
        # Keep the interactive terminal's encoding and replacement behavior.
        for stream in (sys.stdout, sys.stderr):
            if hasattr(stream, 'reconfigure'):
                try:
                    if stream.isatty():
                        stream.reconfigure(errors='replace')
                    else:
                        stream.reconfigure(encoding='utf-8', errors='replace')
                except (OSError, ValueError):
                    pass
    command = argv[0] if argv else 'doctor'
    if command in ('note-read', 'note-edit'):
        human = False  # These commands have a JSON contract, including on a terminal.
    vault = Path(__file__).resolve().parent
    stamp = vault / '.beyin-version'
    installed_version = None
    try:
        installed_version = stamp.read_text(encoding='utf-8').strip() if stamp.is_file() else None
        config_path = vault / '.beyin-runtime.json'
        if not config_path.is_file():
            raise ValueError('Kurulum ayari eksik; resmi V3 installer ile bu vault kurulumunu tamamlayin.')
        try:
            config = json.loads(config_path.read_text(encoding='utf-8'))
        except ValueError as exc:  # JSONDecodeError or UnicodeDecodeError: name the file (#587)
            raise ValueError('Kurulum ayari okunamadi: .beyin-runtime.json gecerli JSON degil (' + str(exc) + ').') from None
        directory = vault / '.claude/scripts'
        sys.path.insert(0, str(directory))
        cli = None
        raw_state = config.get('state') if isinstance(config, dict) else None
        if isinstance(raw_state, str) and raw_state and Path(raw_state).expanduser().is_absolute():
            cli = load_cli(directory)
            state = cli.resolve_state(vault, Path(raw_state))
            try:
                pinned_state = Path(raw_state).expanduser().resolve()
            except (OSError, ValueError, RuntimeError):
                pinned_state = None
            if state != pinned_state:
                print('Uyari: yabanci varsayilan state yolu bu makinenin yerel state klasorune cozumlendi: ' +
                      plain_text(state) + '. Kalici duzeltme: bu makinede install_v3.py ile kur.', file=sys.stderr)
        else:
            # The installer always pins a resolved absolute path. A pin that is not absolute
            # here came from another OS through a synced vault (a Windows C:\ path on POSIX,
            # a /home path on Windows, #249) or was edited by hand. Read as a relative path it
            # would put the state under the working directory, so this machine's default
            # state is used instead; doctor reports the pin it could not use, and every command says
            # so on stderr (#205): stdout stays the command's own result, JSON included.
            cli = load_cli(directory)
            state = cli.default_state(vault)
            print('Uyari: .beyin-runtime.json icindeki state yolu (' + plain_text(raw_state) + ') bu makinede '
                  'mutlak bir yol degil; vault baska bir isletim sisteminden esitlenmis olabilir. Bu makinenin '
                  'varsayilan state klasoru kullaniliyor: ' + plain_text(state) + '. Kalici duzeltme: bu makinede '
                  'install_v3.py ile kur; hook komutlari da boylece bu makineye baglanir.', file=sys.stderr)
        if argv and argv[0] == 'yakala':
            # Optional capture tool: its own small CLI, no index or sync engine.
            import beyin_v3_yakala as yakala
            return yakala.main((['--json'] if force_json else []) + argv[1:], vault=vault, state=state)
        if argv and argv[0] == 'web':
            # Optional local web view; only this explicit command ever starts it.
            import beyin_v3_web as web
            return web.main((['--json'] if force_json else []) + argv[1:], vault=vault, state=state, cli=cli)
        if argv and argv[0] in ('update', 'rollback', 'recover'):
            import argparse
            import beyin_v3_update as updater
            class _UpdateArgumentParser(argparse.ArgumentParser):
                def error(self, message):
                    if force_json:
                        print_error({'error': 'SystemExit', 'message': message})
                        self.exit(2)
                    super().error(message)

            parser = _UpdateArgumentParser()
            parser.add_argument('command', choices=('update', 'rollback', 'recover'))
            mode = parser.add_mutually_exclusive_group()
            mode.add_argument('--check', action='store_true')
            mode.add_argument('--plan', action='store_true')
            parser.add_argument('--package', type=Path)
            parser.add_argument('--metadata-only', action='store_true')
            parser.add_argument('--dismiss')
            args = parser.parse_args(argv)
            if args.command != 'update' and (args.check or args.plan or args.package or args.metadata_only or args.dismiss is not None):
                parser.error('update options require the update command')
            if args.metadata_only and (not args.check or args.package or args.dismiss is not None):
                parser.error('--metadata-only requires --check and cannot use --package or --dismiss')
            if args.dismiss is not None and (args.check or args.plan or args.package):
                parser.error('--dismiss cannot use --check, --plan or --package')
            if args.command == 'update':
                import beyin_v3_releases as releases
                if args.metadata_only: result = releases.check(vault, state)
                elif args.dismiss is not None: result = releases.dismiss(vault, state, args.dismiss)
                elif args.plan: result = updater.update(vault, state, args.package, preview=True)
                else: result = updater.update(vault, state, args.package, args.check)
            elif args.command == 'rollback': result = updater.rollback(vault, state)
            else: result = updater.recover(vault, state)
            if human:
                print_human(human_result(result, command, installed_version))
            else:
                print(json.dumps(result, ensure_ascii=True, indent=2))
            return 0
        cli = cli or load_cli(directory)
        cli_args = ['--vault', str(vault), '--state', str(state)] + (['--json'] if force_json else []) + (argv or ['doctor'])
        if not human:
            return cli.main(cli_args)
        if hasattr(cli, 'main') and 'return_result' in getattr(getattr(cli, 'main'), '__code__', {}).co_varnames:
            result, code = cli.main(cli_args, return_result=True)
            try:
                message = human_result(result, command, installed_version)
            except (ValueError, TypeError, AttributeError):
                # A result human_result cannot shape (history returns a list) prints as the JSON
                # the captured path below shows, not as a failed command.
                message = ('Islem tamamlanamadi; ayrinti icin ayni komutu --json ile calistir.' if code
                           else json.dumps(result, ensure_ascii=True, indent=2))
        else:
            from contextlib import redirect_stdout, redirect_stderr
            import io
            output, error = io.StringIO(), io.StringIO()
            with redirect_stdout(output), redirect_stderr(error):
                code = cli.main(cli_args)
            value = output.getvalue() if not code else error.getvalue()
            try:
                result = json.loads(value)
                message = human_result(result, command, installed_version)
            except (ValueError, TypeError, AttributeError):
                message = 'Islem tamamlanamadi; ayrinti icin ayni komutu --json ile calistir.' if code else value.strip()
        print_human(message, sys.stderr if code else sys.stdout)
        return code
    except Exception as exc:
        error = {'error': type(exc).__name__, 'message': str(exc)}
        if human:
            print_human(human_result(error, command, installed_version), sys.stderr)
        else:
            print_error(error)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
