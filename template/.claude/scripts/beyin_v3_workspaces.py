"""Unified Workspace Registry (RFC #368 P0-C).

Manages user-defined, extensible, multi-source workspaces in .beyin-projects.json
without requiring core Python changes. Pure stdlib, zero added dependencies.
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import unicodedata

from beyin_v3_projects import (
    PROJECT_SCOPES_FILE,
    PROJECT_SCOPES_ALLOWED_KEYS,
    _save_project_scopes,
    read_project_scopes,
    _scope_folder_exists,
    _scope_key,
    _scope_path,
)

WORKSPACE_KINDS = (
    "software",
    "learning",
    "career",
    "research",
    "personal",
    "general",
    "custom",
)

LIFECYCLE_STATES = ("active", "archived")


def _source_key(value):
    return _scope_key(_scope_path(value))


def _serialized_write(function):
    from functools import wraps
    from inspect import signature
    call_signature = signature(function)
    @wraps(function)
    def wrapped(vault_root, *args, **kwargs):
        from types import SimpleNamespace
        from beyin_v3_notes import locked
        if call_signature.bind(vault_root, *args, **kwargs).arguments.get('dry_run'):
            return function(vault_root, *args, **kwargs)
        with locked(SimpleNamespace(root=Path(vault_root).resolve())):
            return function(vault_root, *args, **kwargs)
    return wrapped


def _read_raw_manifest(vault_root):
    path = Path(vault_root) / PROJECT_SCOPES_FILE
    if path.is_symlink():
        raise ValueError("project scopes must be a regular vault-local file")
    if not path.exists():
        return {"schema_version": 2, "workspaces": {}, "folders": {}, "shared_unscoped": False}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        raise ValueError(f"invalid project scopes file: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("project scopes file must contain a JSON object")
    if set(data) - PROJECT_SCOPES_ALLOWED_KEYS:
        raise ValueError(f"unsupported keys in project scopes file: {sorted(set(data) - PROJECT_SCOPES_ALLOWED_KEYS)}")
    # Preserve the established fail-closed scope contract before any mutation.
    read_project_scopes(vault_root)
    return data


def read_workspaces(vault_root):
    """Read all workspaces from .beyin-projects.json or synthesize from legacy folders."""
    data = _read_raw_manifest(vault_root)
    workspaces = data.get("workspaces")
    if isinstance(workspaces, dict):
        res = {}
        for wid, wdata in workspaces.items():
            if isinstance(wdata, dict):
                entry = dict(wdata)
                entry.setdefault("id", wid)
                entry.setdefault("name", wid)
                entry.setdefault("kind", "general")
                entry.setdefault("lifecycle", "active")
                entry.setdefault("sources", [])
                res[wid] = entry
        return res

    folders = data.get("folders", {})
    if isinstance(folders, dict):
        synthesized = {}
        for folder, project in folders.items():
            if not isinstance(project, str) or not project.strip():
                continue
            if project not in synthesized:
                synthesized[project] = {
                    "id": project,
                    "name": project,
                    "kind": "general",
                    "lifecycle": "active",
                    "sources": [],
                    "rules_file": f"projects/{project}/Kurallar.md",
                    "note_roots": [f"notes/{project}"],
                    "memory_policy": "selective",
                }
            if folder not in synthesized[project]["sources"]:
                synthesized[project]["sources"].append(folder)
        return synthesized
    return {}


def list_workspaces(vault_root, include_archived=False, kind=None):
    """Return list of workspace objects sorted by ID."""
    workspaces = read_workspaces(vault_root)
    result = []
    for wid in sorted(workspaces):
        ws = workspaces[wid]
        if not include_archived and ws.get("lifecycle") == "archived":
            continue
        if kind and ws.get("kind") != kind:
            continue
        result.append(ws)
    return result


def inspect_workspace(vault_root, workspace_id):
    """Inspect detailed status and source resolution for a specific workspace."""
    if not isinstance(workspace_id, str) or not workspace_id.strip():
        raise ValueError("workspace ID required")
    workspaces = read_workspaces(vault_root)
    if workspace_id not in workspaces:
        raise ValueError(f"workspace not found: {workspace_id}")
    ws = dict(workspaces[workspace_id])
    sources_detail = []
    for src in ws.get("sources", []):
        exists = _scope_folder_exists(vault_root, src)
        sources_detail.append({"folder": src, "exists": exists})
    ws["sources_detail"] = sources_detail
    rules_file = ws.get("rules_file")
    ws["rules_exist"] = (Path(vault_root) / rules_file).is_file() if rules_file else False
    return ws


@_serialized_write
def create_workspace(vault_root, workspace_id, name=None, kind="general", sources=None, rules_file=None, note_roots=None):
    """Create a new workspace in .beyin-projects.json and bind initial sources."""
    if not isinstance(workspace_id, str) or not workspace_id.strip():
        raise ValueError("workspace ID must be a non-empty string")
    workspace_id = workspace_id.strip()
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", workspace_id):
        raise ValueError("workspace ID must be 1-64 characters alphanumeric, hyphen or underscore")
    if kind not in WORKSPACE_KINDS:
        raise ValueError(f"invalid workspace kind: {kind}. Expected one of: {', '.join(WORKSPACE_KINDS)}")

    data = _read_raw_manifest(vault_root)
    existing_ws = read_workspaces(vault_root)
    if workspace_id in existing_ws:
        raise ValueError(f"workspace already exists: {workspace_id}")

    if "workspaces" not in data or not isinstance(data["workspaces"], dict):
        data["workspaces"] = dict(existing_ws)
    if "folders" not in data or not isinstance(data["folders"], dict):
        data["folders"] = {}
    data["schema_version"] = 2

    validated_sources = []
    if sources:
        if isinstance(sources, str):
            sources = [sources]
        for src in sources:
            norm_src = _scope_path(src)
            if not _scope_folder_exists(vault_root, norm_src):
                raise ValueError(f"source folder not found in vault: {norm_src}")
            src_key = _source_key(norm_src)
            for existing_f, existing_p in data["folders"].items():
                if _source_key(existing_f) == src_key and existing_p != workspace_id:
                    raise ValueError(f"source folder {norm_src} is already assigned to workspace {existing_p}")
            if not any(_source_key(item) == src_key for item in validated_sources):
                validated_sources.append(norm_src)
                data["folders"][norm_src] = workspace_id

    now = datetime.now(timezone.utc).isoformat()
    ws = {
        "id": workspace_id,
        "name": (name.strip() if isinstance(name, str) and name.strip() else workspace_id),
        "kind": kind,
        "lifecycle": "active",
        "sources": validated_sources,
        "rules_file": (rules_file if isinstance(rules_file, str) and rules_file.strip() else f"projects/{workspace_id}/Kurallar.md"),
        "note_roots": (note_roots if isinstance(note_roots, list) else [f"notes/{workspace_id}"]),
        "memory_policy": "selective",
        "created_at": now,
        "updated_at": now,
    }
    data["workspaces"][workspace_id] = ws
    _save_project_scopes(vault_root, data)
    return ws


@_serialized_write
def connect_source(vault_root, workspace_id, folder):
    """Connect an existing vault-relative folder to a workspace."""
    if not isinstance(workspace_id, str) or not workspace_id.strip():
        raise ValueError("workspace ID required")
    if not folder:
        raise ValueError("source folder required")
    norm_src = _scope_path(folder)
    if not _scope_folder_exists(vault_root, norm_src):
        raise ValueError(f"source folder not found in vault: {norm_src}")

    data = _read_raw_manifest(vault_root)
    existing_ws = read_workspaces(vault_root)
    if workspace_id not in existing_ws:
        raise ValueError(f"workspace not found: {workspace_id}")

    if "workspaces" not in data or not isinstance(data["workspaces"], dict):
        data["workspaces"] = dict(existing_ws)
    if "folders" not in data or not isinstance(data["folders"], dict):
        data["folders"] = {}
    data["schema_version"] = 2

    ws = data["workspaces"][workspace_id]
    src_key = _source_key(norm_src)
    for existing_f, existing_p in data["folders"].items():
        if _source_key(existing_f) == src_key and existing_p != workspace_id:
            raise ValueError(f"source folder {norm_src} is already assigned to workspace {existing_p}")

    sources = ws.setdefault("sources", [])
    if not any(_source_key(item) == src_key for item in sources):
        sources.append(norm_src)
    existing_key = next((key for key in data['folders'] if _source_key(key) == src_key), norm_src)
    data["folders"][existing_key] = workspace_id
    ws["updated_at"] = datetime.now(timezone.utc).isoformat()
    data["workspaces"][workspace_id] = ws
    _save_project_scopes(vault_root, data)
    return ws


@_serialized_write
def disconnect_source(vault_root, workspace_id, folder):
    """Disconnect a source folder from a workspace. Never deletes the folder from disk."""
    if not isinstance(workspace_id, str) or not workspace_id.strip():
        raise ValueError("workspace ID required")
    if not folder:
        raise ValueError("source folder required")
    norm_src = _scope_path(folder)

    data = _read_raw_manifest(vault_root)
    existing_ws = read_workspaces(vault_root)
    if workspace_id not in existing_ws:
        raise ValueError(f"workspace not found: {workspace_id}")

    if "workspaces" not in data or not isinstance(data["workspaces"], dict):
        data["workspaces"] = dict(existing_ws)
    if "folders" not in data or not isinstance(data["folders"], dict):
        data["folders"] = {}

    ws = data["workspaces"][workspace_id]
    src_key = _source_key(norm_src)
    matched_src = next((s for s in ws.get("sources", []) if _source_key(s) == src_key), None)
    if not matched_src:
        raise ValueError(f"source folder {norm_src} is not connected to workspace {workspace_id}")

    ws["sources"] = [source for source in ws["sources"] if _source_key(source) != src_key]
    matched_folder = next((f for f in data["folders"] if _source_key(f) == src_key), None)
    if matched_folder and data["folders"][matched_folder] == workspace_id:
        del data["folders"][matched_folder]

    ws["updated_at"] = datetime.now(timezone.utc).isoformat()
    data["workspaces"][workspace_id] = ws
    _save_project_scopes(vault_root, data)
    return ws


@_serialized_write
def archive_workspace(vault_root, workspace_id):
    """Mark a workspace as archived; keeps sources intact on disk."""
    if not isinstance(workspace_id, str) or not workspace_id.strip():
        raise ValueError("workspace ID required")
    data = _read_raw_manifest(vault_root)
    existing_ws = read_workspaces(vault_root)
    if workspace_id not in existing_ws:
        raise ValueError(f"workspace not found: {workspace_id}")

    if "workspaces" not in data or not isinstance(data["workspaces"], dict):
        data["workspaces"] = dict(existing_ws)
    ws = data["workspaces"][workspace_id]
    ws["lifecycle"] = "archived"
    ws["updated_at"] = datetime.now(timezone.utc).isoformat()
    data["workspaces"][workspace_id] = ws
    _save_project_scopes(vault_root, data)
    return ws


@_serialized_write
def unarchive_workspace(vault_root, workspace_id):
    """Restore an archived workspace to active lifecycle."""
    if not isinstance(workspace_id, str) or not workspace_id.strip():
        raise ValueError("workspace ID required")
    data = _read_raw_manifest(vault_root)
    existing_ws = read_workspaces(vault_root)
    if workspace_id not in existing_ws:
        raise ValueError(f"workspace not found: {workspace_id}")

    if "workspaces" not in data or not isinstance(data["workspaces"], dict):
        data["workspaces"] = dict(existing_ws)
    ws = data["workspaces"][workspace_id]
    ws["lifecycle"] = "active"
    ws["updated_at"] = datetime.now(timezone.utc).isoformat()
    data["workspaces"][workspace_id] = ws
    _save_project_scopes(vault_root, data)
    return ws


def plan_workspace(vault_root, workspace_id, kind="general", name=None, description=None, sources=None, rules=None):
    """Generate a verifiable provisioning plan for a workspace without disk mutation (RFC #368 P0-D)."""
    if not isinstance(workspace_id, str) or not workspace_id.strip():
        raise ValueError("workspace ID must be a non-empty string")
    workspace_id = workspace_id.strip()
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", workspace_id):
        raise ValueError("workspace ID must be 1-64 characters alphanumeric, hyphen or underscore")
    if kind not in WORKSPACE_KINDS:
        raise ValueError(f"invalid workspace kind: {kind}. Expected one of: {', '.join(WORKSPACE_KINDS)}")

    existing_ws = read_workspaces(vault_root)
    if workspace_id in existing_ws:
        raise ValueError(f"workspace already exists: {workspace_id}")

    display_name = name.strip() if isinstance(name, str) and name.strip() else workspace_id.replace("-", " ").title()
    desc = description.strip() if isinstance(description, str) and description.strip() else f"{display_name} çalışma alanı"

    # Default folders
    default_project_dir = f"projects/{workspace_id}"
    default_note_dir = f"notes/{workspace_id}"

    source_list = []
    if sources:
        if isinstance(sources, str):
            sources = [s.strip() for s in sources.split(",") if s.strip()]
        for s in sources:
            norm = _scope_path(s)
            if norm not in source_list:
                source_list.append(norm)
    if not source_list:
        source_list = [default_project_dir]

    dirs_to_create = []
    for d in [default_project_dir, default_note_dir] + source_list:
        norm_d = _scope_path(d)
        if norm_d not in dirs_to_create:
            dirs_to_create.append(norm_d)

    # Scaffolding templates
    rules_file_path = f"{default_project_dir}/Kurallar.md"
    rules_body = rules.strip() if isinstance(rules, str) and rules.strip() else "- Bu çalışma alanına ait kararlar ve tercihler yerinde tutulur.\n- Genel vault kuralları geçerlidir."

    rules_content = f"""# {display_name} — Kurallar

{desc}

## Kapsam ve Çalışma İlkeleri
- **Alan Türü:** {kind}
- **Kaynak Klasörleri:** {', '.join(source_list)}
- **Hafıza Politikası:** selective

## Özel Kurallar
{rules_body}
"""

    moc_file_path = f"{default_project_dir}/MOC.md"
    moc_content = f"""# {display_name} — Map of Content (MOC)

{desc}

## Belgeler ve Notlar
- [[projects/{workspace_id}/Kurallar|Çalışma Alanı Kuralları]]
"""

    files_to_create = [
        {"path": rules_file_path, "content": rules_content, "purpose": "rules"},
        {"path": moc_file_path, "content": moc_content, "purpose": "navigation"},
    ]

    return {
        "workspace_id": workspace_id,
        "name": display_name,
        "kind": kind,
        "description": desc,
        "sources": source_list,
        "rules_file": rules_file_path,
        "note_roots": [default_note_dir],
        "memory_policy": "selective",
        "directories": dirs_to_create,
        "files": files_to_create,
    }


@_serialized_write
def provision_workspace(vault_root, workspace_id, kind="general", name=None, description=None, sources=None, rules=None, dry_run=False):
    """Provision a workspace with atomic rollback, template scaffolding, and verification (RFC #368 P0-D)."""
    vault_root = Path(vault_root)
    plan = plan_workspace(vault_root, workspace_id, kind=kind, name=name, description=description, sources=sources, rules=rules)
    if dry_run:
        return {"status": "dry_run", "action": "provision", "plan": plan}

    vault_root = vault_root.resolve()
    def checked_path(relative):
        path = vault_root / relative
        if not path.resolve().is_relative_to(vault_root):
            raise ValueError("workspace output must remain inside the vault")
        return path

    # Validate the complete plan before the first directory or file is created.
    for relative in plan['directories'] + [item['path'] for item in plan['files']]:
        checked_path(relative)
    created_dirs = []
    created_files = []
    manifest_backup = None
    manifest_written = None
    manifest_path = vault_root / PROJECT_SCOPES_FILE
    if manifest_path.exists():
        manifest_backup = manifest_path.read_bytes()

    try:
        # 1. Create directories
        for d in plan["directories"]:
            dir_path = checked_path(d)
            if not dir_path.exists():
                dir_path.mkdir(parents=True, exist_ok=True)
                created_dirs.append(dir_path)

        # 2. Write template files (never overwrite existing user files)
        written_files = []
        for fspec in plan["files"]:
            file_path = checked_path(fspec["path"])
            if not file_path.exists():
                file_path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    with file_path.open('x', encoding='utf-8') as output:
                        created_files.append(file_path)
                        output.write(fspec['content'])
                except FileExistsError:
                    written_files.append({"path": fspec["path"], "status": "preserved"})
                else:
                    written_files.append({"path": fspec["path"], "status": "created"})
            else:
                written_files.append({"path": fspec["path"], "status": "preserved"})

        # 3. Register in .beyin-projects.json
        ws = create_workspace(
            vault_root,
            workspace_id=plan["workspace_id"],
            name=plan["name"],
            kind=plan["kind"],
            sources=list(dict.fromkeys(plan["sources"] + plan["note_roots"])),
            rules_file=plan["rules_file"],
            note_roots=plan["note_roots"],
        )

        manifest_written = manifest_path.read_bytes()

        # 4. Self-verification
        verification = inspect_workspace(vault_root, workspace_id)
        if not verification.get("rules_exist"):
            raise RuntimeError(f"verification failed: rules file missing at {plan['rules_file']}")
        all_sources_exist = all(s.get("exists") for s in verification.get("sources_detail", []))
        if not all_sources_exist:
            raise RuntimeError("verification failed: one or more source folders not found after creation")

        return {
            "status": "provisioned",
            "action": "provision",
            "workspace": ws,
            "created_directories": [str(p.relative_to(vault_root).as_posix()) for p in created_dirs],
            "files": written_files,
            "verified": True,
        }

    except Exception as exc:
        # Atomic rollback on failure
        for f in created_files:
            try:
                if f.exists():
                    f.unlink()
            except OSError:
                pass
        for d in reversed(created_dirs):
            try:
                if d.exists() and not any(d.iterdir()):
                    d.rmdir()
            except OSError:
                pass
        # Restore only our own unchanged snapshot. Preserve intervening changes
        # from an editor or a reentrant caller rather than rolling them back.
        current_manifest = manifest_path.read_bytes() if manifest_path.exists() else None
        if manifest_written is not None and current_manifest == manifest_written:
            if manifest_backup is not None:
                from beyin_v3_sync import atomic
                atomic(manifest_path, manifest_backup.decode('utf-8'))
            else:
                manifest_path.unlink()
        raise


def manage(
    vault_root,
    action,
    workspace_id=None,
    name=None,
    kind=None,
    description=None,
    rules=None,
    source=None,
    dry_run=False,
    include_archived=False,
):
    """Unified CLI dispatcher for beyin.py workspace actions."""
    vault_root = Path(vault_root)
    if action == "list":
        workspaces = list_workspaces(vault_root, include_archived=include_archived, kind=kind)
        return {"status": "ok", "action": "list", "workspaces": workspaces, "count": len(workspaces)}
    elif action == "inspect":
        if not workspace_id:
            raise ValueError("workspace inspect requires workspace ID")
        return inspect_workspace(vault_root, workspace_id)
    elif action == "create":
        if not workspace_id:
            raise ValueError("workspace create requires workspace ID")
        sources = [source] if source else []
        ws = create_workspace(vault_root, workspace_id, name=name, kind=(kind or "general"), sources=sources)
        return {"status": "created", "action": "create", "workspace": ws}
    elif action == "connect":
        if not workspace_id or not source:
            raise ValueError("workspace connect requires workspace ID and --source")
        ws = connect_source(vault_root, workspace_id, source)
        return {"status": "connected", "action": "connect", "workspace": ws, "source": source}
    elif action == "disconnect":
        if not workspace_id or not source:
            raise ValueError("workspace disconnect requires workspace ID and --source")
        ws = disconnect_source(vault_root, workspace_id, source)
        return {"status": "disconnected", "action": "disconnect", "workspace": ws, "source": source}
    elif action == "archive":
        if not workspace_id:
            raise ValueError("workspace archive requires workspace ID")
        ws = archive_workspace(vault_root, workspace_id)
        return {"status": "archived", "action": "archive", "workspace": ws}
    elif action == "unarchive":
        if not workspace_id:
            raise ValueError("workspace unarchive requires workspace ID")
        ws = unarchive_workspace(vault_root, workspace_id)
        return {"status": "unarchived", "action": "unarchive", "workspace": ws}
    elif action == "plan":
        if not workspace_id:
            raise ValueError("workspace plan requires workspace ID")
        sources = [source] if source else None
        plan = plan_workspace(
            vault_root,
            workspace_id,
            kind=(kind or "general"),
            name=name,
            description=description,
            sources=sources,
            rules=rules,
        )
        return {"status": "ok", "action": "plan", "plan": plan}
    elif action == "provision":
        if not workspace_id:
            raise ValueError("workspace provision requires workspace ID")
        sources = [source] if source else None
        return provision_workspace(
            vault_root,
            workspace_id,
            kind=(kind or "general"),
            name=name,
            description=description,
            sources=sources,
            rules=rules,
            dry_run=dry_run,
        )
    else:
        raise ValueError(f"unknown workspace action: {action}")
