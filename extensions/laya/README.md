# Laya & JEV Modüler Eklentisi (Extension)

Bu dizin, Beyin V3 için opsiyonel yerel LLM ve uzaktan danışman (remote advisory / reranking) modülünü barındırır.

## Kapsam
* `beyin_v3_jev.py`: Otomatik bağlam zenginleştirme (`auto_context`), aday inceleme ve cevap doğrulama kuralları.
* `beyin_v3_jev_client.py`: Typesafe, Vercel ve yerel Laya sunucusu HTTP/SSE istemcisi.
* `beyin_v3_jev_contracts.py`: Veri sözleşmeleri ve şema tanımları.
* `beyin_v3_laya.py`: Yerel Laya model sunucusu entegrasyonu (shadow mode).
* `beyin_v3_memory_assessment.py`: Otomatik hafıza değerlendirme ve aday puanlama.

## Mimari İlke
Beyin V3 çekirdeği saf, modelsiz ve tamamen yerel çalışır. Bu eklenti yalnızca `state/jev.json` yapılandırması olan veya yerel Laya sunucusunu bağlamak isteyen sistemler tarafından opsiyonel olarak kullanılır. Çekirdek çalışma zamanı bu dosyalara bağımlı değildir.
