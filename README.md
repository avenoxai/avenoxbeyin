# 🧠 avenoxbeyin v2.1: hatırlamayı unutmayan ikinci beyin

[Obsidian](https://obsidian.md) + [Claude Code](https://claude.com/claude-code) veya Codex üstünde çalışan,
açık kaynak bir **ikinci beyin**. Yerel bir Markdown vault, kalıcı hafıza, sıfır bağımlılık,
sıfır ekstra ücret. Dosya yönetmezsin, konuşursun.

**v1'in tezi devamlılıktı: oturum açılınca geçen oturum bağlama giriyordu.** İşe yarıyordu ama tek
bir kırılgan varsayıma dayanıyordu: modelin oturum biterken hafıza dosyalarını güncellemeyi
hatırlaması. Hatırlamadığı her seferde o gün kayboluyordu. **v2'nin tezi şu: hafıza rica değil,
mekanizmadır.** Artık oturum kapanışını bir kanca yakalıyor, konuşmayı arka planda özetleyip
`daily/` altına günlük log olarak yazıyor, akşamları günde bir kez bir derleyici o logları
`knowledge/` altında birbirine bağlanan makalelere dönüştürüyor. Ertesi sabah bu bilgi tabanının
indeksi kendiliğinden bağlama giriyor. Kimsenin bir şey yazmayı hatırlaması gerekmiyor.

Video izlemene gerek yok, kurulum videosu da yok. Aşağıdaki tek satırı yapıştır, kurulumu Claude
Code'un kendisi yapar.

---

## Hızlı başlangıç

Terminalde `claude` çalıştır ve şunu yapıştır:

```
Read https://avenox.lol/beyin.md and follow it exactly to build my second brain.
```

Bu tek satırlık giriş önce platformu ayırır: macOS/Linux mevcut `SETUP.md` yolunda kalır, yerel
Windows `SETUP-WINDOWS.md` + PowerShell kurucusuna geçer, WSL ise tamamen aynı Linux dağıtımı
içinde kalan POSIX yolunu kullanır. Böylece Windows'ta Bash/macOS komutları çalıştırılmaz.

Ya da macOS, Linux veya WSL'de repoyu doğrudan klonla, üç komut:

```bash
git clone https://github.com/avenoxai/avenoxbeyin.git
cd avenoxbeyin
claude "Read SETUP.md and follow it exactly to set up my second brain from this template."
```

Claude birkaç soru sorar (adın, ne iş yaptığın, AI ortağının adı), vault'u kurar, kancaları bağlar,
masaüstüne 🧠 ikonlu bir kısayol koyar.

Codex kullanıyorsan bu POSIX yolda aynı repoda `codex` açıp `SETUP.md` dosyasını
uygulatabilirsin. Kurulum
`AGENTS.md`, `.agents/skills` ve mutlak yollu `.codex/hooks.json` üretir. İlk açılışta Codex'in
`/hooks` ekranından proje kancalarını onaylaman gerekir; güven hash'leri kullanıcı adına
değiştirilmez. Claude Code ve Codex aynı router, skill ve kanca kodunu okur; iki ayrı kopya yoktur.

### Yerel Windows (WSL değil)

Yerel Windows için ayrı bir kurulum yolu vardır:

```powershell
git clone https://github.com/avenoxai/avenoxbeyin.git
cd avenoxbeyin
claude "Read SETUP-WINDOWS.md and follow it exactly to set up my second brain."
```

Gereksinimler kurulumdan önce **çalıştırılarak** denetlenir (PowerShell 7,
Python 3, Git, Claude Code) ve biri bile eksikse **diske hiçbir şey yazılmaz**.

Ne yapıldığı, hangi Windows tuzaklarının ölçüldüğü ve nelerin iddia edilmediği:
[`docs/WINDOWS-PORT.md`](docs/WINDOWS-PORT.md).

Kapsam dışı: v1 → v2 yükseltme, WSL'den taşınma, PowerShell 5.1 ile çalıştırma.
macOS ve Linux yolları değişmedi.

### Zaten v1 veya v2.0 beynin varsa

Aynı komut yeter. `SETUP.md` önce mevcut bir beyin arar, bulursa yükseltme moduna geçer ve işi
tek bir script'e devreder: `scripts/upgrade.sh`. Yükseltme **sadece ekler**: mevcut hafıza
dosyalarına, Dashboard'a, notlarına dokunulmaz. `daily/`, `knowledge/`, scriptler ve skill'ler
eklenir, dört kanca dosyası yenisiyle değiştirilir, `settings.json` kanca kaydı tekrar tekrar
çalıştırılabilecek şekilde birleştirilir.

Üç şeyi peşinen bilmen iyi olur:

- **Hafıza klasörünün adı `🔮 850-Companion` olmak zorunda.** Kancalar ve scriptler bu sabit yolu
  okuyor. Klasörün adı ortağının adıysa (`🔮 850-Echo` gibi) script bunu `git mv` ile değiştirmeyi
  teklif eder. İçerik hiç değişmez, sadece klasör adı değişir. Hayır dersen yükseltme hiç
  başlamaz ve vault'a v2 damgası vurulmaz; yarım kurulmuş bir v2'den dürüst bir v1 iyidir.
- **İlk iş git anlık görüntüsü.** Alınamazsa yükseltme durur, devam etmez. Geri dönüş her zaman
  açık.
- **Sürüm damgası en sona yazılır.** Kancalar, scriptler, placeholder'lar, kanca sayısı ve
  `.gitignore` koruması tek tek doğrulandıktan sonra. Bir kapı bile geçilmezse `.beyin-version`
  yazılmaz.

---

## v1/v2.0 → v2.1

| | v1 | v2 |
| --- | --- | --- |
| Günlük hafıza | model hatırlarsa yazar | oturum kapanışında **otomatik** yazılır |
| Kanca sayısı | 3 | 4 (`PreCompact` eklendi) |
| Compaction | konuşma sıkıştırılınca kaybolur | sıkıştırma öncesi yakalanır |
| Bilgi tabanı | yok | `knowledge/` altında derlenmiş, birbirine bağlı makaleler |
| Oturum başı bağlam | son oturum + threadler | + kurallar, son journal, bilgi indeksi, bugünün logu |
| Kalıcı kurallar | yok | `Kurallar.md`, "bunu böyle yapma" dediğinde oraya yazılır |
| Sağlık kontrolü | yok | `beyin doktor` skill'i, tek tabloda tanı |
| Eski geçmiş | yok | `geçmiş import`: ChatGPT, Claude, Gemini dışa aktarımları |
| Yükseltme | yok | yerinde, ekleme yapan, tekrar çalıştırılabilir |
| Bağımlılık | bash | bash + python3 (ikisi de sistemde var) |

---

## Mimari

```
   oturum biter                    konuşma sıkışmak üzere
   (SessionEnd)                         (PreCompact)
        |                                    |
        v                                    v
  session-end.sh                       pre-compact.sh
        |                                    |
        +------------------+-----------------+
                           v
                       flush.py           (claude -p --model haiku)
                  transkripti okur, Türkçe özet çıkarır
                           v
                 daily/YYYY-MM-DD.md      <-- makine yazar, sen değil
                           |
        (saat 18'den sonra, günde bir kez, değişen log varsa)
                           v
                      compile.py          (claude -p --model sonnet)
                           v
   knowledge/concepts/*.md + knowledge/connections/*.md + knowledge/index.md
                           |
                           v
                   session-start.sh
        indeksi + bugünün logunu + hafızayı bir sonraki oturuma enjekte eder
```

Yazma tarafı makineye ait, ilişki katmanı sana ait: ortağın hâlâ `Last-Session.md` ve `Threads.md`
dosyalarını kendi eliyle günceller. Makine katmanı onun yerine geçmez, altını doldurur.

## Ne alıyorsun

```
{Ad}OS/
├── 📥 000-Inbox/Dump/        # ham yakalama
├── 🎯 100-Command-Center/    # Dashboard
├── 🏰 300-Projects/          # proje başına bir klasör
├── 🧠 500-Knowledge/         # insanın yazdığı notlar
├── 🛠️ 600-Arsenal/           # araçlar, kişiler, kaynaklar
├── 🔮 850-Companion/         # ortağın kalıcı hafızası (+ Kurallar.md)
├── daily/                    # makine yazar: günlük loglar
├── knowledge/                # makine derler: makaleler + bağlantılar + indeks
├── 📦 900-Archive/
├── 📋 Templates/
└── .claude/                  # kancalar, scriptler, skill'ler (süreklilik motoru)
```

- **İsmini sen koyduğun bir AI ortağı.** Varsayılan dili Türkçe.
- **Süreklilik motoru.** Dört sıfır bağımlılıklı kanca, her açılışta hafızayı bağlama koyar, her
  kapanışta oturumu diske yazar.
- **Dosya tabanlı hafıza.** API anahtarı yok, ücretli servis yok, her şey senin diskinde.
- **Opsiyonel semantik hafıza.** [mem0](https://mem0.ai) ücretsiz katmanı üstüne anlamsal arama
  ekler, temel sürümü tamamen ücretsiz ve kredi kartı istemez. İstemezsen sistem eksiksiz çalışır.
- **Tek tık başlatıcı.** macOS'ta masaüstünde 🧠 ikonlu bir uygulama vault'u anında açar. Linux'ta
  yerine bir `.desktop` kısayolu yazılır (test edilmedi).

## Maliyet, dürüst hâliyle

Ekstra ücret yok; arka plan özetleyici ve derleyici mevcut Claude aboneliğinin günlük limitinden
küçük bir pay kullanır (özet: her oturum sonunda küçük bir Haiku çağrısı; derleme: günde bir
Sonnet çağrısı).

## Gereksinimler

Zorunlu, her platformda: [Claude Code](https://claude.com/claude-code),
[Obsidian](https://obsidian.md) ve Python 3 (macOS/Linux'ta `python3`; yerel Windows'ta çalışan
`python` veya `python3`). Python opsiyonel değil: günlük log da gece derlemesi de onun üstünde
çalışır.

| Platform | Durum | Ne çalışır, ne çalışmaz |
| --- | --- | --- |
| macOS | **test edildi** | hepsi: kancalar, `daily/`, `knowledge/`, 🧠 masaüstü kısayolu |
| Linux | **test edilmedi** | kurulum `uname` ile dallanır: Homebrew, Obsidian cask ve macOS `.app` adımları atlanır, yerine XDG `.desktop` kısayolu yazılır. Vault, kancalar ve scriptler taşınabilir yazıldı ama gerçek bir Linux masaüstünde doğrulanmadı. Denersen sorun aç. |
| Windows | WSL, **veya** yerel Windows | Tek satırlık `beyin.md` girişi iki yolu ayırır. Yerel Windows için [`SETUP-WINDOWS.md`](SETUP-WINDOWS.md). WSL'de motor ve vault aynı Linux dağıtımında, Linux dosya sisteminde kalır; Windows Obsidian ile karma çalışma doğrulanmış sayılmaz. Bkz. [`docs/WINDOWS-PORT.md`](docs/WINDOWS-PORT.md). |

Masaüstü kısayolu macOS'ta `osacompile` ve AppKit kullanır, ikisi de Linux'ta yoktur. Vault'un
kendisi düz Markdown, yani her yerde açılır; kurulum akışının tamamı için doğrulanmış tek platform
şu an macOS.

## Bir şey ters giderse

Vault klasöründe `claude` açıp `beyin doktor` yaz. Kancalar, scriptler, python3, `claude` CLI,
günlük log tazeliği, son derleme durumu, iCloud çakışma dosyaları ve git durumu tek tabloda gelir,
her kırmızı satırın altında düzeltme komutu yazar.

---

## Credits

Bilgi derleme mimarisi Andrej Karpathy'nin LLM bilgi tabanı desenine dayanır:
https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f

Geri kalanı [Avenox](https://avenox.lol) günlük kullandığı sistemden, kişisel veriden arındırılıp
herkes için genelleştirilerek çıkarıldı.

## Lisans

MIT, [LICENSE](LICENSE) dosyasına bak. PR'lar açık.

---

## In English (short version)

**avenoxbeyin** is an open-source AI second brain: an Obsidian vault driven by Claude Code or
Codex, with
memory that survives across sessions. v1 gave you continuity but depended on the model remembering
to write its memory files. v2's thesis is that **memory must be a mechanism, not a discipline**: a
`SessionEnd` and a `PreCompact` hook flush every conversation into `daily/` logs automatically via
a small background Haiku call, and once a day a Sonnet compile pass turns those logs into linked
articles under `knowledge/`. The next session starts with that knowledge index already in context.

On macOS, Linux, or WSL, install with `git clone https://github.com/avenoxai/avenoxbeyin.git && cd
avenoxbeyin && claude "Read SETUP.md and follow it exactly to set up my second brain from this
template."` Codex can follow the same `SETUP.md`; its project hooks are rendered with absolute
paths and require one-time approval in `/hooks`. On native Windows, use the same clone but ask
Claude Code to read `SETUP-WINDOWS.md`; do not run the Bash installer. Already running v1 or v2.0?
The same command detects it and hands the work to one committed script, `scripts/upgrade.sh`:
additive only, your memory files are never touched, the settings merge is idempotent, and it takes
a **verified** git snapshot before it changes anything. Two things it will ask you about, and stop
for if you say no: renaming the memory folder to the fixed `🔮 850-Companion` path (a `git mv`, the
contents never move), and removing v1 hook wiring left behind in `settings.local.json` so hooks
stop firing twice. The `.beyin-version` stamp is the last write of all, only after every gate
passes.

Platform honesty: macOS is the tested POSIX path. The installer branches on `uname` and writes an
XDG `.desktop` launcher instead of a macOS app on Linux, but that path has not been verified on a
real Linux desktop. Native Windows has its own PowerShell installer and CI suite; the generated
Codex wiring is regression-tested, but a live native-Windows Codex session has not yet been
claimed as verified.

No extra cost: everything runs on your existing Claude subscription through `claude -p`. No API
keys, no paid services, bash and python3 stdlib only. Knowledge-compilation architecture credit:
Andrej Karpathy's LLM knowledge base pattern,
https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f. MIT licensed.
