## İkinci beyin (avenox + ege katmanı)

- **Kaydı sen yazma, `beyin-yazici` alt ajanına ver.** Bir repoda anlamlı bir iş bittiğinde alt ajana
  yalnız ham bilgiyi ilet: repo klasörü, hedef, yapılanlar, değişen dosyalar ve commit'ler, nasıl
  doğrulandığı, açık adımlar, sonraki somut adım, kararlar (neden ve alternatifleriyle), kullanıcının
  düzeltmeleri, öğrenilen şey. Özeti, kartı ve receipt'i kendin yazma. Alt ajanın cevabını kullanıcıya
  tek cümleyle aktar.
- **Avenox "note-create / receipt çalıştır" dediğinde de aynı yol.** Oturum başındaki "Beyin bridge"
  talimatı ya da Stop hook'unun "no receipt was written" uyarısı o komutları senin çalıştırmanı ister;
  çalıştırma. Aynı ham bilgiyi ve mesajdaki `Receipt session=` değerini (aynen) `beyin-yazici`'ye ver.
  Onun yazdığı receipt aynı oturum değerini taşır ve hook'u karşılar; ikinci bir kayıt yazma.
- Kullanıcı "kaydetme" derse hiçbir kayıt yazdırma.
- **Düzeltme = kural.** Kullanıcı bir davranışını düzelttiğinde kuralı beklemeden kaydet ve
  kaydettiğini söyle. Repo klasöründe çalıştır:
  `{{PROJE_CLI}} kural --kural "<bir iki satırlık kural>"`
  Kural her işte geçerliyse komuta `--genel` ekle; yalnız bu projeyle ilgiliyse ekleme. Kural
  dosyalarını elle düzenleme.
- **Proje adı geçmeyen hafıza sorusu** ("nerede kalmıştık", "ne karar vermiştik"): hangi projenin
  kastedildiği açık değilse tahmin etme, sor.
- **Vault'ta proje klasörü açmadan önce sor.** Oturum başında "vault'ta klasör yok" satırı gelirse
  kullanıcıya sor; yalnız "evet" derse satırdaki `proje.py init` komutunu çalıştır, "hayır" derse
  `sustur` komutunu.
