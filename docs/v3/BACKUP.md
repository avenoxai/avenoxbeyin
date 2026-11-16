# Kasanı yedekle

Notların, görevlerin, kuralların ve receipt'lerin asıl kaynağı kasa klasöründeki
dosyalardır. Aynı diskteki ikinci klasör disk arızasına karşı yedek değildir.
En az bir kopyayı ayrı bir cihazda veya güvendiğin özel bir depoda tut; eski
sürümlerden de birkaçını sakla. Eşitleme yanlış silmeyi de eşitleyebilir.

## Git bilmeden: tarihli klasör veya ZIP

1. Ajanların ve not düzenleyicinin kasaya yazmayı bitirmesini bekle.
2. Dosya yöneticisinde kasa klasörünü başka bir diske kopyala veya tarihli bir
   ZIP oluştur (ör. `Beyin-2026-11-15.zip`). Gizli dosyaların da kopyalandığını
   kontrol et. Önceki sağlam kopyanın üstüne yazma.
3. `.beyin-note-write/` geçici yazma istekleri, kilitler ve işlem yedekleri
   taşır; bitmiş bir kasanın taşınabilir not yedeğine ekleme. İşlem sürerken
   bu dizini silme veya yedeği tamamlanmış sayma.
4. Vault dışındaki yerel state dizinini canlı cihazlar arasında eşitleme.
   SQLite indeksi yanında yerel kuyruklar, ayarlar ve istemciye özgü bilgiler
   de taşır. Tam kurtarma için gerekiyorsa bütün yazıcılar durmuşken **ayrı,
   özel bir kopyasını** al; state'i gereksiz diye silme. Yeni cihazda kurulum
   ve `sync` kaynak notları yeniden indeksler, ama her yerel ayarı/işlemi
   yeniden oluşturmaz. [Taşınan/yerelde kalan dosyalar](MULTI-MACHINE.md).
5. Kopyayı ayrı bir klasöre açıp birkaç notu, companion arşivini ve ek dosyayı
   okuyarak doğrula. Yedeği yalnız oluşturmak, geri yüklenebildiğini kanıtlamaz.

Bulut klasörü kullanıyorsan hesabın erişim ve sürüm geçmişi ayarlarını kontrol
et. `visibility: private` notun bağlama verilmesini sınırlar; buluta/Git'e
aktarılmasını engellemez. Yedek de aynı özel bilgileri içerir.

## Özel Git deposu: sürüm geçmişi ile

Git bilmiyorsan ilk yol yeterlidir. Git yolunu seçersen:

1. Git sağlayıcında **private** bir depo aç. Hesap, paylaşım ve yükleme kararını
   kendin ver; gerçek notlarını herkese açık depoya gönderme.
2. Kasada Git yoksa `git init` çalıştır. Önce
   [önerilen `.gitignore`](MULTI-MACHINE.md#önerilen-gitignore) kurallarını
   incele; makineye özel kurulum ve state dosyalarını senkron dışında tut.
3. `git status --short` ile dosyaları incele. Yalnız yedeklemek istediğin dosya
   ve klasörleri `git add -- DOSYA_VEYA_KLASOR` ile seç; `git diff --cached --stat`
   ve gerekirse `git diff --cached` ile içeriği gözden geçir. Özel token, anahtar
   veya paylaşılmaması gereken dosyaları ekleme.
4. `git commit -m "Kasa yedegi"` çalıştır. Yeni deponun verdiği uzak adresi
   `git remote add origin OZEL_DEPO_ADRESI` ile ekle. `origin` zaten varsa önce
   doğru hedef olduğunu kontrol et; üzerine yazma.
5. `git branch --show-current` ile dal adını gör, ardından
   `git push -u origin DAL_ADI` ile o dalı gönder. Uzak depoda dosyaların ve
   son commit'in bulunduğunu doğrula. Sonraki değişiklikler için commit/push
   adımlarını tekrar et; bunlar kendiliğinden yapılmaz.
6. Ayrı bir klasöre clone alıp örnek notları açarak geri yüklemeyi dene.
   Yeni cihazda Beyin'i ayrıca kur; makineye özel kurulum dosyalarını eski
   cihazdan kopyalama. [Çoklu cihaz rehberi](MULTI-MACHINE.md).

## Doctor neyi bilir?

`python beyin.py doctor --human` yedek hakkında yalnız bilgi verir. JSON'daki
`backup.remote_configured` yerel Git yapılandırmasında boş olmayan bir uzak
adres bulunduğunu belirtir. Bu adres yerel bir klasöre de işaret edebilir;
ulaşılabilirlik, son push, takip edilen dosyalar veya kurtarılabilirlik
**doğrulanmaz**. `backup_verified` bu nedenle `false` kalır. Uzak adres ve
kimlik bilgileri çıktıya konmaz; ağ bağlantısı, push veya dosya yazımı yapılmaz.

Git yoksa, uzak adres yoksa veya kontrol yapılamıyorsa sonuç `unknown` ya da
`unavailable` olur. Bu, yedeğin olmadığı iddiası değildir: başka araçlarla
aldığın yedek burada görünmez. Genel sağlık durumu değişmez, oturum açılışına
uyarı eklenmez. Bu ilk sürümde susturma ayarı, otomatik yedekleme veya `backup`
komutu yoktur.
