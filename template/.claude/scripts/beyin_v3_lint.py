"""Obsidian-Native Knowledge Organization & Lint (RFC #368 P0-E).

ReDoS-safe wikilink parser, vault link linting (broken links, orphans),
backlink graph analysis, and human-friendly MOC (Maps of Content) generation.
Pure stdlib, zero external dependencies.
"""
from collections import defaultdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import unicodedata

# ReDoS-safe wikilink pattern: bounded character class prevents catastrophic backtracking.
# Matches [[target]], [[target|display]], [[target#heading]], [[target#heading|display]], and embeds ![[...]]
WIKILINK_PATTERN = re.compile(r"!?\[\[([^\[\]\r\n]{1,500})\]\]")

EXCLUDED_DIR_NAMES = {
    ".git", ".claude", ".agents", ".gemini", ".codex", ".kiro", ".omp", ".opencode",
    ".obsidian", ".trash", "node_modules", "__pycache__", "receipts",
}

EXCLUDED_FILE_NAMES = {
    "agents.md", "claude.md", "gemini.md", "skill.md", "hooks.md", "config.md",
    "settings.md", "instructions.md", "codex.md", "setup.md", "install.md",
}

MOC_START_MARKER = "<!-- beyin-moc:start -->"
MOC_END_MARKER = "<!-- beyin-moc:end -->"


def _norm_key(text):
    """Normalize string for cross-platform case-insensitive comparison."""
    if not isinstance(text, str):
        return ""
    return unicodedata.normalize("NFC", text.replace("\\", "/")).strip().casefold()


def extract_wikilinks(text):
    """Extract all wikilinks from markdown text safely.

    Returns list of dicts: target, heading, display, is_embed, raw, line.
    """
    if not isinstance(text, str) or not text:
        return []

    results = []
    lines = text.splitlines()
    for line_idx, line in enumerate(lines, 1):
        for match in WIKILINK_PATTERN.finditer(line):
            raw_inside = match.group(1).strip()
            is_embed = match.group(0).startswith("!")

            # Split display text: [[target|display]]
            if "|" in raw_inside:
                target_part, display_part = raw_inside.split("|", 1)
                target_part = target_part.strip()
                display_part = display_part.strip()
            else:
                target_part = raw_inside
                display_part = None

            # Split heading: [[target#heading]]
            if "#" in target_part:
                file_target, heading_part = target_part.split("#", 1)
                file_target = file_target.strip()
                heading_part = heading_part.strip()
            else:
                file_target = target_part
                heading_part = None

            results.append({
                "raw": match.group(0),
                "file_target": file_target,
                "heading": heading_part,
                "display": display_part,
                "is_embed": is_embed,
                "line": line_idx,
            })
    return results


def build_vault_index(vault_root, target_folder=None):
    """Build fast lookup table of markdown files in the vault."""
    vault_root = Path(vault_root)
    start_dir = (vault_root / target_folder) if target_folder else vault_root
    if not start_dir.exists():
        return {"by_rel": {}, "by_basename": {}}

    files_by_rel = {}
    files_by_basename = defaultdict(list)

    for root, dirs, files in os.walk(start_dir):
        # Prune excluded dirs
        dirs[:] = [
            d for d in dirs
            if not d.startswith(".")
            and d.casefold() not in EXCLUDED_DIR_NAMES
            and not (Path(root) / d).is_symlink()
        ]
        for f in files:
            if not f.lower().endswith(".md") or f.startswith("."):
                continue
            if f.casefold() in EXCLUDED_FILE_NAMES:
                continue
            full_path = Path(root) / f
            try:
                rel_path = full_path.relative_to(vault_root).as_posix()
            except ValueError:
                continue

            rel_key = _norm_key(rel_path)
            files_by_rel[rel_key] = rel_path

            # Store by basename stem and with .md
            stem_key = _norm_key(full_path.stem)
            files_by_basename[stem_key].append(rel_path)

    return {
        "by_rel": files_by_rel,
        "by_basename": dict(files_by_basename),
    }


