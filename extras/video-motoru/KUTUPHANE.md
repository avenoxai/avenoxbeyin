# Varlık kütüphanesi (ajan için)

Ses efekti, müzik, üretilmiş video, görsel ve logo gibi her varlık bir kez üretilir ya da bulunur,
etiketlenir ve sonraki videolarda yeniden kullanılır. Etiketsiz varlık kayıptır: bir ay sonra
kimse onu bulamaz ve aynısı yeniden üretilir.

## Nerede durur

- **Dosyalar** vault dışında, iş klasörü kökündeki `kutuphane/` altında: `ses/`, `muzik/`,
  `video/`, `gorsel/`, `logo/`.
- **Kayıtlar** vault'ta, `Video-Motoru/Kutuphane.md` içinde. Dosyayı arayan önce kayda bakar.

Bir videonun iş klasöründe üretilen varlık yeniden kullanılabilir çıktıysa kütüphaneye kopyalanır.
İş klasöründe kalan varlık o iş arşivlenince kaybolur.

## Kural: önce ara, sonra üret

Yeni bir ses, klip ya da görsel üretmeden ya da indirmeden önce `Kutuphane.md` içinde etiketle
ara. Uyan varsa onu kullan ve "Kullanıldığı" sütununa videoyu ekle. Yakın ama tam uymayan varsa
kullanıcıya göster: çoğu zaman yeter, üretim ise para ve zaman ister.

## Kural: üretince aynı turda etiketle

Bir varlık üretildiği ya da indirildiği turda kaydı yazılır. Kayıt şunları taşır:

| Alan | İçerik |
| --- | --- |
| Kimlik | Kısa ve kalıcı ad, dosya adıyla aynı (`ses-kagit-dusme-01`) |
| Tür | `ses`, `muzik`, `video`, `gorsel`, `logo` |
| Etiketler | Sözlükten, virgülle (aşağıda) |
| Dosya | Kütüphane köküne göre yol |
| Süre ya da boyut | Ses ve video için saniye, görsel için piksel |
| Kaynak | Hangi araç ya da model, hangi sürüm, hangi tarih; indirildiyse nereden |
| Girdi | Üretildiyse prompt ve verilen referans dosyaları |
| Lisans | Nerede kullanılabilir, atıf gerekir mi |
| Kullanıldığı | Hangi videolarda, hangi anda |
| Not | Kalite, bilinen kusur, kullanıcının yorumu |

Dosyaya anlamlı ad ver. `output_3.mp4` yerine `video-masa-ustu-yakin-01.mp4`.

## Etiket sözlüğü

Etiketler serbest metin değildir. `Kutuphane.md` içindeki sözlükten seçilir. Yeni bir etiket
gerekiyorsa önce sözlüğe eklenir, sonra kullanılır. Böylece aynı şey bir yerde `gecis`, başka
yerde `transition` diye geçmez.

Sözlük dört eksenle başlar, kullanıcının işine göre büyür:

- **İşlev:** ne için kullanılır (geçiş, vurgu, zemin, açılış, kapanış gibi)
- **Duygu:** nasıl hissettirir (sakin, gergin, neşeli gibi)
- **İçerik:** içinde ne var (kağıt, klavye, şehir, el gibi)
- **Biçim:** teknik özellik (döngü, şeffaf, dikey, sessiz gibi)

## Üretilmiş video ve görsel

Bir üretim modeliyle (video ya da görsel) çıkan her kabul edilmiş sonuç kütüphaneye girer. Bunlar
için iki şey ayrıca önemlidir:

- **Girdi tam yazılır.** Prompt, verilen referans görseller, süre, oran ve model sürümü. Aynı
  görünümü yeniden üretmenin tek yolu budur.
- **Reddedilen de yazılır.** Kullanıcının beğenmediği üretimin dosyası tutulmaz ama girdisi ve
  neden reddedildiği `Kutuphane.md` içindeki "İşe yaramayan girdiler" başlığına tek satır olarak
  yazılır. Aynı prompta ikinci kez para harcanmaz.

İşe yarayan bir prompt kalıbı kendisi de bir varlıktır. İki kez sonuç verdiyse "Prompt kalıpları"
başlığına, değişen yerleri işaretlenmiş olarak yaz.

## Ses ve müzik

- Lisansı boş bırakma. Bilinmiyorsa "bilinmiyor, yayında kullanma" yaz.
- Aynı efekt arka arkaya iki sahnede duyuluyorsa izleyici fark eder. "Kullanıldığı" sütunu bunun
  için vardır: yerleştirmeden önce aynı videoda nerede geçtiğine bak.
- Seviyesi ölçüldüyse (LUFS, tepe) nota yaz, her videoda yeniden ölçme.

## Bakım

- Diskte olmayan dosyanın kaydını silme, notuna "dosya yok" yaz ve kullanıcıya söyle.
- Kullanıcı bir varlığı "bunu bir daha kullanma" diye işaretlediyse etiketlerine `kullanma` ekle,
  nedenini nota yaz.
- Kayıt sayısı birkaç yüzü geçince `Kutuphane.md` türlere göre ayrı dosyalara bölünebilir. Böldüğün
  gün bunu `Araclar.md` içine yaz.
- Parola, hesap anahtarı ya da ödeme bilgisi kayıtlara girmez.
