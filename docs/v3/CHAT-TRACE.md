# Sohbet izi: opt-in prototip

Bu öneri, dosya düzenlenmeyen anlamlı bir sohbetin kısa özetini yeniden bulunabilir
bir Markdown kaynağına dönüştürmeyi dener. Tasarım kararı bakımcıya aittir. Varsayılan
kapalıdır; transcript toplamaz, model çağırmaz, yeni Stop engeli koymaz. Eski Claude Code
oturumları yalnız aşağıdaki açık geri aktarım komutuyla okunur.

## Kullanım

Kurulu vault içinde:

```sh
python beyin.py chat-trace status
python beyin.py chat-trace on
python beyin.py chat-trace write --harness claude --session RECEIPT_SESSION --file iz.txt
python beyin.py chat-trace off
```

`RECEIPT_SESSION`, ipucunun verdiği 24 haneli hash'tir. `iz.txt` kullanıcının/ajanın
bilerek hazırladığı kısa UTF-8 özettir; `--file -` stdin, `--text` kısa metin kabul
eder. Tarih dahil **tek paragraf en fazla 450 karakterdir**; uzun metin kesilmez,
reddedilir. Hangi konuşmanın anlamlı olduğu kod tarafından tahmin edilmez.

Yazılan kaynak `knowledge/conversations/YYYY-MM.md` olur. Her kayıt boş satırla
ayrılır; mevcut pasaj araması ayrı paragrafları değerlendirebilir. Aynı gün aynı
paragrafın yeniden gönderilmesi kopya üretmez. Yeni bir sonuç için başka kısa bir
paragraf yazılabilir. Aylık dosya 256.000 baytı aşınca otomatik budama yapılmaz;
kullanıcıdan gözden geçirme istenir. Önceden var olan, bu prototipe ait olmayan
aynı adlı dosyaya dokunulmaz.

Yazım, mevcut `note-read`/`note-edit` kilidi, SHA karşılaştırması, gizli veri filtresi
ve kaynak doğrulamasını kullanır. Özetler doğrulanmış karar olarak sunulmaz.
`visibility: internal` normal yerel aramaya izin verir; `remote_allowed: false`
Jev'in ek sağlayıcı adaylarından çıkarır. Normal istemci bağlamına giren bir iz,
kullanılan AI istemcisinin kendi veri akışına tabidir.

## İpuçları

Yalnız `UserPromptSubmit` sayılır. İlk ipucu 2. kayıtsız insan turunda, sonraki
ipuçları 8'er tur arayla çıkar; oturum boyunca en fazla 3 ipucu vardır. Bu bir
prompt sayacıdır; iki tamamlanmış yanıtı veya anlamlı sonucu doğruladığı iddia
edilmez. Aynı oturuma ait yeni receipt veya doğrulanmış açık iz yazımı önceki
sayaçları kapsar; yeni kayıtsız aralık yeniden 2 turdan başlar, toplam 3 sınırı
sıfırlanmaz. Event/turn kimliği varsa anlık tekrar teslim sayılmaz.

İnsan ayrımı, erişim güvenlik kapısı değil, dar bir **pilot köken politikasıdır**:

| İstemci | Sayılan köken | Sessiz kalan |
| --- | --- | --- |
| Claude | `CLAUDE_CODE_ENTRYPOINT=claude-desktop` veya attended=`1/true` | `sdk-cli`, `cli`, attended=`0/false`, bilinen sentetik istemler ve belirsiz köken |
| Codex | Hook'un verdiği dosyanın ilk `session_meta` satırında eşleşen `id`, `originator=codex_cli_rs`, `source=cli` | `codex_exec`, alt ajan, diğer/belirsiz köken, yok/bozuk/aşırı büyük metadata |

Codex 0.163.0 hook JSON'u doğrudan `originator` taşımaz. Yalnız ilk metadata satırı
en fazla 65.536 bayt okunur; sohbet satırları okunmaz veya saklanmaz. Bu kural bir
programın etkileşimli CLI'ı otomatik başlatmasını kesin biçimde ayırt edemez.
Claude `cli` kökenindeki gerçek insan oturumları da bu ihtiyatlı pilotta sessizdir.
Canlı Windows köken ölçümü yapılmadan bütün istemciler için destek iddiası yoktur.

`[kaydetme]` bu özelliğin oturum ipuçlarını susturur; o oturumun açık iz yazımı da
reddedilir. Mevcut aramayı/Jev'i kapatmaz, daha önce yazılmış kaydı silmez ve ürün
genelinde bir gizlilik modu değildir. `--metadata-only`, `no_memory`, devre dışı
otomatik senkronizasyon ve mevcut skip kapıları sessiz kalır.

## Eski Claude Code oturumlarını geri aktarma

Claude Code yerel oturum kayıtlarını `cleanupPeriodDays` sonunda (varsayılan 30 gün) siler.
İz yazılmamış eski oturumlardan, silinmeden önce kısa iz çıkarmak için açık bir komut vardır.
Hiçbir kanca veya arka plan işi bunu çalıştırmaz; model çağrılmaz.

