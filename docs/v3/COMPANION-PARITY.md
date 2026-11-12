# V2 → V3 kişilik ve süreklilik incelemesi

İnceleme tarihi: 2026-09-17. Karşılaştırılan tabanlar: V2 `2e074cc`, yayımlanmış
V3.0.2 `55cd4e8`. Kullanıcının aktardığı “kişilik/performans azaldı” geri bildirimi
bir kullanıcı gözlemidir. Aşağıdaki nedenler koddan doğrulanır; öznel kişilik puanı
veya tüm modeller için kalite artışı ölçülmüş değildir.

## Kayıp nerede oluştu?

| Davranış | V2 | V3.0.2 | Bu düzeltme |
| --- | --- | --- | --- |
| Kimlik kurulumu | Kişiselleştirilmiş CLAUDE ve Companion şablonları | ZIP installer yalnız teknik AGENTS/CLAUDE bloğunu kuruyordu; Core ve ilişki dosyaları oluşturulmuyordu | Ortak düşünme ortağı protokolü ve bir kez oluşturulan kullanıcıya ait başlangıç notları |
| Oturum açılışı | Core okuma talimatı ve deterministik ilişki köprüsü; üst sınır 16.000 karakter | Toplam 5.000 karakter; Core/Soul öncelikli sette yok; yinelenen JSON metadata bütçeyi tüketiyor | Core/Soul, düzeltmeler, son sonuç, aktif konu gövdeleri, son Journal ve bilgi haritası kısa kaynak bölümleriyle taşınıyor |
| “Beni tanıyor musun, nerede kalmıştık?” | İlişki kaynakları açılışta vardı | Sorgudaki kelimeler notla örtüşmüyorsa boş arama sonucu | İlişki sorularında deterministik companion bağlamı; genel arama sözleşmesi korunuyor |
| Journal ve Threads seçimi | Son Journal girişi; ancak Threads yalnız başlık/durum ve 12 satırdı | Journal dosya sonundan kırpılıyor; yeni kayıt en üstteyse kaybolabiliyor; kapalı konular bütçe tüketiyor | En yeni tarihli Journal, aktif Threads gövdesi; kırpma açık ve dosya yoluyla gösteriliyor |
| İlişkiden öğrenme | Core/Journal/Threads/Last-Session/Kurallar açıkça ajanın sorumluluğuydu | beyin skill'i ağırlıklı olarak görev/receipt işlemlerini anlatıyordu | Açık kullanıcı düzeltmesi, gerekçe, açık kalan adım ve anlamlı gözlem için somut yazım protokolü |
| Bilgi damıtma | Model compiler kavram, bağlantı ve bilgi indeksi üretiyordu | Compiler emekli edildi; receipt indeksi üretiliyor, sentez talimatı çok kısaydı | Aktif ajan için kavram/bağlantı/indeks güncelleme akışı; ayrı model çağrısı yok |

Kaynaklar: `scripts/install_v3.py`, `template/CLAUDE.md`,
`template/.claude/hooks/session-start.sh`, `template/.claude/scripts/compile.py`,
`template/.claude/scripts/beyin_v3_hook.py`, `template/.agents/skills/beyin/SKILL.md`.

