"""Obsidian-Native Knowledge Organization & Lint (RFC #368 P0-E).

ReDoS-safe wikilink parser, vault link linting (broken links, orphans),
backlink graph analysis, and human-friendly MOC (Maps of Content) generation.
Pure stdlib, zero external dependencies.
"""
from collections import defaultdict
import codecs
import hashlib
import os
import posixpath
from pathlib import Path
import re
import stat
import tempfile
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

# A heading line; [ \t] instead of \s so a lone "#" never joins the next line into a heading.
HEADING_PATTERN = re.compile(r"#{1,6}[ \t]+(.+)")
# Obsidian ignores links and headings inside fenced code and inline code spans.
FENCE_PATTERN = re.compile(r" {0,3}(`{3,}|~{3,})")
INLINE_CODE_PATTERN = re.compile(r"`[^`\r\n]*`")

ARCHIVE_DIR_NAMES = ("arsiv", "archive", "archives")

# A MOC lists a note only when every reader of the MOC may also read that note.
VISIBILITY_RANK = {"public": 0, "internal": 1, "private": 2}
MAX_MOC_TITLE_CHARS = 120


def _norm_key(text):
    """Normalize string for cross-platform case-insensitive comparison."""
    if not isinstance(text, str):
        return ""
    return unicodedata.normalize("NFC", text.replace("\\", "/")).strip().casefold()


def _lines_outside_code(text):
    """(line number, line) pairs outside fenced code; inline code spans are blanked."""
    fence = None
    for line_idx, line in enumerate(text.splitlines(), 1):
        opener = FENCE_PATTERN.match(line)
        if fence is not None:
            if (opener and opener.group(1)[0] == fence[0] and len(opener.group(1)) >= len(fence)
                    and not line[opener.end():].strip()):
                fence = None
            continue
        if opener:
            fence = opener.group(1)
            continue
        yield line_idx, INLINE_CODE_PATTERN.sub(lambda match: " " * len(match.group(0)), line)


def _headings(text):
    headings = set()
    for _, line in _lines_outside_code(text):
        match = HEADING_PATTERN.fullmatch(line)
        if match:
            headings.add(_norm_key(match.group(1).strip().rstrip("#").strip()))
    return headings


def extract_wikilinks(text):
    """Extract all wikilinks from markdown text safely.

    Returns list of dicts: target, heading, display, is_embed, raw, line.
    """
    if not isinstance(text, str) or not text:
        return []

    results = []
    for line_idx, line in _lines_outside_code(text):
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


def _withheld_path(relative):
    """A path inside a sensitive or archive folder never enters the lint or MOC index."""
    from beyin_v3 import sensitive_fold, sensitive_source
    # sensitive_fold keeps Turkish case rules: "ARŞİV" and NFD "Arşiv" both fold to "arsiv".
    return sensitive_source(relative) or any(sensitive_fold(part) in ARCHIVE_DIR_NAMES
                                             for part in Path(relative).parts[:-1])


def _note_metadata(vault_root, path):
    """Frontmatter of a note lint and moc may read; None when the note is withheld."""
    from beyin_v3_sync import parse
    try:
        relative = path.relative_to(vault_root).as_posix()
        if path.is_symlink() or not path.resolve().is_relative_to(vault_root.resolve()):
            return None
        if _withheld_path(relative):
            return None
        metadata, _ = parse(path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError, UnicodeError):
        return None
    if metadata.get('visibility') == 'private' or metadata.get('status') == 'archived':
        return None
    return metadata


def _eligible_note(vault_root, path):
    return _note_metadata(vault_root, path) is not None


