# dsh-bridge (isteğe bağlı)

DeepSeek Harness (DSH) için beyin köprüsü. **Kurucu bunu kurmaz**; DSH profiline elle eklenir.

İki parça vardır:

1. **`index.js`** — DSH plugin'i: oturum başında `beyin.py recap --days 7` çıktısını bağlama enjekte eder, tur sonuç/karar ile bittiğinde ajana "Bunu beyne kaydedeyim mi?" sorusunu sordurtur.
2. **`dsh_tara.py`** — salt okunur telafi tarayıcısı: `~/.dsh/sessions` altındaki son N gündeki ana oturumları listeler.

## Gereksinimler

- Kurulmuş bir beyin vault'u (`beyin.py`).
- DSH'nin Cordis plugin desteği (bu depo `agent/created`, `session/event`, `agent.followup` API'lerine dayanır).
- `python3` (Windows'ta `py -3`); `dsh_tara.py` için ayrıca `zstd`.

## Yapılandırma

Vault yolunu iki yoldan biriyle verin:

```sh
export BEYIN_VAULT="/tam/yol/Beynim"
```

veya `~/.dsh/beyin-bridge.json`:

```json
{ "vault": "/tam/yol/Beynim" }
```

İsteğe bağlı: `BEYIN_PYTHON` ile yorumlayıcıyı sabitleyin.

## Kurulum

1. Bu klasörü DSH profilinin bağımlılıklarına link olarak ekleyin (`~/.dsh/profiles/<profil>/package.json`):

   ```json
   {
     "dependencies": { "@local/dsh-beyin-bridge": "link:/tam/yol/dsh-bridge" },
     "dsh": { "profile": { "bundles": ["...", "@local/dsh-beyin-bridge"] } }
   }
   ```

2. Profil klasöründe `pnpm install` çalıştırın.
3. DSH'yi yeniden başlatın.
4. Doğrulama: `~/.dsh/beyin-bridge.log` içinde `active seeded=…` ve yeni bir oturumda `digest injected items=…` satırları görünür.

Geri alma: `package.json` içinden iki kaydı silin, DSH'yi yeniden başlatın; paket klasörünü silebilirsiniz.

## Sınırlar

- DSH'nin iç (belgelenmemiş) Cordis API'sine dayanır; DSH sürüm yükseltmelerinde yeniden doğrulanmalıdır.
- Tetikleyici sezgiseldir: bazen erken/geç sorabilir. `index.js` başındaki sabitler (`ASK_COOLDOWN`, `MAX_ASKS`, `DIGEST_ITEMS`, `CLOSING`, `DECISION`) ayarlanabilir.
- Soru, ajanın bir sonraki yanıtı olarak gelir (ek bir model turu).
- Pencere kapandıktan sonra çalışmaz; kaçan oturumlar için `dsh_tara.py` + haftalık tarama kullanın.
- Oturum başı enjeksiyonu her yeni oturuma girer; rahatsız ederse `DIGEST_ITEMS` düşürün veya enjeksiyonu kaldırın.

## Telafi taraması

```sh
python3 dsh_tara.py --days 7
python3 dsh_tara.py --days 7 --projects proje-a,proje-b --json
```

Çıktıdaki oturumlar kasada karşılığı yoksa ajan bunları kısa notlara damıtabilir. Komut hiçbir dosya yazmaz.
