#!/usr/bin/env python3
"""Vault-local entry point; configuration contains no credentials."""
import importlib.util
import json
from pathlib import Path
import sys
import unicodedata
sys.dont_write_bytecode = True



JEV_MODES = {'off': 'kapali', 'shadow': 'golge', 'on': 'acik'}


def jev_mode(mode, provider):
    """Laya is shadow-only: a saved or hand-edited `on` still runs as shadow."""
    if provider == 'laya' and mode in ('shadow', 'on'):
        return 'golge (yalniz)'
    return JEV_MODES.get(mode, 'bilinmiyor')


def update_lines(result):
    status = result.get('status', 'unknown')
    if status == 'available':
        command = 'py -3' if sys.platform == 'win32' else 'python3'
        lines = ['Yeni surum: ' + result['version'],
                 'Surum notlari: ' + result['release_url'],
                 'Guncelle: ' + command + ' beyin.py update']
    else:
        messages = {'disabled': 'Surum bildirimleri kapali.', 'unknown': 'Surum kontrolu henuz yapilmadi.',
                'recovery_required': 'Tamamlanmamis guncelleme veya geri alma var. Kurtarmak icin: beyin.py recover',
                'unavailable': 'Surum kontrolu yapilamadi; guncellik dogrulanmadi.',
                'up_to_date': 'Beyin guncel: ' + str(result.get('current_version', '?')),
                'ahead': 'Kurulu surum resmi stable surumden ileride.'}
        lines = [messages.get(status, 'Surum bilgisi alinamadi.')]
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
    prefix = 'State konumu (bilgi): '
    lines = []
    if 'state_split' in codes:
        lines.append(prefix + 'bu vault icin paket yonlendirmesinin iki tarafinda da kurulu state var (' +
                     ', '.join(plain_text(root) for root in location.get('sibling_state_roots') or []) +
                     '). Oturumun hangi yariyi okudugu paket icinde calisip calismamasina bagli;'
                     ' guncel olani secip tasi: https://github.com/avenoxai/avenoxbeyin/blob/main/docs/v3/UPDATE.md')
    if 'pinned_in_package_container' in codes:
        lines.append(prefix + 'sabitlenmis state koku MSIX paket kapsayicisinin (Packages\\...\\LocalCache) icinde;'
                     ' paket sifirlanir ya da baska kimlikle kurulursa bu yol kaybolur. Tasima: https://github.com/avenoxai/avenoxbeyin/blob/main/docs/v3/UPDATE.md')
    if 'pin_resolves_elsewhere' in codes:
        lines.append(prefix + 'sabitlenmis yol bu surecte baska bir dizine cozumleniyor (' +
                     plain_text(location.get('pinned_resolves_here_to')) +
                     '). doctor komutunu paket icinden ve disindan calistirip karsilastir.')
    if 'pinned_state_not_absolute' in codes:
        lines.append(prefix + '.beyin-runtime.json icindeki state yolu bu makinede mutlak bir yol degil; baska bir'
                     ' isletim sisteminden esitlenmis olabilir. beyin.py bu makinenin varsayilan state dizinini'
                     ' kullaniyor. Kurulum dosyalari makineye ozeldir, esitleme: https://github.com/avenoxai/avenoxbeyin/blob/main/docs/v3/MULTI-MACHINE.md')
    if 'pinned_state_empty' in codes:
        lines.append(prefix + 'sabitlenmis state kokunde kurulum bulunamadi. State tasindiysa kurucuyu yeni'
                     ' --state ile yeniden calistir.')
    if 'effective_state_differs' in codes:
        lines.append(prefix + 'bu calisma sabitlenmis kok yerine baska bir state dizinini okuyor.')
    return lines


def receipt_line_endings_lines(report):
    """Doctor line for the receipt line-ending warning (#205); information only, never a status."""
    if isinstance(report, dict) and report.get('status') == 'warning':
        return ['Receipt satir sonu (bilgi): core.autocrlf=true ve receipts/ sabitlenmemis; baska makineden gelen'
                ' receipt CRLF ile cikip ayni receipt\'in "event id collision" vermesine yol acabilir.'
                ' .gitattributes dosyasina "receipts/** -text" ekle: https://github.com/avenoxai/avenoxbeyin/blob/main/docs/v3/MULTI-MACHINE.md']
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

    reasons = {'ambiguous supersedes reference': 'birden fazla notla eslesiyor',
               'unresolved supersedes reference': 'hedef bulunamadi',
               'supersedes reference points to the note itself': 'notun kendisine isaret ediyor',
               'supersedes reference is part of a cycle': 'dongunun parcasi'}
    lines = ['Supersedes uyarisi: ' + str(count) + ' referans su anda hicbir notu emekli etmiyor; '
             'daha once emekli edilen notlar yeniden baglama girebilir.']
    for issue in issues[:3]:
        reason = issue.get('reason')
        lines.append('  - ' + field(issue.get('source')) + ' -> ' + field(issue.get('value')) +
                     ' (' + field(reasons.get(reason, reason)) + ')')
    if count > min(3, len(issues)):
        lines.append('+' + str(count - min(3, len(issues))) + ' referans daha.')
    lines.append('Hedefi ve yonu kontrol et; dogru notu acik id veya tam vault yoluyla belirt. Ayrinti: --json.')
    return lines


