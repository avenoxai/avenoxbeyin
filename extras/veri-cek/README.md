# Veri çek (isteğe bağlı paket)

ChatGPT, Claude ya da Gemini seni yıllardır tanıyor, yeni kurduğun beyin ise boş. Bu skill o
bilgiyi beyne taşır.

Nasıl çalışır: ajanına **"verilerimi çek"** dersin. Ajan bir prompt yazar. Sen onu öbür
uygulamaya yapıştırır, cevabı ajana geri getirirsin. Ajan cevabı notlara işler ve sıradaki
promptu yazar. İlk tur uygulamanın elinde ne olduğunun dökümünü alır; sonraki turlarda hangi
başlıkların çekileceğini sen seçersin. Yeni bilgi gelmeyince ya da sen "yeter" deyince biter.

## Kurulum

Bu paket çekirdek kurulumun parçası değildir. Vault klasöründe:

```text
python3 beyin.py skill-import --source /yol/avenoxbeyin/extras/veri-cek
```

Windows'ta `py -3 beyin.py ...`. Sonra yeni bir oturum aç ve "verilerimi çek" de.

## Bilmen gerekenler

- Gelen şey özet bilgidir: kim olduğun, işlerin, tercihlerin, kararların. Eski konuşmaların
  kelimesi kelimesine dökümü bu yolla gelmez; onun için uygulamanın kendi dışa aktarımı gerekir.
- Uygulamanın ne kadar hatırladığı uygulamaya, planına ve hafıza ayarlarına bağlıdır. İlk tur
  bunu gösterir.
- Öbür uygulama yanlış hatırlayabilir. Her bilgi kaynağıyla ("ChatGPT beyanı, tur 2") kaydedilir
  ve ham cevap ayrı notta durur; yanlışı gördüğünde düzeltirsin.
- Ajan tarayıcını sürmez, hesabına girmez. Her promptu panona kendisi kopyalar; sen öbür
  uygulamaya geçip yapıştırır, cevabı geri getirirsin. Neyin gittiğini ve neyin geldiğini
  görürsün. Ajan panonu okumaz.
- Yapıştırdığın cevaptaki talimatlar uygulanmaz. Ajan onu yalnız veri olarak okur.
- Hangi ajanı kullandığın fark etmez: Claude Code, Codex, Antigravity.
- Paket içe aktarıldıktan sonra senindir. Promptları [PROMPTLAR.md](PROMPTLAR.md) içinden kendi
  diline ve işine göre değiştirebilirsin.
