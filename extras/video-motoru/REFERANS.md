# Referanstan öğrenme (ajan için)

Kullanıcı beğendiği bir videoyu gösterip "böyle olsun" ya da "bundan öğren" dediğinde bu sayfayı
izle. Amaç videoyu kopyalamak değil, onu iyi yapan mekanizmaları adlandırıp deftere geçirmek.
Sonuç `Referanslar.md` içine yazılır ve sonraki her videoda okunur.

## 1. Kaynağı al

- Kullanıcı dosya verdiyse onu kullan. Bağlantı verdiyse indirmeden önce sor: indirme aracı
  kurmak ve dosyayı diske yazmak kullanıcının onayını ister.
- Video dosyası vault'a girmez. İş klasörü kökünde `referanslar/<kaynak-adi>/` altına koy.
  Klasöre kısa ve nötr bir ad ver (`referanslar/sakin-anlatim-1/` gibi).
- Kullanıcıya tek soru sor: bu videoda neyi beğeniyor? Cevabı kendi sözleriyle `Referanslar.md`
  içine yaz. Cevap "bilmiyorum, genel hali" ise bunu da yaz ve analizi dar tutma.

## 2. Videoya bak

Dökümden tahmin yürütme, karelere bak. Önce genel bakış şeridi:

```text
ffmpeg -i kaynak.mp4 -vf "fps=1/10,scale=320:-1,tile=5x4" serit-%02d.jpg
```

Bu her on saniyeden bir kare alıp yirmilik sayfalara dizer. Kesim yerlerini bulmak için:

```text
ffmpeg -i kaynak.mp4 -vf "select='gt(scene,0.3)',showinfo" -f null - 2> kesimler.txt
```

`kesimler.txt` içindeki `pts_time` değerleri kesim anlarıdır. Bir mekanizmayı yakından görmek
için o andan birkaç kare al:

```text
ffmpeg -ss 00:01:23 -i kaynak.mp4 -frames:v 1 -vf scale=640:-1 kare-0123.jpg
```

Ses önemliyse konuşmanın dökümünü çıkar (`npx hyperframes transcribe`) ve müziğin, efektlerin
nerede girip çıktığını not et.

## 3. Mekanizmaları adlandır

Her bulgu tek bir mekanizmadır ve dört şey taşır:

| Alan | İçerik |
| --- | --- |
| Zaman | Videoda nerede (`01:23-01:31`) |
| Ne oluyor | Gördüğün şey, ölçüyle: kaç saniye, ekranın neresinde, kaç öğe |
| Neden çalışıyor | İzleyicide ne yapıyor |
| Bizde nasıl | Kendi markamızla nasıl uygulanır, hangi sahne türüne denk gelir |

Bakılacak yerler: açılışın ilk on saniyesi, kesim sıklığı, yazının ekranda kalma süresi ve
boyutu, renk sayısı, geçiş türleri, yakınlaştırmalar, müziğin ve efektlerin konuşmayla ilişkisi,
boş bırakılan yerler, kapanış.

"Güzel görünüyor" bir bulgu değildir. "Her rakam tek başına, ekranın ortasında, 1,5 saniye
kalıyor ve altında tek satır açıklama var" bir bulgudur.

## 4. Kareyi sakla

Her mekanizmanın yanında onu gösteren kare durur, yoksa üç ay sonra not anlaşılmaz. Kareleri
küçült (en çok 640 piksel genişlik, JPEG) ve defterin yanındaki `referans-kareleri/<kaynak-adi>/`
klasörüne koy. Küçük kareler vault'ta durabilir, video durmaz. Kareyi yazdıktan sonra aç ve
anlattığın anı gerçekten gösterdiğini kontrol et.

## 5. Kullanıcıya göster, sonra yaz

Bulguları kullanıcıya kısa bir liste olarak göster ve hangilerini istediğini sor. Yalnız
onaylananlar kurala dönüşür:

- Her videoda geçerli olan `Marka.md` içine,
- Belirli bir sahne türüne ait olan `Sahne-Envanteri.md` içine "referans" notuyla,
- Denenecek ama henüz kesinleşmemiş olan `Acik-Isler.md` içine gider.

Reddedilen bulguyu da `Referanslar.md` içinde "alınmadı" diye bırak. Aynı öneriyi ikinci kez
getirmemek için.

## Sınırlar

- Mekanizmayı al, içeriği alma. Başkasının görüntüsü, müziği, logosu, metni ve karakteri kendi
  videona girmez.
- Tek bir referans marka değildir. Referanstan gelen kural kullanıcının `Marka.md` içindeki açık
  bir tercihiyle çelişiyorsa kullanıcıya sor.
- Kareler çalışma notudur. Onları yayınlama, paylaşma.
