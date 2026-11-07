---
name: beyin-doktor
description: Beynin sağlık kontrolü. Hook'lar, script'ler, hafıza dosyaları, günlük loglar, derleme durumu ve vault hijyeni tek tabloda raporlanır. "beyin doktor", "doktor", "sağlık kontrolü", "beyin çalışıyor mu", "hafıza bozuk mu" dendiğinde veya bir hafıza mekanizmasının sessizce çalışmadığından şüphelenildiğinde kullan.
---

# Beyin Doktoru

Bu skill beynin mekanik katmanını denetler: hook'lar tetikleniyor mu, script'ler çalışmış mı,
loglar tazeliğini koruyor mu, vault kirlenmiş mi. Amaç sessiz arızayı görünür yapmak.

## Nasıl çalışırsın

1. Vault kökünde (CLAUDE.md/AGENTS.md'nin bulunduğu klasör) çalış. Tüm yollar göreceli, mutlak yol yazma.
2. Aşağıdaki kontrolleri **Bash ile sırayla çalıştır**. Komutları olduğu gibi kullan, tahmin etme.
3. Her kontrolün çıktısını 🟢 / 🟡 / 🔴 olarak sınıfla.
4. Sonucu tek bir tabloda ver, her 🔴 için bir düzeltme satırı yaz.
5. En sonda tek cümlelik hüküm ver.

Kontroller salt okunurdur. Hiçbir şeyi kendiliğinden düzeltme, önce raporla, sonra kullanıcı
isterse düzelt.

## Kontroller

### 1. Hook dosyaları var mı ve çalıştırılabilir mi

```bash
for h in session-start prompt-counter session-end pre-compact; do f=".claude/hooks/$h.sh"; if [ ! -f "$f" ]; then echo "$h: DOSYA YOK"; elif [ ! -x "$f" ]; then echo "$h: calistirilabilir degil"; else echo "$h: ok"; fi; done
```

🟢 dördü de `ok`. 🔴 eksik veya çalıştırılabilir değil.
Düzeltme: `chmod +x .claude/hooks/*.sh`, dosya yoksa depodan kopyala.

### 2. Hook'lar settings.json içinde bağlı mı

```bash
for h in session-start prompt-counter session-end pre-compact; do if grep -q "$h.sh" .claude/settings.json 2>/dev/null; then echo "$h: bagli"; else echo "$h: SETTINGS ICINDE YOK"; fi; done
```

🟢 dördü de bağlı. 🔴 biri bile eksikse o hook hiç çalışmıyor demektir.
Düzeltme: `.claude/settings.json` içindeki `hooks` bloğuna eksik olayı ekle (SessionStart,
UserPromptSubmit, SessionEnd, PreCompact), sonra Claude Code'u yeniden başlat.

### 3. Özyineleme koruması her hook'ta var mı

```bash
for f in .claude/hooks/*.sh; do if grep -q 'BEYIN_INVOKED_BY' "$f"; then echo "$(basename "$f"): guard var"; else echo "$(basename "$f"): GUARD YOK"; fi; done
```

🟢 hepsinde var. 🔴 eksik. Guard'ı olmayan hook, arka plan `claude -p` çağrısında tekrar
tetiklenir ve sonsuz döngü riski doğar.
Düzeltme: dosyanın shebang'inden hemen sonraki satıra `[ -n "${BEYIN_INVOKED_BY:-}" ] && exit 0` ekle.

### 3b. Codex router, skill ve kanca bağlantıları

```bash
test -L AGENTS.md && [ "$(readlink AGENTS.md)" = "CLAUDE.md" ] && echo "router: ortak" || echo "router: AYRI/YOK"; test -L .agents/skills && [ "$(readlink .agents/skills)" = "../.claude/skills" ] && echo "skills: ortak" || echo "skills: AYRI/YOK"; test -L .codex/hooks && [ "$(readlink .codex/hooks)" = "../.claude/hooks" ] && echo "hooks: ortak" || echo "hooks: AYRI/YOK"; python3 -c "import json,os;d=json.load(open('.codex/hooks.json')); w={'SessionStart':('session-start.sh',15),'UserPromptSubmit':('prompt-counter.sh',5),'PreCompact':('pre-compact.sh',10),'SessionEnd':('session-end.sh',3)}; print('codex:', 'ok' if all(sum(n in (h.get('command') or '') and os.path.isabs((h.get('command') or '').strip(chr(39)+chr(34))) and h.get('timeout')==t for m in d.get('hooks',{}).get(e,[]) for h in m.get('hooks',[]))==1 for e,(n,t) in w.items()) else 'BOZUK')" 2>/dev/null || echo "codex: BOZUK/YOK"
```

🟢 dört satır da ortak/ok. 🔴 ise Codex aynı motoru görmüyor veya göreli/eski bir komut okuyor.
Düzeltme: `python3 .claude/scripts/render_codex_hooks.py --vault "$PWD" --platform posix`, sonra
Codex içinde `/hooks` ekranından değişen proje kancalarını onayla. Güven hash'ini elle yazma.

### 4. python3 ve claude CLI yolda mı

```bash
if command -v python3 >/dev/null 2>&1; then echo "python3: $(python3 -V 2>&1)"; else echo "python3: YOK"; fi; if command -v claude >/dev/null 2>&1; then echo "claude: var"; else echo "claude: YOK"; fi
```

🟢 ikisi de var. 🔴 python3 yoksa flush ve derleme hiç çalışmaz, claude yoksa arka plan
özetleyici sessizce durur.
Düzeltme: python3 kur (macOS: Xcode command line tools, Linux: dağıtım paketi), claude CLI'yi
PATH'e ekle.

### 5. python3-missing işareti

```bash
if [ -f .claude/scripts/.state/python3-missing ]; then echo "isaret VAR, tarih: $(head -1 .claude/scripts/.state/python3-missing 2>/dev/null)"; else echo "isaret yok"; fi
```

🟢 işaret yok. 🔴 işaret var: hook'lar python3 bulamadığı için JSON kaçışını yapamamış.
Düzeltme: python3'ü kur, sonra dosyayı sil: `rm .claude/scripts/.state/python3-missing`.

### 6. Günlük log tazeliği

```bash
f=$(ls -t daily/*.md 2>/dev/null | head -1); if [ -z "$f" ]; then echo "daily: hic log yok"; else m=$(stat -f %m "$f" 2>/dev/null || stat -c %Y "$f"); n=$(date +%s); echo "daily: $f, $(( (n - m) / 3600 )) saat once yazildi"; fi
```

🟢 48 saatten yeni. 🟡 48 ile 96 saat arası. 🔴 96 saatten eski veya hiç log yok.
Düzeltme: oturum bitir ve yeniden başlat, sonra bu kontrolü tekrarla. Hâlâ boşsa 1, 2 ve 4
numaralı kontrollere dön, arıza flush zincirinde.

### 7. Derleme durumu

```bash
f=".claude/scripts/.state/compile-state.json"; if [ -f "$f" ]; then m=$(stat -f %m "$f" 2>/dev/null || stat -c %Y "$f"); n=$(date +%s); echo "state: $(( (n - m) / 3600 )) saat once guncellendi"; python3 -c "import json;d=json.load(open('.claude/scripts/.state/compile-state.json'));print('last_run:',d.get('last_run','yok'));print('last_status:',d.get('last_status','yok'));print('ingested:',len(d.get('ingested',{})),'log')" 2>/dev/null || echo "state dosyasi bozuk, JSON okunamadi"; else echo "compile: state dosyasi yok, henuz hic derleme calismadi"; fi
```

🟢 `last_run` 48 saatten yeni ve `last_status` `ok`. 🟡 state yok ama vault yeni kurulmuş veya
henüz akşam 18:00 olmamış. 🔴 `last_status` `fail:` ile başlıyor veya 48 saatten eski.
Düzeltme: elle bir tur çalıştır ve hatayı gör: `python3 .claude/scripts/compile.py --dry-run`,
sonra `python3 .claude/scripts/compile.py`.

### 8. Sağlık kayıtlarındaki son hatalar

```bash
if [ -f .claude/scripts/.state/health.json ]; then tail -c 2000 .claude/scripts/.state/health.json; else echo "health: kayit yok"; fi
```

🟢 kayıt yok veya son kayıt 7 günden eski. 🔴 son 48 saatte hata kaydı var.
Düzeltme: `component` alanına bak. `flush` ise transkript veya claude CLI, `compile` ise model
çağrısı sorunlu. Hatayı okuduktan sonra dosyayı silebilirsin, script yeniden yazar.

### 9. Bilgi indeksi büyüklüğü

```bash
if [ -f knowledge/index.md ]; then echo "index: $(wc -l < knowledge/index.md | tr -d ' ') satir"; else echo "index: DOSYA YOK"; fi
```

Oturum başında indeksin sadece ilk 150 satırı bağlama giriyor.
🟢 150 satır ve altı. 🟡 151 ile 300 satır arası, alt sıralar artık enjekte edilmiyor.
🔴 300 satırın üstü: özet indeks zamanı.
Düzeltme: indeksi tema başlıklarına göre grupla, eski satırları tek bir özet satırında topla,
detay makalede kalsın. Dosya hiç yoksa depodaki tohum dosyayı geri koy.

### 10. iCloud çakışma dosyaları

```bash
find . -name "* 2.*" -not -path "./.git/*" 2>/dev/null | head -20
```

🟢 çıktı boş. 🔴 çıktı var: iCloud aynı dosyanın iki kopyasını tutmuş, hafıza dosyalarının
bir kısmı yanlış kopyada olabilir.
Düzeltme: listelenen her dosyayı aslıyla karşılaştır (`diff "dosya.md" "dosya 2.md"`), gerekli
içeriği asıl dosyaya taşı, sonra çakışma kopyasını sil. Kullanıcıya sormadan silme.

### 11. Git deposu ve kaydedilmemiş değişiklik

```bash
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then echo "git: var, kaydedilmemis: $(git status --porcelain | wc -l | tr -d ' ') dosya"; else echo "git: REPO YOK"; fi
```

🟢 repo var ve kaydedilmemiş dosya 50'nin altında. 🟡 50 üstü birikmiş. 🔴 repo yok, yani
hafızanın geri alınabilir bir geçmişi yok.
Düzeltme: repo yoksa `git init` ve ilk commit. Birikme varsa commit at.

### 12. Sürüm dosyası

```bash
if [ -f .beyin-version ]; then echo "surum: $(head -1 .beyin-version)"; else echo "surum: DOSYA YOK, v1 vault"; fi
```

🟢 `2.1.0`. 🟡 `2.0.0` veya dosya yok: harness-neutral yükseltme yapılabilir.
Düzeltme: SETUP.md içindeki yükseltme yolunu (B modu) uygula. Yükseltme mevcut hafıza
dosyalarına dokunmaz, sadece eksik parçaları ekler.

### 13. Kurallar dosyası

```bash
if [ -f "🔮 850-Companion/Kurallar.md" ]; then echo "kurallar: var, $(wc -l < "🔮 850-Companion/Kurallar.md" | tr -d ' ') satir"; else echo "kurallar: YOK"; fi
```

🟢 var. 🟡 yok: kullanıcının düzeltmeleri kalıcı hale gelmiyor.
Düzeltme: depodaki tohum `Kurallar.md` dosyasını `🔮 850-Companion/` altına kopyala.

### 14. Çift etkin kanca (settings.json + settings.local.json)

```bash
python3 - <<'PYCHK'
import json, os
EV = ("SessionStart", "UserPromptSubmit", "SessionEnd", "PreCompact")
V1 = ("session-start.sh", "prompt-counter.sh", "session-end.sh", "pre-compact.sh")
n = {e: 0 for e in EV}
for f in (".claude/settings.json", ".claude/settings.local.json"):
    try:
        d = json.load(open(f, encoding="utf-8"))
    except FileNotFoundError:
        continue
    except ValueError:
        print("%s: BOZUK JSON" % f); raise SystemExit(0)
    if not isinstance(d, dict):
        print("%s: JSON nesnesi degil" % f); continue
    for ev, ms in (d.get("hooks") or {}).items():
        if ev not in EV: continue
        for m in (ms or []):
            for h in (m.get("hooks") or []):
                if any(b in (h.get("command") or "") for b in V1):
                    n[ev] += 1
for e in EV:
    print("%s: %d" % (e, n[e]))
PYCHK
```

🟢 dört olayın da sayısı tam olarak `1`. 🔴 herhangi biri `2` veya daha fazla: o olayda
kancalar her seferinde iki kez çalışıyor, yani her prompt iki kez sayılıyor ve her oturum
sonunda iki flush tetikleniyor. `0` ise o olay hiç bağlı değil.
Düzeltme: `.claude/settings.local.json` içindeki beyin kanca girdisini sil, ilgisiz
kancalara ve `env`, `permissions` gibi diğer anahtarlara dokunma. Tek bağlantı
`.claude/settings.json` içinde kalmalı.

### 15. Vault içinde sır taşıyabilecek yedek artığı

```bash
find . -path ./.git -prune -o -type f \( -name "*.yedek" -o -name "*.yedek-*" -o -name "settings.local.json.*" -o -name "*.bak" -o -name "*.orig" \) -print 2>/dev/null | head -10; echo "---"; git ls-files 2>/dev/null | grep -E 'settings\.local\.json|\.yedek|\.env$' || echo "izlenen sirli dosya yok"
```

🟢 iki bölüm de boş. 🔴 bir yedek dosyası çıkarsa: bu dosyalar `settings.local.json`
kopyası olabilir ve API anahtarı taşır; ikinci bölümde bir şey çıkarsa sır zaten git
tarafından izleniyor demektir.
Düzeltme: yedeği vault dışına taşı ve `chmod 600` ver; git izliyorsa
`git rm --cached <dosya>` ile izlemeden çıkar, `.gitignore` kuralını doğrula, ve
sızmış anahtarı sağlayıcıdan **iptal edip yenile**.

## Rapor formatı

Tüm kontroller bittikten sonra tek tablo bas:

```
| Kontrol | Durum | Bulgu |
| --- | --- | --- |
| Hook dosyaları | 🟢 | dördü de yerinde ve çalıştırılabilir |
| settings.json bağlantısı | 🟢 | dört olay da bağlı |
| Codex bağlantıları | 🟢 | router, skill ve kanca store ortak; hooks.json mutlak |
| Özyineleme koruması | 🟢 | hepsinde var |
| python3 ve claude | 🟢 | python3 3.11.6, claude var |
| python3-missing işareti | 🟢 | işaret yok |
| Günlük log tazeliği | 🟡 | son log 51 saat önce |
| Derleme durumu | 🔴 | last_status fail:timeout |
| Sağlık kayıtları | 🔴 | dün compile hatası |
| Bilgi indeksi | 🟢 | 42 satır |
| iCloud çakışmaları | 🟢 | temiz |
| Git | 🟢 | repo var, 3 dosya kaydedilmemiş |
| Sürüm | 🟢 | 2.1.0 |
| Kurallar | 🟢 | var, 24 satır |
| Çift etkin kanca | 🔴 | SessionEnd 2 kez bağlı |
| Sır yedeği artığı | 🟢 | temiz |
```

Tablodan sonra sadece 🔴 satırlar için "Düzeltme:" ile başlayan birer satır yaz, komutu da ver.
Sonra tek cümlelik hüküm: örneğin "Beyin ayakta ama derleyici iki gündür takılı, önce onu çöz."
Her şey yeşilse hüküm de kısa olsun: "Beyin sağlıklı, yapılacak bir şey yok."
