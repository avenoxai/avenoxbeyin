## İkinci beyin (avenox + ege katmanı)

- **Kaydı sen yazma, `beyin-yazici` alt ajanına ver.** Bir repoda anlamlı bir iş bittiğinde ya da
  avenox hook'u kayıt (receipt) istediğinde alt ajana yalnız ham bilgiyi ilet: repo klasörü, hedef,
  yapılanlar, değişen dosyalar ve commit'ler, nasıl doğrulandığı, açık adımlar, sonraki somut adım,
  kararlar (neden ve alternatifleriyle), öğrenilen şey ve hook verdiyse `Receipt session=` değeri
  (aynen). Özeti, kartı ve receipt'i kendin yazma; hook'un "note-create + receipt" tarifinin yerine
  bu yol geçer. Alt ajanın cevabını kullanıcıya tek cümleyle aktar.
- Kullanıcı "kaydetme" derse hiçbir kayıt yazdırma.
- **Düzeltme = kural.** Kullanıcı bir davranışını düzelttiğinde kuralı beklemeden yaz ve yazdığını
  söyle. Yalnız o projeyle ilgiliyse projenin `Kurallar.md` dosyasına tarihli tek madde
  (`- YYYY-AA-GG · kural`), her işte geçerliyse vault'un genel `Kurallar.md` dosyasına; ikisinde de
  avenox'un `note-read` + `note-edit` komutlarıyla.
- **Proje adı geçmeyen hafıza sorusu** ("nerede kalmıştık", "ne karar vermiştik"): hangi projenin
  kastedildiği açık değilse tahmin etme, sor.
- **Vault'ta proje klasörü açmadan önce sor.** Oturum başında "vault'ta klasör yok" satırı gelirse
  kullanıcıya sor; yalnız "evet" derse satırdaki `proje.py init` komutunu çalıştır, "hayır" derse
  `sustur` komutunu.