def resolve_wikilink(vault_root, link_target, source_file=None, vault_index=None):
    """Resolve a wikilink target to a vault-relative path.

    Supports exact path, parent-relative path, and Obsidian-style basename lookup.
    """
    if vault_index is None:
        vault_index = build_vault_index(vault_root)

    by_rel = vault_index.get("by_rel", {})
    by_basename = vault_index.get("by_basename", {})

    # 1. Internal link within same file: [[#Heading]]
    if not link_target:
        if source_file:
            return {"exists": True, "resolved_path": source_file, "is_internal": True}
        return {"exists": False, "resolved_path": None, "is_internal": True}

    norm_target = link_target.replace("\\", "/").strip().strip("/")
    target_has_ext = norm_target.lower().endswith(".md")
    target_with_ext = norm_target if target_has_ext else f"{norm_target}.md"
    target_stem = Path(norm_target).stem

    # 2. Check exact relative path from vault root
    k_ext = _norm_key(target_with_ext)
    if k_ext in by_rel:
        return {"exists": True, "resolved_path": by_rel[k_ext]}

    # 3. Check relative to source_file parent folder if source_file provided
    if source_file:
        source_parent = Path(source_file).parent.as_posix()
        if source_parent and source_parent != ".":
            candidate_rel = f"{source_parent}/{target_with_ext}"
            k_cand = _norm_key(candidate_rel)
            if k_cand in by_rel:
                return {"exists": True, "resolved_path": by_rel[k_cand]}

    # 4. Check basename lookup (Obsidian standard shortest path)
    k_stem = _norm_key(target_stem)
    if k_stem in by_basename:
        candidates = by_basename[k_stem]
        if candidates:
            return {"exists": True, "resolved_path": candidates[0]}

    return {"exists": False, "resolved_path": None}


def lint_vault(vault_root, folder=None):
    """Lint markdown notes in vault for broken wikilinks, orphans, and link graph health."""
    vault_root = Path(vault_root)
    vault_index = build_vault_index(vault_root)
    all_files = list(vault_index["by_rel"].values())

    # If folder is specified, restrict scan target
    if folder:
        norm_folder = _norm_key(folder.strip().strip("/"))
        target_files = [f for f in all_files if _norm_key(f).startswith(norm_folder + "/") or _norm_key(f) == norm_folder]
    else:
        target_files = all_files

    broken_links = []
    outgoing_links = defaultdict(set)
    incoming_links = defaultdict(set)
    all_extracted_count = 0

    for rel_path in target_files:
        full_path = vault_root / rel_path
        try:
            content = full_path.read_text(encoding="utf-8-sig")
        except Exception:
            continue

        links = extract_wikilinks(content)
        all_extracted_count += len(links)

        for link in links:
            # Skip non-markdown media embeds unless checking file existence
            if link["is_embed"] and link["file_target"] and not link["file_target"].lower().endswith(".md"):
                if "." in link["file_target"]:
                    embed_path = vault_root / link["file_target"]
                    if not embed_path.exists() and not (full_path.parent / link["file_target"]).exists():
                        broken_links.append({
                            "source": rel_path,
                            "line": link["line"],
                            "target": link["file_target"],
                            "raw": link["raw"],
                            "type": "embed",
                        })
                continue

            res = resolve_wikilink(vault_root, link["file_target"], source_file=rel_path, vault_index=vault_index)
            if res["exists"]:
                resolved = res["resolved_path"]
                outgoing_links[rel_path].add(resolved)
                incoming_links[resolved].add(rel_path)
            else:
                broken_links.append({
                    "source": rel_path,
                    "line": link["line"],
                    "target": link["file_target"],
                    "raw": link["raw"],
                    "type": "wikilink",
                })

    # Identify orphans (no incoming and no outgoing)
    orphans = []
    for f in target_files:
        if len(outgoing_links[f]) == 0 and len(incoming_links[f]) == 0:
            orphans.append(f)

    # Identify unlinked notes (has outgoing but no incoming)
    unlinked = []
    for f in target_files:
        if len(incoming_links[f]) == 0 and f not in orphans:
            unlinked.append(f)

    return {
        "status": "ok" if not broken_links else "issues_found",
        "scanned_files_count": len(target_files),
        "total_links_count": all_extracted_count,
        "broken_links_count": len(broken_links),
        "orphan_count": len(orphans),
        "unlinked_count": len(unlinked),
        "broken_links": broken_links,
        "orphans": sorted(orphans),
        "unlinked": sorted(unlinked),
    }


