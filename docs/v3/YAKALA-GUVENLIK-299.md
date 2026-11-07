# Yakala güvenlik güncellemesi (#299 / #302)

Bu güncelleme adres ayrıştırıcıları arasındaki farkları kapatır. Host'ta yüzde kodlaması,
ASCII dışı karakterler ve alternatif sayısal IP yazımları reddedilir. İçerik işlenirken
Defuddle veya YouTube yardımcısı başlamadan önce DNS sonuçlarının tamamı kontrol edilir;
`localtest.me` ve özel IP'ye çözülen `nip.io` adresleri de reddedilir.

Dosya bağlantılarında UNC, sürücü harfi, üst dizin ve Windows aygıt adları `resolve()`
çalışmadan reddedilir. Göreli dosya yolu çözüldükten sonra vault içinde kalması da kontrol edilir;
Windows junction'ları dışarıdaki dosyaları okutamaz.

Yeni `.ham/` kayıtlarında her dosyaya özgü rastgele kimlikle açılış ve kapanış işareti vardır.
Kaynağın kendi yazdığı "kaynak bitti" veya "kullanıcı talimatı" başlıkları bu sınırı kapatmaz.
İçerik yine güvenilmeyen veridir; bu işaretler prompt injection'a karşı tek başına güvence değildir.
Eski ham kayıtlar değiştirilmez; gerektiğinde kartı `yakala isle <id>` ile yeniden işle.

## Mevcut Yakala kurulumunu yenile

Beyin güncellemesi betiği yeniler; `yakala kur` tarafından yazılmış `beyin-yakala` skill'i
kendiliğinden yenilenmez. Bu güvenlik güncellemesini aldıktan sonra vault klasöründe bir kez:

```powershell
py -3 beyin.py yakala kur
```

macOS / Linux'ta `python3 beyin.py yakala kur`. Yalnız şablon ve skill kurduysan aynı komuta
`--kisayol-yok` ekle. Kayıtlı tuş ve gelen kutusu seçimi korunur. Kurulum işaretini taşıyan
skill kopyaları yeni güvenilmeyen içerik ve sınır kurallarını alır; kullanıcının kendi yazdığı
işaretsiz skill değiştirilmez. Test, eski işaretli skill'in yenilenmesini paket kurulumunda doğrular.

## Sınırlar

Yakalama ağa çıkmaz; bu PR'daki mevcut davranış gereği yerel/intranet URL'leri yakalamada da
reddedilir. Önceden alınmış Web Clipper içeriği ağ bağlantısı ve DNS sorgusu olmadan işlenir,
ancak kartın URL'si aynı sözdizimi/IP kontrolünden geçer.

DNS kontrolü ile yardımcının bağlantısı arasında DNS cevabı değişebilir (TOCTOU / rebinding).
Harici Defuddle/yt-dlp süreçlerinin yönlendirmeleri de bu kontrolü atlayabilir.
Bu kalan sınırlar için ayrı ağ izolasyonu gerekir.

Dosya kartı yalnız kendi `dosyalar/` klasöründeki yakalanmış dosyaları okur; vault içindeki kasa
türü klasörler, `.obsidian/` ve başka notlar `## Dosyalar` bağıyla `.ham/` içine kopyalanamaz.
