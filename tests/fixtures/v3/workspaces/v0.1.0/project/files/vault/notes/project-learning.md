---
{
  "id": "project-learning",
  "kind": "note",
  "revision": 1,
  "visibility": "internal",
  "facts": {
    "fixture": true
  },
  "entity_type": "technical_learning",
  "workspace_relations": [
    {
      "id": "project-learning-topic",
      "workspace_id": "project",
      "type": "belongs_to",
      "from": {
        "workspace_id": "project",
        "record_id": "project-learning"
      },
      "to": {
        "workspace_id": "project",
        "record_id": "project-topic"
      },
      "evidence_refs": []
    }
  ]
}
---
Önceki kararın varsayımını güncel dosya sürümünde doğrula.
