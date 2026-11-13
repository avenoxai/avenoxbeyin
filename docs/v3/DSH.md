# DeepSeek Harness (DSH)

DSH'de Claude Code/Codex tarzı shell hook'u yoktur. Bu yüzden V3 kurulumu DSH için hook dosyası yazmaz; istemci, talimat ve skill keşfi üzerinden aynı motora bağlanır:

- DSH, proje kökünden çalışma klasörüne kadar `AGENTS.md` ve `CLAUDE.md` zincirini okur (varsayılan 65.536 bayt bütçe).
- Yerel skill sağlayıcısı `<proje kökü>/.agents/skills` dizinini tarar; installer `beyin`, `beyin-doktor`, `beyin-guncelle` (paket `beyin-kontrol` içeriyorsa o da) skill'lerini buraya yazar.
- `beyin.py` istemciden bağımsız bir CLI'dır: `python3 "<vault>/beyin.py" --vault "<vault>" <komut>` her klasörden çalışır.

Kurulum sonrası vault klasöründe yeni bir DSH oturumu açmak yeterlidir; ek plugin veya hook güven onayı yoktur.

## Otomatik olmayan adımlar

Hook olmadığı için şu üç adım ajanın kendisindedir:

| Adım | Komut |
| --- | --- |
| Bağlam arama | `python3 "<vault>/beyin.py" --vault "<vault>" context "<soru>" --project <proje>` |
| Not yazma + eşitleme | `note-create --file <json>` → `sync` |
| Kaynak bağlı makbuz | `receipt --harness dsh --event-id "<tekil>" --summary "<özet>" --ref "<not yolu>"` |

Varsayılan `AGENTS.md` bloğu bu sırayı anlatır; kasa dışındaki projeler için [global köprü](#kasa-dışındaki-projeler) bölümüne bakın.

## Makbuz uyumu

`dsh` yeni bir istemci adıdır. Eski sürümler bilinmeyen `harness` değerini reddedip eşitlemeyi düşürdüğü için `dsh` makbuzları `harness: manual` + `client: dsh` olarak yazılır; bu sürüm ve sonrası bunu `dsh` olarak okur, eski sürümler `manual` olarak görür ve eşitleme bozulmaz (`pi` ile aynı desen, #385).

## İsteğe bağlı: oturum köprüsü eklentisi

Depoda `extras/dsh-bridge/` altında isteğe bağlı bir DSH plugin'i ve telafi tarayıcısı bulunur. Kurucu bunları **kurmaz**; DSH profiline elle eklenir. Yaptıkları:

1. **Oturum başı:** `beyin.py recap --days 7` çalıştırıp son kayıtları bağlama enjekte eder.
2. **Tur sonu:** sonuç/karar sinyali gördüğünde ajana "Bunu beyne kaydedeyim mi?" sorusunu sordurtur.

Plugin DSH'nin iç Cordis API'sine dayanır (`agent.followup`, `session/event`, `agent/created`), profil `package.json` ve `cordis.patch.yml` değişikliği ve DSH'yi yeniden başlatma gerektirir. Kurulum, geri alma ve sınırlar `extras/dsh-bridge/README.md` içindedir.

## Kasa dışındaki projeler

DSH hook'u olmadığı için `bridge --harness dsh` yoktur. Bunun yerine:

1. Tüm oturumlara yüklenen kullanıcı-geneli `AGENTS.md` (`$DSH_HOME/AGENTS.md` veya `~/.agents/AGENTS.md`) içine vault yolunu ve `context`/`receipt` komutlarını yazın.
2. İsteğe bağlı olarak her proje klasörüne kısa bir `AGENTS.md` işaretçisi koyun (proje kimliği + vault yolu).
3. `~/.agents/skills/merkez-beyin/SKILL.md` gibi bir kullanıcı skill'i, kasaya mutlak yolla bağlanan komutları taşır.

Bu üç dosya DSH'nin kendi keşif mekanizmasını kullanır; beyin'in yönettiği dosyalar değildir, `beyin.py update` onlara dokunmaz.

## Telafi taraması

Ajan bir oturumu kapatmayı unutursa oturum kaydı `~/.dsh/sessions/<proje>/<oturum>/session.v4.jsonl.zstd` altında kalır. `extras/dsh-bridge/dsh_tara.py` son N gündeki ana oturumları (subagent'lar hariç) listeler:

```sh
python3 extras/dsh-bridge/dsh_tara.py --days 7 --projects proje-a,proje-b
```

Yalnız okur; hiçbir dosya yazmaz. Çıktıdaki oturumlar kasada karşılığı yoksa ajan bunları kısa notlara damıtabilir.

## Yaşam döngüsü eşlemesi

DSH hook göndermediği için `SessionStart`, `UserPromptSubmit`, `Stop`, `PreCompact`, `SessionEnd` olayları oluşmaz; `beyin.py doctor` çıktısında DSH istemcisi "henüz doğrulanmadı" görünür. Bu bir hata değil, istemcinin hook'suz olmasının sonucudur. Makbuzlar yine `--harness dsh` ile yazılır ve `recap`/`context` aynı şekilde çalışır.

## Doğrulama sınırı

Bu belge, DSH'nin dosya sistemi tabanlı keşif davranışına (proje kökü, `AGENTS.md` zinciri, `.agents/skills` kökü) dayanır. Kurulum ve CLI akışı sentetik bir vault'ta `tests/v3_dsh_test.py` ile sınanır; oturum köprüsü eklentisi DSH'nin iç API'sine dayandığı için sürüm değişikliklerinde yeniden doğrulanmalıdır. Gerçek istemci teslimi iddiası için [gerçek istemci kontrolleri](LIVE-CLIENTS.md) ölçütü geçerlidir.