def build_vault_index(vault_root, target_folder=None):
    """Build fast lookup table of markdown files in the vault."""
    vault_root = Path(vault_root)
    start_dir = (vault_root / target_folder) if target_folder else vault_root
    if not start_dir.exists():
        return {"by_rel": {}, "by_basename": {}, "attachments": {}}

    files_by_rel = {}
    files_by_basename = defaultdict(list)
    attachments = defaultdict(list)

    for root, dirs, files in os.walk(start_dir):
        # Prune excluded dirs
        dirs[:] = [
            d for d in dirs
            if not d.startswith(".")
            and d.casefold() not in EXCLUDED_DIR_NAMES
            and not (Path(root) / d).is_symlink()
        ]
        for f in files:
            if f.startswith("."):
                continue
            full_path = Path(root) / f
            if not f.lower().endswith(".md"):
                # Images, PDFs and other attachments: Obsidian finds them by name anywhere.
                try:
                    rel_path = full_path.relative_to(vault_root).as_posix()
                except ValueError:
                    continue
                if not full_path.is_symlink() and not _withheld_path(rel_path):
                    attachments[_norm_key(f)].append(rel_path)
                continue
            if f.casefold() in EXCLUDED_FILE_NAMES:
                continue
            try:
                rel_path = full_path.relative_to(vault_root).as_posix()
            except ValueError:
                continue

            if not _eligible_note(vault_root, full_path):
                continue
            rel_key = _norm_key(rel_path)
            files_by_rel[rel_key] = rel_path

            # Store by basename stem and with .md
            stem_key = _norm_key(full_path.stem)
            files_by_basename[stem_key].append(rel_path)

    return {
        "by_rel": files_by_rel,
        "by_basename": dict(files_by_basename),
        "attachments": dict(attachments),
    }


def _attachment_exists(vault_index, target, source_file=None):
    """Resolve a non-note target from the index only; nothing outside the vault is probed."""
    norm = target.replace("\\", "/").strip().strip("/")
    candidates = vault_index.get("attachments", {}).get(_norm_key(posixpath.basename(norm)), [])
    if not candidates or "/" not in norm:
        return bool(candidates)
    keys = {_norm_key(path) for path in candidates}
    if norm.startswith(("./", "../")):
        parent = posixpath.dirname(source_file or "")
        return _norm_key(posixpath.normpath(posixpath.join(parent, norm))) in keys
    suffix = "/" + _norm_key(norm)
    return any(("/" + key).endswith(suffix) for key in keys)


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
    # Only ".md" is a note extension: "Sürüm v1.2" is the note "Sürüm v1.2.md" and
    # "diagram.png" is never the note "diagram.md".
    target_stem = posixpath.basename(target_with_ext)[:-3]

    # 2. Check exact relative path from vault root
    k_ext = _norm_key(target_with_ext)
    if k_ext in by_rel:
        return {"exists": True, "resolved_path": by_rel[k_ext]}

    # 3. Check relative to source_file parent folder if source_file provided ("./x" from a root note too)
    if source_file:
        source_parent = Path(source_file).parent.as_posix()
        base = "" if source_parent in ("", ".") else source_parent + "/"
        candidate_rel = posixpath.normpath(base + target_with_ext)
        k_cand = _norm_key(candidate_rel)
        if k_cand in by_rel:
            return {"exists": True, "resolved_path": by_rel[k_cand]}

    if "/" in norm_target:
        # Obsidian resolves a partial path by its ending ("kavramlar/temel" ->
        # "knowledge/kavramlar/temel.md"), never by the file name alone, so a
        # missing "missing/note" stays broken. "./" and "../" stay relative only.
        if norm_target.startswith(("./", "../")):
            return {"exists": False, "resolved_path": None}
        suffix = "/" + _norm_key(target_with_ext)
        matches = sorted((path for path in by_basename.get(_norm_key(target_stem), [])
                          if ("/" + _norm_key(path)).endswith(suffix)),
                         key=lambda path: (path.count("/"), path))
        if matches:
            return {"exists": True, "resolved_path": matches[0]}
        return {"exists": False, "resolved_path": None}

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
    if folder:
        # A mistyped folder must not read as a clean "0 files scanned" result.
        folder_path = (vault_root / folder.strip().strip("/")).resolve()
        if not folder_path.is_relative_to(vault_root.resolve()) or not folder_path.is_dir():
            raise ValueError(f"lint folder not found in vault: {folder}")
        folder = folder_path.relative_to(vault_root.resolve()).as_posix()
        folder = None if folder == "." else folder
    vault_index = build_vault_index(vault_root)
    all_files = list(vault_index["by_rel"].values())

    # If folder is specified, restrict scan target
    if folder:
        norm_folder = _norm_key(folder.strip().strip("/"))
        target_files = [f for f in all_files if _norm_key(f).startswith(norm_folder + "/") or _norm_key(f) == norm_folder]
    else:
        target_files = all_files
    target_set = set(target_files)

    broken_links = []
    outgoing_links = defaultdict(set)
    incoming_links = defaultdict(set)
    all_extracted_count = 0

    for rel_path in all_files:
        full_path = vault_root / rel_path
        try:
            content = full_path.read_text(encoding="utf-8-sig")
        except Exception:
            continue

        links = extract_wikilinks(content)
        if rel_path in target_set:
            all_extracted_count += len(links)

        for link in links:
            res = resolve_wikilink(vault_root, link["file_target"], source_file=rel_path, vault_index=vault_index)
            # An attachment (image, PDF) unless a note itself has a dotted name ("Sürüm v1.2").
            if (not res["exists"] and Path(link["file_target"]).suffix
                    and not link["file_target"].lower().endswith(".md")):
                if not _attachment_exists(vault_index, link["file_target"], rel_path):
                    broken_links.append({
                        "source": rel_path,
                        "line": link["line"],
                        "target": link["file_target"],
                        "raw": link["raw"],
                        "type": "embed" if link["is_embed"] else "wikilink",
                    })
                continue

            if res['exists'] and link['heading']:
                try:
                    target_text = (vault_root / res['resolved_path']).read_text(encoding='utf-8-sig')
                    if _norm_key(link['heading']) not in _headings(target_text):
                        res = {'exists': False, 'resolved_path': None}
                except (OSError, UnicodeError):
                    res = {'exists': False, 'resolved_path': None}
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

    broken_links = [link for link in broken_links if link["source"] in target_set]

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


