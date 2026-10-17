"""Read-only workspace membership lookup used by the shared source reader.

The dependent experience branch adds activation and request-scoped runtime here.
"""
from beyin_v3_workspaces import safe_path, parse_document


def scope_ids(registry,record):
    declared=record.get('workspace_id')
    if isinstance(declared,str) and declared:return {declared}
    path=safe_path(registry.state,'workspace-adoptions.json')
    if not path.exists():return set()
    values=parse_document(path.read_bytes())
    return set(values.get(record['id'],[]))
