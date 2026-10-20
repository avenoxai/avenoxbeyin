"""Folder-based project scope management (.beyin-projects.json) for explicit-project gates. No models."""
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unicodedata

PROJECT_SCOPES_FILE = ".beyin-projects.json"


def _scope_path(value):
    if not isinstance(value, str):
        raise ValueError("project scope folder must be a string")
    folder = unicodedata.normalize("NFC", value.replace("\\", "/")).strip().strip("/")
    parts = folder.split("/")
    if not folder or value.strip().startswith(("/", "\\")) or re.match(r"[A-Za-z]:", folder) or ".." in parts or "." in parts:
        raise ValueError("project scope folder must be vault-relative")
    return folder


def _scope_key(value):
    return unicodedata.normalize("NFC", value.replace("\\", "/")).casefold()


def _scope_folder_exists(vault_root, folder):
    """Every configured folder must name a real vault folder under the matching rule.

    A mistyped or renamed folder would otherwise match nothing, and with shared_unscoped
    its notes would silently join every other project's scope.
    """
    current = Path(vault_root)
    for part in folder.split("/"):
        key = _scope_key(part)
        try:
            with os.scandir(current) as entries:
                match = next((entry.name for entry in entries if entry.is_dir() and _scope_key(entry.name) == key), None)
        except OSError:
            return False
        if match is None:
            return False
        current = current / match
    return True


def read_project_scopes(vault_root):
    """None when the vault has no scope file: every gate keeps its exact-field behavior.

    folders maps a vault-relative folder to a project; a record's own non-empty project
    field wins. shared_unscoped lets a project query also see records no folder or field
    assigns to any project. Another project's records never enter a project's scope.
    Missing folders are skipped with a warning so unrelated projects remain fully functional.
    """
    path = Path(vault_root) / PROJECT_SCOPES_FILE
    if path.is_symlink():
        raise ValueError("project scopes must be a regular vault-local file")
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (ValueError, OSError) as exc:
        raise ValueError("invalid project scopes file; expected JSON") from exc
    if not isinstance(data, dict) or set(data) - {"folders", "shared_unscoped"}:
        raise ValueError("project scopes accept only folders and shared_unscoped")
    folders = data.get("folders", {})
    shared = data.get("shared_unscoped", False)
    if not isinstance(folders, dict) or not isinstance(shared, bool):
        raise ValueError("folders must be an object and shared_unscoped a boolean")
    mapped = {}
    missing = []
    for folder, project in folders.items():
        if not isinstance(project, str) or not project.strip():
            raise ValueError("project scope names must be non-empty strings")
        try:
            folder = _scope_path(folder)
        except ValueError as exc:
            # Still fail closed, but name the entry so the user can find it (#419).
            raise ValueError(f"{exc}: {folder!r} in {PROJECT_SCOPES_FILE}") from None
        if not _scope_folder_exists(vault_root, folder):
            missing.append(folder)
            try:
                sys.stderr.write(f"Warning: project scope folder not found in the vault: {folder}\n")
                sys.stderr.flush()
            except Exception:
                pass
            # Retain the mapping so stale --no-sync rows never become shared notes.
        key = _scope_key(folder)
        if mapped.get(key, project) != project:
            raise ValueError("project scope folder is assigned twice: " + folder)
        mapped[key] = project
    # Longest folder first, so a nested folder can belong to a different project.
    return {
        "folders": sorted(mapped.items(), key=lambda item: -len(item[0])),
        "shared_unscoped": shared,
        "missing_folders": missing,
    }