def index_exclusion_lines(report, pruned=False):
    """Lines for the user's index exclusions (state/index.json); shared by doctor, sync and preferences."""
    if not isinstance(report, dict):
        return []
    if report.get('status') == 'unavailable':
        return ['Indeks kapsami denetlenemedi (' + plain_text(report.get('error', 'unknown')) +
                '); indeks dislama ayarlari ve klasor ipuclari dogrulanamadi.']
    lines = []
    if report.get('excluded_dirs'):
        lines.append('Indeks disi klasor adlari (senin listen, her derinlikte): ' +
                     ', '.join(plain_text(name) for name in report['excluded_dirs']) +
                     (' (bu taramada ' + str(report.get('pruned', 0)) + ' klasor atlandi)' if pruned else '') + '.')
    if report.get('error'):
        lines.append('index.json gecersiz (' + plain_text(report['error']) +
                     '); klasor haric tutma listesi uygulanmiyor, her sey indeksleniyor.')
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
    return (f'Sabitlenmis (beyin:pin) {count} kural acilis payina sigmiyor{where} (bilgi): sabitlenenler '
            'kural payinin en fazla yarisini kullanir. Isareti en onemli birkac maddeye indir.')


def unreadable_note_reason(reason):
    """Map sync warning reasons to plain, actionable Turkish for doctor --human (#964)."""
    raw = str(reason or '').strip()
    low = raw.lower()
    if not raw:
        return 'okunamadi veya ayristirilamadi'
    if 'unterminated frontmatter' in low:
        return "ust bilgi (frontmatter) '---' kapatilmamis; kapanis '---' satiri ekle"
    if 'not valid utf-8' in low or ('utf-8' in low and any(tok in low for tok in ('codec', 'decode', 'invalid', 'surrogate'))):
        return 'UTF-8 degil (baska kodlama, or. UTF-16/Windows-1254); notu UTF-8 olarak yeniden kaydet'
    if 'unsupported yaml metadata' in low:
        return 'desteklenmeyen veya bozuk YAML ust bilgisi; ust bilgiyi sade YAML veya JSON olarak duzelt'
    if 'frontmatter must be an object' in low:
        return 'ust bilgi (frontmatter) gecerli bir nesne degil; alanlari kontrol et'
    if 'unresolved git conflict markers' in low:
        return 'cozulmemis git cakisma isaretleri (<<<<<<< / ======= / >>>>>>>) var; birlestirmeyi tamamla'
    if low.startswith('nested ') and 'top-level frontmatter' in low:
        parts = raw.split()
        field = plain_text(parts[1]) if len(parts) > 1 else 'alan'
        return f"ust bilgide ic ice '{field}' alani gecersiz; ust duzeye tasi"
    if 'invalid visibility' in low:
        return 'gecersiz visibility degeri; public, internal veya private kullan'
    if 'positive revision required' in low:
        return 'gecersiz revision degeri; 1 veya daha buyuk tam sayi olmali'
    if 'facts must be an object' in low:
        return 'gecersiz facts alani; anahtar-deger nesnesi olmali'
    if low.endswith(' must be a string'):
        field = plain_text(raw[:-len(' must be a string')].strip())
        return f"gecersiz '{field}' alani; metin olmali"
    if 'inference validity must be current or rejected' in low:
        return 'gecersiz validity degeri; current veya rejected olmali'
    if 'rejected_reason required for rejected validity' in low:
        return 'validity: rejected icin rejected_reason alani eksik'
    if 'rejected_at must be an iso date' in low:
        return 'gecersiz rejected_at tarihi; YYYY-MM-DD bicimi kullan'
    if 'supersedes must contain record ids' in low or 'record cannot supersede itself' in low:
        return 'gecersiz supersedes alani; not kimliklerini kontrol et'
    if 'workspace_readers must be distinct' in low:
        return 'gecersiz workspace_readers alani'
    if 'record id required' in low:
        return 'gecersiz id alani; bos olmayan metin olmali'
    if 'task has embedded frontmatter' in low:
        return "gorev govdesinde ikinci '---' ust bilgisi var; duzelt"
    if low.startswith('file sync conflict copy'):
        return 'senkron cakisma kopyasi; asil notla karsilastirip birini sil'
    if low.startswith('file sync copy of a receipt'):
        return 'makbuz senkron cakisma kopyasi; asil makbuzla karsilastirip kopyayi sil'
    if 'symlink source rejected' in low:
        return 'sembolik baglanti (symlink) indekse alinmaz; gercek dosya kullan'
    if 'source outside vault' in low or 'source missing or outside vault' in low:
        return 'dosya vault disinda veya bulunamadi'
    if 'source changed while scanning' in low:
        return 'tarama sirasinda degisti; beyin.py sync ile tekrar dene'
    if 'invalid daily layout' in low:
        return '.beyin-daily-layout.json gecersiz; dosyayi duzelt'
    if 'state regression detected' in low:
        return 'durum zaman damgasi ileride; beyin.py sync --full calistir'
    if any(tok in low for tok in ('expecting property name', 'expecting value', "expecting ','", "expecting ':'",
                                  'unterminated string', 'extra data', 'invalid control character')):
        return 'bozuk JSON ust bilgisi (frontmatter); sozdizimini duzelt'
    if 'permission denied' in low or 'errno 13' in low or 'access is denied' in low:
        return 'dosya okunamadi (izin hatasi)'
    if 'unreadable or invalid source' in low:
        return 'not okunamadi veya ayristirilamadi; beyin.py sync --json ile ayrintiya bak'
    fallback = plain_text(raw)
    return fallback[:117] + '...' if len(fallback) > 120 else fallback


