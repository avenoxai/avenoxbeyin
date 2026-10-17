# Kullanıcı tanımlı workspace'ler

[#368](https://github.com/avenoxai/avenoxbeyin/issues/368) kapsamında ders, kariyer, proje ve özel çalışma alanlarının kaynaklarını, görevlerini ve öğrenimlerini tek Beyin içinde birlikte kullanma altyapısı.

## Teknik sözleşme

Sürüm **workspace/0.1.0**: [davranış sözleşmesi](v0.1.0/CONTRACT.md) ve [JSON Schema](v0.1.0/schema.json). Sözleşme sürümü ürünün yayımlanmış sürümünü veya bütün kabul kontrollerinin tamamlandığını göstermez.

Workspace başına JSON teknik tanımdır. Gerçek makine yolları ve kaynak izinleri yerel state'te tutulur. Kanonik Markdown ve mevcut SyncEngine/SQLite kullanılır. Kaynağı profile eklemek erişim yetkisi üretmez; kaynak içeriği talimat veya kullanıcı onayı değildir.

Sözleşmenin alan, yetki veya hata anlamı değişirse sürümlü fark hazırlanır. Mevcut fixture ve beklenen çıktılar sessizce değiştirilmez. Sentetik örnekler tests/fixtures/v3/workspaces/v0.1.0/ altındadır; şema kontrolü çekirdeğe üçüncü taraf bağımlılığı eklemez.
