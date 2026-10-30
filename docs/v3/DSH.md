# DeepSeek Harness (DSH)

DSH desktop, Claude Code uyumlu komut hook'larını `dsh-hooks-claude-code`
eklentisiyle çalıştırır. V3 bu köprüyü kullanır: ortak `beyin_v3_hook.py` aynı
Claude biçimli payload'ı ve aynı `hookSpecificOutput.additionalContext` cevabını
alır. DSH hesabı veya uygulaması kurucu tarafından kurulmaz ve **kurucu
`~/.dsh` altındaki hiçbir dosyayı yazmaz**; profil yaması kullanıcıya aittir.

## Kurulum

Vault klasörünü seç ve repo kökünden çalıştır:

```sh
python3 scripts/install_v3.py --vault "/tam/yol/Beynim"
python3 "/tam/yol/Beynim/beyin.py" sync
python3 "/tam/yol/Beynim/beyin.py" doctor
```

Windows'ta `python3` yerine `py -3` kullan. Kurucu vault içine şunu yazar:

- `.claude/dsh/beyin-v3-hooks.json`: DSH'nin okuduğu Claude biçimli dört komut
  (`SessionStart`, `UserPromptSubmit`, `PostToolUse`, `Stop`). Vault, state ve
  Python yolu mutlaktır; `PostToolUse` eşleştiricisi DSH'nin küçük harfli araç
  adlarını (`edit|write|str_replace_editor`) kullanır.

## Profil yaması (bir kez, elle)

`beyin.py doctor --json` çıktısındaki `dsh.manual_setup.block` alanı, profiline eklenecek
girdileri hazır verir. Profil dosyası `~/.dsh/profiles/<profil>/cordis.patch.yml`
(desktop için genelde `desktop`) ve DSH tarafından **izlenmez**; değişiklikten
sonra DSH'yi yeniden başlat. Tipik blok:

```yaml
- id: sandbox-policy
  name: "@deepseek-ai/dsh-sandbox-policy"
  config:
    mode: !!js process.env.DSH_PERMISSION_MODE ?? 'workspace-write'
    workspaceRoot: "/tam/yol/state"
- insert:
    - id: hooks-claude-code
      name: "@deepseek-ai/dsh-hooks-claude-code"
      config:
        configPath: "/tam/yol/Beynim/.claude/dsh/beyin-v3-hooks.json"
```

Kurallar:

- Profilde zaten bir `sandbox-policy` ya da `hooks-claude-code` girdisi varsa
  **ikinci bir girdi ekleme**; var olanın `workspaceRoot` / `configPath`
  değerini yukarıdakiyle değiştir. DSH aynı id'yi son gelen girdiyle uygular;
  çift `insert` kaydı hook'ları iki kez çalıştırabilir.
- `workspaceRoot`, state dizinini (ya da onun bir üst dizinini) kapsamalıdır.
  State yolu `beyin.py doctor --json` içindeki `state_location.effective_state` alanında yazar.
