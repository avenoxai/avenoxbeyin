---
name: video-motoru
description: Boş başlayan, her videoda senin markanı ve geri bildirimini not alarak gelişen video motoru (HyperFrames ile HTML'den video). Tetik - "video yap", "motion video", "grafik yap", "videoyu kurgula", "video motorunu kur", "marka defteri", "kurgu notu". Kullanma - tek not kaydetmek veya kasada aramak (beyin), kurulum sorunu (beyin-doktor).
---

# Video motoru

Bu skill hazır bir stil getirmez. İlk gün elinde yalnız boş bir defter ve kurulum notları vardır.
Motoru büyüten şey senin her işte tuttuğun notlardır: kullanıcının markası, sevdiği ve reddettiği
şeyler, çalışan sahneler, yazılan araçlar. Aynı düzeltmeyi kullanıcıdan iki kez duyuyorsan defter
işini yapmamış demektir.

Videoyu üreten araç [HyperFrames](https://github.com/heygen-com/hyperframes) (HTML'i kare kare
videoya çevirir). Üçüncü taraf bir araçtır, avenoxbeyin'in parçası değildir. Kurulumu ve komutları
[KURULUM.md](KURULUM.md) içinde.

## Defter

Defter kullanıcının vault'unda, `Video-Motoru` adlı bir klasörde durur. Yerini bilmiyorsan
vault'ta `Video-Motoru/Marka.md` dosyasını ara. Yoksa "İlk çalıştırma" bölümüne geç.

| Dosya | Ne tutar | Ne zaman yazılır |
| --- | --- | --- |
| `Marka.md` | Renk, yazı tipi, ton, logo, yasaklar, format | Marka hakkında yeni bir şey öğrenince |
| `Dersler.md` | Tarihli dersler, en yenisi üstte | Kullanıcı bir şeyi düzeltince ya da onaylayınca |
| `Sahne-Envanteri.md` | Yapılmış sahne türleri ve dosya yolları | Yeniden kullanılabilir bir sahne çıkınca |
| `Araclar.md` | Kurulu araçlar, sürümler, yazılan betikler | Bir şey kurunca ya da betik yazınca |
| `Acik-Isler.md` | "Bir dahaki sefere" kalanlar | İş biterken |

Her video ayrıca kendi iş klasöründe bir `KURGU-NOTU.md` taşır.

Ağır medya (ham çekim, render, ses) vault'a girmez. Vault eşitleniyorsa büyük dosyalar onu
kilitler. İş klasörleri vault dışında durur, defterde yalnız yolları yazar.

## İlk çalıştırma

Defter yoksa sırayla:

1. [KURULUM.md](KURULUM.md) adımlarını uygula. Eksik olanı kullanıcıya söyle, onun adımı olanı
   onun yerine yapma.
2. Kullanıcıya iki şeyi sor: defter vault'ta nereye kurulsun, iş klasörleri diskte nerede dursun.
   Cevaba göre `Video-Motoru` klasörünü aç ve bu skill'in `sablonlar/` klasöründeki beş dosyayı
   oraya kopyala. İş klasörü kökünü `Araclar.md` içine yaz.
3. Marka görüşmesini yap. Soruları `Marka.md` içindeki başlıklardan al, bir seferde en çok üç
   soru sor. Kullanıcının cevaplamadığı başlığı boş bırak. Tahminle doldurma: boş başlık dürüst,
   uydurulmuş marka kuralı zararlıdır.
4. Kullanıcının elinde beğendiği bir video, site ya da görsel varsa iste. Ondan çıkardığın her
   kuralı kullanıcıya göster, onayladığını `Marka.md` içine yaz.
5. Kurduğun sürümleri ve tarihi `Araclar.md` içine yaz.

Marka boşken de video yapılır. O durumda sade bir varsayılanla başla ve kullanıcıya bunun
varsayılan olduğunu söyle. İlk önizlemeye gelen tepki defterin ilk gerçek satırlarıdır.

## Her işte

**Başlarken.** `Marka.md` dosyasının tamamını, `Dersler.md` dosyasının `[kalıcı]` satırlarını ve
son yirmi satırını, `Acik-Isler.md` dosyasını oku. `Sahne-Envanteri.md` içinde yapacağın sahneye
benzeyen varsa sıfırdan yazma: kopyala, uyarla. Alt ajan kullanıyorsan ilgili dersleri ve örnek
sahne yollarını brifine koy, "deftere bak" deme.

**Üretirken.** HyperFrames'in kendi skill'lerini izle (`hyperframes`, `hyperframes-cli`). Bu skill
onların yerini tutmaz, üstüne marka ve hafıza katmanı koyar. İkisi çelişirse render sözleşmesinde
HyperFrames, görünüm ve tonda defter kazanır.

**Göstermeden bitirme.** Önce önizleme ya da taslak render ver, kullanıcının notunu bekle. Son
render onaydan sonra gelir. Kullanıcı "sorma, bitir" dediyse bunu `Dersler.md` içine tercih olarak
yaz ve ona göre çalış.

**Geri bildirim gelince.** Düzeltmeyi yap, sonra aynı turda deftere yaz. İş bitince yazarım deme,
unutulur.

**Bitince.** İş klasörüne `sablonlar/KURGU-NOTU.md` dosyasını kopyalayıp doldur. Yeni sahneleri
`Sahne-Envanteri.md` içine, yazdığın betikleri `Araclar.md` içine, yarım kalanları `Acik-Isler.md`
içine taşı. Alt ajanlar deftere kendileri yazmaz, bulgularını raporlar. Defteri tek el yazar.

## Not alma kuralları

- **Biçim:** `- 2026-01-15 [video-adı] Ne öğrenildi. Neden: kullanıcının gerekçesi.` Gerekçe yoksa
  sor ya da "Neden: belirtilmedi" yaz. Gerekçesiz kural başka bir duruma uygulanamaz.
- **Kullanıcının sözünü koru.** "Renkler soğuk kalmış" dediyse bunu yaz. Kendi yorumunu
  ekleyeceksen "Yorum:" diye ayır.
- **Tek seferlik istek ders değildir.** "Bu videoda başlık kırmızı olsun" o işin notudur.
  "Başlıklarım hep böyle" bir derstir. Emin değilsen bir kez sor.
- **Onay da derstir.** Kullanıcı bir sahneyi beğendiyse neyi beğendiğini yaz. Yalnız hataları
  tutan defter motoru çekingen yapar.
- **`[kalıcı]`.** Aynı ders ikinci kez çıktıysa ya da kullanıcı "hep böyle" dediyse satırı
  `[kalıcı]` diye işaretle ve kuralı `Marka.md` içine taşı. Taşıdığını satırın sonuna yaz.
- **Çelişki.** Yeni ders eskisiyle çelişiyorsa eskiyi silme. Üstünü çiz, yanına yenisinin
  tarihini yaz. Kullanıcı fikir değiştirmiş olabilir, neden değiştiği de bilgidir.
- **Budama.** `Dersler.md` iki yüz satırı geçince `[kalıcı]` olmayan ve altı aydır kullanılmayan
  satırları dosyanın sonundaki "Arşiv" başlığına indir. Silme.
- **Gizlilik.** Deftere parola, API anahtarı, token ya da ödeme bilgisi yazma. Yalnız nerede
  durduklarını yaz.

## Sınırlar

- Yayınlama, bir yere yükleme ve mesaj gönderme kullanıcının açık onayını ister.
- Ücretli bir servis (bulut render, görsel ya da ses üretimi) kullanmadan önce sor.
- HyperFrames skill paketini kurmak kullanıcının adımıdır. Eksikse komutu söyle, kendin çalıştırma.
- Başkasının videosundan stil öğrenirken mekanizmayı al, markayı ve içeriği kopyalama.
