# Mobil ve Harici Yakalama Sunucusu (`ingest-server`)

İkinci beyin masaüstü başında kısayollar, CLI ve hook mekanizmalarıyla çok hızlı çalışır. Ancak masadan kalktığınızda yolda, mobilde (iOS / Android) veya harici bir cihazda aklınıza gelen bir fikri, okuduğunuz bir makaleyi veya tweet'i anında kasaya göndermek istediğinizde `beyin.py ingest-server` devreye girer.

Bu sunucu, **yerel-first ve mutlak mahremiyet** prensipleriyle tasarlanmıştır:
- **Tek yönlü (Append-only):** Dışarıdan gelen veriler doğrudan `000-Inbox/` (veya mevcut inbox) klasörüne zaman damgalı Markdown dosyaları olarak yazılır.
- **Sıfır veri sızıntısı:** Sunucu kasa içindeki hiçbir dosyayı HTTP üzerinden dışarıya servis etmez. Yalnızca sağlık yoklaması (`GET /health`) ve veri alımı (`POST /ingest` veya `POST /yakala`) açıktır.
- **Sıfır ek bağımlılık:** Python standart kütüphanesiyle (`http.server`) çalışır; pip veya ek paket gerektirmez.
- **Sertleştirilmiş güvenlik:** Zamanlama saldırılarına karşı sabit zamanlı (`hmac.compare_digest`) token doğrulaması, 1MB üst sınır, frontmatter enjeksiyon koruması ve `untrusted: true` güven etiketi ile çalışır.

---

## 1. Sunucuyu Başlatma

### Yerel Test (Sadece bu bilgisayar)
```bash
python3 beyin.py ingest-server --port 8484
# Windows:
py -3 beyin.py ingest-server --port 8484
```

### Ev Ağı veya Tailscale Üzerinden Erişim (Token Korumalı)
Telefonunuzdan veya harici cihazınızdan erişmek için yerel ağ arayüzüne bağlayın ve mutlaka bir Bearer token belirleyin:
```bash
python3 beyin.py ingest-server --host 0.0.0.0 --port 8484 --token "COK_GIZLI_PAROLANIZ"
```
*(Token ortam değişkeni olarak da verilebilir: `export BEYIN_INGEST_TOKEN="COK_GIZLI_PAROLANIZ"`)*

> [!TIP]
> **Güvenli Ağ Önerisi (Tailscale / WireGuard):** Sunucu portunu doğrudan genel internete (modem port yönlendirmesiyle) açmak yerine ücretsiz bir mesh VPN olan [Tailscale](https://tailscale.com/) kullanmanız önerilir. Bu sayede yalnızca sizin cihazlarınız (telefonunuz ve bilgisayarınız) şifreli bir tünel üzerinden haberleşir.

---

## 2. API Uç Noktaları

### `GET /health`
Sunucunun ayakta olup olmadığını kontrol eder.
```json
{
  "status": "ok",
  "service": "beyin-ingest-server",
  "version": "1.0.0",
  "inbox": "ready"
}
```

### `POST /ingest` veya `POST /yakala`
Gelen kutusuna yeni bir not fırlatır.

**İstek Başlıkları (Headers):**
- `Content-Type: application/json`
- `Authorization: Bearer <token>` *(Eğer token belirlenmişse)*

`application/json` ile gönderilen bozuk JSON ve geçersiz UTF-8 gövdeleri HTTP 400 ile reddedilir; gelen kutusuna dosya yazılmaz. Düz metin göndermek için `Content-Type: text/plain` kullanın.

**Örnek JSON Gövdesi:**
```json
{
  "title": "Dağıtık Sistemlerde SQLite Kilitleri",
  "url": "https://example.com/sqlite-locking",
  "text": "WAL modunda dahi Windows üzerinde açık handle'lar silme işlemlerini kilitliyor.",
  "tags": ["sqlite", "mimari", "mobil"]
}
```

**Başarılı Cevap (HTTP 201 Created):**
```json
{
  "status": "accepted",
  "file": "000-Inbox/20261012-073000-dagitik-sistemlerde-sqlite-kilitleri.md",
  "bytes": 284,
  "message": "Kayit basariyla gelen kutusuna eklendi."
}
```

Düz metin (raw text) gönderildiğinde de otomatik olarak ayrıştırılıp ilk satırı başlık yapılarak gelen kutusuna kaydedilir.

---

## 3. Apple Kestirmeler (iOS Shortcuts) Entegrasyonu

iPhone veya iPad'inizden Safari'de gezinirken veya herhangi bir metni seçtiğinizde tek tıkla ikinci beyninize göndermek için:

1. **Kestirmeler (Shortcuts)** uygulamasını açın ve **+ (Yeni Kestirme)** oluşturun.
2. Sağ alt köşedeki ayar simgesine basıp **"Paylaşma Sayfasında Göster" (Show in Share Sheet)** seçeneğini açın.
3. Şu eylemleri ekleyin:
   - **Metin:** `{"text": "[Kestirme Girdisi]", "title": "Mobil Paylaşım"}`
   - **URL İçeriğini Al (Get Contents of URL):**
     - URL: `http://<bilgisayar-tailscale-ip>:8484/ingest`
     - Yöntem: `POST`
     - Başlıklar:
       - `Content-Type`: `application/json`
       - `Authorization`: `Bearer COK_GIZLI_PAROLANIZ`
     - İstek Gövdesi: `JSON`
4. Kestirmeyi kaydedin. Artık iOS paylaşım menüsünden herhangi bir link veya metni seçip doğrudan kasanıza atabilirsiniz.

---

## 4. Android Entegrasyonu (HTTP Shortcuts / Termux)

- Google Play'den ücretsiz **HTTP Shortcuts** uygulamasını indirerek tek tıkla ana ekranınıza bir yakalama widget'ı ekleyebilirsiniz.
- Ya da Termux üzerinden basit bir alias:
```bash
yakala() {
  curl -X POST "http://100.x.y.z:8484/ingest" \
    -H "Authorization: Bearer COK_GIZLI_PAROLANIZ" \
    -H "Content-Type: application/json" \
    -d "{\"text\": \"$*\"}"
}
```

---

## 5. Yakalananları İşleme (Sindirim)

Mobil veya harici cihazlardan toplanan girdiler `000-Inbox/` klasöründe birikir. Masanıza döndüğünüzde:
1. `python3 beyin.py triage` çalıştırarak gelen kutusunda kaç kaynak olduğunu ve en eskilerini görün.
2. Ajanınıza `"Gelen kutusundakileri işle"` veya `"beyin-yakala"` diyerek ham kayıtların kalıcı `knowledge/` notlarına ve görevlere dönüştürülmesini sağlayın.

Güvenlik: yerel ağda dinleme Bearer token gerektirir. Token yalnız Authorization başlığıyla kabul edilir; URL sorgusuna yazılmaz. Bağlantı başına 5 saniye okuma zaman aşımı uygulanır.

Tokensiz loopback modunda Origin taşıyan tarayıcı yazma istekleri ve cross-site Fetch Metadata reddedilir; yerel CLI yakalamaları çalışır. Tarayıcı CORS erişimi token korumalı mod gerektirir.
