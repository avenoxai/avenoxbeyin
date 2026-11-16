# Fork skill'leri

Bu klasörde yalnız bu fork'a ait (upstream'de olmayan) skill'ler durur. Upstream'in kurduğu
skill'ler (`beyin`, `beyin-doktor`, `beyin-guncelle`, `beyin-kontrol` …) `template/` altındadır
ve burada kopyalanmaz.

| Skill | Ne yapar | Nereye kurulur | Durum |
|---|---|---|---|
| [beyin-skill-onerici](beyin-skill-onerici/SKILL.md) | Kayıt, kural ve kararlarda tekrar eden iş akışlarını bulur, en çok 3 skill önerir; onaylananı oluşturur, sonucu `_Sistem/Skill Önerileri.md` defterine yazar | Editör makinede vault | Yazıldı, henüz hiçbir makineye kurulmadı |

## Bir skill'i makineye kurmak

Kurulum = skill klasörünü olduğu gibi kopyalamak. İki seçenek var:

1. **Global** (her repoda görünsün): klasörü `~/.claude/skills/<skill-adi>/` altına kopyala.
2. **Vault** (yalnız vault oturumlarında görünsün): klasörü vault içinde hem
   `.agents/skills/<skill-adi>/` hem `.claude/skills/<skill-adi>/` altına kopyala.
   `.agents/skills/` ortak kaynaktır, `.claude/skills/` Claude Code'un gördüğü kopyadır.

`beyin-skill-onerici` vault komutlarını (`beyin.py`) kullandığı için **vault** seçeneğiyle,
yalnız editör makineye kurulur.

Kurduktan sonra yeni bir Claude Code oturumu aç; skill listesinde görünmesi yeterli kontroldür.

## Upstream skill araçlarıyla ilişkisi

Avenox'ta `beyin.py skill-sync` (`.agents/skills` ile istemci kopyalarını eşitler) ve
`beyin.py skill-import --source <klasör>` (seçilen tek skill'i vault'a alır) komutları var.
Biz fork skill'lerini bilerek bunlardan **ayrı** tutuyoruz: elle kopyalıyoruz ve güncellemeyi
de elle yapıyoruz. Böylece upstream'in yönettiği skill'lerle karışmaz, `beyin.py update`
sırasında çakışma çıkmaz.

## Kurallar

- Depo herkese açık: skill metnine e-posta, makine adı, şirket iç bilgisi, şifre veya mutlak
  kullanıcı yolu yazılmaz.
- Her skill'in frontmatter'ında `name` (klasör adıyla aynı) ve `description` bulunur;
  `description` ne zaman kullanılacağını ve ne zaman kullanılmayacağını söyler.
