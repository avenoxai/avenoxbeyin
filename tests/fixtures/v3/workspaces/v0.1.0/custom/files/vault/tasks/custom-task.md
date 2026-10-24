---
{
  "id": "custom-task",
  "kind": "task",
  "revision": 1,
  "visibility": "internal",
  "facts": {
    "fixture": true
  },
  "entity_type": "shooting_task",
  "workspace_relations": [
    {
      "id": "custom-task-topic",
      "workspace_id": "custom",
      "type": "belongs_to",
      "from": {
        "workspace_id": "custom",
        "record_id": "custom-task"
      },
      "to": {
        "workspace_id": "custom",
        "record_id": "custom-topic"
      },
      "evidence_refs": []
    }
  ],
  "status": "active",
  "due_at": "2026-10-23",
  "next_action": "Kaynak ve önceki öğrenimle devam et"
}
---
Son çekim sonucuyla bir sonraki denemeyi hazırlayalım.
