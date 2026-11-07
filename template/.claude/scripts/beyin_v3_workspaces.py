"""Unified Workspace Registry (RFC #368 P0-C, P0-D).

Workspace metadata (name, kind, lifecycle, rules file, note roots, provisioning
state) lives in its own vault-local file, ``.beyin-workspaces.json``. Folder
bindings stay in the existing ``.beyin-projects.json`` ``folders`` map, whose
format does not change: an older release reads that file exactly as before and
ignores the registry, so an update rollback keeps every explicit-project gate
working. A workspace's sources are always derived from that map, so a later
``project-scope add/remove`` (from this or an older release) can never leave
the registry pointing at a stale binding. Pure stdlib, zero added dependencies.
"""
from datetime import datetime, timezone
from functools import wraps
from inspect import signature
import json
import os
from pathlib import Path
import re
import tempfile
import uuid

from beyin_v3_projects import (
    PROJECT_SCOPES_FILE,
    _save_project_scopes,
    read_project_scopes,
    _scope_folder_exists,
    _scope_key,
    _scope_path,
)

WORKSPACES_FILE = ".beyin-workspaces.json"
REGISTRY_SCHEMA_VERSION = 1

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

# A record left by an interrupted or failed provisioning; `workspace provision <id>` resumes it.
RESUMABLE_PROVISION_STATES = ("pending", "failed")


class WorkspaceProvisionError(RuntimeError):
    """Provisioning failed; ``workspace_reconciliation`` reports what the rollback left behind."""

    def __init__(self, message, reconciliation):
        super().__init__(message)
        self.workspace_reconciliation = reconciliation


def _now():
    return datetime.now(timezone.utc).isoformat()


def _source_key(value):
    return _scope_key(_scope_path(value))


def _serialized_write(function):
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


def _validate_id(workspace_id):
    if not isinstance(workspace_id, str) or not workspace_id.strip():
        raise ValueError("workspace ID must be a non-empty string")
    workspace_id = workspace_id.strip()
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", workspace_id):
        raise ValueError("workspace ID must be 1-64 characters alphanumeric, hyphen or underscore")
    return workspace_id


def _validate_kind(kind):
    if kind not in WORKSPACE_KINDS:
        raise ValueError(f"invalid workspace kind: {kind}. Expected one of: {', '.join(WORKSPACE_KINDS)}")


def _required_id(workspace_id):
    if not isinstance(workspace_id, str) or not workspace_id.strip():
        raise ValueError("workspace ID required")
    return workspace_id.strip()


def _read_registry(vault_root):
    """(registry, present). Fails closed on a symlink, invalid JSON or wrong field types."""
    path = Path(vault_root) / WORKSPACES_FILE
    if path.is_symlink():
        raise ValueError("workspace registry must be a regular vault-local file")
    if not path.exists():
        return {"schema_version": REGISTRY_SCHEMA_VERSION, "workspaces": {}}, False
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (ValueError, OSError) as exc:
        raise ValueError(f"invalid workspace registry {WORKSPACES_FILE}; expected JSON") from exc
    if not isinstance(data, dict) or not isinstance(data.get("workspaces"), dict):
        raise ValueError(f"{WORKSPACES_FILE} must be an object with a workspaces object")
    version = data.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValueError(f"{WORKSPACES_FILE} schema_version must be a positive integer")
    for workspace_id, entry in data["workspaces"].items():
        if not isinstance(entry, dict):
            raise ValueError(f"workspace registry entry must be an object: {workspace_id}")
        for field in ("name", "kind", "lifecycle", "rules_file", "memory_policy"):
            if field in entry and not isinstance(entry[field], str):
                raise ValueError(f"workspace {workspace_id}: {field} must be a string")
        roots = entry.get("note_roots", [])
        if not isinstance(roots, list) or not all(isinstance(root, str) for root in roots):
            raise ValueError(f"workspace {workspace_id}: note_roots must be a list of strings")
        if not isinstance(entry.get("provision", {}), dict):
            raise ValueError(f"workspace {workspace_id}: provision must be an object")
    return data, True