def unreadable_notes_lines(result):
    """Doctor lines for unreadable/warning notes from sync health (#964)."""
    sw = result.get('sync_warnings')
    if isinstance(sw, dict) and ('warnings' in sw or 'count' in sw):
        items = [w for w in (sw.get('warnings') or []) if isinstance(w, dict)]
        raw_count = sw.get('count')
        count = max(raw_count if isinstance(raw_count, int) and not isinstance(raw_count, bool) else 0, len(items))
    else:
        health = result.get('hook-health.json')
        sync_data = health.get('sync') if isinstance(health, dict) else None
        raw = sync_data.get('warnings') if isinstance(sync_data, dict) else None
        items = [
            w for w in (raw or [])
            if isinstance(w, dict)
            and not str(w.get('reason', '')).startswith('possible file sync copy')
            and w.get('kind') not in ('missing_receipt_source', 'receipt_view')
            and w.get('reason') not in ('manual receipt view edit preserved', 'untracked receipt view preserved')
        ]
        count = len(items)
    if not count:
        return []
    limit = 5
    lines = ['Okunamayan veya indekslenmeyen notlar (' + str(count) + '):']
    for item in items[:limit]:
        src = plain_text(item.get('source', '?'))
        if len(src) > 160:
            src = src[:157] + '...'
        lines.append('  - ' + src + ': ' + unreadable_note_reason(item.get('reason')))
    shown = min(limit, len(items))
    if count > shown:
        lines.append('  +' + str(count - shown) + ' not daha (ayrinti icin --json).')
    lines.append('Bu notlar duzeltilene kadar indekste ve baglamda yer almaz. Ayrinti: --json.')
    return lines


