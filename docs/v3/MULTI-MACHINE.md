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
| `🔮 850-Companion/Arşiv/` | evet | `companion-compact` metni canlı dosyadan çıkarıp arşive taşır ve yerine tek bir işaret satırı bırakır. Arşiv tek makinede kalırsa öbür makine içi boş bir işaret görür. Arşivdeki `visibility: private` otomatik bağlama girmez demektir, git dışı demek değildir. |
| `receipts/` | evet | Receipt dosyası yazıldıktan sonra değişmez ve adı `event_id` özetidir (`beyin_v3_sync.py` `receipt()`). Öbür makine yeni receipt'i bir sonraki `sync`'te kendi veritabanına alır (`_scan_receipts()`). |
| `daily/v3/`, `knowledge/v3/` | hayır | Bu görünümler yerel veritabanındaki receipt'lerden üretilir (`beyin_v3_projections.py` `project_receipts()`). Receipt'ler eşitlendiyse aynı saat dilimindeki iki makinede aynı çıkar; dosya adı makinenin yerel günüdür, farklı saat dilimindeki makineler aynı receipt'i farklı güne koyabilir. Git'e alınırlarsa öbür makineden gelen dosya elle düzenlenmiş sayılır (`manual receipt view edit preserved`) ve o görünüm artık güncellenmez. |
| `AGENTS.md`, `CLAUDE.md` | evet | Kullanıcının kendi talimatları da bu dosyalardadır. 3.7.0'dan beri Beyin bloğu makine yolu taşımaz (`python3 beyin.py sync`), aynı sürümü kuran her makinede aynıdır. Önce güncelleyen makinenin bloğu git'le öbürüne gelirse öbür makinenin güncellemesi onu kendi bloğu sayar ve çakışma vermez. |
| Kurulum dosyaları | hayır | Bir kısmı bu makinenin yollarını taşır (`.beyin-runtime.json`, hook dosyaları, Hermes ve OMP eklentileri); geri kalanı bu makinede kurulu sürüme aittir. Güncellemeyi önce yapan makinenin dosyaları git'le öbürüne geçerse o makinenin kurulum kaydıyla uyuşmaz; yeniden kurulum `Reinstall conflict: managed file changed` hatasıyla durur. |

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
.opencode/plugins/beyin-v3.js
.omp/hooks/pre/beyin-v3.ts
# Yerel veritabanından üretilen görünümler: her makine kendisininkini üretir
daily/v3/
knowledge/v3/
```

Kontrol: kurulumdan ya da güncellemeden hemen sonra `git status --short` boş olmalıdır.

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
- **Aynı makinede paralel oturumlar:** `preferences --parallel-sessions on` açıkken ajan,
  aynı vault'ta son 45 dakikada etkin başka bir oturum varsa ilk isteminde tek satırlık bir
  uyarı alır ([PREFERENCES.md](PREFERENCES.md#paralel-oturum-bildirimi)). İşaretler makineye
  özel runtime klasöründe durur; öbür makinedeki oturumları görmez, onlar için yukarıdaki git
  adımları geçerlidir.
- **Çakışma çözülmeden oturum açma:** `pull --rebase` çakışmada durduğunda dosyada
  `<<<<<<<`, `=======`, `>>>>>>>` işaretleri kalır. Bu halde açılan oturumda `sync` dosyayı
  olduğu gibi indeksler ve işaretler sonraki bağlama girer; ajan onları içerik sanabilir.
  `sync` ve `doctor` bunu bildirmez. Önce çakışmayı çöz (`git status` temiz olmalı), sonra
  oturum aç.
- **Receipt `event_id`'sini makineler arasında tekil tut:** `event_id`'yi ajan seçer ve dosya
  adı onun özetidir. İki makine aynı gün aynı konuya aynı adı verirse (`ortak-konu-2026-09-27`)
  iki farklı receipt aynı dosyaya düşer ve `pull --rebase` `CONFLICT (add/add)` ile durur.
  Çakışma bir tarafın dosyası seçilerek çözülürse öbür makinenin veritabanında kendi özeti
  kalır; `sync` uyarı vermez ve iki makinenin görünümleri sessizce ayrışır. Bunu önlemek için
  `event_id`'nin sonuna kart başlığındaki gibi `Receipt session=` değerinin ilk 8 karakterini
  ekle: `ortak-konu-2026-09-27-3f9a1c2b`.

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
