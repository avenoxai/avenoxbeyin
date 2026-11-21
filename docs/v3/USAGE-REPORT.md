# Kullanım raporu: enjekte edilen kaynak sonradan açılıyor mu?

`scripts/evaluate_v3_usage.py` repo içi bir geliştirici aracıdır (#172). Yayın paketine girmez, `beyin.py` komutu değildir. `evaluate_v3*.py` sentetik soru setleriyle "doğru not geliyor mu?" sorusunu ölçer; bu araç gerçek kullanımda "kancanın bağlama koyduğu kaynağı ajan sonradan açıyor mu?" sorusuna bakar.

```
python3 scripts/evaluate_v3_usage.py --vault /vault/yolu            # JSON, son 14 gün
python3 scripts/evaluate_v3_usage.py --vault /vault/yolu --human    # okunur tablo
python3 scripts/evaluate_v3_usage.py --vault /vault/yolu --days 0   # tüm geçmiş
```

## Ne okur, ne yazmaz

- Yalnız Claude Code transkriptleri: `~/.claude/projects/<klasör>/*.jsonl` ve alt ajanlar için `<klasör>/<oturum>/subagents/*.jsonl` (`CLAUDE_CONFIG_DIR` tanımlıysa onun altındaki `projects/`). Klasör adı Claude Code'un kuralıyla vault yolundan türetilir: harf ve rakam dışındaki her UTF-16 birimi `-` olur, 200 karakteri aşan adlara Claude Code'un özeti eklenir. Oturumu vault kökü dışında başlattıysanız klasörü `--projects-dir` ile verin.
- Dosyaları satır satır akışla okur; dev transkript belleğe alınmaz. `--days N` dosya değişiklik zamanına göre süzer.
- Salt okunur, ağsız, modelsiz. Vault'a, V3 state dizinine veya başka bir yere hiçbir şey yazmaz.
- Varsayılan çıktı yalnız sayaç ve orandır. Not yolları yalnız `--names` ile basılır; bu çıktıyı paylaşmadan önce kontrol edin.

## Nasıl sayar

- Bağlam yalnız `attachment.type == "hook_additional_context"` kayıtlarından ve `V3 source-backed context` işaretli metinden okunur. Tur bağlamında JSON'un `records[].source` alanları, açılış (companion) bağlamında kancanın kendi bölüm etiketleri (`[<kaynak>]`, `[Related source: ...]`, `[Knowledge map: ...]`, `Latest receipt (<kaynak>; ...`) kullanılır. Not adı serbest metinde aranmaz.
- Bir kaynak, aynı transkriptte enjeksiyondan **sonra** açıldıysa "açıldı" sayılır. İki sınır birlikte verilir:
  - `read_grep`: `Read` ile ya da yolu o dosya olan `Grep` ile açılanlar (alt sınır).
  - `with_shell`: bunlara ek olarak Bash/PowerShell komutunda dosyanın geçmesi. Yazma gibi görünen komutlar (`git add/commit/mv/rm/stash...`, `sed -i`, `cp/mv/rm/tee`, `Set-Content`/`Out-File`, dosyaya yönlendirme, Python yazma çağrıları) bütünüyle sayılmaz. Komutta geçmek okunduğunu kanıtlamaz; bu üst sınırdır.
- Ayrı `note-read` sütunu: aynı transkriptte enjeksiyondan sonra bu kasanın `beyin.py` başlatıcısına görünen, tek ve doğrudan bir içerik okuma girişimini sayar (ör. `python3 ./beyin.py note-read "notes/örnek not.md"` veya `py -3 .\beyin.py note-read notes/örnek.md`). Kabuk zincirleri, değişkenler, komut dosyasının içi, başka kasanın başlatıcısı ve yalnız hash isteyen `--sha-only` bu sayaca girmez. Göreli başlatıcı için transkriptte çalışma dizini gerekir; POSIX kasa yolu harf büyüklüğüyle eşleşmelidir. Başlatıcı seçenekleriyle (ör. komuttan önce `--json`) yazılan çağrılar tanınmaz. Bu sayaç yalnız tanınan doğrudan, değişkensiz komut biçimlerinin alt sınırıdır; saha çalışmasının toplam `note-read` sayısını yeniden üretmez. Araç sonucuna bakılmaz: dosya bulunmamış veya okuma başarısız olmuş olabilir. JSON'da satırın `attempted_note_read` alanı sonradan girişim yapılan önerileri, kapsamın `note_read_attempts` alanı gözlemlenen tüm uygun çağrıları sayar. Eski iki sınır aynıdır; sütunlar örtüşebilir, toplanmamalıdır.
- Kırılımlar: tür (`SessionStart` / `UserPromptSubmit`), sıra (1., 2., 3., 4+. kaynak), tam basılan ve kırpılan companion dosyaları ayrı satırda, bilgi haritası ve son receipt ayrı satırda.
- Ters yön: V3 bağlamı olan bir transkriptte, o ana kadar hiçbir enjeksiyonda geçmediği halde açılan vault notları (`.md`, nokta ile başlayan klasörler hariç). Ajanın işin gereği açtığı dosyalar da buraya düşer; bu sayı da üst sınırdır.
- Sentetik turlar kancanın kendi sınıflayıcısıyla (`beyin_v3_hook.is_synthetic_prompt`, kopya değil import) ayrılır ve ana oranlara karışmaz. Alt ajan transkriptleri ayrı bölümde sayılır.
- Transkript biçimi belgelenmemiştir ve sürümle değişebilir. Tanınmayan kayıtlar atlanır ve `skipped` altında sayılır; sayı büyükse rapora güvenmeden önce biçimi kontrol edin.

## Yorumlama sınırı

"Açılmadı", "işe yaramadı" demek değildir: önizleme yetmiş olabilir. Rapor arama kalitesine ve gürültüye işaret eder, başarı oranı değildir. Yalnız Claude Code transkriptleri okunur; Codex, Antigravity, OpenCode, Hermes ve OMP oturumları sayılmaz.