class MocConflict(ValueError):
    """The MOC file changed outside this command; nothing of the user's text was replaced."""


def _link_title(title, fallback):
    # "]]", "[[" or "|" inside a title would end or split the generated wikilink.
    cleaned = " ".join(re.sub(r"[\[\]|]", " ", title).split())
    if len(cleaned) > MAX_MOC_TITLE_CHARS:
        cleaned = cleaned[:MAX_MOC_TITLE_CHARS].rstrip() + "…"
    return cleaned or fallback


def _moc_reader(metadata, relative, scopes):
    """(scope, visibility rank) of the MOC exactly as retrieval will gate it.

    retrieval delivers a record to an explicit --project only inside its scope
    (beyin_v3.record_scope) and to an audience only by its visibility.
    """
    from beyin_v3 import record_scope
    project = metadata.get("project")
    if project is not None and not isinstance(project, str):
        raise ValueError(f"{relative} has a non-text project field; it was left unchanged")
    # An unknown visibility lists only public notes; sync refuses to index such a file anyway.
    rank = VISIBILITY_RANK.get(metadata.get("visibility", "internal"), VISIBILITY_RANK["public"])
    return record_scope({"project": project, "source": relative}, scopes), rank


def _moc_skip_reason(metadata, relative, scopes, moc_scope, moc_rank):
    """Why a note's title must stay out of this MOC, or None when every MOC reader may read it."""
    from beyin_v3 import _rejected_inference, record_scope
    if (metadata.get("trust") == "untrusted" or metadata.get("trusted") is False
            or metadata.get("status") == "untrusted" or metadata.get("kind") == "untrusted"
            or _rejected_inference(metadata)):
        return "untrusted"  # the same records retrieval never delivers
    project = metadata.get("project")
    if project is not None and not isinstance(project, str):
        return "other_scope"
    # A nested folder can belong to another project; its titles never enter this MOC.
    if record_scope({"project": project, "source": relative}, scopes) != moc_scope:
        return "other_scope"
    if VISIBILITY_RANK.get(metadata.get("visibility", "internal"), len(VISIBILITY_RANK)) > moc_rank:
        return "narrower_visibility"
    return None


