# Video motoru (isteğe bağlı başlangıç paketi)

Boş başlayan bir video motoru. Ajanın [HyperFrames](https://github.com/heygen-com/hyperframes) ile
video üretir ve her işte senin markanı, düzeltmelerini ve çalışan sahneleri vault'undaki bir
deftere yazar. İlk videoda hiçbir şey bilmez. Onuncu videoda renklerini, tempoyu, neyi sevmediğini
ve hangi sahneyi yeniden kullanabileceğini bilir.

Hazır bir stil, marka, ses ya da görsel içermez. Paketin içinde yalnız yönergeler ve boş şablonlar var:

- `SKILL.md`: ajanın ne zaman okuyacağını, ne zaman yazacağını ve nasıl not alacağını anlatır.
- `KURULUM.md`: ajan için kurulum notları (Node.js, FFmpeg, HyperFrames, skill paketi, telemetri).
- `REFERANS.md`: beğendiğin bir videodan öğrenme standardı. Ajan videoya kare kare bakar, onu iyi
  yapan mekanizmaları adlandırır, karesini saklar ve onayladıklarını kurala çevirir.
- `KUTUPHANE.md`: ses, müzik, üretilmiş video, görsel ve logo için etiketli kütüphane. Ajan her
  varlığı üretildiği anda etiketler ve yenisini üretmeden önce kütüphanede arar.
- `sablonlar/`: boş defter dosyaları (`Marka`, `Dersler`, `Sahne-Envanteri`, `Araclar`,
  `Acik-Isler`, `Referanslar`, `Kutuphane`) ve video başına doldurulan `KURGU-NOTU`.

## Kurulum

Bu paket çekirdek kurulumun parçası değildir, kurucu onu kendiliğinden getirmez. İstersen
vault klasöründe:

```text
python3 beyin.py skill-import --source /yol/avenoxbeyin/extras/video-motoru
```

Sonra ajanına **"video motorunu kur"** de. Ajan ön koşulları kontrol eder, defterin nereye
kurulacağını sorar ve kısa bir marka görüşmesi yapar. Bilmediğin soruyu boş geç, defter zamanla
dolar.

## Bilmen gerekenler

- HyperFrames üçüncü taraf bir araçtır. Node.js 22+, FFmpeg ve ilk çağrıda internet ister,
  kullanım sayaçları gönderir (kapatılabilir, `KURULUM.md` bölüm 4).
- Ağır medya vault'a girmez. İş klasörleri diskte ayrı bir yerde durur, vault'ta yalnız defter.
- Defter düz Markdown'dır. Elle düzeltebilir, satır silebilir, başka ajana okutabilirsin.
- Paket içe aktarıldıktan sonra senindir. Güncelleme onu ezmez, sen de istediğin gibi
  değiştirebilirsin.