def check_project_scopes(vault_root):
    """Inspect .beyin-projects.json for doctor. Reads only local file and vault directory names."""
    path = Path(vault_root) / PROJECT_SCOPES_FILE
    if not path.exists() and not path.is_symlink():
        return {"status": "absent", "folders": {}, "shared_unscoped": False,
                "missing_folders": [], "duplicate_folders": [], "invalid_entries": [], "warnings": []}
    if path.is_symlink():
        return {"status": "error", "error": "project scopes must be a regular vault-local file",
                "folders": {}, "shared_unscoped": False, "missing_folders": [],
                "duplicate_folders": [], "invalid_entries": [], "warnings": ["symlink_not_allowed"]}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        return {"status": "error", "error": f"invalid JSON: {type(exc).__name__}",
                "folders": {}, "shared_unscoped": False, "missing_folders": [],
                "duplicate_folders": [], "invalid_entries": [], "warnings": ["invalid_json"]}
    if not isinstance(data, dict):
        return {"status": "error", "error": "expected JSON object",
                "folders": {}, "shared_unscoped": False, "missing_folders": [],
                "duplicate_folders": [], "invalid_entries": [], "warnings": ["not_an_object"]}
    unknown_keys = sorted(set(data) - {"folders", "shared_unscoped"})
    if unknown_keys:
        return {"status": "error", "error": "unsupported keys: " + ", ".join(unknown_keys),
                "folders": {}, "shared_unscoped": False, "missing_folders": [],
                "duplicate_folders": [], "invalid_entries": [], "warnings": ["unsupported_keys"]}
    folders = data.get("folders", {})
    shared = data.get("shared_unscoped", False)
    if not isinstance(folders, dict) or not isinstance(shared, bool):
        return {"status": "error", "error": "folders must be an object and shared_unscoped a boolean",
                "folders": {}, "shared_unscoped": False, "missing_folders": [],
                "duplicate_folders": [], "invalid_entries": [], "warnings": ["invalid_types"]}

    missing_folders = []
    duplicate_folders = []
    invalid_entries = []
    seen = {}
    valid_folders = {}

    for raw_folder, project in folders.items():
        if not isinstance(project, str) or not project.strip():
            invalid_entries.append(f"{raw_folder}: invalid project name")
            continue
        try:
            folder = _scope_path(raw_folder)
        except ValueError as exc:
            invalid_entries.append(f"{raw_folder}: {exc}")
            continue
        key = _scope_key(folder)
        if key in seen and seen[key] != project:
            duplicate_folders.append(f"{folder} (assigned to {seen[key]} and {project})")
        seen[key] = project
        if not _scope_folder_exists(vault_root, folder):
            missing_folders.append(folder)
        else:
            valid_folders[folder] = project

    warnings = []
    if missing_folders:
        warnings.append(f"missing_folders: {len(missing_folders)}")
    if duplicate_folders:
        warnings.append(f"duplicate_folders: {len(duplicate_folders)}")
    if invalid_entries:
        warnings.append(f"invalid_entries: {len(invalid_entries)}")

    status = "error" if (duplicate_folders or invalid_entries) else ("warning" if missing_folders else "ok")
    return {
        "status": status,
        "folders": valid_folders,
        "shared_unscoped": shared,
        "missing_folders": missing_folders,
        "duplicate_folders": duplicate_folders,
        "invalid_entries": invalid_entries,
        "warnings": warnings,
    }


