#!/usr/bin/env python3
"""kur: one command that installs this fork's core layer on a machine (or removes it).

    kur.py --machine <etiket> --project-root <yol> [--project-root ...] --vault <yol>
           [--ayarlar] [--force] [--dry-run]
    kur.py --uninstall [--dry-run]

Steps (each idempotent; every file that changes is first copied to
``<config>/ege-kur-yedek/<timestamp>/``; ``<config>`` = CLAUDE_CONFIG_DIR or ~/.claude):

1. ``<config>/git-devir.json``  shared config (machine, project roots, vault, python, proje.py path)
2. ``<config>/settings.json``   git-devir hooks + proje SessionStart hook; every other hook is kept
3. ``<config>/agents``, ``<config>/skills``  copies of ege/agents/*.md and ege/skills/<skill>/
4. ``<config>/CLAUDE.md``       managed block from ege/claude/CLAUDE-kural.md
5. vault ``beyin.py``           upstream bridge install; with --ayarlar also the preferences

The vault itself is never written by this script (step 5 only calls the vault's own CLI).
Standard library only; Windows, macOS, Linux.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

EGE = Path(__file__).resolve().parent
sys.path.insert(0, str(EGE / "hooks"))
import install_git_devir as igd  # noqa: E402

PROJE_SCRIPT = EGE / "hooks" / "proje.py"
PROJE_MARK = ("/proje.py", "\\proje.py")
PROJE_MODES = {"SessionStart": "start"}
PROJE_TIMEOUTS = {"SessionStart": 10}
RULE_FILE = EGE / "claude" / "CLAUDE-kural.md"
CLI_PLACEHOLDER = "{{PROJE_CLI}}"  # replaced with "<python> <proje.py>" for this machine
BLOCK_START = "<!-- ege-beyin:start -->"
BLOCK_END = "<!-- ege-beyin:end -->"
BLOCK_NOTE = "<!-- Bu bloğu ege/kur.py yönetir; elle düzenleme (kaynak: ege/claude/CLAUDE-kural.md). -->"
VAULT_ONLY_MARK = ".yalniz-vault"  # a skill folder holding this file is never installed globally
MANIFEST = "ege-kur.json"
BACKUP_DIR = "ege-kur-yedek"
PREFERENCES = (["--project-context", "on"], ["--secret-filter", "on"], ["--context-dedupe", "on"],
               ["--inbox-report", "on", "--inbox-folder", "00 Gelen"])
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


class Run:
    """One installer run: knows the config folder, collects report lines, makes backups."""

    def __init__(self, dry: bool):
        self.dry = dry
        self.base = igd.config_dir()
        self.stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        self.backup_root = None
        self.changes = 0
        self.problems = []

    def say(self, text=""):
        print(text)

    def step(self, title):
        self.say(f"\n== {title}")

    def change(self, text, done, planned):
        """One file change: ``done`` is printed after a real run, ``planned`` in a dry run."""
        self.changes += 1
        self.say(("  [yapılacak] " if self.dry else "  [yapıldı]   ") + f"{text} {planned if self.dry else done}")

    def same(self, text):
        self.say("  [aynı]      " + text)

    def problem(self, text):
        self.problems.append(text)
        self.say("  [SORUN]     " + text)

    def backup(self, path: Path):
        """Copy a file that is about to change or disappear into this run's backup folder."""
        if self.dry or not path.is_file():
            return
        if self.backup_root is None:
            root, index = self.base / BACKUP_DIR / self.stamp, 1
            while root.exists():
                index += 1
                root = self.base / BACKUP_DIR / f"{self.stamp}-{index}"
            self.backup_root = root
        try:
            relative = path.resolve().relative_to(self.base.resolve())
        except ValueError:
            relative = Path(path.name)
        target = self.backup_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)

    def write(self, path: Path, raw: bytes):
        if self.dry:
            return
        self.backup(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_bytes(raw)
        os.replace(tmp, path)

    def remove(self, path: Path):
        if self.dry:
            return
        self.backup(path)
        path.unlink()
        parent = path.parent
        while parent != self.base and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent


def read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def read_manifest(run: Run) -> dict:
    data = read_json(run.base / MANIFEST)
    files = data.get("files")
    return {"files": files if isinstance(files, dict) else {},
            "claude_md": data.get("claude_md") if isinstance(data.get("claude_md"), dict) else {}}


def write_manifest(run: Run, manifest: dict):
    path = run.base / MANIFEST
    text = json.dumps({"version": 1, **manifest}, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    if run.dry or (path.is_file() and path.read_bytes() == text.encode("utf-8")):
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    igd.write_atomic(path, text)


# --------------------------------------------------------------------------- 1. config

def step_config(run: Run, args):
    run.step("1. Ortak ayar dosyası (git-devir.json)")
    path = run.base / "git-devir.json"
    config = igd.build_config(args, read_json(path))
    config["python"] = str(args.python)
    config["proje_py"] = str(PROJE_SCRIPT)
    raw = (json.dumps(config, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    if path.is_file() and path.read_bytes() == raw:
        run.same(str(path))
    else:
        run.write(path, raw)
        run.change(f"{path} (makine={config['machine']}, kökler={', '.join(config['project_roots'])}, "
                   f"vault={config['vault_path']})", "yazıldı", "yazılacak")
    return config


# --------------------------------------------------------------------------- 2. hooks

def step_hooks(run: Run, python: str, install: bool):
    run.step("2. Hook'lar (settings.json)")
    path = run.base / "settings.json"
    data = igd.read_settings(path)
    new, removed_a, added_a = igd.merged_settings(data, python, install)
    new, removed_b, added_b = igd.merged_settings(new, python, install, script=PROJE_SCRIPT, modes=PROJE_MODES,
                                                  timeouts=PROJE_TIMEOUTS, mark=PROJE_MARK)
    if new == data:
        run.same(f"{path} (git-devir ve proje hook'ları " + ("zaten kurulu)" if install else "zaten yok)"))
        return
    run.write(path, (json.dumps(new, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    if install:
        names = [f"{event} → git_devir.py {igd.MODES[event]}" for event in added_a]
        names += [f"{event} → proje.py {PROJE_MODES[event]}" for event in added_b]
        run.change(f"{path}: " + "; ".join(names), "eklendi (diğer hook'lara dokunulmadı)",
                   "eklenecek (diğer hook'lara dokunulmaz)")
    else:
        run.change(f"{path}: {removed_a + removed_b} hook girdisi", "kaldırıldı (diğer hook'lara dokunulmadı)",
                   "kaldırılacak (diğer hook'lara dokunulmaz)")


# --------------------------------------------------------------------------- 3. agents and skills

def is_agent(path: Path) -> bool:
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeDecodeError):
        return False
    if not lines or lines[0].strip() != "---":
        return False
    for line in lines[1:]:
        if line.strip() == "---":
            return False
        if line.startswith("name:"):
            return True
    return False


def source_files(ege: Path):
    """({destination relative to <config>: source path}, [skipped vault-only skill names])."""
    files, skipped = {}, []
    agents = ege / "agents"
    for path in sorted(agents.glob("*.md")) if agents.is_dir() else []:
        if is_agent(path):
            files[f"agents/{path.name}"] = path
    skills = ege / "skills"
    for folder in sorted(skills.iterdir()) if skills.is_dir() else []:
        if not folder.is_dir() or not (folder / "SKILL.md").is_file():
            continue
        if (folder / VAULT_ONLY_MARK).exists():
            skipped.append(folder.name)
            continue
        for path in sorted(folder.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                files[f"skills/{folder.name}/{path.relative_to(folder).as_posix()}"] = path
    return files, skipped


def step_files(run: Run, manifest: dict, force: bool):
    run.step("3. Ajan ve skill dosyaları")
    files, skipped = source_files(EGE)
    recorded = manifest["files"]
    for relative, source in files.items():
        target = run.base / relative
        raw = source.read_bytes()
        digest = sha256(raw)
        current = sha256(target.read_bytes()) if target.is_file() else None
        if current == digest:
            run.same(str(target))
        elif current is None or current == recorded.get(relative) or force:
            run.write(target, raw)
            if current is None:
                run.change(str(target), "kopyalandı", "kopyalanacak")
            elif current == recorded.get(relative):
                run.change(str(target), "güncellendi", "güncellenecek")
            else:
                run.change(str(target), "üzerine yazıldı (--force; eskisi yedekte)",
                           "üzerine yazılacak (--force; eskisi yedeklenir)")
        else:
            run.problem(f"{target} bu kurulumun yazdığından farklı (elle değişmiş ya da başka kaynaktan); "
                        "dokunulmadı. Üzerine yazmak için --force.")
            continue
        recorded[relative] = digest
    for relative in sorted(set(recorded) - set(files)):  # no longer shipped by the fork
        target = run.base / relative
        if target.is_file() and sha256(target.read_bytes()) == recorded[relative]:
            run.remove(target)
            run.change(f"{target} (fork'ta artık yok)", "kaldırıldı", "kaldırılacak")
        del recorded[relative]
    for name in skipped:
        run.say(f"  [atlandı]   skills/{name}: yalnız vault'a kurulur (bkz. ege/skills/README.md)")
    if not files:
        run.say("  Kopyalanacak dosya yok.")


def step_files_uninstall(run: Run, manifest: dict):
    run.step("3. Ajan ve skill dosyaları")
    for relative, digest in sorted(manifest["files"].items()):
        target = run.base / relative
        if not target.is_file():
            continue
        if sha256(target.read_bytes()) == digest:
            run.remove(target)
            run.change(str(target), "silindi", "silinecek")
        else:
            run.problem(f"{target} kurulumdan sonra değişmiş; bırakıldı.")
    if not manifest["files"]:
        run.say("  Bu kurulumun kopyaladığı dosya kaydı yok.")
    manifest["files"] = {}


# --------------------------------------------------------------------------- 4. CLAUDE.md

def block_span(text: str):
    """(start, end) of the managed block, None when absent; raises ValueError when damaged."""
    starts, ends = text.count(BLOCK_START), text.count(BLOCK_END)
    if not starts and not ends:
        return None
    if starts != 1 or ends != 1 or text.index(BLOCK_START) > text.index(BLOCK_END):
        raise ValueError("yönetilen blok işaretleri bozuk (start/end tek ve sıralı olmalı)")
    return text.index(BLOCK_START), text.index(BLOCK_END) + len(BLOCK_END)


def proje_cli(python) -> str:
    """The command the rule block names; forward slashes work in every shell Claude Code uses."""
    parts = [str(item).replace(chr(92), "/") for item in (python, PROJE_SCRIPT)]
    return " ".join(f'"{part}"' if " " in part else part for part in parts)


def step_claude_md(run: Run, manifest: dict, install: bool, python=sys.executable):
    run.step("4. Global kural bloğu (CLAUDE.md)")
    path = run.base / "CLAUDE.md"
    exists = path.is_file()
    try:
        text = path.read_bytes().decode("utf-8") if exists else ""
        span = block_span(text)
    except (OSError, ValueError) as exc:
        run.problem(f"{path}: {exc}; dosyaya dokunulmadı.")
        return
    nl = "\r\n" if "\r\n" in text else "\n"
    state = manifest["claude_md"]
    if install:
        rule = RULE_FILE.read_text(encoding="utf-8").strip().replace(CLI_PLACEHOLDER, proje_cli(python))
        block = nl.join([BLOCK_START, BLOCK_NOTE, *rule.splitlines(), BLOCK_END])
        if span:
            new = text[:span[0]] + block + text[span[1]:]
        else:
            sep = "" if not text or text.endswith(nl + nl) else (nl if text.endswith(nl) else nl + nl)
            new = text + sep + block + nl
            state.update(created=not exists, sep=sep)
    else:
        if not span:
            run.same(f"{path} (yönetilen blok yok)")
            return
        before, after = text[:span[0]], text[span[1]:]
        sep = state.get("sep") if isinstance(state.get("sep"), str) else ""
        if after.startswith(nl):
            after = after[len(nl):]
        if sep and before.endswith(sep):
            before = before[:len(before) - len(sep)]
        new = before + after
        if not new.strip() and state.get("created"):
            run.remove(path)
            run.change(f"{path} (yalnız yönetilen blok vardı, dosyayı bu kurulum oluşturmuştu)",
                       "silindi", "silinecek")
            manifest["claude_md"] = {}
            return
        manifest["claude_md"] = {}
    if new == text:
        run.same(f"{path} (blok güncel)")
        return
    run.write(path, new.encode("utf-8"))
    done, planned = (("yenilendi", "yenilenecek") if span else ("eklendi", "eklenecek")) if install else (
        "kaldırıldı", "kaldırılacak")
    run.change(f"{path}: yönetilen blok", done + "; işaretlerin dışındaki metne dokunulmadı",
               planned + "; işaretlerin dışındaki metne dokunulmaz")


# --------------------------------------------------------------------------- 5. avenox bridge

def step_bridge(run: Run, config: dict, python: str, ayarlar: bool):
    run.step("5. Avenox köprüsü" + (" ve ayarlar" if ayarlar else ""))
    vault = Path(config["vault_path"])
    beyin = vault / "beyin.py"
    if not beyin.is_file():
        run.problem(f"{beyin} yok; köprü adımı atlandı. Önce avenox'u resmi paketten vault'a kur, sonra "
                    "bu komutu yeniden çalıştır.")
        return
    bridge = ["bridge", "--harness", "claude"]
    for root in config["project_roots"]:
        bridge += ["--project-root", root]
    commands = [bridge + ["--install"]] + ([["preferences", *p] for p in PREFERENCES] if ayarlar else [])
    for arguments in commands:
        command = [python, str(beyin), *arguments]
        run.say("  $ " + (subprocess.list2cmdline(command) if os.name == "nt" else shlex.join(command)))
        if run.dry:
            run.say("    [yapılacak] çalıştırılacak")
            continue
        try:
            proc = subprocess.run(command, cwd=str(vault), stdin=subprocess.DEVNULL, capture_output=True,
                                  timeout=300, creationflags=NO_WINDOW)
            code = proc.returncode
            output = (proc.stdout + proc.stderr).decode("utf-8", "replace").strip()
        except (OSError, subprocess.TimeoutExpired) as exc:
            code, output = -1, f"{type(exc).__name__}: {exc}"
        tail = "\n".join("    | " + line for line in output.splitlines()[-8:])
        if code == 0:
            run.say("    [tamam] çıkış kodu 0")
        else:
            run.problem(f"`beyin.py {' '.join(arguments)}` başarısız (çıkış kodu {code}); önceki adımlar "
                        "geri alınmadı." + ("\n" + tail if tail else ""))
    if not ayarlar:
        run.say("  Not: önerilen avenox ayarları (project-context, secret-filter, context-dedupe, inbox-report) "
                "yalnız --ayarlar ile açılır.")


# --------------------------------------------------------------------------- entry point

def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Fork'un çekirdek katmanını bu makineye kurar veya kaldırır.")
    parser.add_argument("--machine", help="Makine etiketi: is-5090, ev, macbookneo … (hostname değil).")
    parser.add_argument("--project-root", action="append", default=[], help="Repoların durduğu kök (tekrarlanabilir).")
    parser.add_argument("--vault", help="Obsidian vault yolu.")
    parser.add_argument("--ayarlar", action="store_true", help="Önerilen avenox preferences ayarlarını da açar.")
    parser.add_argument("--force", action="store_true", help="Elle değişmiş ajan/skill kopyalarının üzerine yazar.")
    parser.add_argument("--uninstall", action="store_true", help="Yalnız bu kurulumun eklediklerini kaldırır.")
    parser.add_argument("--dry-run", action="store_true", help="Hiçbir şey yazmadan ne yapılacağını gösterir.")
    parser.add_argument("--python", default=sys.executable, help="Hook'ları ve beyin.py'yi çalıştıracak Python.")
    parser.add_argument("--auto-owner", action="append", help="git-devir: tam otomatik GitHub sahibi.")
    parser.add_argument("--auto-host", action="append", help="git-devir: tam otomatik git sunucusu.")
    parser.add_argument("--max-file-mb", type=float, help="git-devir: wip yedeğine girecek en büyük dosya (MB).")
    args = parser.parse_args(argv)

    run = Run(args.dry_run)
    run.say(f"ege/kur.py · {'kaldırma' if args.uninstall else 'kurulum'}"
            f"{' · DENEME (hiçbir şey yazılmaz)' if run.dry else ''} · hedef: {run.base}")
    manifest = read_manifest(run)

    if args.uninstall:
        step_hooks(run, args.python, install=False)
        step_files_uninstall(run, manifest)
        step_claude_md(run, manifest, install=False)
        if not run.dry:
            (run.base / MANIFEST).unlink(missing_ok=True)
        run.step("Özet")
        run.say(f"  {run.changes} dosya değişikliği {'yapılacak' if run.dry else 'yapıldı'}; "
                f"{len(run.problems)} sorun.")
        run.say("  Bırakılanlar: git-devir.json, ~/.claude/git-devir/ (log ve notlar), vault ve avenox köprüsü.")
        run.say("  Avenox köprüsünü de kaldırmak için vault klasöründe: "
                "python beyin.py bridge --harness claude --uninstall")
    else:
        if not args.vault or not Path(args.vault).expanduser().is_dir():
            raise SystemExit("--vault var olan bir klasör olmalı (Sync bitmeden kurma).")
        igd.read_settings(run.base / "settings.json")  # stop before writing anything if it is unreadable
        config = step_config(run, args)
        step_hooks(run, args.python, install=True)
        step_files(run, manifest, args.force)
        step_claude_md(run, manifest, install=True, python=args.python)
        write_manifest(run, manifest)
        step_bridge(run, config, args.python, args.ayarlar)
        run.step("Özet")
        run.say(f"  {run.changes} dosya değişikliği {'yapılacak' if run.dry else 'yapıldı'}; "
                f"{len(run.problems)} sorun.")
        if run.dry:
            run.say("  Bu bir denemeydi. Aynı komutu --dry-run olmadan çalıştır.")
        else:
            run.say("  Sıradaki adımlar:")
            run.say("  1. Açık Claude Code oturumlarını kapat, repo köklerinden birindeki bir repoda YENİ oturum aç.")
            run.say("  2. Oturum başında `Proje: <ad> (<sahip/repo>)` satırını gör; klasör yoksa ajan sana sorar.")
            run.say("  3. Küçük bir değişiklik yaptır; bitince `30 Projeler/<ad>/Oturumlar/` altında yeni kart "
                    "ve avenox'ta yeni kayıt oluşmalı.")
    if run.backup_root:
        run.say(f"  Yedekler: {run.backup_root}")
    for text in run.problems:
        run.say("  SORUN: " + text.splitlines()[0])
    return 1 if run.problems else 0


if __name__ == "__main__":
    sys.exit(main())
