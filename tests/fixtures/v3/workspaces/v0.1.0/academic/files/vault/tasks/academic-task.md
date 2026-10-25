---
{
  "id": "academic-task",
  "kind": "task",
  "revision": 1,
  "visibility": "internal",
  "facts": {
    "fixture": true
  },
  "entity_type": "assignment",
  "workspace_relations": [
    {
      "id": "academic-task-topic",
      "workspace_id": "academic",
      "type": "belongs_to",
      "from": {
        "workspace_id": "academic",
        "record_id": "academic-task"
      },
      "to": {
        "workspace_id": "academic",
        "record_id": "academic-topic"
      },
      "evidence_refs": []
    }
  ],
  "status": "active",
  "due_at": "2026-10-23",
  "next_action": "Kaynak ve önceki öğrenimle devam et"
}
---
Bu dersin ödevine geri bildirimle devam edelim.
