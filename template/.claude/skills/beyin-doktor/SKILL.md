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

### 3c. Antigravity kanca adaptörü

```bash
python3 -c "import json;d=json.load(open('.agents/hooks.json'));h=d.get('avenox-beyin',{});print('antigravity:', 'ok' if len(h.get('PreInvocation',[]))==1 and len(h.get('Stop',[]))==1 else 'BOZUK')" 2>/dev/null || echo "antigravity: BOZUK/YOK"
```

🟢 `ok`. 🔴 ise Antigravity hafızayı enjekte etmiyor veya son turu günlüğe taşımıyor.
Düzeltme: `python3 .claude/scripts/render_antigravity_hooks.py --vault "$PWD" --platform posix`.

### 4. python3 ve model CLI yolda mı

```bash
if command -v python3 >/dev/null 2>&1; then echo "python3: $(python3 -V 2>&1)"; else echo "python3: YOK"; fi; if command -v claude >/dev/null 2>&1; then echo "claude: var"; elif command -v agy >/dev/null 2>&1; then echo "agy: var"; else echo "model CLI: YOK"; fi
```

🟢 python3 ile `claude` veya `agy` var. 🔴 python3 yoksa motor hiç çalışmaz; iki model CLI da
yoksa arka plan özetleyici durur.
Düzeltme: python3 kur; kullandığın Claude Code veya Antigravity CLI'yi PATH'e ekle.

### 4b. Model CLI oturum açmış mı

Varlık yetmez. Arka plan özetleyici ve derleyici `claude -p` ile çalışır; CLI kurulu ama oturum
kapalıysa her flush `Not logged in` alıp ölür ve **hiçbir yerde günlük oluşmaz**. Motor çalışır
görünür, `daily/` boş kalır: 6 numaralı kontrol kırmızıya döner ama sebebi orada görünmez.
Bir GUI uygulamasında oturum açık olması CLI'yi kapsamaz, kimlik depoları ayrıdır.
`auth status` token harcamaz.

```bash
if command -v claude >/dev/null 2>&1; then
  if claude auth status 2>/dev/null | grep -q '"loggedIn": *true'; then echo "oturum: acik"; else echo "oturum: KAPALI"; fi
elif command -v agy >/dev/null 2>&1; then echo "agy kullaniliyor, bu kontrol claude'a ozgu"
else echo "model CLI: YOK"; fi
```

🟢 `oturum: acik`. 🔴 `oturum: KAPALI`: en sinsi arıza budur.
Düzeltme: **kullanıcı kendisi** `claude auth login` çalıştırsın, tarayıcıda OAuth açar. Bu adımı
ajan olarak sen yapamazsın. Giriş bittikten sonra `claude auth status` ile doğrula, bir oturum
açıp kapat ve 6 numaralı kontrolü tekrarla.

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

`health.json` satır başına bir JSON kaydı tutar (JSONL). Kayıtların hepsi arıza değildir:
`error` alanı `warn:` ile başlıyorsa motor sorunu **kendi çözmüştür** (örneğin
`warn:summary-schema-retried` — özetin ilk çıktısı şemaya uymadı, yeniden denendi, tuttu; günlük
log eksiksiz yazıldı). Uyarıyı hata sayan bir kontrol sağlıklı bir vault'ta kalıcı kırmızı üretir
ve kullanıcıyı doktoru görmezden gelmeye alıştırır. İkisini ayır.

```bash
python3 - <<'PYCHK'
import json, os, time
p = ".claude/scripts/.state/health.json"
if not os.path.exists(p):
    print("health: kayit yok"); raise SystemExit(0)
now = time.time(); err = warn = 0
for line in open(p, encoding="utf-8"):
    line = line.strip()
    if not line: continue
    try: r = json.loads(line)
    except ValueError: continue
    yas = int((now - r.get("ts", now)) / 3600)
    e = str(r.get("error", ""))
    if e.startswith("warn:"):
        tur = "UYARI"
        if yas < 48: warn += 1
    else:
        tur = "HATA"
        if yas < 48: err += 1
    print("%s | %s | %s | %d saat once" % (tur, r.get("component", "?"), e, yas))
print("ozet: son 48 saatte %d hata, %d uyari" % (err, warn))
PYCHK
```

