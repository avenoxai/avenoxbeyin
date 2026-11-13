# DeepSeek Harness (DSH): açık CLI kullanımı

V3 `context --harness dsh` ve `receipt --harness dsh` değerlerini tanır. Bu destek
istemciye özgü otomatik hook, oturum köprüsü veya canlı bağlam teslimi anlamına gelmez.
DSH iç API'sine veya Node bağımlılığına ihtiyaç duymaz.

Kurulu CLI'ı açıkça çağırın:

```sh
python3 "<vault>/beyin.py" --vault "<vault>" context "<soru>" --harness dsh --project <proje>
python3 "<vault>/beyin.py" --vault "<vault>" receipt --harness dsh --event-id "<tekil>" --summary "<özet>" --ref "<not yolu>"
```

Windows'ta `python3` yerine `py -3` kullanılabilir. Aynı kaynak, gizlilik ve proje
filtreleri geçerlidir; yeni bir otomatik not aktarımı yoktur.

## Makbuz ve rollback uyumu

DSH makbuzları `harness: manual` + `client: dsh` olarak yazılır. Yeni sürüm `dsh`
olarak okur; 3.12.0 gibi eski okuyucular desteklemedikleri yeni `harness` değerini
almak yerine `manual` görür. İstemci tercihi `.beyin-preferences.json` içine
kaydedilmez. Eski sürüme dönüşte DSH makbuzları korunur; `--harness dsh` seçeneğinin
eski CLI'da bulunması beklenmez.

## Doğrulama sınırı

Kurulu paket üzerinde CLI, makbuz tekrarları, yeniden eşitleme ve kaynak filtreleri
sentetik vault testleriyle doğrulanır. DSH canlı istemcisi, talimat/skill keşfi ve
hook teslimi bu desteğin parçası değildir. #673'teki isteğe bağlı oturum köprüsü ve
oturum tarayıcısı bu dar değişikliğe dahil edilmemiştir; ayrı güvenlik ve yaşam
döngüsü incelemesi gerektirir. [Gerçek istemci kontrolleri](LIVE-CLIENTS.md).
