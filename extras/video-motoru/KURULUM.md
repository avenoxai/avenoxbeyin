# Kurulum notları (ajan için)

Bu sayfa ilk çalıştırmada ve bir komut "bulunamadı" dediğinde okunur. Komutlar HyperFrames
sürümüyle değişebilir. Buradaki bir komut çalışmazsa `npx hyperframes <komut> --help` çıktısı
esastır, bu sayfa değil. Farkı görünce `Araclar.md` içine not düş.

## 1. Ön koşullar

HyperFrames'in istediği iki şey var: Node.js 22 ya da üstü ve FFmpeg.

```text
node --version
ffmpeg -version
```

Eksik olanı kullanıcıya söyle ve kurmadan önce onayını al. Yaygın yollar:

| Sistem | Node.js | FFmpeg |
| --- | --- | --- |
| macOS (Homebrew) | `brew install node` | `brew install ffmpeg` |
| Windows (winget) | `winget install OpenJS.NodeJS.LTS` | `winget install Gyan.FFmpeg` |
| Linux | dağıtımın paket yöneticisi ya da nvm | dağıtımın paket yöneticisi |

Kurulumdan sonra yeni bir terminal açılmadan `node` görünmeyebilir. Sürümü yeniden kontrol et.

## 2. HyperFrames

Ayrı bir kurulum gerekmez, `npx` ilk çağrıda paketi indirir.

```text
npx hyperframes doctor --json
```

`doctor` her durumda 0 ile çıkar. Sonucu çıkış koduna göre değil, JSON'daki `ok` alanına ve
`checks` listesindeki başarısız satırlara göre oku. Tarayıcı eksikse:

```text
npx hyperframes browser ensure
```

## 3. HyperFrames skill'leri

Kare sözleşmesini, komut döngüsünü ve hazır blokları anlatan skill paketi ayrıca kurulur. Bu
kullanıcının adımıdır, sen çalıştırma. Önce durumu oku:

```text
npx hyperframes skills check
```

Eksikse kullanıcıya şunu çalıştırmasını söyle:

```text
npx hyperframes skills
```

HyperFrames CLI çalışmıyorsa aynı paket `npx skills add heygen-com/hyperframes --all` ile de
gelir. Skill'ler kurulduktan sonra yeni bir oturum gerekebilir.

## 4. Telemetri

HyperFrames kullanım sayaçları gönderir. Bunu kullanıcıya ilk kurulumda bir kez söyle, karar onun.

```text
npx hyperframes telemetry status
npx hyperframes telemetry disable
```

Ortam değişkeni tercih edilirse `HYPERFRAMES_NO_TELEMETRY=1` aynı işi yapar.

## 5. İlk proje

İş klasörü vault dışında durur. Kullanıcının seçtiği kökte:

```text
npx hyperframes init <is-klasoru> --skip-skills
```

`--skip-skills` her zaman verilir, yoksa `init` skill kurmaya kalkar.

## 6. Çalışma döngüsü

```text
npx hyperframes lint
npx hyperframes check --json
npx hyperframes snapshot
npx hyperframes preview --background
npx hyperframes render --quality draft -o taslak.mp4
npx hyperframes render --quality delivery -o final.mp4
```

- `check` kapıdır: hata kalmışken render alma. Lint hatası varken yerleşim ve kontrast denetimi
  hiç çalışmaz, "0 samples" hiçbir şeyin denetlenmediği anlamına gelir.
- `snapshot` videoyu görmenin yoludur. Yazdığı PNG'lere gerçekten bak, kontrolden geçti diye
  görmeden teslim etme.
- Render bitince dosyanın boş olmadığını ve süresinin beklenenle tuttuğunu `ffprobe` ile doğrula.

## 7. Deftere yaz

Kurulum bitince `Araclar.md` içine tarihle birlikte şunları yaz: Node.js, FFmpeg ve HyperFrames
sürümleri, işletim sistemi, iş klasörü kökü, telemetri kararı. Bir sonraki oturumda kurulumu
yeniden sorgulamak yerine bu satırlara bakarsın.

Bu makinede bir komutun farklı davrandığını görürsen (yol, izin, kod sayfası, eksik codec) onu da
aynı dosyaya "Bu makineye özel" başlığı altına yaz. Kurulum notları da defterin parçasıdır ve
aynı şekilde büyür.
