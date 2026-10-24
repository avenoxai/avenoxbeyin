---
{
  "id": "custom-result",
  "kind": "note",
  "revision": 1,
  "visibility": "internal",
  "facts": {
    "fixture": true,
    "observation": "Kayıtlı ölçüm"
  },
  "entity_type": "shooting_result",
  "workspace_relations": [
    {
      "id": "custom-result-topic",
      "workspace_id": "custom",
      "type": "belongs_to",
      "from": {
        "workspace_id": "custom",
        "record_id": "custom-result"
      },
      "to": {
        "workspace_id": "custom",
        "record_id": "custom-topic"
      },
      "evidence_refs": []
    }
  ]
}
---
1/60 saniyede hareket bulanıklığı görüldü.
