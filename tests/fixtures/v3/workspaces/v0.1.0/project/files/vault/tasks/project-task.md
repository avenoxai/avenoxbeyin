---
{
  "id": "project-task",
  "kind": "task",
  "revision": 1,
  "visibility": "internal",
  "facts": {
    "fixture": true
  },
  "entity_type": "implementation_task",
  "workspace_relations": [
    {
      "id": "project-task-topic",
      "workspace_id": "project",
      "type": "belongs_to",
      "from": {
        "workspace_id": "project",
        "record_id": "project-task"
      },
      "to": {
        "workspace_id": "project",
        "record_id": "project-topic"
      },
      "evidence_refs": []
    }
  ],
  "status": "active",
  "due_at": "2026-10-23",
  "next_action": "Kaynak ve önceki öğrenimle devam et"
}
---
Güncel dosya ve önceki kararla göreve devam edelim.