def missing_companion_lines(report):
    """Doctor lines for missing expected companion files (#964)."""
    if not isinstance(report, dict):
        return []
    status = report.get('status')
    if status == 'ambiguous_directory':
        return ['Hafiza (companion) klasoru belirsiz: birden fazla companion klasoru var; kullanilacak klasoru netlestir.']
    if status == 'unavailable':
        return ['Hafiza (companion) dosyalari denetlenemedi (' + plain_text(report.get('error', 'unknown')) + ').']
    missing = [name for name in (report.get('missing') or []) if isinstance(name, str) and name.strip()]
    if not missing:
        return []
    identity_missing = bool(report.get('identity_missing'))
    shown = []
    for name in missing:
        if name == 'Core.md' and identity_missing and 'Soul.md' not in missing:
            shown.append('Core.md (veya Soul.md)')
        else:
            shown.append(plain_text(name))
    directory = report.get('directory')
    where = ' (' + plain_text(directory) + ')' if directory else ''
    return ['Eksik hafiza (companion) dosyalari' + where + ': ' + ', '.join(shown) +
            '. Oturum baslangici bu dosyalari yukleyemez; yedekten geri yukle veya yeniden olustur.']


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
        labels = {'never_seen': 'Henuz gercek istemci oturumu gozlenmedi.',
                  'observed_metadata': 'Oturum olaylari gozleniyor.',
                  'pending': 'Bekleyen isler var.', 'needs_attention': 'Kontrol gerektiren bir sorun var.'}
        lines = ['Beyin ' + (installed_version or 'surumu bilinmiyor'), labels.get(status, 'Saglik kontrolu tamamlandi.'),
                 'Bekleyen is: ' + str(result.get('pending_events', 0))]
        lines += unreadable_notes_lines(result)
        lines += missing_companion_lines(result.get('companion_sources'))
        for name, details in result.get('lifecycle', {}).items():
            lines.append(name + ': ' + ('olay goruldu' if details.get('status') == 'observed_metadata' else 'henuz dogrulanmadi'))
        jev = result.get('jev') or {}
        if jev.get('mode', 'off') == 'off':
            lines.append('Jev: kapali')
        else:
            lines.append('Jev: ' + jev_mode(jev['mode'], jev.get('provider')) +
                         (', saglayici: laya (' if jev.get('provider') == 'laya' else ' (') + 'otomatik baglam: ' +
                         ('acik' if jev.get('automatic_model_calls') else 'kapali') + '), son 24 saat ' +
                         str(jev.get('last_24h', {}).get('calls', 0)) + ' cagri')
            if jev.get('context_mode_notice'):
                cm = ascii_text(jev.get('context_mode', (result.get('preferences') or {}).get('context_mode', '?')))
                lines.append("Jev otomatik baglam yalniz 'context_mode: turn' ile calisir; su an '" +
                             cm + "' modunda. Etkinlestirmek icin: beyin.py preferences --context-mode turn")
        # Context-fill reminder (issue #685)
        ctx_fill = result.get('context_fill') or {}
        if ctx_fill.get('enabled'):
            lines.append('Bağlam doluluk hatırlatması: aktif (ilk eşik ' + str(ctx_fill.get('first_threshold', 150000)) +
                         ', tekrar ' + str(ctx_fill.get('repeat_threshold', 75000)) + ' token)')
        elif ctx_fill.get('enabled') is False:
            lines.append('Bağlam doluluk hatırlatması: kapali')
        elif ctx_fill.get('status') == 'invalid':
            lines.append('Bağlam doluluk hatırlatması: ayar gecersiz (' + str(ctx_fill.get('error', 'unknown')) + ')')
        if result.get('secrets_redacted'):
            lines.append('Sir suzgeci ' + str(result['secrets_redacted']) + ' eslesmeyi [REDACTED] olarak yazdi.')
        cov = result.get('receipt_coverage')
        if isinstance(cov, dict) and cov.get('total', 0) > 0:
            ratio = cov.get('ratio')
            pct = int(round(ratio * 100)) if ratio is not None else 0
            d7 = cov.get('last_7d', {})
            d7_text = ''
            if d7.get('total', 0) > 0 and d7.get('ratio') is not None:
                d7_text = ', son 7 gun: %' + str(int(round(d7['ratio'] * 100)))
            # A question-only session needs no receipt; name how many of the missing ones saw no edit (#212).
            unobserved = cov.get('missing_no_observed_edit')
            unobserved_text = ('; ' + str(unobserved) + ' eksik oturumda duzenleme gozlenmedi'
                               if isinstance(unobserved, int) and unobserved > 0 else '')
            lines.append('Makbuz kapsami: %' + str(pct) + ' (' + str(cov['covered']) + '/' + str(cov['total']) + ' oturum' + d7_text + ')'
                         + unobserved_text)
        hermes_without_terminal = cov.get('hermes_without_terminal', 0) if isinstance(cov, dict) else 0
        if hermes_without_terminal > 0:
            lines.append('Hermes: ' + str(hermes_without_terminal) +
                         ' oturumda son tur icin kapanis sinyali gozlenmedi (makbuz kapsami disinda).')
        kf = result.get('knowledge_freshness')
        if isinstance(kf, dict) and kf.get('status') == 'unavailable':
            lines.append('Son bilgi damitmasi: olculemedi (' + str(kf.get('error') or 'hata') + ')')
        elif isinstance(kf, dict):
            if kf.get('last_distilled_at') is not None:
                days = kf.get('days_ago', 0)
                time_text = 'bugun' if days == 0 else ('1 gun once' if days == 1 else str(days) + ' gun once')
                lines.append('Son bilgi damitmasi: ' + time_text + ' (o tarihten beri ' + str(kf.get('receipts_since', 0)) + ' makbuz)')
            else:
                lines.append('Son bilgi damitmasi: henuz kavram notu damitilmadi (toplam ' + str(kf.get('receipts_since', 0)) + ' makbuz)')
        scheduled = result.get('scheduled_check') or {}
        if scheduled.get('status') in ('ok', 'needs_attention', 'error'):
            lines.append('Zamanlanmis denetim (bilgi): son kosu ' + str(scheduled.get('age_hours', '?')) + ' saat once, ' +
                         {'ok': 'saglikli', 'needs_attention': 'dikkat gerekiyor', 'error': 'denetim tamamlanamadi'}[scheduled['status']] + '.')
        for conflict in result.get('instruction_conflicts', []):
            lines.append('Talimat celiskisi: ' + conflict['file'] + ' icinde V2 derleyici ifadesi var ("' + conflict.get('snippet', '') + '"). Bu metin V3 ajan damitmasiyla celisiyor; eski derleyici talimatlarini kaldir.')
        if result.get('receipt_view_conflicts'):
            lines.append('Receipt gorunumu cakismasi: ' + ', '.join(plain_text(c['source']) for c in result['receipt_view_conflicts']) +
                         '. Gorunumleri arsivleyip yeniden uretmek icin: ' +
                         ('py -3' if sys.platform == 'win32' else 'python3') + ' beyin.py sync --recover-views')
        if result.get('skill_conflicts'):
            lines.append('Skill kopyalari ayristi: ' + ', '.join(result['skill_conflicts']) + '. Iki surum de korundu.')
        if result.get('skill_unmanaged'):
            lines.append('Skill klasorundeki yonetilmeyen girdiler (bilgi): ' + ', '.join(result['skill_unmanaged']) + '.')
        if result.get('excluded_components'):
            lines.append('Haric tutulan bilesenler: ' + ', '.join(result['excluded_components']) + '.')
        if result.get('pending_exclusions') or result.get('exclusions_pending'):
            lines.append('Haric tutma degisikligi bekliyor (' + ', '.join(result.get('pending_exclusions', [])) +
                         '): bir sonraki kurulum ya da guncellemede uygulanir.')
        if result.get('exclusions_error'):
            lines.append('.beyin-exclusions.json gecersiz; guncelleme duzeltilene kadar durur: ' + result['exclusions_error'])
        hygiene = result.get('companion_hygiene') or {}
        for name in hygiene.get('over_limit', []):
            entry = hygiene['files'][name]
            lines.append('Hafiza hijyeni: ' + name + ' ' + str(entry['chars']) + ' karakter (sinir ' + str(entry['limit']) +
                         '). Ajanina "' + ('py -3' if sys.platform == 'win32' else 'python3') +
                         ' beyin.py companion-compact" calistirmasini soyle; eski kayitlar arsive tasinir, metin silinmez.')
        storage = result.get('state_storage') or {}
        if storage.get('compact_suggested'):
            lines.append('Yerel indeks (bilgi): ' + megabytes(storage['database_bytes']) + ' MB, bunun ' +
                         megabytes(storage['free_bytes']) + ' MB bos sayfa. Diske geri vermek icin: ' +
                         ('py -3' if sys.platform == 'win32' else 'python3') + ' beyin.py state-compact')
        rules_opening = result.get('companion_rules_opening') or {}
        if rules_opening.get('status') == 'stale':
            lines.append('Kurallar.md kaynak dogrulamasi guncel degil (bilgi); acilis olcumu icin beyin.py sync calistir.')
        if rules_opening.get('status') == 'clipped':
            lines.append('Kurallar.md acilista kirpiliyor (bilgi): kural metni ' + str(rules_opening['chars']) +
                         ' karakter; Claude Code acilisi bu ayarlarla (' + str(rules_opening['budget']) +
                         ' karakter) en fazla ' + str(rules_opening['share']) + ' karakterini tam gosterir' +
                         (' (gerekceler de cikarildi)' if rules_opening.get('reasons_omitted') else '') +
                         '. Oturuma ozel bildirimler gercek acilista bu payi azaltabilir. '
                         'Kurallari kisa tut; uzun gerekceyi ve ayrintiyi ayri bir nota (or. Kurallar-Detay.md) tasi.')
            omitted = rules_opening.get('omitted_rules')
            if omitted:
                lines.append(f'Gosterilmeyen kural siralari {omitted["first"]}-{omitted["last"]} '
                             f'({omitted["count"]}/{omitted["total"]}).')
            if rules_opening.get('pinned_omitted'):
                lines.append(pinned_overflow(rules_opening['pinned_omitted']))
        for proj in rules_opening.get('project_rules') or []:
            source = plain_text(proj.get('source') or proj.get('project') or 'Kurallar.md')
            if proj.get('status') == 'stale':
                lines.append('Proje Kurallar.md kaynak dogrulamasi guncel degil (bilgi): ' + source +
                             '; acilis olcumu icin beyin.py sync calistir.')
            elif proj.get('status') == 'clipped':
                lines.append('Proje Kurallar.md acilista kirpiliyor (bilgi): ' + source + ' ' +
                             str(proj['chars']) + ' karakter; Claude Code proje acilisi bu ayarlarla (' +
                             str(rules_opening.get('budget')) + ' karakter) en fazla ' + str(proj['share']) +
                             ' karakterini tam gosterir' +
                             (' (gerekceler de cikarildi)' if proj.get('reasons_omitted') else '') +
                             '. Kurallari kisa tut; eski/uzun ayrintiyi arsiv veya detay notuna tasi.')
                p_omitted = proj.get('omitted_rules')
                if p_omitted:
                    lines.append(f'Gosterilmeyen proje kural siralari ({source}) '
                                 f'{p_omitted["first"]}-{p_omitted["last"]} '
                                 f'({p_omitted["count"]}/{p_omitted["total"]}).')
                if proj.get('pinned_omitted'):
                    lines.append(pinned_overflow(proj['pinned_omitted'], source))
        checked, total = rules_opening.get('project_rules_checked'), rules_opening.get('project_rules_total')
        if isinstance(checked, int) and isinstance(total, int) and checked < total:
            lines.append(f'Proje Kurallar.md olcumu ilk {checked} dosyayla sinirli (bilgi): {total} dosyanin '
                         f'{total - checked} tanesi olculmedi.')
        references = result.get('instruction_references') or {}
        if references.get('status') == 'unavailable':
            lines.append('Talimat ve skill baglanti denetimi tamamlanamadi (bilgi): ' +
                         plain_text(references.get('error', 'unknown')) + '.')
        elif references.get('dead_count'):
            shown = ', '.join(entry['file'] + ':' + str(entry['line']) + ' -> ' + entry['target']
                              for entry in references['dead'][:3])
            more = references['dead_count'] - min(3, len(references['dead']))
            lines.append('Talimat ve skill dosyalarinda kirik baglanti (bilgi): ' + shown +
                         (' ve ' + str(more) + ' tane daha' if more > 0 else '') + '.')
        supersedes = result.get('supersedes') or {}
        lines += supersedes_lines(supersedes.get('dead'), supersedes.get('dead_count'))
        boundary_report = result.get('boundary') or {}
        boundary_lines = {
            'parent_obsidian_index': 'Kok siniri (bilgi): ust klasorde de bir .obsidian var; Obsidian ust klasoru acip bu vault\'u alt klasor sayabilir.',
            'no_root_obsidian': 'Kok siniri (bilgi): vault kokunde .obsidian yok; Obsidian\'da tam olarak bu klasoru ac.',
        }
        for finding in boundary_report.get('findings') or []:
            code = str(finding).split(':', 1)[0]
            if code == 'kasa_excluded':
                lines.append('Kasa sinifi klasor (bilgi): ' + ', '.join(plain_text(name) for name in boundary_report.get('sensitive_excluded') or []) +
                             '; hijyen taramalari bunlari atlar, tam garanti icin kaynaklarina visibility: private ekle.')
            elif code == 'code_inside_vault':
                lines.append('Kok siniri (bilgi): vault icinde kod klasoru var (' + ', '.join(plain_text(name) for name in boundary_report.get('code_dirs', [])[:5]) +
                             '); kod repo\'sunu hafiza vault\'unun disinda tut.')
            elif code == 'nested_git_repository':
                lines.append('Kok siniri (bilgi): vault icinde ayri git deposu var (' + ', '.join(plain_text(name) for name in boundary_report.get('nested_repositories', [])[:3]) + ').')
            elif code == 'backup_artifacts':
                lines.append('Kok siniri (bilgi): kokte yedek ya da esitleme kopyasi var (' + ', '.join(plain_text(name) for name in boundary_report.get('backup_artifacts', [])[:5]) +
                             '); ozel bir kopya tasiyor olabilir.')
            elif code in boundary_lines:
                lines.append(boundary_lines[code])
        review = result.get('review') or {}
        if review.get('due_count'):
            shown = ', '.join(plain_text(entry['source']) + ' (' + str(entry['days_overdue']) + ' gun)' for entry in review['due'][:3])
            more = review['due_count'] - min(3, len(review['due']))
            lines.append('Yeniden bakma tarihi gelen not (review_at, bilgi): ' + shown +
                         (' ve ' + str(more) + ' tane daha' if more > 0 else '') + '.')
        if review.get('invalid_count'):
            lines.append('Gercek tarih olmayan review_at (bilgi): ' +
                         ', '.join(plain_text(entry['source']) for entry in review['invalid'][:3]) + '.')
        closed = result.get('closed_tasks') or {}
        if closed.get('closed_count'):
            shown = ', '.join(plain_text(entry['source']) + ' (' + str(entry['days_old']) + ' gun)' for entry in closed.get('closed')[:3])
            more = closed['closed_count'] - min(3, len(closed.get('closed') or []))
            lines.append(str(closed.get('days', 30)) + ' gunden eski kapali gorev tasks/ altinda duruyor (bilgi, tasima senin kararin): ' + shown +
                         (' ve ' + str(more) + ' tane daha' if more > 0 else '') + '.')
        cap = result.get('word_cap') or {}
        if cap.get('over_count'):
            shown = ', '.join(plain_text(entry['file']) + ' (' + str(entry['words']) + ' kelime)' for entry in cap.get('over', [])[:3])
            more = cap['over_count'] - min(3, len(cap.get('over') or []))
            lines.append('Kelime tavani (' + str(cap['cap']) + ') asan not (bilgi): ' + shown +
                         (' ve ' + str(more) + ' tane daha' if more > 0 else '') + '.')
        promo = result.get('promotion') or {}
        if promo.get('hot'):
            lines.append('Sicak klasorler (son ' + str(promo.get('window_days', 30)) + ' gun dokunma): ' +
                         ', '.join(plain_text(entry['folder']) + ' (' + str(entry['touches']) + ')' for entry in promo['hot'][:3]))
        if promo.get('cold'):
            lines.append('Soguk klasorler (terfi karari senin): ' +
                         ', '.join(plain_text(entry['folder']) + ' (' + str(entry['days_quiet']) + ' gun)' for entry in promo['cold'][:3]))
        waiting = [entry for entry in (result.get('inbox') or {}).get('folders') or []
                   if entry.get('attention') or entry.get('error')]
        if waiting:
            lines.append('Gelen kutusunda bekleyen (bilgi, isleme karari senin): ' + ', '.join(
                plain_text(entry['folder']) + (' (okunamadi: ' + plain_text(entry['error']) + ')' if entry.get('error') else
                                               ' (' + str(entry['notes']) + ' not, en eskisi ' + str(entry['oldest_days']) + ' gun)')
                for entry in waiting[:3]) + (' ve ' + str(len(waiting) - 3) + ' klasor daha' if len(waiting) > 3 else '') + '.')
        parallel = result.get('parallel_sessions') or {}
        if parallel.get('enabled'):
            lines.append('Paralel oturum bildirimi: acik (son 45 dakikada etkin ' + str(parallel.get('active', 0)) + ' oturum isareti)')
        elif parallel.get('valid') is False:
            lines.append('parallel-sessions.json gecersiz; paralel oturum bildirimi kapali sayiliyor.')
        dedupe = result.get('context_dedupe') or {}
        if dedupe.get('enabled'):
            lines.append('Oturum ici tekrar onleme: acik (' + str(dedupe.get('sessions', 0)) + ' oturum defteri)')
        elif dedupe.get('valid') is False:
            lines.append('context-dedupe.json gecersiz; oturum ici tekrar onleme kapali sayiliyor.')
        usage = result.get('note_usage') or {}
        if isinstance(usage, dict) and usage.get('tracked', 0) > 0:
            totals = usage.get('feedback_totals', {})
            lines.append(f"Not kullanim takibi: {usage.get('tracked', 0)} kayit, {usage.get('dead_weight_count', 0)} olu agirlik (dead-weight), geri bildirim [ok: {totals.get('ok', 0)}, wrong: {totals.get('wrong', 0)}, stale: {totals.get('stale', 0)}]")

        queue = result.get('mutation_queue') or {}
        if queue.get('valid') is False:
            lines.append('mutation-queue-settings.json gecersiz; cevrimdisi kuyruk kapali sayiliyor.')
        if queue.get('enabled') or queue.get('queue_exists'):
            lines.append('Cevrimdisi mutasyon kuyrugu: ' + ('acik' if queue.get('enabled') else 'kapali') +
                         (' (kuyruk dosyasi okunamadi)' if queue.get('status') == 'invalid' else
                          ' (' + str(queue.get('pending_count', 0)) + ' bekleyen, ' + str(queue.get('conflict_count', 0)) +
                          ' cakisma, ' + str(queue.get('failed_count', 0)) + ' basarisiz)'))
        devices = result.get('devices') or {}
        if devices.get('valid') is False:
            lines.append('devices.json gecersiz; coklu cihaz duzeni kapali sayiliyor.')
        elif devices.get('enabled'):
            lines.append('Coklu cihaz: ' + str(devices.get('level')) +
                         (' / ' + str(devices['phone_sync']) if devices.get('phone_sync') else ''))
        index_scope = result.get('index_exclusions') or {}
        lines += index_exclusion_lines(index_scope)
        hint = index_scope.get('vendored_hint') or {}
        if hint.get('notes'):
            more = len(hint.get('dirs') or []) - 3
            lines.append('Indeks kapsami (bilgi): baska projelerin kod kopyasi gibi gorunen klasorlerde ' + str(hint['notes']) +
                         ' not indeksli (' + ', '.join(plain_text(entry['dir']) + ': ' + str(entry['notes'])
                                                       for entry in (hint.get('dirs') or [])[:3]) +
                         (' ve baska klasorler' if more > 0 or hint.get('truncated') else '') +
                         '). Senin notun degilse indeks disinda tut: ' + ('py -3' if sys.platform == 'win32' else 'python3') +
                         ' beyin.py preferences ' + ' '.join('--index-exclude-dir ' + plain_text(name)
                                                            for name in hint.get('names') or []))
        arch_hint = index_scope.get('archive_hint') or {}
        if arch_hint.get('notes'):
            more = len(arch_hint.get('dirs') or []) - 3
            py_cmd = 'py -3' if sys.platform == 'win32' else 'python3'
            exclude_args = ' '.join('--index-exclude-dir ' + (('"' + plain_text(name) + '"') if ' ' in name else plain_text(name))
                                    for name in arch_hint.get('names') or [])
            lines.append('Arsiv kapsami (bilgi): arsiv klasorlerinde emekli edilmemis (canli sayilan) ' + str(arch_hint['notes']) +
                         ' not indeksli (' + ', '.join(plain_text(entry['dir']) + ': ' + str(entry['notes'])
                                                       for entry in (arch_hint.get('dirs') or [])[:3]) +
                         (' ve baska klasorler' if more > 0 or arch_hint.get('truncated') else '') +
                         '). Baglamdan uzak tutmak icin notlara "status: archived" yaz veya klasoru indeks disinda tut: ' +
                         py_cmd + ' beyin.py preferences ' + exclude_args)
        lines += state_location_lines(result.get('state_location'))
        lines += receipt_line_endings_lines(result.get('receipt_line_endings'))
        backup = result.get('backup') or {}
        if backup.get('status') == 'remote_configured':
            lines.append('Yedek (bilgi): Git uzak adresi ayarli; guncel ve geri yuklenebilir ikinci kopya dogrulanmadi. docs/v3/BACKUP.md')
        elif backup:
            lines.append('Yedek (bilgi): Bu kasanin ikinci kopyasi dogrulanamadi; baska yolla aldiginiz yedek burada gorunmez. Disk arizasina karsi ayri bir kopya tutun. docs/v3/BACKUP.md')

        paths = result.get('hook_paths') or {}
        if paths.get('status') == 'stale':
            lines.append('Hook yollari baska bir kuruluma isaret ediyor (' + str(len(paths.get('stale', []))) + ' eski yol). ' +
                         'Guncellemek icin: ' + ('py -3' if sys.platform == 'win32' else 'python3') + ' beyin.py hook-reconcile')
        tracked = result.get('tracked_machine_files') or {}
        bridges = result.get('global_bridge_paths') or {}
        if bridges.get('status') == 'stale':
            harnesses = sorted({row.get('harness') for row in bridges.get('stale', [])
                                if row.get('harness') in ('claude', 'codex', 'antigravity', 'opencode')})
            commands = ', '.join('bridge --harness ' + harness + ' ... --install' for harness in harnesses)
            lines.append('Genel kopru yollari eski kuruluma isaret ediyor (bilgi): ' + commands +
                         '. Yeni vault icinden onceki --project-root, --event ve --context-chars secimlerini koruyarak '
                         'yeniden kur. Genel ayarlar degistirilmedi.')
        if tracked.get('status') == 'warning':
            lines.append('Git makineye ozel kurulum dosyalarini izliyor (bilgi): ' +
                         ', '.join(plain_text(name) for name in tracked.get('tracked', [])) +
                         '. Her makinenin kendi yedegini koruyarak docs/v3/MULTI-MACHINE.md rehberini incele; otomatik degisiklik yapilmadi.')
        elif tracked.get('status') in ('unavailable', 'skipped'):
            lines.append('Git kurulum dosyasi kontrolu (bilgi): ' + plain_text(tracked.get('status')) +
                         ' (' + plain_text(tracked.get('reason', 'unknown')) + ').')
        scopes = result.get('project_scopes') or {}
        if scopes.get('status') == 'error' and scopes.get('error'):
            lines.append('Proje kapsamlari (.beyin-projects.json) gecersiz: ' + plain_text(scopes['error']))
        elif scopes.get('status') in ('error', 'warning'):
            # Entry-level problems: name each entry instead of a bare "hata" (#419).
            details = []
            if scopes.get('missing_folders'):
                details.append('kasada bulunamayan klasorler: ' + ', '.join(plain_text(f) for f in scopes['missing_folders']))
            if scopes.get('duplicate_folders'):
                details.append('birden fazla projeye atanan klasorler: ' + ', '.join(plain_text(f) for f in scopes['duplicate_folders']))
            if scopes.get('invalid_entries'):
                details.append('gecersiz girdiler: ' + ', '.join(plain_text(f) for f in scopes['invalid_entries']))
            if scopes.get('duplicate_folders') or scopes.get('invalid_entries'):
                details.append('bu girdiler atlaniyor, adlandirdiklari kasa klasorleri hicbir projeye acilmiyor' +
                               (' ve shared_unscoped duzeltilene kadar kapali' if scopes.get('shared_unscoped') else ''))
            lines.append('Proje kapsamlari (.beyin-projects.json) dikkat gerektiriyor: ' + '; '.join(details) +
                         '. Duzenlemek veya temizlemek icin: ' + ('py -3' if sys.platform == 'win32' else 'python3') +
                         ' beyin.py project-scope prune')
        if status in ('needs_attention', 'pending'):
            lines.append('Ajanina "beyin doktor" diyerek ayrintiyi inceletebilirsin.')
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
                if stream.isatty():
                    stream.reconfigure(errors='replace')
                else:
                    stream.reconfigure(encoding='utf-8', errors='replace')
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
            print(human_result(result, command, installed_version) if human else json.dumps(result, ensure_ascii=True, indent=2))
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
        print(message, file=sys.stderr if code else sys.stdout)
        return code
    except Exception as exc:
        error = {'error': type(exc).__name__, 'message': str(exc)}
        if human:
            print(human_result(error, command, installed_version), file=sys.stderr)
        else:
            print_error(error)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