def _writable_registry(vault_root):
    registry, _ = _read_registry(vault_root)
    if registry["schema_version"] > REGISTRY_SCHEMA_VERSION:
        raise ValueError(f"{WORKSPACES_FILE} was written by a newer Beyin release; update before changing workspaces")
    return registry


def _save_registry(vault_root, registry):
    path = Path(vault_root) / WORKSPACES_FILE
    if path.is_symlink():
        raise ValueError("workspace registry must be a regular vault-local file")
    content = (json.dumps(registry, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    fd, temporary = tempfile.mkstemp(prefix=".beyin-workspaces-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _read_scopes(vault_root):
    """The raw scope file after the runtime's own fail-closed validation; None when absent."""
    if read_project_scopes(vault_root) is None:
        return None
    path = Path(vault_root) / PROJECT_SCOPES_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (ValueError, OSError) as exc:
        raise ValueError("invalid project scopes file; expected JSON") from exc
    if (not isinstance(data, dict) or set(data) - {"folders", "shared_unscoped"}
            or not isinstance(data.get("folders", {}), dict)
            or not isinstance(data.get("shared_unscoped", False), bool)):
        raise ValueError("project scopes changed while reading; retry")
    return data


def _folders(scopes):
    return dict((scopes or {}).get("folders", {}))


def _owner(folders, folder):
    """(stored spelling, project) of the binding for this folder, or (None, None)."""
    key = _source_key(folder)
    return next(((name, project) for name, project in folders.items() if _source_key(name) == key),
                (None, None))


def _bind(vault_root, workspace_id, sources):
    """Bind folders in .beyin-projects.json; returns the folders this call added."""
    scopes = _read_scopes(vault_root)
    data = dict(scopes) if scopes is not None else {"folders": {}, "shared_unscoped": False}
    folders = _folders(data)
    added = []
    for folder in sources:
        _, owner = _owner(folders, folder)
        if owner == workspace_id:
            continue
        if owner is not None:
            raise ValueError(f"source folder {folder} is already assigned to workspace {owner}")
        folders[folder] = workspace_id
        added.append(folder)
    if added:
        data["folders"] = folders
        data.setdefault("shared_unscoped", False)
        _save_project_scopes(vault_root, data)
    return added


def _unbind(vault_root, workspace_id, sources):
    """Remove only bindings that still name this workspace; returns the folders removed."""
    scopes = _read_scopes(vault_root)
    if scopes is None:
        return []
    data = dict(scopes)
    folders = _folders(data)
    removed = []
    for folder in sources:
        name, owner = _owner(folders, folder)
        if name is not None and owner == workspace_id:
            del folders[name]
            removed.append(folder)
    if removed:
        data["folders"] = folders
        _save_project_scopes(vault_root, data)
    return removed


def _legacy_entry(project):
    return {
        "id": project,
        "name": project,
        "kind": "general",
        "lifecycle": "active",
        "rules_file": f"projects/{project}/Kurallar.md",
        "note_roots": [f"notes/{project}"],
        "memory_policy": "selective",
    }


def _view(workspace_id, entry, folders, registered):
    view = dict(entry)
    view["id"] = workspace_id
    view.setdefault("name", workspace_id)
    view.setdefault("kind", "general")
    view.setdefault("lifecycle", "active")
    view.setdefault("note_roots", [])
    view["sources"] = [_scope_path(folder) for folder, project in folders.items() if project == workspace_id]
    view["registered"] = registered
    if isinstance(view.get("provision"), dict):
        view["provision"] = {key: value for key, value in view["provision"].items() if key != "token"}
    return view


def read_workspaces(vault_root):
    """Registered workspaces plus every project the scope file names without a registry entry."""
    registry, _ = _read_registry(vault_root)
    folders = _folders(_read_scopes(vault_root))
    result = {workspace_id: _view(workspace_id, entry, folders, True)
              for workspace_id, entry in registry["workspaces"].items()}
    for project in folders.values():
        if project not in result:
            result[project] = _view(project, _legacy_entry(project), folders, False)
    return result


def _workspace_view(vault_root, workspace_id):
    view = read_workspaces(vault_root).get(workspace_id)
    # A legacy project whose last folder was just disconnected has no record left.
    return view if view is not None else _view(workspace_id, _legacy_entry(workspace_id), {}, False)


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
    workspace_id = _required_id(workspace_id)
    workspaces = read_workspaces(vault_root)
    if workspace_id not in workspaces:
        raise ValueError(f"workspace not found: {workspace_id}")
    ws = dict(workspaces[workspace_id])
    ws["sources_detail"] = [{"folder": src, "exists": _scope_folder_exists(vault_root, src)}
                            for src in ws.get("sources", [])]
    rules_exist = False
    if isinstance(ws.get("rules_file"), str):
        try:
            rules_exist = (Path(vault_root) / _scope_path(ws["rules_file"])).is_file()
        except ValueError:
            rules_exist = False
    ws["rules_exist"] = rules_exist
    return ws


def _validated_sources(vault_root, folders, workspace_id, sources):
    if isinstance(sources, str):
        sources = [sources]
    validated = []
    for source in sources or []:
        folder = _scope_path(source)
        if not _scope_folder_exists(vault_root, folder):
            raise ValueError(f"source folder not found in vault: {folder}")
        _, owner = _owner(folders, folder)
        if owner is not None and owner != workspace_id:
            raise ValueError(f"source folder {folder} is already assigned to workspace {owner}")
        if not any(_source_key(item) == _source_key(folder) for item in validated):
            validated.append(folder)
    return validated


@_serialized_write
def create_workspace(vault_root, workspace_id, name=None, kind="general", sources=None, rules_file=None, note_roots=None):
    """Register a new workspace and bind its initial source folders."""
    workspace_id = _validate_id(workspace_id)
    _validate_kind(kind)
    registry = _writable_registry(vault_root)
    folders = _folders(_read_scopes(vault_root))
    if workspace_id in registry["workspaces"] or workspace_id in folders.values():
        raise ValueError(f"workspace already exists: {workspace_id}")
    validated = _validated_sources(vault_root, folders, workspace_id, sources)

    now = _now()
    entry = {
        "id": workspace_id,
        "name": (name.strip() if isinstance(name, str) and name.strip() else workspace_id),
        "kind": kind,
        "lifecycle": "active",
        "rules_file": (rules_file if isinstance(rules_file, str) and rules_file.strip() else f"projects/{workspace_id}/Kurallar.md"),
        "note_roots": (list(note_roots) if isinstance(note_roots, list) else [f"notes/{workspace_id}"]),
        "memory_policy": "selective",
        "created_at": now,
        "updated_at": now,
    }
    registry["workspaces"][workspace_id] = entry
    added = _bind(vault_root, workspace_id, validated) if validated else []
    try:
        _save_registry(vault_root, registry)
    except Exception:
        if added:
            _unbind(vault_root, workspace_id, added)
        raise
    return _workspace_view(vault_root, workspace_id)


@_serialized_write
def connect_source(vault_root, workspace_id, folder):
    """Connect an existing vault-relative folder to a workspace."""
    workspace_id = _required_id(workspace_id)
    if not folder:
        raise ValueError("source folder required")
    norm_src = _scope_path(folder)
    if not _scope_folder_exists(vault_root, norm_src):
        raise ValueError(f"source folder not found in vault: {norm_src}")
    if workspace_id not in read_workspaces(vault_root):
        raise ValueError(f"workspace not found: {workspace_id}")
    _bind(vault_root, workspace_id, [norm_src])
    return _workspace_view(vault_root, workspace_id)


@_serialized_write
def disconnect_source(vault_root, workspace_id, folder):
    """Disconnect a source folder from a workspace. Never deletes the folder from disk."""
    workspace_id = _required_id(workspace_id)
    if not folder:
        raise ValueError("source folder required")
    norm_src = _scope_path(folder)
    if workspace_id not in read_workspaces(vault_root):
        raise ValueError(f"workspace not found: {workspace_id}")
    # Only a binding that still names this workspace is removed; a folder another
    # project-scope add reassigned keeps its new owner.
    if not _unbind(vault_root, workspace_id, [norm_src]):
        raise ValueError(f"source folder {norm_src} is not connected to workspace {workspace_id}")
    return _workspace_view(vault_root, workspace_id)


def _set_lifecycle(vault_root, workspace_id, lifecycle):
    workspace_id = _required_id(workspace_id)
    registry = _writable_registry(vault_root)
    folders = _folders(_read_scopes(vault_root))
    entry = registry["workspaces"].get(workspace_id)
    now = _now()
    if entry is None:
        if workspace_id not in folders.values():
            raise ValueError(f"workspace not found: {workspace_id}")
        # A legacy project gets a registry record the first time its metadata changes.
        entry = _legacy_entry(workspace_id)
        entry["created_at"] = now
    entry["lifecycle"] = lifecycle
    entry["updated_at"] = now
    registry["workspaces"][workspace_id] = entry
    _save_registry(vault_root, registry)
    return _view(workspace_id, entry, folders, True)


@_serialized_write
def archive_workspace(vault_root, workspace_id):
    """Mark a workspace as archived; its sources, scope bindings and files stay intact."""
    return _set_lifecycle(vault_root, workspace_id, "archived")


@_serialized_write
def unarchive_workspace(vault_root, workspace_id):
    """Restore an archived workspace to active lifecycle."""
    return _set_lifecycle(vault_root, workspace_id, "active")


def plan_workspace(vault_root, workspace_id, kind="general", name=None, description=None, sources=None, rules=None):
    """Generate a verifiable provisioning plan for a workspace without disk mutation (RFC #368 P0-D)."""
    return _plan(vault_root, workspace_id, kind, name, description, sources, rules)


def _plan(vault_root, workspace_id, kind="general", name=None, description=None, sources=None, rules=None, resume=False):
    workspace_id = _validate_id(workspace_id)
    _validate_kind(kind)

    if not resume and workspace_id in read_workspaces(vault_root):
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

    # The marker pair marks the only block `beyin.py moc` may later regenerate.
    from beyin_v3_lint import MOC_END_MARKER, MOC_START_MARKER
    moc_file_path = f"{default_project_dir}/MOC.md"
    moc_content = f"""# {display_name} — Map of Content (MOC)

{desc}

## Belgeler ve Notlar
- [[projects/{workspace_id}/Kurallar|Çalışma Alanı Kuralları]]

{MOC_START_MARKER}
{MOC_END_MARKER}
"""

    files_to_create = [
        {"path": rules_file_path, "content": rules_content, "purpose": "rules"},
        {"path": moc_file_path, "content": moc_content, "purpose": "navigation"},
    ]

    # The folder holding the rules and MOC is always bound too: the rules carry the user's
    # own text and must stay inside this workspace's scope even with --source elsewhere.
    bindings = []
    for folder in source_list + [default_project_dir, default_note_dir]:
        if not any(_source_key(item) == _source_key(folder) for item in bindings):
            bindings.append(folder)

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
        "bindings": bindings,
    }


def _outside_scope(root, workspace_id, files):
    """Files retrieval would not keep inside this workspace (beyin_v3.record_scope)."""
    from beyin_v3 import read_project_scopes as runtime_scopes, record_scope
    from beyin_v3_sync import parse
    scopes = runtime_scopes(root) or {"folders": [], "shared_unscoped": False}
    outside = []
    for relative in files:
        try:
            metadata, _ = parse((root / relative).read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, UnicodeError):
            metadata = {"project": None}
        if record_scope({"project": metadata.get("project"), "source": relative}, scopes) != workspace_id:
            outside.append(relative)
    return outside


def _make_dirs(path, created):
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        try:
            directory.mkdir()
        except FileExistsError:
            continue
        created.append(directory)


def _finish_provision(vault_root, workspace_id, token):
    registry = _writable_registry(vault_root)
    entry = registry["workspaces"].get(workspace_id)
    if not isinstance(entry, dict) or (entry.get("provision") or {}).get("token") != token:
        raise RuntimeError("workspace registry entry changed during provisioning")
    entry["provision"] = {"status": "complete", "completed_at": _now()}
    _save_registry(vault_root, registry)


def _compensate(root, workspace_id, token, resumed, created_dirs, created_files, bound, error):
    """Undo this attempt's own changes only; report anything left behind explicitly."""
    report = {
        "workspace_id": workspace_id,
        "rollback": "complete",
        "removed_files": [],
        "preserved_files": [],
        "removed_directories": [],
        "kept_directories": [],
        "unbound_folders": [],
        "kept_bindings": [],
        "registry": "removed",
        "errors": [],
    }

    def relative(path):
        return path.relative_to(root).as_posix()

    for item in reversed(created_files):
        path = item["path"]
        try:
            if path.is_symlink() or not path.is_file():
                continue
            current = path.read_bytes()
            # Only the bytes this attempt wrote (or a prefix left by a failed write) are ours.
            if current == item["content"] or (not item["complete"] and item["content"].startswith(current)):
                path.unlink()
                report["removed_files"].append(relative(path))
            else:
                report["preserved_files"].append(relative(path))
        except OSError as exc:
            report["errors"].append(f"{relative(path)}: {type(exc).__name__}")
    for path in reversed(created_dirs):
        if not path.is_dir() or path.is_symlink():
            continue
        try:
            path.rmdir()
            report["removed_directories"].append(relative(path))
        except OSError:
            report["kept_directories"].append(relative(path))
    try:
        report["unbound_folders"] = _unbind(root, workspace_id, bound)
    except Exception as exc:
        report["errors"].append(f"{PROJECT_SCOPES_FILE}: {type(exc).__name__}")
    try:
        folders = _folders(_read_scopes(root))
        report["kept_bindings"] = [folder for folder in bound if _owner(folders, folder)[1] == workspace_id]
    except Exception as exc:
        report["kept_bindings"] = list(bound)
        report["errors"].append(f"{PROJECT_SCOPES_FILE}: {type(exc).__name__}")

    try:
        registry = _writable_registry(root)
        entry = registry["workspaces"].get(workspace_id)
        if entry is None:
            report["registry"] = "absent"
        elif (entry.get("provision") or {}).get("token") != token:
            # Someone else changed this record meanwhile; never overwrite their edit.
            report["registry"] = "kept"
            report["errors"].append(f"{WORKSPACES_FILE}: record changed during provisioning")
        elif resumed or report["errors"] or report["kept_bindings"]:
            # Leftovers stay attached to a visible, resumable record instead of an anonymous binding.
            entry["provision"] = {"status": "failed", "failed_at": _now(), "error": str(error)[:300]}
            _save_registry(root, registry)
            report["registry"] = "kept_failed"
        else:
            del registry["workspaces"][workspace_id]
            _save_registry(root, registry)
    except Exception as exc:
        report["registry"] = "kept"
        report["errors"].append(f"{WORKSPACES_FILE}: {type(exc).__name__}")

    if report["errors"] or report["kept_bindings"] or report["registry"] == "kept":
        report["rollback"] = "incomplete"
    if report["registry"] in ("kept", "kept_failed"):
        report["recovery"] = [f"beyin.py workspace inspect {workspace_id}",
                              f"beyin.py workspace provision {workspace_id}"]
    return report


@_serialized_write
def provision_workspace(vault_root, workspace_id, kind="general", name=None, description=None, sources=None, rules=None, dry_run=False):
    """Provision a workspace with compensating rollback, template scaffolding and verification (RFC #368 P0-D).

    A registry record marked ``provision.status: pending`` is written before the first
    file, so an interrupted run stays visible. On failure only this attempt's own changes
    are undone (unchanged files it created, empty directories it created, bindings that
    still name the workspace, its own pending record); concurrent changes by others are
    kept. Anything left behind is reported in ``workspace_reconciliation`` and the record
    stays ``failed``; running the same provision command again resumes it.
    """
    vault_root = Path(vault_root)
    workspace_id = _validate_id(workspace_id)
    previous = _read_registry(vault_root)[0]["workspaces"].get(workspace_id)
    resume = (isinstance(previous, dict)
              and (previous.get("provision") or {}).get("status") in RESUMABLE_PROVISION_STATES)
    plan = _plan(vault_root, workspace_id, kind=kind, name=name, description=description,
                 sources=sources, rules=rules, resume=resume)
    if dry_run:
        result = {"status": "dry_run", "action": "provision", "plan": plan}
        if resume:
            result["resume"] = True
        return result

    root = vault_root.resolve()

    def checked_path(relative):
        path = root / relative
        if not path.resolve().is_relative_to(root):
            raise ValueError("workspace output must remain inside the vault")
        return path

    # Validate the complete plan before the first directory, file or record is written.
    for relative in plan['directories'] + [item['path'] for item in plan['files']]:
        checked_path(relative)
    bind_sources = plan["bindings"]
    folders = _folders(_read_scopes(root))
    for folder in bind_sources:
        _, owner = _owner(folders, folder)
        if owner is not None and owner != workspace_id:
            raise ValueError(f"source folder {folder} is already assigned to workspace {owner}")

    registry = _writable_registry(root)
    token = uuid.uuid4().hex
    now = _now()
    entry = dict(previous) if resume else {"id": workspace_id, "lifecycle": "active", "created_at": now}
    entry.update({
        "id": workspace_id,
        "name": plan["name"],
        "kind": plan["kind"],
        "rules_file": plan["rules_file"],
        "note_roots": plan["note_roots"],
        "memory_policy": plan["memory_policy"],
        "updated_at": now,
        "provision": {"status": "pending", "token": token, "started_at": now},
    })
    registry["workspaces"][workspace_id] = entry
    _save_registry(root, registry)

    created_dirs = []
    created_files = []
    bound = []
    try:
        for d in plan["directories"]:
            _make_dirs(checked_path(d), created_dirs)

        # Template files: exclusive create, an existing or concurrently created user file is kept.
        written_files = []
        for fspec in plan["files"]:
            file_path = checked_path(fspec["path"])
            _make_dirs(file_path.parent, created_dirs)
            content = fspec["content"].encode("utf-8")
            try:
                with file_path.open("xb") as output:
                    item = {"path": file_path, "content": content, "complete": False}
                    created_files.append(item)
                    output.write(content)
                item["complete"] = True
            except FileExistsError:
                written_files.append({"path": fspec["path"], "status": "preserved"})
            else:
                written_files.append({"path": fspec["path"], "status": "created"})

        bound = _bind(root, workspace_id, bind_sources)

        verification = inspect_workspace(root, workspace_id)
        if not verification.get("rules_exist"):
            raise RuntimeError(f"verification failed: rules file missing at {plan['rules_file']}")
        if not all(s.get("exists") for s in verification.get("sources_detail", [])):
            raise RuntimeError("verification failed: one or more source folders not found after creation")
        bound_keys = {_source_key(source) for source in verification.get("sources", [])}
        unbound = [folder for folder in bind_sources if _source_key(folder) not in bound_keys]
        if unbound:
            raise RuntimeError("verification failed: source folders not bound: " + ", ".join(unbound))
        outside = _outside_scope(root, workspace_id, [item["path"] for item in plan["files"]])
        if outside:
            raise RuntimeError("verification failed: workspace files outside the workspace scope: "
                               + ", ".join(outside))

        _finish_provision(root, workspace_id, token)
        result = {
            "status": "provisioned",
            "action": "provision",
            "workspace": _workspace_view(root, workspace_id),
            "created_directories": [p.relative_to(root).as_posix() for p in created_dirs],
            "files": written_files,
            "verified": True,
        }
        if resume:
            result["resumed"] = True
        return result

    except Exception as exc:
        report = _compensate(root, workspace_id, token, resume, created_dirs, created_files, bound, exc)
        if report["rollback"] == "complete" and report["registry"] in ("removed", "absent"):
            message = f"workspace provision failed and was rolled back: {exc}"
        else:
            message = (f"workspace provision failed: {exc}; rollback {report['rollback']}, workspace record "
                       f"{report['registry']}. Inspect with: beyin.py workspace inspect {workspace_id}; "
                       f"resume with: beyin.py workspace provision {workspace_id}")
        raise WorkspaceProvisionError(message, report) from exc


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
