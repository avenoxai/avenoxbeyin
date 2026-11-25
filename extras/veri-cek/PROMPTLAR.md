# Veri çek: prompt şablonları

Bunlar başlangıç metinleridir. Kullanıcının diline çevir, `<...>` yerlerini doldur, o ana kadar
öğrendiklerine göre daralt. Her promptu kullanıcıya tek bir kod bloğunda ver.

## Envanter

İlk tur. Amaç bilgi almak değil, uygulamanın elinde ne olduğunu ve ona nasıl ulaştığını
görmek.

```text
Seni uzun süredir kullanıyorum ve benim hakkımda bildiklerini kendi not sistemime taşıyorum.
Bu ilk adım: elinde ne olduğunun dökümünü istiyorum, ayrıntıyı sonra soracağım.

Şu bölümleri sırayla, düz Markdown olarak yaz:

1. Erişim: Benim hakkımda kayıtlı hafızan var mı? Geçmiş sohbetlerime bakabiliyor musun?
   Özel talimatlarımı görebiliyor musun? Her biri için evet ya da hayır, tek satır.
2. Kayıtlı hafıza: Hakkımda kayıtlı her maddeyi, değiştirmeden ve özetlemeden, numaralı liste
   olarak yaz.
3. Özel talimatlar: Sana verdiğim kalıcı talimatları olduğu gibi yaz.
4. Projeler ve işler: Benimle üzerinde çalıştığın her proje ya da iş için tek satır. Adı, ne
   olduğu, son durumu.
5. Sık dönen konular: Projeler dışında sana en çok ne sordum? Konu başına tek satır.
6. Kişiler: Sohbetlerimizde adı geçen kişiler ve benimle ilişkileri, kişi başına tek satır.

Her maddenin sonuna kaynağını yaz: (hafıza), (geçmiş sohbet) ya da (tahmin).
Bilmediğin bölüme "bilmiyorum" yaz. Tahmin yürütme, boşluk doldurma.
Parola, anahtar, kart ya da kimlik numarası gibi değerleri yazma; yalnız var olduklarını söyle.
Cevap yarıda kesilirse "devam" yazacağım, kaldığın yerden sürdür.
```

## Derinleşme

Seçilen her başlık (ya da en çok üç yakın başlık) için bir tur.

```text
Benim hakkımda bildiklerini kendi not sistemime taşımaya devam ediyorum. Bu turda yalnız şu
konuyu istiyorum: <başlık>

Şimdiye kadar bildiğim: <iki üç satır özet>
Bunları tekrar etme, ötesini yaz.

Şu başlıklarla, düz Markdown olarak:

- Durum: Bu konu bugün nerede duruyor? En son ne konuştuk?
- Kararlar: Verdiğim kararlar ve her birinin nedeni. Tarihini biliyorsan yaz.
- Denenip bırakılanlar: Neyi denedik, neden vazgeçtik?
- Tercihlerim: Bu konuda nasıl çalışmayı sevdiğim, neyi istemediğim. Varsa kendi cümlemle.
- Kişiler ve araçlar: Adı geçen kişiler, şirketler, kullandığım araçlar.
- Açık kalanlar: Yarım kalan iş, cevaplanmamış soru, verdiğim söz.

Her maddenin sonuna kaynağını yaz: (hafıza), (geçmiş sohbet) ya da (tahmin).
Emin olmadığın yere "emin değilim" yaz, bilmediğine "bilmiyorum". Tahmin etme.
Parola, anahtar, kart ya da kimlik numarası gibi değerleri yazma.
Cevap yarıda kesilirse "devam" yazacağım, kaldığın yerden sürdür.
```

## Boşluk sorusu

Bir cevap belirli bir noktayı havada bıraktıysa kısa bir ara tur.

```text
Bir önceki cevabında şunu yazdın: "<alıntı>"
Bunu açar mısın: <tek, somut soru>
Bilmiyorsan "bilmiyorum" yaz.
```

## Kapanış sorusu

Bir uygulamayı kapatmadan önce son tur.

```text
Benim hakkımda bildiklerini kendi not sistemime taşıdım. Şu başlıkları aldım:
<işlenen başlıkların listesi>

Bu listenin dışında, benimle çalışacak birinin bilmesi gereken ve henüz sormadığım bir şey var
mı? Varsa konu başına tek satır yaz, yoksa "yok" yaz.
```