[Issue #27](https://github.com/avenoxai/avenoxbeyin/issues/27) boş SessionStart sorgusunda
ilişki kaynaklarının seçilmediğini doğrudan raporladı. Önceki #29 düzeltmesi dört dosyayı
önceliklendirdi ama Core/Soul ve eksik kurulum/öğrenme protokolünü tamamlamadı.
[Issue #21](https://github.com/avenoxai/avenoxbeyin/issues/21) ise V2'nin Threads gövdesini
kaybettiğini gösterir: V2'nin hatalı satır filtresi geri getirilmedi. Windows hook
arızaları da algılanan unutkanlığa katkı yapmış olabilir; bunlar V3.0.2'de ayrı olarak
düzeltildi. Yeni [#32](https://github.com/avenoxai/avenoxbeyin/issues/32) raporu V3.0.1
kurulumunu anlatır; V3.0.2'de tekrar üretildiği varsayılmadı.

## Kurulum ve veri koruması

`beyin_v3_companion.py` pakete dahil edilir. Temiz installer eksik başlangıç notlarını
bir kez oluşturur. V2 veya V3'ten yönetilen güncellemede yeni worker ilk çalışmada aynı
kontrolü yapar. Önce mevcut Companion/Echo dizini, yoksa tek yerel Core/Soul dizini
kullanılır; birden fazla kimlik adayı varsa seçim uydurulmaz. Arşiv ve symlink dizinleri
kimlik olarak keşfedilmez. Kullanıcının Soul dosyası varsa rakip bir Core oluşturulmaz.

Başlangıç notları paket manifestinin yönetilen dosyalarına eklenmez. Kişisel değişiklikler,
güncelleme ve rollback/uninstall sonrasında kalır. Bir kez başlatıldıktan sonra kullanıcının
sildiği notlar yeniden yaratılmaz. Başlatma işareti yalnız dış yerel runtime state'indedir.
Öğrenme ve ilişki yazımları aktif ajan tarafından yapılır; worker kullanıcı geçmişi uydurmaz.

Normal 5.000, Ekonomik 2.000 karakter sınırı ve kayıtlı tercihler korunur. Daha az
bağlamda bütün dosyanın sığacağı iddia edilmez: kırpılan kaynağa okuma işareti eklenir.
Private/untrusted ve indekslendikten sonra değişmiş kayıtlar otomatik bağlamdan dışlanır.
`no_memory` olayları bağlam enjekte etmez ve kuyruğa yazılmaz.

Bütçe paylaşımı ([#45](https://github.com/avenoxai/avenoxbeyin/issues/45)): `Kurallar.md`
ve `Last-Session.md` eşit paylaşımdan önce bir taban pay alır; kurallar dosyası sığmadığında
yalnız sonu değil, başı ve sonu birlikte gelir ve aradaki işaret kaç karakterin atlandığını
ve dosyayı okumak gerektiğini söyler. Kesme satır sınırındadır, yarım kural gelmez. Bilgi
haritası ve ilişkili kaynaklar payını aldıktan sonra artan bütçe kırpılmış companion
bölümlerine geri verilir; sınır hiçbir durumda aşılmaz.

Açılışta yer darsa tam genel/proje kuralları (gerekçeleri çıkarmak gerekebilir), en üstteki
tarihli devir kartı ve bütün açık konuların kısa görünümü önce ayrılır. Kuralların taban
payı kategoriye aittir; genel ve proje dosyaları aynı payı bölüşür. Kimlik, üslup, Journal,
bilgi haritası, receipt ve sorgu sonuçları kalan alanı kullanır. Bu çekirdek de sığmıyorsa
önce kimlik, Journal ve öbür isteğe bağlı kaynaklar kısa bir başa iner; devir dosyası en üstteki
kartını ve en az taban payını korur, konuların kısa görünümü kalanı alır. Bu kalan, eski orantılı
paylaşımın konulara verdiğinden azsa her kaynak önce o orantılı payı (korunan kısmıyla ya da taban
payıyla sınırlı) alır; artan alan sırayla kalan kurallara, en üstteki devir kartına ve konuların
kısa görünümüne gider. Böylece kurallar ne kadar uzarsa uzasın devir kartı ve konu adımları eski
paylaşımın gösterdiğinden azına inmez. Kuralların kendisi de sığmıyorsa sınırlı paylar ve açık
kırpma işaretleri geçerlidir; keyfi uzunluktaki kuralların her sonlu bütçeye tam sığacağı
söylenmez. Kaynak dosyalar bu işlemde değiştirilmez.
Bu çekirdek bir tabandır, tavan değildir: kalan alan önce bütün kaynaklara paylaşılır. Böylece
paralel oturumların daha eski devir kartları ve konu kartlarının tamamı sığdıkça gelir; en
üstteki kart her zaman tam ve önce gelir, kesilen devir ayrıntısı `[truncated: read source]`
işaretiyle biter. Konu kartları ya tam ya kısa görünümleriyle gelir; tam kartın kullanamadığı
alan öbür kaynaklara geri döner.

Konu ve devir kartı sınırları bulunurken kod çiti yalnız aynı işaretle, en az açılış
uzunluğunda ve ardından yalnız boşluk varsa kapanır. İşaretten sonra örnek metin bulunan
satır çiti kapatmaz; içindeki başlıklar yeni konu veya eski devir kartı sayılmaz.

`Threads.md` içindeki `###` başlıklarının her biri kısa görünümde korunur. `**Status:**` /
`**Durum:**` ile `**Next step:**` / `**Sonraki adım:**` paragrafları varsa bunlar, devam
satırlarıyla birlikte aynen alınır; kalın yazılmamış `Status:` / `Next:` alanları da tanınır.
İlk durum ve sonraki adım alanı güncel karttır; sonraki tarihli geçmiş paragrafları bu kısa
görünümü büyütmez. Ayrıntı paragrafı boş satırla ayrılır. Eski serbest
metin biçiminde ilk cümle ve bekleyen iş/hatırlatma belirten cümleler aynen seçilir.
Alan daralırsa bu eski biçimden bir bekleyen adım cümlesi seçilir; başlıkların kendisi de
sığamayacak kadar küçük bütçelerde bütün başlıkların görünmesi garanti edilmez.
Bu seçim anlamsal bir özet değildir; etiketsiz veya farklı ifadeli bir sonraki adımı
bulacağı garanti edilmez. Kısa görünüm ayrıntı ve etiketsiz adımlar için kaynağa yönlendirir.
Kapalı konular ve kod çitlerindeki örnek başlıklar bu görünümün parçası değildir.
Kod çitlerinin içindeki örnek durum/sonraki adım alanları ve bekleyen iş cümleleri de kısa
görünüme alınmaz; alan yetip tam konu kartı gösterildiğinde kod örnekleri aynen korunur.
Kısa görünüm de sığmadığında bütün başlıklar korunur; alan dosyadaki sırayla konulara en az
80 karakterlik dilimler halinde verilir, sonrakiler yalnız başlıklarıyla gelir ve bölüm
`[Thread overview clipped; read source]` işaretiyle biter. Yüzlerce konuda her başlığa bir iki
karakter düşüp anlamsız parçalar oluşmaz. Başlıklar alanın neredeyse tamamını alıp daha az konuya
durum bırakacaksa konular dosyadaki sırayla tam kısa görünümleriyle gelir, ardından sığan
başlıklar ve aynı işaret; böylece alan büyüdükçe görünen sonraki adım sayısı azalmaz.

`CLAUDE.md` içindeki bir `@yol` başvurusu, o dosyanın geçerli istemci oturumunda
başarıyla yüklendiğini kanıtlamaz ([#610](https://github.com/avenoxai/avenoxbeyin/issues/610)).
Örneğin Claude Code'un [`claudeMdExcludes`](https://code.claude.com/docs/en/settings-reference#claudemdexcludes)
ayarı başvuruyu içeren talimat dosyasını dışlayabilir. Bu yüzden başlangıç, devam,
sıkıştırma sonrası ve süreklilik sorularında companion kaynakları normal kaynak doğrulama,
gizlilik ve bütçe kurallarıyla gösterilir; başvuru bulundu diye gövdeleri düşürülmez.
İstemci aynı dosyayı ayrıca yüklerse tekrar oluşabilir; doğrulanmamış bir yüklemeye güvenip
kuralları kaybetmekten kaçınılır. Açılış `Loaded in full` iddiası üretmez; `doctor` içindeki
`companion_rules_opening` de dosya başvurusuna bakarak `imported: true` ya da kırpılmamış
sonucu vermez, gerçek companion payını raporlar. Bu düzeltme yeni ayar veya opt-out eklemez
ve gerçek Claude Code yüklemesinin doğrulandığı anlamına gelmez.

Claude/Codex açılışında yeniden üretim, istemcinin UTF-16/UTF-8 ölçüsüne sığana kadar
küçülen bütçeyle tekrarlanır. Hook'un ayrı bildirimleri de karakter bütçesinden düşülür;
sondaki güvenlik kesmesinin companion kaynaklarını kaybetmesine güvenilmez.

Bu paylaşımdan önce companion dosyaları kaynak doğrulamalı anlık görüntüden 200.000
karakterlik bir üst sınırla okunur ([#374](https://github.com/avenoxai/avenoxbeyin/issues/374)).
Payından kısa kalan dosya artanı uzun dosyalara bırakır; böylece kural kırpması ve
`rules X-Y (n of N)` işaretindeki numaralar dosyanın tam metni üzerinden hesaplanır.
Önceden pay eşit bölünüyordu ve yaklaşık 40.000 karakteri aşan bir `Kurallar.md` bu
katmanda başını kaybediyordu: dosyanın başındaki özet açılışa hiç ulaşmıyor, işaret de
kuralları kesik metinden sayıyordu. Üst sınırı aşan bir `Kurallar.md` artık bu katmanda da
başı ve sonuyla tutulur; `Journal.md` en yeni kayıtlar altta olduğu için sonuyla tutulmaya
devam eder.

Kapsamlı kurallar (#333, [#375](https://github.com/avenoxai/avenoxbeyin/issues/375)): tek bir
projeye ya da alana ait düzeltme `projects/<proje>/Kurallar.md` dosyasına yazılır; global
`Kurallar.md` içinde en çok `[kapsam: <proje>]` etiketli tek satırlık işaretçi kalır. Etiket
`[kapsam: ...]` ya da `[scope: ...]` biçimindedir (köşeli parantezsiz `kapsam: ad` da tanınır),
virgülle birden çok ad alır; `genel`, `global`, `all` ve `herkes` etiketsiz kuralla aynıdır. Etiket, satır başında `-`, `*`, `+`,
`1.` ya da `1)` ile açılan liste maddesinin herhangi bir satırında durabilir; madde boş satıra
ya da başlığa kadar sürer. Ad karşılaştırması büyük/küçük harf ve Türkçe karakter farkını yok
sayar. Hook başka projelerin etiketli maddelerini yalnız kanca girdisinde etkin bir `project`
adı geldiğinde eler; o zaman vault kökündeki `projects/`, `Projeler/` ve `projeler/` altında
bu adı birebir taşıyan klasörden ilk bulunan `Kurallar.md` (yoksa `rules.md`) dosyasını da
ekler. Sembolik bağ ve vault dışına çıkan yol okunmaz; dosya indekslenmiş ve `private`
olmayan bir kaynak olmalıdır. Claude Code kanca girdisinde `project` alanı göndermez: orada
etiketli kurallar da açılışa girer ve proje dosyası otomatik gelmez. Bu yüzden işaretçi satırı
ajana dosyayı ne zaman açacağını söyler. Etkin projeyi `cwd` üzerinden bulma ayrı bir iştir
([#288](https://github.com/avenoxai/avenoxbeyin/issues/288)).

## Bilgi haritası ve isteğe bağlı `INDEKS.md` (#595)

Açılıştaki bilgi haritası varsayılan olarak `knowledge/index.md` dosyasıdır: derleyicinin
tuttuğu makale listesi. Bilgi tabanı büyüdükçe bu liste haritanın payında (companion kaynakları
sığarken en fazla 1.500, kırpılırken en fazla 600 karakter) kesilir ve alttaki satırlar görünmez
olur. [#595](https://github.com/avenoxai/avenoxbeyin/issues/595) bunun yerine yazarın elle
tuttuğu kısa bir yönlendirme kartını önerdi: her satır notun ne olduğunu ve ne zaman açılacağını
söyler, içerikler açılışa girmez.

Bu isteğe bağlıdır. `knowledge/` klasörünün kendisinde `INDEKS.md` varsa (Türkçe yazımlar
`İNDEKS.md`, `İndeks.md`, `Indeks.md` ve `indeks.md` de tanınır) açılış haritası olarak makale
listesinin yerine bu kart gelir; etiket `[Knowledge map: knowledge/INDEKS.md]` olur. Pay ve
kırpma kuralları aynıdır: uzun bir kart da kesilir ve kesim işaretlenir. Kart yoksa hiçbir şey
değişmez. Önerilen biçim:

```markdown
# Bilgi tabanı: ne zaman aç

42 makale; içerikleri açılışa girmez, yalnız konu açılınca ilgili makaleyi aç.

| Konu | Ne zaman aç |
| --- | --- |
| [[concepts/musteri-notlari\|Müşteri notları]] | Sözleşme maddesi ya da teslim tarihi konuşulursa |
| [[concepts/arac-listesi\|Araç listesi]] | Yeni bir araç seçilirken |
```

Kart her açılışta okunur, bu yüzden kısa tutulmalıdır: her satır tek not ve tek "ne zaman aç"
cümlesi. Derleyici (`compile.py`) kartı yazmaz ve silmez; satırları yazar ya da ajan tutar.

Kart diğer companion kaynaklarıyla aynı kapılardan geçer: indekslenmiş, son eşitlemeden beri
değişmemiş, `private` ya da `untrusted` olmayan bir kaynak olmalıdır. Bu koşullardan biri
tutmazsa açılış eski davranışla `knowledge/index.md` dosyasını gösterir; kartın metni hiçbir
durumda açılışa sızmaz. Soru taşımayan açılışta kart makale listesinin yerini tutar: alttaki
ilgili kaynaklar bölümü ne listeyi ne de kartı yeniden ekler. Soru taşıyan açılışta liste,
soruyla eşleşirse ilgili kaynak olarak gelebilir. Tam gösterilen bir harita, kart olsun ya da
olmasın, ilgili kaynaklarda tekrar edilmez.

Yalnız `knowledge/` kökündeki kart açılışa girer. Başka klasörlerdeki `INDEKS.md` dosyaları
sıradan notlardır: soru onlarla eşleşirse ilgili kaynak olarak gelirler, açılışa kendiliğinden
eklenmezler. `Threads.md` açık konuların açılış görünümünü kendisi taşır; ayrı klasördeki
konu dosyaları için bir kart, taslak üreten bir script ya da `doctor` denetimi bu sürümde yoktur.

## Boyut sınırı ve arşivleme (#96)

[Issue #96](https://github.com/avenoxai/avenoxbeyin/issues/96) beş haftalık bir kasada
`Last-Session.md` dosyasının yaklaşık 94.000, `Threads.md` dosyasının yaklaşık 57.000
karaktere büyüdüğünü raporladı. Bağlam bütçesi bu dosyaları kırpıyordu, fakat kırpma işareti
ajanı dosyanın tamamını okumaya yönlendiriyordu ve büyüme hiçbir çıktıda görünmüyordu. Kök
neden talimattaydı: "güncelle" deniyor, biçim ve sınır verilmiyordu. Her mesajı ayrı görev
sayan istemcilerde güncelleme her turda yeni tarihli paragraf olarak ekleniyordu.

Düzeltme dört parçadır:

- **Talimat.** `Last-Session.md` içinde oturum başına bir devir kartı açılır (`## YYYY-MM-DD HH:MM · <etiket> · <session_id[:8]>`)
  ve oturum yalnız kendi kartını yerinde günceller; başka oturumların kartları ezilmez. `Threads.md`
  içinde konu yerinde güncellenir. Süreklilik kontrolü her cevapta değil, anlamlı bir iş parçası
  bittiğinde yapılır. `## Previous`/`## Önceki` bölümüne ajan dokunmaz; eski kartları yalnız
  aşağıdaki komut taşır.
- **Ölçülebilir sınır.** Varsayılan sınır `Last-Session.md` için 3.000, `Threads.md` için
  8.000 karakterdir. Sayım Unicode karakteridir; bayt veya UTF-16 birimi değildir, bu yüzden
  Türkçe veya emoji yoğun bir dosya olduğundan büyük görünmez. `preferences
  --last-session-chars N --threads-chars N` sınırı 1.000 ile 200.000 arasında değiştirir,
  `0` kapatır. Ayar vault içindeki `.beyin-preferences.json` dosyasına değil, runtime
  klasöründeki `companion-limits.json` dosyasına yazılır. Eski sürümler bilinmeyen tercih
  alanını reddettiği için bu seçim rollback sonrasında hook'ları bozmaz; karşılığında ayar
  makineye özeldir. Bozuk bir sınır dosyası sessizce ezilmez, varsayılanlar kullanılır.
- **Görünürlük.** Sınır aşılırsa companion bağlamının ilk satırı tek bir `Memory hygiene:`
  uyarısıdır. Uyarı dosya adını, gerçek boyutu ve sınırı yazar, ajana dosyayı baştan sona
  okumamasını ve `companion-compact` çalıştırmasını söyler. Uyarı bağlam bütçesinin
  içindedir. `doctor` bütün companion dosyalarının boyutunu `companion_hygiene` altında
  raporlar; `--human` çıktısında sınırı aşan her dosya için bir satır görünür. Bu bir
  senkronizasyon arızası değildir, `doctor` durumunu değiştirmez. `Kurallar.md`,
  `Core.md`/`Soul.md` ve `Journal.md` bilerek birikir; boyutları yalnız bilgi olarak
  gösterilir, bunlara sınır uygulanmaz. Hook `Kurallar.md` dosyasını doğrudan okur ve
  sığmadığında başını ve sonunu atlanan kural aralığıyla birlikte verir (#45, #196).
  `doctor` ayrıca `companion_rules_opening` altında Claude Code açılışının kural metninin
  o anki karakter payını (`share`) hook'un kendi hesabıyla verir; sayı sabit bir tavan değildir,
  daha kısa bir kural dosyası isteğe bağlı ayrıntının yerini alabilir. Pay
  öbür companion dosyalarının o anki boyutuna ve receipt'e göre değişir. Kurallar
  kırpılıyorsa `--human` çıktısında tek satır görünür. Bilgi amaçlıdır, `doctor` durumunu
  değiştirmez ve hiçbir dosya yazmaz (#374). Ayrıca açılışta `Kurallar.md` kırpılıyorsa oturum
  başı bağlamının tepesinde `Memory hygiene: Kurallar.md opens clipped (share X of Y chars)...`
  uyarısı yer alır (#461).
- **Kayıpsız arşivleme.** `python3 beyin.py companion-compact` açık bir komuttur; hook,
  worker veya zamanlayıcı onu kendiliğinden çalıştırmaz. Yalnız sınırı aşan dosyada önce
  `## Previous`/`## Önceki` (Last-Session) veya `## Closed`/`## Kapanan`/`## Kapalı`
  (Threads) bölümünün gövdesini, sonra dosya sınıra inene kadar en eski tarihli kayıtları
  taşır. Tarihli kayıt, ilk karakterlerinde ISO (`2026-10-12`) ya da yazıyla
  (`12 Ekim 2026`, `12 October 2026`; aralıkta son gün: `4→5 Ekim 2026`) tarih bulunan bir
  başlık, paragraf veya liste maddesidir ve bir sonraki kayda ya da tarihsiz başlığa kadar sürer. Tarihli bir
  başlık bir kart açar: aynı ya da daha üst seviyedeki bir sonraki başlığa kadar içindeki
  tarihli satırlar ve alt başlıklar o kartındır; kart bölünmeden ya kalır ya da bütün
  halde taşınır (#118). Devir kartının
  ve her konu başlığının en yeni kaydı yerinde kalır. Threads'te `## Active`/`## Aktif`/`## Açık`
  bölümünün doğrudan altındaki tarihli satırlar bir konunun kendisi olabileceği için taşınmaz.
  Taşınan satırlar kelimesi kelimesine ve sırasıyla companion klasöründeki
  `Arşiv/<Dosya>-YYYY-MM.md` dosyasına eklenir. Arşiv `visibility: private` işaretlidir;
  otomatik bağlama ve olağan aramaya girmez, gerektiğinde doğrudan açılır. Canlı dosyada
  arşiv yolunu gösteren tek bir satır kalır. Frontmatter, kod blokları, CRLF satır sonları
  ve tarihsiz yapı korunur. Taşınacak tarihli kayıt yoksa ya da taşımadan sonra dosya
  hala sınırı aşıyorsa komut dosyanın kalan tam metnini aynı aylık arşive yedekler
  (`backup: written`). Aynı metin SHA-256 özetiyle bu ayın arşivinde ve önceki 12 aylık
  arşivde (toplam en fazla 8 MiB okuma) aranır; bulununca yeniden yazılmaz, rapor
  `backup: exists` ve gerçek arşiv yolunu verir. Ayın ilk yedeği her zaman tam metindir.
  Dosya aynı ay içinde yalnız biraz değiştiyse (#596) yeni yedek o aylık dosyadaki son
  tam yedeğe göre fark yedeği olarak yazılır (`backup_format: delta`); fark metnin en
  fazla yarısı kadarsa. Fark başka bir aya ya da başka bir farka dayanmaz, her aylık
  dosya kendi başına geri kurulur. Her kayıt uzunluk ve SHA-256 ile doğrulanır;
  depodaki `scripts/restore_v3_compact_backup.py` doğrulanmış sürümleri listeler ve
  seçileni vault dışında yeni bir dosyaya yazar. Eksik, değişmiş, okunamayan, sembolik
  bağlı veya bu sınırların dışındaki arşiv bir yedeğin kanıtı sayılmaz; komut güvenli
  biçimde yeni yedek yazar. Eski kopyaları silmez. Kesintiden sonra yeniden çalışan
  taşıma da aynı sınırlı taramayla önceki kaydı tanır ve metni ikinci kez eklemez;
  pencerenin dışındaki bir kayıt yalnız fazladan bir kopyaya yol açar, metin kaybına
  değil. Tarama sırasında canlı dosya değişirse eski yedek kabul edilmez ve `conflict` döner.
  Komut `needs_rewrite` döner; ajan dosyayı ancak bundan sonra sınır içinde
  yeniden yazar, böylece özetleme hiçbir metnin tek kopyası olmaz (#168). Arşiv
  yazıldıktan sonra canlı dosya değişmişse arşiv eski baytlarına döner ve `conflict`
  raporlanır. `--dry-run` hiçbir şey yazmadan planı gösterir. Komut model çağırmaz ve
  bitince yeni boyutları indekslemek için `sync` çalıştırır.
- **Karar defteri.** Taşınan metindeki karar satırları (`**Karar:**`, `**Karar (Ad):**`,
  `**Karar 2 (Ad):**`, `**Decision:**`; liste maddesi olabilir, kod bloğu içindekiler sayılmaz)
  ayrıca companion klasöründeki `Kararlar.md` dosyasına, geldikleri kartın başlığı ve arşiv
  yoluyla kelimesi kelimesine eklenir. Aynı satır ikinci kez eklenmez; dosya yalnız eklenerek
  büyür. `Kararlar.md` `visibility: internal` bir nottur, arşivin aksine aramaya ve bağlama
  girer; böylece kartı arşive giden bir karar görünür kalır. Canlı kartta kalan karar
  kopyalanmaz. Rapor `decisions_added` (`--dry-run`'da `decisions_planned`) sayısını verir.

`tests/v3_companion_hygiene_test.py` sentetik verilerle #96'daki iki biçimi (en üste eklenen
tarihli paragraflar ve budanmamış `## Previous Sessions`; konu başına onlarca tarihli
güncelleme ve kapalı bölüm) kurar. Uyarının her bütçede tek satır geldiğini, sınırdaki
dosyada gelmediğini, tek ek kaydın bir sonraki oturumda yakalandığını, sayımın karakterle
yapıldığını, `doctor` raporunu, ayarın doğrulanmasını ve arşivlemenin satır satır kayıpsız,
sıralı, tekrarlanabilir ve eşzamanlı düzenlemeye karşı güvenli olduğunu sınar.

## Doğrulama

`tests/v3_companion_test.py` sentetik verilerle temiz ZIP kurulumu, üç istemcide aynı
bağlam, Normal/Ekonomik/alt sınır bütçeleri, ilişki sorusu, yanlış kimlik/arşiv ayrımı,
private/untrusted/değişmiş kaynaklar, büyük ve yeni-giriş-üstte Journal, bilgi haritası,
güncelleme/rollback koruması ve tek seferlik başlangıç davranışını sınar.

İlk sekiz regresyon senaryosu aynı dosyayla yayımlanmış `v3.0.2` üzerinde çalıştırıldı:
7 başarısız, 1 başarılı. Düzeltmede sekizi de geçti. Son genişletilmiş companion paketi
13/13 geçti. Eski sürümün başarısızlıkları özellikle eksik Core/kimlik, 2.000 karakterde
kaybolan bağlam, boş ilişki sorgusu ve no-memory ihlalini görünür kıldı.

Yerel tam suite 199/199, bunun içindeki V3 suite 152/152 geçti. Donmuş semantik sözleşme
development 10/10 ve holdout 6/6 olarak korundu. Gerçek V3.0.2 kodundan kurulmuş eski
güncelleyiciyle yerel `3.0.3` aday ZIP'ine geçiş, ilk worker'da eksik notların başlatılması,
hook'tan kimlik geri okuma ve 3.0.2'ye rollback sırasında kişisel notların korunması geçti.
Bu aday paket test sürümüdür; yayımlanmış release iddiası değildir.

```sh
python3 -m unittest discover -s tests -p 'v3_companion_test.py'
python3 -m unittest discover -s tests -p 'v3_*test.py'
python3 scripts/evaluate_v3.py
# Aynı regresyonları açılmış eski checkout üzerinde çalıştırmak için:
BEYIN_TEST_REPO=/path/to/v3.0.2 python3 tests/v3_companion_test.py
```

Bunlar kaynak aktarımı, kurulum ve koruma sözleşmelerinin testleridir. Yeni canlı model
konuşmalarında kişilik kalitesi için kör kullanıcı karşılaştırması yapılmadı. Öğrenme
protokolünün uygulanması aktif ajanın talimat takibine bağlıdır; mevcut hafıza verisi
otomatik olarak geriye dönük yeniden yazılmaz.

Karar defteri kaynak kartın gizlilik ve güven sınırını genişletmez: private, untrusted veya reddedilmiş kaynaklardan karar kopyalanmaz. Yeni `Kararlar.md` yerel aramaya açık (`visibility: internal`) ve uzak danışmana kapalıdır (`remote_allowed: false`). Var olan defterin de `remote_allowed: false` veya `visibility: private` taşıması gerekir; bozuk ya da uygun olmayan defterde kartlar taşınmadan dikkat uyarısı döner. Aynı satır farklı kartlarda geçiyorsa her kartın kararı ayrı korunur; aynı kartın tekrar denemesi yeni kopya eklemez.
