# Kiro (IDE 1.x ve CLI V3)

Bu kaynak sürümündeki V3 kurucu Kiro için vault içine proje düzeyinde bağlantı kurar.
Kiro desteği içermeyen eski ZIP paketleri yerine bu kaynak checkout'unu veya
Kiro adaptörünü içeren daha yeni bir paketi kullan. Kiro hesabı veya programı
kurucu tarafından kurulmaz.

## Kurulum

Vault klasörünü seç ve repo kökünden çalıştır:

```sh
python3 scripts/install_v3.py --vault "/tam/yol/Beynim"
python3 "/tam/yol/Beynim/beyin.py" doctor --human
```

Windows'ta `python3` yerine `py -3` kullan. Kurucu şunları yazar:

- `.kiro/hooks/beyin-v3.json`: Kiro'nun `v1` proje hook biçiminde dört komut. Başka
  `.kiro/hooks/*.json` dosyalarına dokunmaz.
- `.kiro/skills/{beyin,beyin-doktor,beyin-guncelle,beyin-kontrol}/SKILL.md`: `.agents/skills`
  ile eşitlenen başlangıç skill'leri.
- Vault kökündeki `AGENTS.md` içinde yönetilen Beyin bloğu. Kiro bu dosyayı
  doğrudan okur; ayrı bir `.kiro/steering/` kopyası gerekmez.

Kiro IDE'de vault klasörünü aç, Agent Hooks panelinde Beyin hook'larını incele ve
yeni bir oturum başlat. Kiro CLI V3'te vault klasöründen yeni oturum açıp `/hooks`
ile etkin bağlantıları incele. Kiro'nun normal klasör, komut ve hook izinlerini
inceleyerek ver; kurucu izin kaydı veya onay üretmez. Özel veri içermeyen bir
deneme notu oluştur, `beyin.py sync` çalıştır ve yeni oturumda bilgiyi yalnız
otomatik hook bağlamından geri iste. Bu test başarısızsa `beyin.py doctor` ve
`beyin.py context "konu" --harness kiro` ile kaynağı doğrudan kontrol et.

## Hook davranışı ve sınırlar

`SessionStart` açılış bağlamını, `UserPromptSubmit` ilgili kaynakları getirir;
`PostToolUse` Kiro'nun `write` araç kategorisinde düzenleme kontrol noktası
oluşturur. `Stop` tur sonunu kaydeder. `SessionEnd` yalnız CLI V3'te bulunduğu
için ortak IDE/CLI kurulumunda kullanılmaz; eşitleme `Stop` ile sürer.
Kiro'da belgelenmiş `PreCompact` olayı yoktur. Kabuk komutlarıyla yapılan dosya
değişiklikleri `PostToolUse` için `write` eşleşmesini tetiklemez; sonraki istem
veya `beyin.py sync` kaynağı tazeler.

Kiro başarılı komut hook'unun stdout'unu düz metin olarak bağlama ekler. Kurulu
`beyin_v3_kiro.py` adaptörü ortak Python hook'unun JSON sonucundan yalnız metni
çıkarır; boş olaylarda hiçbir şey yazmaz. Hook bir model çağırmaz ve özel sohbet
transkriptini arşivlemez. İstemci kapalıyken sürekli çalışan servis yoktur.

Kiro'nun eski IDE 0.x `.kiro.hook` ve CLI 2.x profil hook biçimleri bu kurulumun
hedefi değildir. Özel Kiro agent profilleri steering ve skill kaynaklarını
otomatik yüklemeyebilir; böyle bir profil kullanıyorsan kendi `resources`
ayarında `skill://.kiro/skills/*/SKILL.md` ve gerekirse `file://AGENTS.md` kaynağını açıkça
ekle. Aynı adlı kullanıcı skill'leri sessizce ezilmez; çakışma `doctor` içinde
görünür.

Kaynaklar: [Kiro Hooks](https://kiro.dev/docs/hooks/),
[hook olayları](https://kiro.dev/docs/hooks/types/),
[hook çıktısı](https://kiro.dev/docs/hooks/actions/),
[AGENTS.md steering](https://kiro.dev/docs/steering/),
[skills](https://kiro.dev/docs/skills/).