🟢 kayıt yok, ya da son 48 saatte `0 hata` (uyarı olabilir). 🟡 son 48 saatte uyarı var ama hata
yok: motor kendini toparlamış, tekrarlıyorsa bak. 🔴 son 48 saatte en az bir `HATA`.
Düzeltme: `component` alanına bak. `flush` ise transkript veya claude CLI — önce 4b'yi kontrol
et, `claude-exit-1` neredeyse her zaman kapalı oturum demektir. `compile` ise model çağrısı
sorunlu. Kaydı okuduktan ve sebebini giderdikten sonra dosyayı silebilirsin, script yeniden yazar.

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

🟢 `2.3.0`. 🟡 `2.2.0`, `2.1.0`, `2.0.0` veya dosya yok: yükseltme yapılabilir.
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

### 16. Grafik bütünlüğü (kırık bağlantı + yetim not)

```bash
python3 .claude/scripts/graf_kontrol.py
```

Hafıza düz bir dosya yığını değil **graf**: bir not ancak ona giden bir bağlantı varsa
bulunur. Yetim not diskte durur ama ajan ona ulaşmak için bütün vault'u taramak zorunda
kalır, yani ya token yakar ya bulamayıp uydurur. Kırık bağlantı ise haritada var olmayan
bir adresi gösterir. İkisini de başka hiçbir kontrol yakalamıyor.

🟢 kırık bağlantı 0. 🟡 kırık bağlantı 1–20. 🔴 kırık bağlantı 20 üstü.
Yetim sayısı tek başına 🔴 değildir: taslak, arşivlik ve tek seferlik notlar doğal olarak
yetimdir. Kanca tarafından her oturumda enjekte edilen çekirdek dosyalar tarayıcıda açıkça
muaf tutulur; kalan sonuçlar erişim kolaylığı için danışmanlık sinyalidir.
Düzeltme: `--tam` ile listeyi aç. Kırık bağlantı için ya hedef notu oluştur ya bağlantıyı
düzelt; hedef bilerek yoksa (belgede örnek olarak geçen `[[wikilink]]` gibi) dokunma.
Yetim not için ilgili üs nottan veya `Dashboard.md`'den bağlantı ver.

## Rapor formatı

Tüm kontroller bittikten sonra tek tablo bas:

```
| Kontrol | Durum | Bulgu |
| --- | --- | --- |
| Hook dosyaları | 🟢 | dördü de yerinde ve çalıştırılabilir |
| settings.json bağlantısı | 🟢 | dört olay da bağlı |
| Codex bağlantıları | 🟢 | router, skill ve kanca store ortak; hooks.json mutlak |
| Antigravity bağlantıları | 🟢 | PreInvocation ve Stop adaptörü kayıtlı |
| Özyineleme koruması | 🟢 | hepsinde var |
| python3 ve model CLI | 🟢 | python3 3.11.6, agy var |
| Model CLI oturumu | 🟢 | açık |
| python3-missing işareti | 🟢 | işaret yok |
| Günlük log tazeliği | 🟡 | son log 51 saat önce |
| Derleme durumu | 🔴 | last_status fail:timeout |
| Sağlık kayıtları | 🔴 | dün compile hatası |
| Bilgi indeksi | 🟢 | 42 satır |
| iCloud çakışmaları | 🟢 | temiz |
| Git | 🟢 | repo var, 3 dosya kaydedilmemiş |
| Sürüm | 🟢 | 2.3.0 |
| Kurallar | 🟢 | var, 24 satır |
| Çift etkin kanca | 🔴 | SessionEnd 2 kez bağlı |
| Sır yedeği artığı | 🟢 | temiz |
| Grafik bütünlüğü | 🟡 | 4 kırık bağlantı, 12 yetim not |
```

Tablodan sonra sadece 🔴 satırlar için "Düzeltme:" ile başlayan birer satır yaz, komutu da ver.
Sonra tek cümlelik hüküm: örneğin "Beyin ayakta ama derleyici iki gündür takılı, önce onu çöz."
Her şey yeşilse hüküm de kısa olsun: "Beyin sağlıklı, yapılacak bir şey yok."