- **`mode` satırını `workspace-write` gibi sabit bir değere çevirme.** Profil
  yaması bir girdinin `config` değerini bütünüyle değiştirir, birleştirmez;
  sabit değer kullanıcının `DSH_PERMISSION_MODE` seçimini ezer. Ayrıca
  `permission.defaultPreset` açıkça yazılmamışsa `danger-full-access` gibi bir
  modda sandbox modu ile approval politikası hiçbir preset'e uymaz ve DSH
  0.2.1-rc.3 açılmaz (DSH upstream #7567).
  `doctor`, `defaultPreset` yokken varsayılan dışı bir mod görürse uyarır.
- `permission.defaultPreset` açıksa yeni oturumların presetini o belirler;
  `sandbox-policy` girdisi yalnız oturumsuz hook çağrılarını etkiler.

**Bu adım DSH 0.2.1-rc.3'e özgüdür.** DSH hook komutlarına oturum sandbox
politikasını geçirmeye başladığında (ya da hook başına kum havuzu atlama /
`additionalWritableRoots` verdiğinde) `sandbox-policy` girdisi gereksizleşir ve
kaldırılmalıdır. Aynı nedenle `additionalWritableRoots` rc.3'te yoktur ve
yazılmamalıdır.

## Doctor yalnız okur

`beyin.py doctor --json` çıktısındaki `dsh` alanı:

- `hook_config`: yönetilen hook dosyası, dört olay, adaptörün bu vault'a
  sabitlenmesi ve `PostToolUse` eşleştiricisi.
- `state`: state dizininin bu kabuktan yazılabilirliği, **son gözlenen DSH hook
  olayı** ve yaşı, son eşitleme durumu ve varsa son hook hatası. DSH'nin kum
  havuzu dışarıdan ölçülemez; `writable` yalnız bu kabuğun görünümüdür, gerçek
  kanıt `last_hook_event` alanıdır.
- `profiles`: bulunan her `cordis.patch.yml` için `hook`, `sandbox` ve
  `permission_default_preset`; okunamayan girdi varsa profil `ok` sayılmaz.
- `manual_setup`: eksikse eklenecek blok ve düzeltme notları.

`dsh` durumu `ok`, `manual_setup`, `needs_attention` ya da
`hook_config_missing` olur. Doctor ve `beyin.py doctor` profili **değiştirmez**,
yedek almaz ve `dsh-profile` benzeri bir yazma komutu yoktur.

## Hook davranışı ve sınırlar

DSH hook komutlarını ajanın kullandığı `ctx.shell` üzerinden çalıştırır. Hook
isteği oturum politikası taşımadığı için kum havuzu **oturumsuz** çözülür:
deployment modu ve `workspaceRoot`. Varsayılan kök DSH profil klasörüdür; state
dizini orada olmadığından hook yazımı `Operation not permitted` ile reddedilir.
Bu yüzden yukarıdaki `sandbox-policy` girdisi gerekir.

DSH tek profille her klasörü açabilir. Kurulu adaptör çalışma dizini (payload
`cwd`) vault dışındaysa sessizce `{}` döner; böylece başka projelerde Beyin
bağlamı enjekte edilmez. DSH'nin köprüsü `PreCompact` ve `SessionEnd`
olaylarını taşımaz; bunlar kaydedilmez ve eşitleme `Stop` ile sürer. Kabuk
komutuyla yapılan düzenlemeler `PostToolUse` üretmez; sonraki tur veya
`beyin.py sync` kaynağı tazeler. Hook bir model çağırmaz ve sohbet
transkriptini arşivlemez.

DSH köprüsü `additionalContext` metnini kırpmadan geçirir (kurulu
`dsh-hook-protocol` kaynağında bir üst sınır yok). Bu yüzden `dsh`,
`CLIENT_TEXT_LIMITS` tablosuna eklenmez — o tablo yalnız belgelenmiş kesme
davranışı olan istemcileri tutar — ve otomatik bağlamın tek tavanı
`context_chars` ayarıdır.

## Kaldırma ve geri alma

`install_v3.py --uninstall` ve `rollback` yalnız vault'a ait dosyaları
(`.claude/dsh/`, adaptör) kaldırır. Profil yaması kullanıcıya ait olduğu için
otomatik temizlenmez: DSH adaptörünü içermeyen bir sürüme dönüldüğünde
`sandbox-policy` ve `hooks-claude-code` girdilerini kendin silmelisin; yoksa
DSH silinmiş bir hook dosyasına işaret eder ve köprü hiç hook kaydetmez
(fail-open). Aynı şey DSH'yi artık kullanmayacaksan da geçerlidir.

## Makbuz geriye uyumu

Eski bir sürümün makbuz doğrulayıcısı (v3.12.0) frontmatter'da yalnız kendi
bildiği istemci adlarını kabul eder. Bu yüzden `dsh` makbuzu diske
`harness: manual` + `client: dsh` olarak yazılır: eski kod `client` anahtarını
yok sayar, sync `invalid harness` ile `degraded` dönmez ve makbuzu `manual`
olarak indeksler. Bu sürüm aynı dosyayı okurken `client` bilinen bir istemciyi
gösteriyorsa makbuzu yeniden `dsh` sayar; `dsh` checkpoint'i kendi makbuzuyla
kapanır. Tanınmayan ya da metin olmayan bir `client` makbuzu `manual` bırakır,
native bir harness `client` taşısa bile kendi adını korur.

## Doğrulama

`beyin.py sync` çalıştır, DSH'yi yeniden başlat ve özel veri içermeyen bir
deneme notu oluştur. Yeni oturumda bilgiyi yalnız hook bağlamından iste; ardından
`beyin.py doctor --json` içindeki `dsh.state.last_hook_event` alanının dolduğunu
ve `last_sync_status` değerinin `succeeded` olduğunu kontrol et. Bağlam
gelmiyorsa `beyin.py context "konu" --harness dsh` ile kaynağı doğrudan kontrol
et ve `dsh.profiles[].warnings` alanını oku.