def _save_project_scopes(vault_root, data):
    path = Path(vault_root) / PROJECT_SCOPES_FILE
    content = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    fd, temp_path = tempfile.mkstemp(prefix=".beyin-projects-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def manage(vault_root, action, folder=None, project=None, shared_unscoped=None):
    """CLI handler for beyin.py project-scope list|add|remove|prune."""
    vault_root = Path(vault_root)
    path = vault_root / PROJECT_SCOPES_FILE

    if action == "list":
        report = check_project_scopes(vault_root)
        report["action"] = "list"
        return report

    if path.is_symlink():
        raise ValueError('project scopes must be a regular vault-local file')
    if path.exists():
        # Never replace malformed user configuration with an empty object.
        data = json.loads(path.read_text(encoding='utf-8-sig'))
        if (not isinstance(data, dict) or set(data) - {'folders', 'shared_unscoped'} or
                not isinstance(data.get('folders', {}), dict) or
                not isinstance(data.get('shared_unscoped', False), bool)):
            raise ValueError('invalid project scopes configuration; preserve and repair it first')
        for configured_folder, configured_project in data.get('folders', {}).items():
            if action != "prune":
                _scope_path(configured_folder)
            if not isinstance(configured_project, str) or not configured_project.strip():
                raise ValueError('project scope names must be non-empty strings')

    if action == "add":
        if not folder or not isinstance(project, str) or not project.strip():
            raise ValueError("project-scope add requires both --folder and --project")
        folder_norm = _scope_path(folder)
        if not _scope_folder_exists(vault_root, folder_norm):
            raise ValueError("project scope folder not found in the vault: " + folder_norm)
        data = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8-sig"))
            except Exception as exc:
                raise ValueError(f"existing {PROJECT_SCOPES_FILE} is invalid JSON: {exc}") from exc
            if not isinstance(data, dict):
                data = {}
        folders = data.get("folders", {})
        if not isinstance(folders, dict):
            folders = {}
        # An explicit add reassigns this folder's existing normalized spelling.
        for existing in list(folders):
            if _scope_key(existing) == _scope_key(folder_norm):
                del folders[existing]
        folders[folder_norm] = project.strip()
        data["folders"] = folders
        if shared_unscoped is not None:
            data["shared_unscoped"] = (shared_unscoped == "on")
        elif "shared_unscoped" not in data:
            data["shared_unscoped"] = False
        _save_project_scopes(vault_root, data)
        return {"status": "saved", "action": "add", "folder": folder_norm, "project": project.strip(),
                "folders": folders, "shared_unscoped": data["shared_unscoped"]}

    if action == "remove":
        if not folder:
            raise ValueError("project-scope remove requires --folder")
        folder_norm = _scope_path(folder)
        if not path.exists():
            raise ValueError(f"{PROJECT_SCOPES_FILE} does not exist")
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception as exc:
            raise ValueError(f"existing {PROJECT_SCOPES_FILE} is invalid JSON: {exc}") from exc
        folders = data.get("folders", {})
        if not isinstance(folders, dict):
            raise ValueError(f"{PROJECT_SCOPES_FILE} folders must be an object")

        target_key = _scope_key(folder_norm)
        matched_folder = next((f for f in folders if _scope_key(f) == target_key), None)
        if not matched_folder:
            raise ValueError(f"folder not found in project scopes: {folder_norm}")
        del folders[matched_folder]
        data["folders"] = folders
        _save_project_scopes(vault_root, data)
        return {"status": "saved", "action": "remove", "folder": matched_folder,
                "folders": folders, "shared_unscoped": data.get("shared_unscoped", False)}

    if action == "prune":
        if not path.exists():
            return {"status": "ok", "action": "prune", "pruned": [], "message": f"{PROJECT_SCOPES_FILE} absent", "folders": {}}
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception as exc:
            raise ValueError(f"existing {PROJECT_SCOPES_FILE} is invalid JSON: {exc}") from exc
        folders = data.get("folders", {})
        if not isinstance(folders, dict):
            raise ValueError(f"{PROJECT_SCOPES_FILE} folders must be an object")
        pruned = []
        for raw_folder in list(folders.keys()):
            try:
                norm = _scope_path(raw_folder)
                if not _scope_folder_exists(vault_root, norm):
                    pruned.append(raw_folder)
                    del folders[raw_folder]
            except ValueError:
                pruned.append(raw_folder)
                del folders[raw_folder]
        if pruned:
            data["folders"] = folders
            _save_project_scopes(vault_root, data)
            return {"status": "saved", "action": "prune", "pruned": pruned, "folders": folders,
                    "shared_unscoped": data.get("shared_unscoped", False)}
        return {"status": "ok", "action": "prune", "pruned": [], "message": "no missing folders to prune",
                "folders": folders, "shared_unscoped": data.get("shared_unscoped", False)}

    raise ValueError("unknown project-scope action: " + str(action))


def resolve_active_project(vault, cwd):
    """Resolve the active project name from the current working directory.
    Matches against .beyin-projects.json rules or standard folder names
    ('projects', 'Projeler', 'projeler'). Returns None if unscoped.
    """
    if vault is None or cwd is None:
        return None
    try:
        vault_root = Path(vault).resolve(strict=True)
        current = Path(cwd).resolve(strict=True)
    except (RuntimeError, OSError, TypeError, ValueError):
        # Handle symlink loops, deleted directories, or permission issues gracefully
        return None

    try:
        if not current.is_relative_to(vault_root):
            return None
        rel_path = current.relative_to(vault_root).as_posix()
    except (ValueError, TypeError):
        return None

    try:
        scopes = read_project_scopes(vault_root)
    except ValueError:
        scopes = None

    if scopes and "folders" in scopes:
        target = _scope_key(rel_path)
        for folder, project in scopes.get("folders", []):
            if target == folder or target.startswith(folder + "/"):
                return project

    parts = rel_path.split("/")
    if len(parts) >= 2 and parts[0] in ("projects", "Projeler", "projeler"):
        return parts[1]

    return None