def _plain_folder_parts(vault_root, folder_path):
    relative = folder_path.relative_to(vault_root)
    for part in relative.parts:
        if part.startswith(".") or part.casefold() in EXCLUDED_DIR_NAMES:
            raise ValueError("MOC folder must be a regular note folder")


def _moc_name(moc_filename):
    name = moc_filename if isinstance(moc_filename, str) else ""
    if (not name or name != name.strip() or "/" in name or "\\" in name or name.startswith(".")
            or not name.casefold().endswith(".md")):
        raise ValueError("MOC file must be a plain .md file name inside the target folder")
    return name


def _snapshot(path):
    """(bytes, identity) of a regular file, None when absent. identity = hash, mtime, size, inode."""
    try:
        before = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("MOC output must be a regular file")
    with open(path, "rb") as handle:
        data = handle.read()
    return data, (hashlib.sha256(data).hexdigest(), before.st_mtime_ns, before.st_size, before.st_ino)


def _rename(source, destination):
    from beyin_v3_sync import _safe_replace
    _safe_replace(source, destination)


def _write_temporary(directory, data):
    fd, temporary = tempfile.mkstemp(prefix=".beyin-moc-", suffix=".tmp", dir=directory)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    return temporary


def _replace_if_unchanged(path, data, expected):
    """Replace path with data only while it still matches the snapshot the new text came from.

    Obsidian does not take Beyin's lock, so the bytes, mtime, size and inode are checked
    again right before the rename. On POSIX the old file stays open across the rename: an
    in-place save that lands in the remaining window is detected and its bytes put back.
    """
    temporary = _write_temporary(path.parent, data)
    held = None
    try:
        if os.name != "nt":  # Windows cannot rename over a file this process holds open.
            try:
                held = os.open(path, os.O_RDONLY)
            except FileNotFoundError:
                raise MocConflict("MOC changed during update; nothing was replaced, rerun moc") from None
        current = _snapshot(path)
        if current is None or current[1] != expected or (held is not None and os.fstat(held).st_ino != expected[3]):
            raise MocConflict("MOC changed during update; nothing was replaced, rerun moc")
        _rename(temporary, path)
        if held is not None:
            after = os.fstat(held)
            os.lseek(held, 0, os.SEEK_SET)
            chunks = []
            while True:
                chunk = os.read(held, 1 << 16)
                if not chunk:
                    break
                chunks.append(chunk)
            user = b"".join(chunks)
            if (after.st_mtime_ns, after.st_size, hashlib.sha256(user).hexdigest()) != (expected[1], expected[2], expected[0]):
                restored = _snapshot(path)
                if restored is not None and restored[0] == data:
                    back = _write_temporary(path.parent, user)
                    try:
                        _rename(back, path)
                    finally:
                        if os.path.exists(back):
                            os.unlink(back)
                    raise MocConflict("MOC changed while it was being replaced; the edit was restored, rerun moc")
                raise MocConflict("MOC changed while it was being replaced; the newest file was kept, rerun moc")
    finally:
        if held is not None:
            os.close(held)
        if os.path.exists(temporary):
            os.unlink(temporary)


def generate_moc(vault_root, target_folder="knowledge", moc_filename="MOC.md"):
    from types import SimpleNamespace
    from beyin_v3_notes import locked
    with locked(SimpleNamespace(root=Path(vault_root).resolve())):
        return _generate_moc(vault_root, target_folder, moc_filename)