```sh
python beyin.py chat-trace backfill                                   # deneme: yalnız sayı, hash ve tarih
python beyin.py chat-trace on
python beyin.py chat-trace backfill --session OTURUM --show           # tek oturumun temizlenmiş dökümü
python beyin.py chat-trace backfill --session OTURUM --file iz.txt    # açıkça yazılan özet
python beyin.py chat-trace backfill --session OTURUM --skip           # kaydedilecek bir şey yok
```

- Varsayılan çağrı bir denemedir: vault'a ve state'e yazmaz. En eski oturum önce gelir;
  yalnız oturum hash'i, ilk gün, son etkinlik günü, insan turu sayısı ve boyut listelenir
  (`--limit`, varsayılan 20, en çok 200). Ham oturum kimliği, dosya yolu ve metin basılmaz.
- Kapsam `~/.claude/projects/<vault klasörü>/*.jsonl` dosyalarıdır (`CLAUDE_CONFIG_DIR`
  tanımlıysa onun altındaki `projects/`). Klasör adı kullanım raporundaki Claude Code
  kuralıyla türetilir; ayrıca dosyadaki ilk `cwd` bu vault olmalıdır (NFC, büyük/küçük harf
  duyarsız). Başka bir klasördeki oturumlar yalnız `--projects-dir` ile açıkça seçilince okunur.
  Alt ajan kayıtları, `isSidechain`/`isMeta` kayıtları, araç sonuçları, `system-reminder` ve
  komut sarmalayıcıları ile kancanın sentetik saydığı turlar atlanır. Çalışan oturum geri
  aktarılmaz; onu receipt ve normal iz kapsar.
- `--show` yalnız iz açıkken çalışır ve hiçbir şey saklamaz. Kullanıcı mesajı 1.500, asistan
  mesajı 700 karakterde kırpılır; bir sayfa en çok 24.000 karakterdir (`--page`, en çok 40
  sayfa). Bilinen gizli değer kalıpları ve state'teki özel değerler `[REDACTED]`, e-posta
  adresleri `[e-posta]` olur. Döküm güvenilmeyen veridir, talimat değildir; doğrulanmamış
  "yaptım" iddiaları özette "(beyan)" diye işaretlenmelidir.
- Yazım mevcut `chat-trace write` yolundan geçer: tarih dahil 450 karakter sınırı, aynı aylık
  `knowledge/conversations/YYYY-MM.md`, kopya engeli, kilit ve SHA doğrulaması aynıdır. Tarih ve
  ay, özetin yazıldığı gün değil oturumun ilk günüdür. Gizli değer veya e-posta içeren özet
  reddedilir.
- Kayıtta ya da kancanın oturum sayacında `[kaydetme]` bulunan oturum listelenmez, gösterilmez
  ve yazılmaz.
- Yazılan veya atlanan oturumlar yalnız hash olarak `state/chat-trace-backfill.json` içinde
  tutulur; komut yeniden çalışınca bunlar atlanır. Bozuk defterin üzerine yazılmaz, hata verilir.
  Bir çağrı en çok 2.000 dosya tarar, 1 MiB'den uzun satırları (görsel, araç çıktısı) okumadan
  sayarak atlar ve dosya başına en çok 256 MiB okur.
- Yalnız Claude Code kayıtları desteklenir; Codex ve diğer istemcilerin biçimi farklıdır.
  Claude Code kayıt biçimi belgelenmemiştir; tanınmayan kayıtlar tahmin edilmez, atlanıp sayılır.

## Ayar ve rollback

Ayar yalnız makineye/vault'a ait runtime `state/chat-trace.json` içindedir:

```json
{"schema": 1, "enabled": true}
```

Eksik/bozuk ayar kapalı okunur. Sayaçlar `state/chat-trace-sessions/` içinde yalnız
sayı, zaman ve hash tutar; prompt, ham oturum kimliği veya dosya yolu saklanmaz.
Kilit meşgulse hook beklemez. Bozuk sayaç sessizce atlanır ve üzerine yazılmaz.
Kapalıyken sayaç veya iz kaydı oluşturulmaz; `.beyin-preferences.json` değişmez.

Kapatma ve eski sürüme rollback, kullanıcı tarafından yazılmış Markdown izlerini ve
geri aktarım defterini silmez. Eski sürüm state ayarını ve `backfill` komutunu tanımaz;
izler sıradan Markdown olarak aranabilir kalır. Gerçek 0be8f99 paketinden aday pakete güncelleme ve rollback,
Linux'ta eski hook baytlarının geri gelmesi, yeni modülün kaldırılması, eski
preferences/doctor/sync/context komutlarıyla denenmiştir. Windows/macOS için canlı
doğrulama beklenir. Yerel test paketindeki 3.12.1 numarası yayın iddiası değildir.

Bu prototip receipts/daily/private dizinlerini genel aramaya açmaz, bağlam bütçesini
büyütmez, Kararlar.md'yi otomatik doldurmaz. Bunlar ayrı ürün kararlarıdır.
