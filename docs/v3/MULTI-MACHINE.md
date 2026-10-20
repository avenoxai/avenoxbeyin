# Birden çok makinede kullanım

Beyin yerel önceliklidir: makineler arasında senkron kodu yoktur ve Beyin git kurmaz
([UPDATE](UPDATE.md)). Aynı vault'u iki bilgisayarda (ör. iş ve ev) git gibi bir araçla
eşitliyorsan bu belge hangi dosyaların eşitleneceğini ve iki makinenin aynı gün nasıl
çalışacağını anlatır ([#112](https://github.com/avenoxai/avenoxbeyin/issues/112)).

Her makine Beyin'i kendisi kurar ve günceller. Runtime (SQLite veritabanı) vault dışında,
makineye özeldir ve eşitlenmez; her makine onu vault'taki Markdown'dan kendisi kurar.

## Ne eşitlenir

| Yol | Git'e girer mi | Neden |
|---|---|---|
| Notlar ve companion dosyaları | evet | Markdown kaynak gerçektir. |
| `.beyin-daily-layout.json` | evet | `preferences --daily-layout flat\|v3` vault'un ortak görünüm yolunu seçer. Aynı vault'a yazan makineler aynı tercihi ve bu seçeneği destekleyen motor sürümünü kullanmalıdır. |
| `🔮 850-Companion/Arşiv/` | evet | `companion-compact` metni canlı dosyadan çıkarıp arşive taşır ve yerine tek bir işaret satırı bırakır. Arşiv tek makinede kalırsa öbür makine içi boş bir işaret görür. Arşivdeki `visibility: private` otomatik bağlama girmez demektir, git dışı demek değildir. |
| `receipts/` | evet | Receipt dosyası yazıldıktan sonra değişmez ve adı `event_id` özetidir (`beyin_v3_sync.py` `receipt()`). Öbür makine yeni receipt'i bir sonraki `sync`'te kendi veritabanına alır (`_scan_receipts()`). |
| `receipts/*.md.view-provenance.json` | evet | Bir receipt'in üretilmiş görünüme dayandığı için dışlandığını, immutable olay kimliğiyle kaydeder. State yeniden kurulunca eski receipt dışarıda kalır; aynı günlük yoluna dayanan yeni insan receipt'leri engellenmez. İçerik taşımaz; bu dosyalar silinmemelidir. |
| `daily/v3/`, `knowledge/v3/` | hayır | Bu görünümler yerel veritabanındaki receipt'lerden üretilir (`beyin_v3_projections.py` `project_receipts()`). Receipt'ler eşitlendiyse aynı saat dilimindeki iki makinede aynı çıkar; dosya adı makinenin yerel günüdür, farklı saat dilimindeki makineler aynı receipt'i farklı güne koyabilir. Git'e alınırlarsa öbür makineden gelen dosya elle düzenlenmiş sayılır (`manual receipt view edit preserved`) ve o görünüm artık güncellenmez. |
| `AGENTS.md`, `CLAUDE.md` | evet | Kullanıcının kendi talimatları da bu dosyalardadır. 3.7.0'dan beri Beyin bloğu makine yolu taşımaz (`python3 beyin.py sync`), aynı sürümü kuran her makinede aynıdır. Önce güncelleyen makinenin bloğu git'le öbürüne gelirse öbür makinenin güncellemesi onu kendi bloğu sayar ve çakışma vermez. |
| Kurulum dosyaları | hayır | Bir kısmı bu makinenin yollarını taşır (`.beyin-runtime.json`, hook dosyaları, Hermes ve OMP eklentileri); geri kalanı bu makinede kurulu sürüme aittir. Güncellemeyi önce yapan makinenin dosyaları git'le öbürüne geçerse o makinenin kurulum kaydıyla uyuşmaz; yeniden kurulum `Reinstall conflict: managed file changed` hatasıyla durur. |

`--daily-layout flat` seçildiğinde üretilen günlükler `daily/YYYY-MM-DD.md` içindedir.
İnsan günlükleri de burada bulunabildiği için tüm `daily/` klasörünü Git dışında bırakma;
yalnız üretilen dosyaları ayır. Yerel gün ve saat dilimi kuralı iki düzende de aynıdır.
Ayrıntılar [günlük receipt düzeninde](PREFERENCES.md#günlük-receipt-düzeni).

## Önerilen `.gitignore`

```gitignore
# Beyin kurulumu: her makine Beyin'i kendisi kurar ve günceller
.beyin-runtime.json
.beyin-version
beyin.py
Beyni Guncelle.cmd
.claude/settings.local.json
.claude/scripts/
.claude/hermes-plugin/
.claude/skills/beyin/
.claude/skills/beyin-doktor/
.claude/skills/beyin-guncelle/
.agents/hooks.json
.agents/skills/beyin/
.agents/skills/beyin-doktor/
.agents/skills/beyin-guncelle/
.codex/config.toml
.codex/hooks.json
.kiro/hooks/beyin-v3.json
.kiro/skills/beyin/
.kiro/skills/beyin-doktor/
.kiro/skills/beyin-guncelle/
.opencode/plugins/beyin-v3.js
.omp/hooks/pre/beyin-v3.ts
# companion-compact süreçler arası kilidi; kalıcıdır ama kullanıcı verisi değildir
**/.beyin-compact.lock
# Aynı makinedeki not yazarlarının yerel kilidi, istekleri ve tam bayt yedekleri
.beyin-note-write/
# Yerel veritabanından üretilen görünümler: her makine kendisininkini üretir
daily/v3/
knowledge/v3/
```

Kontrol: kurulumdan ya da güncellemeden hemen sonra `git status --short` boş olmalıdır.

`.beyin-runtime.json` yine de başka bir işletim sisteminden geldiyse (Windows'ta `C:\...`,
macOS ya da Linux'ta `/...` yolu) bu makinede mutlak bir yol değildir. `beyin.py` o zaman bu
makinenin varsayılan state dizinini kullanır; her komut bunu stderr'e tek satırla söyler
(stdout ve `--json` çıktısı değişmez) ve `doctor` `pinned_state_not_absolute` olarak gösterir.
Böyle bir vault'un hook dosyaları da öbür makinenin yollarını taşır; `doctor` onları `hook_paths`
altında listeler, bu makinede kurulum ikisini birlikte düzeltir. Kurucu da böyle bir yolu değişmiş dosya saymaz, bu makinenin yoluyla yeniden
yazar ([#249](https://github.com/avenoxai/avenoxbeyin/issues/249)). 3.8.1 ve öncesinin
`beyin.py`'si bu yolu okuyamadığı için `beyin.py update` çalışmaz; bir kez yeni paketin
kurucusunu çalıştır (`python3 scripts/install_v3.py --vault "<vault>"`; bu makinede özel
state kullandıysan `--state` ile onu ver), sonra dosyayı `.gitignore`'a ekle.

Aynı veya farklı işletim sisteminden (Windows, Linux, macOS) ya da başka bir kullanıcı hesabından
gelen varsayılan state yolları (`AppData/Local/beyin-v3/<vault-hash>`, `.local/state/beyin-v3/<vault-hash>`
veya `Library/Application Support/beyin-v3/<vault-hash>`) yabancı pin sayılır; CLI ve lifecycle hook bu
makinenin ve kullanıcının yerel varsayılan state dizinine geri döner. Özel `--state` dizinleri bu
sezgisel eşlemeden etkilenmez; taşınan vault'ta `doctor` ile pin ve etkin state'i ayrıca doğrula.

`companion-compact` kilit dosyasını companion klasöründe kalıcı bırakır. Dosyanın
silinmemesi, POSIX üzerinde kilit tutulurken aynı yolda yeni bir inode açılmasını önler.
Kilit yalnız aynı paylaşılan dosya sistemini gören süreçleri koordine eder; senkronizasyon
araçlarıyla çoğaltılmış ayrı çalışma kopyaları ve kilit semantiği sunmayan NFS/SMB
kurulumları bu garantinin dışındadır.

- **3.6.0 ve öncesi:** O sürümlerde blok bu makinenin mutlak komut yolunu taşıyordu. İki makine
  de 3.7.0'a geçene kadar `AGENTS.md`'yi (bloğu kendisi taşıyorsa `CLAUDE.md`'yi de) `.gitignore`'da
  tut; ikisi de güncellenince satırı kaldırabilirsin.
- Kendi skill'lerin, `.claude/settings.json` veya başka istemci ayarların bu listede yoktur;
  onları eşitleyip eşitlememek senin kararın.

## Önerilen `.gitattributes`

```gitattributes
# Receipt dosyaları bayt bayt karşılaştırılır: satır sonu dönüştürülmez
receipts/** -text
# Kart ve kayıt düzenindeki companion dosyaları: iki makinenin eklemeleri birlikte kalır
**/Last-Session.md merge=union
**/Journal.md merge=union
**/Arşiv/*.md merge=union
```

**`receipts/** -text`:** Beyin receipt dosyasını bayt bayt karşılaştırır. Git for Windows
kurulumunun varsayılan seçeneği `core.autocrlf=true`'dur ve bu ayar öbür makineden gelen receipt'in satır
sonlarını CRLF'ye çevirir. Bu satır olmadan o makinede receipt özetine `\r` karışır, günlük
görünüm öbür makinedekinden farklı çıkar ve aynı receipt yeniden gönderilince
`ReceiptConflict: event id collision` hatası alınır.

**`merge=union`:** Git iki tarafın satırlarını da tutar, çakışma çıkarmaz. Satır silmez ama
çift bırakabilir. Bu yüzden yalnız yeni kaydın eklendiği dosyalar için uygundur:
oturum başına kartlı `Last-Session.md`, `Journal.md` ve arşiv. `Threads.md`, `Kurallar.md`
ve `Core.md` bilerek listede değil. Bu dosyalarda bir bilginin tek güncel hali vardır;
iki makine aynı satırı değiştirirse görünür bir çakışma, sessizce yan yana kalan iki
çelişkili satırdan iyidir.

Union ile birleşen kartların sırası zamana göre olmayabilir ve iki kart arasındaki boş satır
tekilleşebilir. `companion-compact` kartları konumlarına değil başlıktaki tarih ve saate
göre sıralar, en yenisini korur.

## Aynı gün iki makinede çalışmak

- **Last-Session:** her oturum kendi kartını açar ve yalnız onu düzenler
  ([companion protokolü](COMPANION-PARITY.md)). İki makinenin kartları union ile birleşir.
- **Threads, Kurallar, Core:** ilgili bölüm yerinde düzenlenir. Git çakışma verirse güncel
  olanı elle seç.
- **Git adımları:** çalışmaya başlamadan önce `pull`; bitince `commit`, `pull --rebase`,
  `push`. Aynı vault'ta birden çok oturum açıksa git komutlarını aynı anda birden çok
  oturumdan çalıştırma; bir oturumdan ya da günün sonunda tek seferde gönder.
- **Paralel oturumlar ve çapraz cihaz farkındalığı:** `preferences --parallel-sessions on` açıkken ajan,
  aynı vault'ta son 45 dakikada etkin başka bir oturum varsa ilk isteminde tek satırlık bir
  uyarı alır ([PREFERENCES.md](PREFERENCES.md#paralel-oturum-bildirimi)). Oturumlar vault kökündeki
  hafif `.presence/<session_id[:24]>.json` dosyaları ve makine-yerel runtime işaretleri ile
  senkronize edilir; oturum kapandığında (`SessionEnd`) dosya temizce silinir, 45 dakikadan eski
  işaretler otomatik elenir ([#205](https://github.com/avenoxai/avenoxbeyin/issues/205)).
- **Çakışma çözülmeden oturum açma:** `pull --rebase` çakışmada durduğunda dosyada
  `<<<<<<<`, `=======`, `>>>>>>>` işaretleri kalır. `sync` bu üç işareti sırayla taşıyan
  dosyayı (receipt dahil) indekslemez ve `unresolved git conflict markers` uyarısıyla `degraded`
  döner; dosya çözülene kadar bağlamdan çıkar ([#205](https://github.com/avenoxai/avenoxbeyin/issues/205)).
  Kod bloğundaki işaretler de sayılır: notta alıntılanmış bir çakışma örneği de uyarı verir. Önce çakışmayı çöz (`git status` temiz olmalı), sonra
  oturum aç.
- **Dosya senkron servisinin çakışma kopyaları:** iCloud, Dropbox, Syncthing, Google Drive gibi
  servisler çakışmada iki tarafı iki dosya olarak bırakır ([#205](https://github.com/avenoxai/avenoxbeyin/issues/205)).
  `sync` hiçbir dosyayı taşımaz, silmez ya da yeniden adlandırmaz; hangi tarafın doğru olduğuna
  sen karar verirsin.
  - Servisin kendi damgasını taşıyan kopya (`Not (… conflicted copy …).md`, `Not (Case Conflict).md`,
    `Not.sync-conflict-20261006-120000-ABCDEF7.md`) indekse alınmaz; `file sync conflict copy`
    uyarısıyla `degraded` döner. Asıl not normal indekslenir.
  - Numaralı ad (`Not 2.md`, `Not (1).md`) aynı klasörde `Not.md` varken yalnız listelenir:
    `sync_copy_candidates` alanında iki dosya ve içeriklerinin aynı olup olmadığı (`same_content`)
    görünür, ikisi de indekste kalır ve sync durumu değişmez. Böyle adlandırılmış gerçek bir not
    (`Sezon 2.md`) düşmez; listeden çıkarmak için adını değiştir. Kopya notun `id`'sini de taşıyorsa
    iki dosya ayrıca `duplicate source id` çakışmasıyla bağlamdan çıkar.
  - `receipts/` altında `<özet>.md` adına eklenmiş her şey (`<özet> 2.md`, `<özet> (conflicted copy).md`)
    receipt kopyasıdır: indekslenmez, `file sync copy of a receipt` uyarısıyla görünür. Receipt'ler
    yalnız eklenir; kopyayı asılla karşılaştırıp sil.
- **Receipt `event_id`'sini makineler arasında tekil tut:** `event_id`'yi ajan seçer ve dosya
  adı onun özetidir. İki makine aynı gün aynı konuya aynı adı verirse (`ortak-konu-2026-09-27`)
  iki farklı receipt aynı dosyaya düşer ve `pull --rebase` `CONFLICT (add/add)` ile durur.
  Çakışma bir tarafın dosyası seçilerek çözülürse öbür makinenin veritabanında kendi özeti
  kalır; `sync` uyarı vermez ve iki makinenin görünümleri sessizce ayrışır. Bunu önlemek için
  `event_id`'nin sonuna kart başlığındaki gibi `Receipt session=` değerinin ilk 8 karakterini
  ekle: `ortak-konu-2026-09-27-3f9a1c2b`.

## Aynı makinede birden çok ajan

Claude Code ve Codex aynı vault'taki insan yazımı notları düzenlerken doğrudan
dosya yazmak yerine `note-read` ve `note-edit` kullanır. Last-Session.md, Threads.md,
Journal.md ve proje notları bu akışa dahildir. Görevler için revision kontrollü
`task-update`, receipt ve üretilen görünümler için kendi V3 komutları kullanılır.

```text
python3 beyin.py note-read "proje/Threads.md"
python3 beyin.py note-edit --file istek.json
```

Windows'ta `python3` yerine `py -3` kullan. `note-read` JSON olarak `source`,
`sha256`, `content` verir; dosya yoksa son iki alan `null` olur. `source`, mevcut
dosya ve üst klasörlerin diskteki gerçek yazımıdır; büyük/küçük harf veya Windows
kısa ad takma adıyla istek verilmişse girişten farklı olabilir. Sonraki istekte
bu dönen yolu kullan. Kanonik yol için yönetilen, gizli ve hariç tutulan kaynak
kuralları yeniden denetlenir. `content`, UTF-8 BOM
ve CRLF dahil okunan metni korur; hash ham baytların SHA-256 özetidir. İsteği bu
okumadan oluştur, satır sonlarını dönüştürme:

```json
{"source":"proje/Threads.md","expected_sha256":"okumadan-gelen-64-haneli-hash","edits":[{"old":"tek bir kez geçen eski metin","new":"yeni metin"}]}
```

Her `old` boş olmayan metin olmalı ve uygulandığı anda tam bir kez geçmelidir;
birden çok düzenleme sırayla uygulanır. Mevcut dosyada tam `content` verilmez.
Yeni dosyada `expected_sha256: null` ve `content` kullan, `edits` verme.
`secret-filter` açıksa sır içeren sonuç yazımdan ve yedekten önce reddedilir;
metin sessizce değiştirilmez. Önceden yazılmış sırrı silen düzenleme kabul edilir.
`--file -` (varsayılan) JSON'u stdin'den okur. Çıktı her zaman JSON'dur:
`written` veya `unchanged` çıkış 0, `conflict` çıkış 2, `error` çıkış 1.
Eksik kaynak argümanı, eksik `--file` değeri ve bilinmeyen seçenek gibi komut
kullanım hataları `usage_error` ve çıkış 64 verir; `note-read` / `note-edit`
için bunlar da stdout'ta JSON'dur. `--help` olağan yardım metnini gösterir.
Çakışmada yeniden oku ve diğer ajanın metnini koruyarak hedefli düzenlemeyi yeniden
kur. Sadece hash'i yenileyip eski metni yeniden yazma; doğrudan dosya yazımıyla
korumayı atlama. Atomik değiştirme tamamlandıktan sonraki dosya denetimi, dizin
fsync, sync veya veritabanı hatası `conflict` / çıkış 2 ve `write_occurred: true`
verir. Bu sonuç ayrıca kanonik `source`, yazılan baytların `written_sha256` özeti,
varsa `backup` yolu ve `cause` hata türünü taşır. Yazım gerçekleşmiştir; başka
bir editör sonradan değiştirmiş olabileceğinden bu hash güncel içerik garantisi
değildir. Önce `note-read` ile yeniden oku ve `sync` çalıştır; aynı düzenlemeyi
körü körüne tekrarlama. Bu durumda başarı bildirimi ve oturum işaretleyicisi
üretilmez.

Yönetilen görünümler insan notu olarak okunmaz veya düzenlenmez. Dosyanın
`generated` / `kind` başlıkları dışarıdan silinse bile güncel `receipt_views`
kaydı varsa komut yazımdan önce reddedilir; kanonik dosya yolu denetlenir.
Yalnız dosya adı veya emekliye ayrılmış görünümün geçmişi ret nedeni değildir:
aynı `daily/GÜN.md` yolundaki sıradan insan notları kullanılabilir.
Sahiplik veritabanı okunamazsa yazım başlamadan hata döner.

Kilit ve önceki baytların base64 JSON yedekleri vault içindeki
`.beyin-note-write/` altındadır. MSIX uygulamaları AppData'yı sanallaştırabildiği
için aynı `.beyin-runtime.json` state yolu bile iki istemcide farklı klasöre
ulaşabilir. Vault içindeki kilit bu ayrışmadan etkilenmez. Üstteki `.gitignore`
satırını ekle; araç ayrıca bu yerel klasörde `*` içeren bir `.gitignore` oluşturur.
İstek dosyalarını da burada benzersiz adla tutup iş bitince kendi isteğini sil.
Yedekler hassas not metni taşıyabilir, eşitleme ve Git dışında kalmalıdır;
otomatik yedek temizliği yoktur. `writer.lock` kalıcıdır, açık yazarlar varken silme.

OS kilidi en çok 15 saniye bekler ve süreç kapanınca bırakılır. `note-create`
de varlık denetimi, kaynak yazımı ve indeks geri okumasında aynı kilidi alır;
farklı state kökleri kullanan istemciler de aynı yeni notu sessizce ezemez.
Başarısız `note-create` işleminden kalan journal'ın `sync` ile yeniden
oynatılması da bu kilide dahildir. Kilit başka yazardaysa bu `sync` o journal
kaydını beklemeden atlar ve korur; kilidi tutan yazarın kendi `sync`'i ya da sonraki
`sync` onu tamamlar. Bu durum çakışma sayılmaz, hook bağlamı düşmez. Böylece
veritabanı işlemi açıkken başka yazarın kilidi beklenmez.
Kilit altında
hash kontrolü, tekil hedef denetimi, tam bayt yedeği, geçici dosyaya yazım ve atomik
değiştirme yapılır; değiştirmeden hemen önce ve sonra kaynak yeniden okunur.
POSIX'te kaynak dosyanın üst dizini değiştirmeden sonra fsync edilir; bu adım
başarısızsa yazılmış fakat doğrulanamamış sonuç döner. Windows'ta bu stdlib
API'siyle dizin fsync yapılmaz; platformun rename ve dosya flush davranışı geçerlidir.
Başarılı yazım ilgili V3 indeksini günceller. Claude Code oturum kimliği varsa
receipt hatırlatma penceresi açılır; otomatik receipt gönderilmez.

Koruma işbirliğine dayalıdır. Obsidian, companion yazıcıları ve doğrudan dosya
araçları bu kilidi almaz; öncesi/sonrası denetim bütün dış yarışları önleyemez.
Aynı notu eşzamanlı bu araçlarla düzenleme. Kilit çok dosyalı işlem veya ayrı
makinelerdeki git çalışma kopyaları için garanti vermez. `.beyin-note-write/`
klasörünü bulut senkronundan da hariç tut. Kaynak yolu vault-göreli görünür
Markdown olmalıdır; yönetilen kaynaklar, yapılandırma, gizli yollar, symlink ve
junction yolları reddedilir.

## Vault'u başka klasöre ya da hesaba taşımak

Hook dosyaları kurulumun mutlak yollarını taşır (Python, `beyin_v3_hook.py`, `--vault`,
`--state`). Kurulu bir vault başka bir klasöre ya da başka bir Windows hesabına taşınınca
(`C:\Users\<eski>\...`) her yaşam döngüsü hook'u genel bir hatayla düşer. `doctor` bunu
`hook_paths: stale` ve `needs_attention` olarak gösterir; her satır dosyayı, olayı ve eski
yolu (`hook_script_missing`, `other_vault`, `python_missing`) verir ([#204](https://github.com/avenoxai/avenoxbeyin/issues/204)).

Çözüm, kurucuyu bu makinede yeni vault yoluyla yeniden çalıştırmaktır. Eski state klasörünü
(ya da kopyasını) `--state` ile ver; kurulum kaydı oradadır ve kurucu yolları taşıyan bütün
dosyaları yeniden üretir:

```text
py -3 scripts/install_v3.py --vault "C:\Users\<yeni>\Beynim" --state "<eski state klasörünün kopyası>"
```

Eski state yoksa kurucu kayıtsız bulduğu kurulum dosyalarını (`.beyin-runtime.json` gibi)
`Unmanaged file conflict` ile korur ve durur. Hook dosyaları başka makineden git'le geldiyse
içlerindeki eski Beyin girdileri, Windows'un PowerShell `-EncodedCommand` biçiminde olsalar da
kurulumda Beyin'in kendi girdisi olarak tanınır ve yenileriyle değiştirilir; yanlarında
çalışmayan bir kopya kalmaz.

## Nasıl doğrulandı

Windows 11, Git 2.55 (`core.autocrlf=true`), Python 3.13, `main` @ db1f23d. Yerel bir çıplak
depo ve iki vault; Beyin her birine ayrı `--state` ile kuruldu.

- Yukarıdaki `.gitignore` ile ikinci makinede kurulumdan sonra `git status` boş kaldı
  (`AGENTS.md` geçici nottaki gibi `.gitignore`'daydı; `CLAUDE.md` git'teydi ve değişmedi).
- Yönetilen bir betik başka sürümden gelmiş gibi değiştirilince o makinede yeniden kurulum
  `Reinstall conflict: managed file changed` hatası verdi.
- A'nın receipt'i B'de `sync` sonrası veritabanına ve günlük görünüme girdi. B'nin receipt'i
  A'ya geçti. `daily/v3/<gün>.md` ve `knowledge/v3/outcomes.md` iki makinede bayt bayt aynı çıktı.
- `receipts/** -text` kaldırılınca: B'deki receipt CRLF'ye döndü, görünümler farklılaştı,
  aynı receipt'in yeniden gönderilmesi `event id collision` verdi.
- İki makine aynı anda Last-Session'a kart, Journal'a kayıt ekleyip aynı Threads satırını
  değiştirdi: `pull --rebase` Last-Session ve Journal'ı iki kaydı da koruyarak birleştirdi,
  Threads'te çakışmayla durdu.
- Threads çakışma işaretleriyle dururken: ilk SessionStart dosyayı değişmiş kaynak diye dışarıda
  bıraktı; `sync`'ten sonraki SessionStart bağlamında işaretler vardı. `sync` `warnings: []`
  döndü, `doctor` bir şey göstermedi.
- İki vault aynı `event_id` ile farklı özetli receipt yazdı: ikisi de aynı `receipts/<özet>.md`,
  `pull --rebase` add/add çakışmasıyla durdu. A'nın dosyası seçilip devam edilince B'de `sync`
  `conflicts: []` döndü; B'nin `recap` ve `daily/v3` çıktısı B'nin özetini göstermeye devam etti.

## Satır sonu: `core.autocrlf=true` ve receipt'ler

Bu kontrol [#205](https://github.com/avenoxai/avenoxbeyin/issues/205) için eklendi. Git for Windows
varsayılanı `core.autocrlf=true`'dur ve `receipts/** -text` satırı yoksa bu makine öbür makineden gelen
receipt'i CRLF olarak çıkarır. Sonuç sessizdir: `sync` ve `doctor` hata vermez, ama aynı receipt'in
bayt özeti iki makinede farklı olur ve receipt öbür makineden yeniden gönderilince
`ReceiptConflict: event id collision` hatası gelir. Yukarıdaki [`.gitattributes`](#önerilen-gitattributes)
satırı bunu önler; `eol=lf` de aynı işi görür.

`doctor` artık vault bir git deposuysa ve `core.autocrlf=true` iken `receipts/` için `-text` ya da
`eol=lf` yoksa `receipt_line_endings` alanında `warning` bildirir (insan çıktısında bir "Receipt satir
sonu (bilgi)" satırı). Bu yalnız bilgidir, `doctor` durumunu değiştirmez ve hiçbir şeyi düzeltmez.

Uyarıyı gördüysen:

1. `.gitattributes` dosyasına `receipts/** -text` ekle ve commit'le.
2. Bu makinede receipt dosyalarını LF olarak yeniden çıkar (çalışma ağacın temizken):
   `git rm -r receipts` ardından `git checkout HEAD -- receipts`.
3. Dosyalar düzelse de bu makinenin yerel indeksi CRLF'li özeti tutmaya devam eder ve aynı receipt
   yine `event id collision` verir. State klasöründeki `memory.sqlite3` dosyasını yedeğe taşı ve
   `python3 beyin.py sync` çalıştır; indeks Markdown'dan yeniden kurulur.

Kontrol: `git check-attr text -- receipts/x.md` çıktısı `text: unset` olmalıdır.

Presence eşitlemesi varsayılan olarak kapalıdır. Her cihazda `preferences --parallel-sessions on`
ile açılır. Yerel `<state>/parallel-presence-device.json` dosyası rastgele bir cihaz kimliği
tutar; iki cihaz aynı kullanıcı ve state yolunu kullansa da diğerinin açık kaydını silmez.
Bu kimlik dosyasını vault ile eşitleme. `doctor` kimlik veya presence dosyası oluşturmaz.