def extract_note_title(full_path):
    """Extract human-readable title from markdown note (title: in frontmatter or # Heading or stem)."""
    try:
        content = Path(full_path).read_text(encoding="utf-8-sig")
    except Exception:
        return Path(full_path).stem

    lines = content.splitlines()
    in_fm = False
    fm_title = None
    first_heading = None

    for idx, line in enumerate(lines[:30]):
        stripped = line.strip()
        if idx == 0 and stripped == "---":
            in_fm = True
            continue
        if in_fm:
            if stripped == "---":
                in_fm = False
            elif stripped.startswith("title:"):
                val = stripped.split("title:", 1)[1].strip().strip('"').strip("'")
                if val:
                    fm_title = val
            continue
        if stripped.startswith("# ") and not stripped.startswith("## "):
            first_heading = stripped[2:].strip()
            break

    if fm_title:
        return fm_title
    if first_heading:
        return first_heading
    return Path(full_path).stem


def generate_moc(vault_root, target_folder="knowledge", moc_filename="MOC.md"):
    """Generate or update an Obsidian Map of Content (MOC) note.

    Preserves user handwritten content outside markers. Pure stdlib.
    """
    vault_root = Path(vault_root)
    folder_path = vault_root / target_folder
    if not folder_path.exists():
        raise ValueError(f"target folder not found in vault: {target_folder}")

    moc_path = folder_path / moc_filename
    moc_rel = moc_path.relative_to(vault_root).as_posix()

    # Collect notes in folder (excluding moc itself)
    notes_by_category = defaultdict(list)
    for root, dirs, files in os.walk(folder_path):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d.casefold() not in EXCLUDED_DIR_NAMES]
        for f in files:
            if not f.lower().endswith(".md") or f.startswith("."):
                continue
            f_path = Path(root) / f
            if f_path.resolve() == moc_path.resolve():
                continue

            rel_to_vault = f_path.relative_to(vault_root).as_posix()
            rel_to_folder = f_path.relative_to(folder_path).as_posix()
            title = extract_note_title(f_path)

            category = Path(rel_to_folder).parent.as_posix()
            if category == ".":
                category = "Genel"
            else:
                category = category.replace("-", " ").title()

            notes_by_category[category].append({
                "rel_path": rel_to_vault,
                "title": title,
                "stem": f_path.stem,
            })

    # Sort categories and notes
    generated_lines = []
    generated_lines.append(f"{MOC_START_MARKER}")
    total_notes = 0

    if not notes_by_category:
        generated_lines.append("_Bu klasörde henüz indekslenecek not bulunmuyor._")
    else:
        for cat in sorted(notes_by_category):
            cat_notes = sorted(notes_by_category[cat], key=lambda x: x["title"].casefold())
            generated_lines.append(f"### {cat}")
            for n in cat_notes:
                generated_lines.append(f"- [[{n['rel_path']}|{n['title']}]]")
                total_notes += 1
            generated_lines.append("")

    generated_lines.append(f"{MOC_END_MARKER}")
    generated_block = "\n".join(generated_lines).strip()

    # Read existing content if exists
    if moc_path.exists():
        existing_text = moc_path.read_text(encoding="utf-8-sig")
        if MOC_START_MARKER in existing_text and MOC_END_MARKER in existing_text:
            # Replace only the generated block
            pattern = re.compile(
                re.escape(MOC_START_MARKER) + r".*?" + re.escape(MOC_END_MARKER),
                re.DOTALL
            )
            new_text = pattern.sub(generated_block, existing_text)
            action_status = "updated"
        else:
            # Append generated block to existing content, keeping user text intact
            new_text = existing_text.rstrip() + "\n\n## Otomatik Dizin\n\n" + generated_block + "\n"
            action_status = "appended"
    else:
        # Create fresh MOC file
        folder_display = target_folder.replace("-", " ").title()
        new_text = f"""# {folder_display} — Map of Content (MOC)

Bu not, `{target_folder}` altındaki notların navigasyon ve içerik haritasıdır.

## Belgeler ve Notlar

{generated_block}
"""
        action_status = "created"

    # Write file safely
    moc_path.parent.mkdir(parents=True, exist_ok=True)
    moc_path.write_text(new_text, encoding="utf-8")

    return {
        "status": action_status,
        "action": "moc",
        "file": moc_rel,
        "indexed_notes_count": total_notes,
        "categories_count": len(notes_by_category),
    }


def manage(vault_root, action, folder=None, moc_file="MOC.md"):
    """CLI dispatcher for beyin.py lint and moc commands."""
    vault_root = Path(vault_root)
    if action == "lint":
        return lint_vault(vault_root, folder=folder)
    elif action == "moc":
        target = folder or "knowledge"
        return generate_moc(vault_root, target_folder=target, moc_filename=moc_file)
    else:
        raise ValueError(f"unknown lint/moc action: {action}")
