# DeepSeek Harness (DSH)

DSH desktop, Claude Code uyumlu komut hook'larını `dsh-hooks-claude-code`
eklentisiyle çalıştırır. V3 bu köprüyü kullanır: ortak `beyin_v3_hook.py` aynı
Claude biçimli payload'ı ve aynı `hookSpecificOutput.additionalContext` cevabını
alır. DSH hesabı veya uygulaması kurucu tarafından kurulmaz.

## Kurulum

Vault klasörünü seç ve repo kökünden çalıştır:

```sh
python3 scripts/install_v3.py --vault "/tam/yol/Beynim" --dsh-profile "$HOME/.dsh/profiles/desktop"
python3 "/tam/yol/Beynim/beyin.py" doctor --json
```

Windows'ta `python3` yerine `py -3` kullan. `--dsh-profile` isteğe bağlıdır;
profil klasörünü ya da doğrudan `cordis.patch.yml` dosyasını verebilirsin.
Kurucu şunları yazar:

- `.claude/dsh/beyin-v3-hooks.json`: DSH'nin okuduğu Claude biçimli dört
  komut (`SessionStart`, `UserPromptSubmit`, `PostToolUse`, `Stop`). Vault,
  state ve Python yolu mutlaktır.
- `--dsh-profile` verildiyse profil yamasına iki işaretli blok: `sandbox-policy`
  ve `hooks-claude-code` insert'i. İşlem idempotenttir; ilk yazımdan önce
  `cordis.patch.yml.bak-beyin-<zaman>` yedeği alınır.

Profil yaması `~/.dsh/profiles/<profil>/cordis.patch.yml` dosyasıdır ve DSH
tarafından izlenmez; değişiklikten sonra **DSH'yi yeniden başlat**. Profili
sonradan bağlamak için:

```sh
python3 beyin.py dsh-profile apply  --profile "$HOME/.dsh/profiles/desktop"
python3 beyin.py dsh-profile status --profile "$HOME/.dsh/profiles/desktop"
python3 beyin.py dsh-profile remove --profile "$HOME/.dsh/profiles/desktop"
```

`apply` iki bloğu da ekler. Profilde zaten yönetilmeyen bir `sandbox-policy`
veya `hooks-claude-code` girdisi varsa ona **dokunmaz**, uyarır. Bu durumda
mevcut girdinin `workspaceRoot` değerini state dizinine, `configPath` değerini
`.claude/dsh/beyin-v3-hooks.json` dosyasına kendin çevir. State kökü taşındıysa
`apply --replace` yalnız kendi `sandbox-policy` bloğunu yeniden yazar.

## Hook davranışı ve sınırlar

DSH hook komutlarını ajanın kullandığı `ctx.shell` üzerinden çalıştırır. Hook
isteği oturum politikası taşımadığı için kum havuzu **oturumsuz** çözülür:
deployment modu ve `workspaceRoot`. Varsayılan kök DSH profil klasörüdür; state
dizini orada olmadığından hook yazımı `Operation not permitted` ile reddedilir.
Bu yüzden `sandbox-policy` bloğu `mode: workspace-write` ve
`workspaceRoot: <state dizini>` yazar. DSH 0.2.1-rc.3'te yalnız bu iki anahtar
okunur; `additionalWritableRoots` yoktur ve yazılmamalıdır. Profil yaması
girdinin `config` değerini bütünüyle değiştirir, birleştirmez; bu yüzden `mode`
her zaman `workspaceRoot` ile birlikte yazılır. Blok yalnız oturumsuz çağrıları
etkiler; normal oturumların kendi sandbox preseti değişmez.

DSH tek profille her klasörü açabilir. Kurulu adaptör çalışma dizini vault
dışındaysa sessizce `{}` döner; böylece başka projelerde Beyin bağlamı
enjekte edilmez. `PostToolUse` eşleştiricisi DSH'nin küçük harfli araç adlarını
(`edit|write|str_replace_editor`) kullanır. DSH'nin köprüsü `PreCompact` ve
`SessionEnd` olaylarını taşımaz; bunlar kaydedilmez ve eşitleme `Stop` ile
sürer. Kabuk komutuyla yapılan düzenlemeler `PostToolUse` üretmez; sonraki tur
veya `beyin.py sync` kaynağı tazeler. Hook bir model çağırmaz ve sohbet
transkriptini arşivlemez.

## YAML düzenleme sınırı

Kurucu Python standart kütüphanesiyle çalışır ve genel bir YAML ayrıştırıcısı
değildir. Profil yaması için yalnız şu dar biçimi destekler:

- üst düzeyde `- ` ile başlayan girdilerden oluşan bir dizi;
- kendi `# beyin-v3:begin ...` / `# beyin-v3:end ...` işaretli blokları;
- basit `id:`, `name:`, `configPath:` ve `workspaceRoot:` skalerleri.

Flow biçimi (`[{...}]`), üst düzey eşleme, bozuk veya çift işaretli blok
reddedilir; dosya değiştirilmez. Yazma sırasında `.bak-beyin-*` yedeği alınır ve
geçici dosya atomik olarak yerine konur. İşaretli blokların dışındaki kullanıcı
metni korunur. Bu yüzden elle eklenen karmaşık YAML'ı kurucuya düzelttirmeye
çalışma; önce dosyayı kendin sadeleştir.

## Kaldırma ve geri alma

`beyin.py dsh-profile remove` yalnız kendi iki bloğunu siler; yabancı girdiler
kalır. `install_v3.py --uninstall` kayıtlı profili de temizler. DSH adaptörünü
içermeyen bir sürüme `rollback` yapıldığında profil blokları otomatik kaldırılır;
aksi halde DSH silinmiş bir hook dosyasına işaret ederdi. Profil yedeği
`cordis.patch.yml.bak-beyin-*` adıyla yanında durur.

## Doğrulama

`beyin.py doctor --json` çıktısındaki `dsh` alanı hook dosyasını, dört olayı,
komutların bu vault'a sabitlenip sabitlenmediğini, state yazılabilirliğini ve
profilin iki bloğunu raporlar. Beklenen `status: ok` değeridir. DSH yeniden
başlatıldıktan sonra özel veri içermeyen bir deneme notu oluştur, `beyin.py sync`
çalıştır ve yeni oturumda bilgiyi yalnız hook bağlamından iste. Bağlam gelmiyorsa
`beyin.py context "konu" --harness dsh` ile kaynağı doğrudan kontrol et ve
`doctor` içindeki `dsh.profile` uyarısını oku.