def _generate_moc(vault_root, target_folder="knowledge", moc_filename="MOC.md"):
    """Generate or update an Obsidian Map of Content (MOC) note.

    Only a file this command created, or one carrying exactly one marker pair, is
    rewritten, and only between the markers; user text outside them is kept byte for byte.
    """
    vault_root = Path(vault_root).resolve()
    folder_path = (vault_root / target_folder).resolve()
    if not folder_path.is_relative_to(vault_root):
        raise ValueError("MOC folder must remain inside the vault")
    if not folder_path.is_dir():
        raise ValueError(f"target folder not found in vault: {target_folder}")
    _plain_folder_parts(vault_root, folder_path)

    moc_path = folder_path / _moc_name(moc_filename)
    if not moc_path.resolve().is_relative_to(vault_root):
        raise ValueError("MOC output must remain inside the vault")
    moc_rel = moc_path.relative_to(vault_root).as_posix()

    # One snapshot: the MOC's own scope and visibility, the marker check and the final
    # replace all use exactly these bytes.
    from beyin_v3 import read_project_scopes
    from beyin_v3_sync import parse
    scopes = read_project_scopes(vault_root) or {"folders": [], "shared_unscoped": False}
    snapshot = _snapshot(moc_path)
    moc_metadata = {}
    if snapshot is not None:
        original, identity = snapshot
        bom = codecs.BOM_UTF8 if original.startswith(codecs.BOM_UTF8) else b""
        try:
            existing_text = original[len(bom):].decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError(f"{moc_rel} is not UTF-8 text; it was left unchanged") from None
        start = existing_text.find(MOC_START_MARKER)
        end = existing_text.find(MOC_END_MARKER)
        if (existing_text.count(MOC_START_MARKER) != 1 or existing_text.count(MOC_END_MARKER) != 1
                or end < start):
            raise ValueError(f"{moc_rel} has no single {MOC_START_MARKER} ... {MOC_END_MARKER} pair; "
                             "only that generated block is ever rewritten. Add the pair where the index "
                             "should go, or choose another --file")
        try:
            moc_metadata, _ = parse(existing_text)
        except ValueError:
            raise ValueError(f"{moc_rel} frontmatter could not be read; it was left unchanged") from None
    moc_scope, moc_rank = _moc_reader(moc_metadata, moc_rel, scopes)

    # Collect notes in folder (excluding moc itself)
    notes_by_category = defaultdict(list)
    skipped = {"other_scope": 0, "narrower_visibility": 0, "untrusted": 0}
    for root, dirs, files in os.walk(folder_path):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d.casefold() not in EXCLUDED_DIR_NAMES]
        for f in files:
            if not f.lower().endswith(".md") or f.startswith("."):
                continue
            f_path = Path(root) / f
            if f_path == moc_path or f_path.resolve() == moc_path.resolve():
                continue
            metadata = _note_metadata(vault_root, f_path)
            if metadata is None:
                continue

            rel_to_vault = f_path.relative_to(vault_root).as_posix()
            reason = _moc_skip_reason(metadata, rel_to_vault, scopes, moc_scope, moc_rank)
            if reason is not None:
                skipped[reason] += 1
                continue
            rel_to_folder = f_path.relative_to(folder_path).as_posix()
            title = _link_title(extract_note_title(f_path), f_path.stem)

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

    if snapshot is not None:
        newline = "\r\n" if "\r\n" in existing_text else "\n"
        new_text = (existing_text[:start] + generated_block.replace("\n", newline)
                    + existing_text[end + len(MOC_END_MARKER):])
        if new_text == existing_text:
            action_status = "unchanged"
        else:
            _replace_if_unchanged(moc_path, bom + new_text.encode("utf-8"), identity)
            action_status = "updated"
    else:
        # Create fresh MOC file
        folder_display = target_folder.replace("-", " ").title()
        new_text = f"""# {folder_display} — Map of Content (MOC)

Bu not, `{target_folder}` altındaki notların navigasyon ve içerik haritasıdır.

## Belgeler ve Notlar

{generated_block}
"""
        try:
            # Exclusive create: a file an editor created after the absence check is never replaced.
            with moc_path.open("x", encoding="utf-8", newline="") as output:
                output.write(new_text)
        except FileExistsError:
            raise MocConflict("MOC file appeared during update; nothing was replaced, rerun moc") from None
        action_status = "created"

    return {
        "status": action_status,
        "action": "moc",
        "file": moc_rel,
        "indexed_notes_count": total_notes,
        "categories_count": len(notes_by_category),
        # Notes this MOC's readers may not read: another project's scope, a narrower
        # visibility than the MOC's own, or untrusted ingested text.
        "moc_scope": moc_scope,
        "skipped": skipped,
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
