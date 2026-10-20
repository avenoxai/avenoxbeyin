# Sohbet izi: opt-in prototip

Bu öneri, dosya düzenlenmeyen anlamlı bir sohbetin kısa özetini yeniden bulunabilir
bir Markdown kaynağına dönüştürmeyi dener. Tasarım kararı bakımcıya aittir. Varsayılan
kapalıdır; transcript toplamaz, model çağırmaz, yeni Stop engeli koymaz.

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

## Ayar ve rollback

Ayar yalnız makineye/vault'a ait runtime `state/chat-trace.json` içindedir:

```json
{"schema": 1, "enabled": true}
```

Eksik/bozuk ayar kapalı okunur. Sayaçlar `state/chat-trace-sessions/` içinde yalnız
sayı, zaman ve hash tutar; prompt, ham oturum kimliği veya dosya yolu saklanmaz.
Kilit meşgulse hook beklemez. Bozuk sayaç sessizce atlanır ve üzerine yazılmaz.
Kapalıyken sayaç veya iz kaydı oluşturulmaz; `.beyin-preferences.json` değişmez.

Kapatma ve eski sürüme rollback, kullanıcı tarafından yazılmış Markdown izlerini
silmez. Eski sürüm state ayarını görmezden gelir; izler sıradan Markdown olarak
aranabilir kalır. Gerçek 0be8f99 paketinden aday pakete güncelleme ve rollback,
Linux'ta eski hook baytlarının geri gelmesi, yeni modülün kaldırılması, eski
preferences/doctor/sync/context komutlarıyla denenmiştir. Windows/macOS için canlı
doğrulama beklenir. Yerel test paketindeki 3.12.1 numarası yayın iddiası değildir.

Bu prototip receipts/daily/private dizinlerini genel aramaya açmaz, bağlam bütçesini
büyütmez, Kararlar.md'yi otomatik doldurmaz. Bunlar ayrı ürün kararlarıdır.
