#!/usr/bin/env python3
"""Moodle Book & Glossary Sync Tool with Robust Versioning and Conflict Protection.

Provides fast, bidirectional synchronization between Moodle books/glossaries
and human-/agent-friendly local files with YAML frontmatter.

Features:
- Fast SSH-based execution (<1 sec) via daemon user.
- Local YAML frontmatter header for transparent metadata + clean HTML body.
- Conflict detection (protects against concurrent remote/local modifications).
- Automatic timestamped snapshots (.sync_backups/) for rollbacks.
- Automated Git commit integration for full revision tracking.
- Visual unified diffing and detailed status inspection.

Builds upon canonical Moodle client at projects/homelab-infra/school/moodle/moodle_client.py.
"""

from __future__ import annotations

import argparse
import base64
import datetime
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

# Import canonical moodle_client
SCRIPT_DIR = Path(__file__).resolve().parent
HOMELAB_MOODLE = SCRIPT_DIR.parent.parent / "homelab-infra" / "school" / "moodle"
if str(HOMELAB_MOODLE) not in sys.path:
    sys.path.insert(0, str(HOMELAB_MOODLE))

try:
    import moodle_client
except ImportError:
    print(f"Error: Could not import moodle_client from {HOMELAB_MOODLE}", file=sys.stderr)
    sys.exit(1)

try:
    import yaml
except ImportError:
    print("PyYAML is required. Run: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

DEFAULT_CONFIG = SCRIPT_DIR / "config.yaml"
BACKUPS_DIR = SCRIPT_DIR / ".sync_backups"


def transliterate_uk(text: str) -> str:
    """Transliterate Ukrainian text to ASCII slug (KMU 2010 rules)."""
    translit_map = {
        "а": "a", "б": "b", "в": "v", "г": "h", "ґ": "g", "д": "d", "е": "e", "є": "ye",
        "ж": "zh", "з": "z", "и": "y", "і": "i", "ї": "yi", "й": "y", "к": "k", "л": "l",
        "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
        "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch", "ь": "",
        "ю": "yu", "я": "ya", "’": "", "'": "", "`": "", "–": "-", "—": "-",
    }
    s = text.lower()
    out: list[str] = []
    for char in s:
        if char in translit_map:
            out.append(translit_map[char])
        elif char.isalnum():
            out.append(char)
        elif char in [" ", "_", "-", ".", ":", "/", "\\", ","]:
            out.append("_")
    slug = re.sub(r"_+", "_", "".join(out)).strip("_")
    return slug[:45]


def normalize_text(text: str) -> str:
    """Normalize line endings and whitespace for reliable diffing and hashing."""
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def parse_chapter_file(filepath: Path) -> tuple[dict[str, Any], str]:
    """Parse a local chapter HTML file with YAML frontmatter."""
    content = filepath.read_text(encoding="utf-8")
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            frontmatter_str = parts[1]
            body = parts[2].lstrip("\r\n")
            meta = yaml.safe_load(frontmatter_str) or {}
            return meta, normalize_text(body)
    return {}, normalize_text(content)


def format_chapter_file(meta: dict[str, Any], body: str) -> str:
    """Format metadata and HTML body into a single file with YAML frontmatter."""
    clean_meta = {
        "id": int(meta["id"]),
        "book_id": int(meta.get("book_id", meta.get("bookid", 0))),
        "course_id": int(meta.get("course_id", meta.get("course", 6))),
        "pagenum": int(meta["pagenum"]),
        "subchapter": int(meta.get("subchapter", 0)),
        "title": str(meta["title"]),
        "hidden": int(meta.get("hidden", 0)),
        "timemodified": int(meta.get("timemodified", 0)),
    }
    frontmatter = yaml.dump(clean_meta, allow_unicode=True, sort_keys=False).strip()
    return f"---\n{frontmatter}\n---\n{normalize_text(body)}\n"


def format_timestamp(ts: int | float | None) -> str:
    if not ts:
        return "—"
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


class MoodleSyncManager:
    def __init__(self, config_path: Path = DEFAULT_CONFIG):
        if not config_path.exists():
            raise FileNotFoundError(f"Config file not found: {config_path}")
        with open(config_path, "r", encoding="utf-8") as f:
            self.config = yaml.safe_load(f)
        self.root_dir = config_path.parent
        self.backups_dir = self.root_dir / ".sync_backups"

    def _exec(self, php_code: str) -> Any:
        return moodle_client.run_moodle_php(php_code)

    def get_book_configs(self, target: str | None = None) -> list[dict[str, Any]]:
        books = self.config.get("Books", [])
        if target and target.lower() != "all":
            filtered = [b for b in books if b["name"] == target or str(b["book_id"]) == target or str(b.get("cmid")) == target]
            if not filtered:
                raise ValueError(f"Book '{target}' not found in config.yaml")
            return filtered
        return books

    def get_glossary_configs(self, target: str | None = None) -> list[dict[str, Any]]:
        glossaries = self.config.get("Glossaries", [])
        if target and target.lower() != "all":
            filtered = [g for g in glossaries if g["name"] == target or str(g["glossary_id"]) == target or str(g.get("cmid")) == target]
            if not filtered:
                raise ValueError(f"Glossary '{target}' not found in config.yaml")
            return filtered
        return glossaries

    # =========================================================================
    # SNAPSHOTS & BACKUP MANAGEMENT
    # =========================================================================

    def create_snapshot(self, action: str, targets: list[Path]) -> Path | None:
        """Create a timestamped snapshot of specified files/directories before changes."""
        existing = [t for t in targets if t.exists()]
        if not existing:
            return None
        now_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        snap_dir = self.backups_dir / f"{now_str}_{action}"
        snap_dir.mkdir(parents=True, exist_ok=True)

        for src in existing:
            rel = src.relative_to(self.root_dir)
            dest = snap_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            if src.is_dir():
                shutil.copytree(src, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dest)

        return snap_dir

    def list_backups(self) -> None:
        """List available snapshots."""
        if not self.backups_dir.exists():
            print("Знімків бекапів не знайдено.")
            return
        snaps = sorted(self.backups_dir.iterdir(), reverse=True)
        print(f"=== ЗНІМКИ ВЕРСІЙ У .sync_backups/ ({len(snaps)}) ===")
        for s in snaps:
            if s.is_dir():
                print(f"• {s.name}")

    def rollback(self, snapshot_name: str) -> None:
        """Rollback local files from a snapshot."""
        snap_dir = self.backups_dir / snapshot_name
        if not snap_dir.exists():
            matches = [d for d in self.backups_dir.iterdir() if snapshot_name in d.name]
            if len(matches) == 1:
                snap_dir = matches[0]
            else:
                raise FileNotFoundError(f"Snapshot '{snapshot_name}' not found")

        print(f"⏪ Відновлення файлів зі знімка: {snap_dir.name}...")
        for item in snap_dir.rglob("*"):
            if item.is_file():
                rel = item.relative_to(snap_dir)
                dest = self.root_dir / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, dest)
                print(f"  • Відновлено: {rel}")
        print("✅ Файли успішно відновлено!")

    # =========================================================================
    # GIT INTEGRATION
    # =========================================================================

    def git_commit(self, message: str) -> bool:
        """Stage tracked books/glossaries and create a Git commit."""
        try:
            # Stage config, books, glossaries
            subprocess.run(["git", "add", "config.yaml", "books/", "glossaries/"], cwd=self.root_dir, check=True)
            status_res = subprocess.run(["git", "status", "--porcelain"], cwd=self.root_dir, capture_output=True, text=True, check=True)
            if not status_res.stdout.strip():
                print("ℹ️ Немає змін для створення Git-коміту.")
                return False

            cmd = ["git", "commit", "-m", message]
            res = subprocess.run(cmd, cwd=self.root_dir, capture_output=True, text=True, check=True)
            print(f"📦 Створено Git-коміт: {res.stdout.splitlines()[0]}")
            return True
        except subprocess.CalledProcessError as e:
            print(f"❌ Помилка створення Git-коміту: {e.stderr or e}")
            return False

    # =========================================================================
    # PULL OPERATIONS (WITH CONFLICT PROTECTION)
    # =========================================================================

    def pull_book(self, book_cfg: dict[str, Any], force: bool = False) -> None:
        book_id = book_cfg["book_id"]
        dir_path = self.root_dir / book_cfg["directory"]
        dir_path.mkdir(parents=True, exist_ok=True)

        print(f"📥 Отримання розділів книги '{book_cfg['name']}' (Book #{book_id})...")
        php = f"""<?php
define('CLI_SCRIPT', true);
require('/opt/bitnami/moodle/config.php');
global $DB;
$bookid = {book_id};
$book = $DB->get_record('book', ['id' => $bookid]);
$chapters = $DB->get_records('book_chapters', ['bookid' => $bookid], 'pagenum ASC');
echo json_encode([
    'book' => $book,
    'chapters' => array_values($chapters),
], JSON_UNESCAPED_UNICODE);
"""
        res = self._exec(php)
        if not res or "chapters" not in res:
            print(f"❌ Помилка отримання книги #{book_id}: {res}")
            return

        book_info = res.get("book", {})
        chapters = res.get("chapters", [])
        remote_chs = {int(c["id"]): c for c in chapters}

        # Check for unpushed local modifications
        local_files = sorted(dir_path.glob("*.html"))
        locally_modified: list[Path] = []
        for lf in local_files:
            meta, body = parse_chapter_file(lf)
            chid = meta.get("id")
            if chid and chid in remote_chs:
                r_body = normalize_text(remote_chs[chid].get("content", ""))
                if body != r_body or meta.get("title") != remote_chs[chid].get("title"):
                    locally_modified.append(lf)

        if locally_modified and not force:
            print(f"⚠️ УВАГА: Виявлено {len(locally_modified)} локально змінених розділів:")
            for lf in locally_modified:
                print(f"  • {lf.name}")
            print("Виконайте pull з прапорцем --force для перезапису або зробіть push / backup.")
            return

        # Snapshot before overwriting
        self.create_snapshot(f"pre_pull_{book_cfg['name']}", [dir_path])

        # Write _book.json metadata
        book_meta_file = dir_path / "_book.json"
        with open(book_meta_file, "w", encoding="utf-8") as f:
            json.dump({
                "book_id": book_info.get("id"),
                "name": book_info.get("name"),
                "course_id": book_info.get("course"),
                "intro": book_info.get("intro"),
                "numbering": book_info.get("numbering"),
                "customtitles": book_info.get("customtitles"),
                "chapters_count": len(chapters),
            }, f, indent=2, ensure_ascii=False)

        # Clear old files and write fresh
        for old_file in local_files:
            old_file.unlink()

        for ch in chapters:
            pagenum = int(ch["pagenum"])
            chid = int(ch["id"])
            title = ch.get("title", f"chapter_{chid}")
            slug = transliterate_uk(title) or "chapter"
            filename = f"{pagenum:02d}_ch{chid}_{slug}.html"
            filepath = dir_path / filename

            meta = {
                "id": chid,
                "book_id": book_id,
                "course_id": book_cfg.get("course_id", 6),
                "pagenum": pagenum,
                "subchapter": int(ch.get("subchapter", 0)),
                "title": title,
                "hidden": int(ch.get("hidden", 0)),
                "timemodified": int(ch.get("timemodified", 0)),
            }
            body = ch.get("content", "")
            filepath.write_text(format_chapter_file(meta, body), encoding="utf-8")

        print(f"✅ Успішно синхронізовано {len(chapters)} розділів у: {book_cfg['directory']}/")

    def pull_glossary(self, gloss_cfg: dict[str, Any], force: bool = False) -> None:
        gloss_id = gloss_cfg["glossary_id"]
        file_path = self.root_dir / gloss_cfg["file"]
        file_path.parent.mkdir(parents=True, exist_ok=True)

        if file_path.exists():
            self.create_snapshot(f"pre_pull_{gloss_cfg['name']}", [file_path])

        print(f"📥 Отримання глосарію '{gloss_cfg['name']}' (Glossary #{gloss_id})...")
        res = moodle_client.list_glossary_entries(glossary_id=gloss_id)
        if not res or "entries" not in res:
            print(f"❌ Помилка отримання глосарію #{gloss_id}: {res}")
            return

        entries = res.get("entries", [])
        data = {
            "glossary_id": gloss_id,
            "name": gloss_cfg.get("title", gloss_cfg["name"]),
            "course_id": gloss_cfg.get("course_id", 6),
            "entries_count": len(entries),
            "entries": entries,
        }
        with open(file_path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, allow_unicode=True, sort_keys=False, default_flow_style=False)

        print(f"✅ Збережено {len(entries)} статей у: {gloss_cfg['file']}")

    # =========================================================================
    # STATUS & DIFF OPERATIONS
    # =========================================================================

    def status(self, target_book: str | None = None) -> None:
        books = self.get_book_configs(target_book)
        for b_cfg in books:
            book_id = b_cfg["book_id"]
            dir_path = self.root_dir / b_cfg["directory"]
            if not dir_path.exists():
                print(f"⚠️ Папка {dir_path} не існує (виконайте pull)")
                continue

            php = f"""<?php
define('CLI_SCRIPT', true);
require('/opt/bitnami/moodle/config.php');
global $DB;
$chapters = $DB->get_records('book_chapters', ['bookid' => {book_id}], 'pagenum ASC');
echo json_encode(array_values($chapters), JSON_UNESCAPED_UNICODE);
"""
            remote_chs = {int(c["id"]): c for c in (self._exec(php) or [])}
            local_files = sorted(dir_path.glob("*.html"))

            print(f"\n=== СТАТУС КНИГИ: {b_cfg['name']} (Book #{book_id}) ===")
            print(f"{'File / Chapter':<45} | {'State':<16} | {'Loc p.':<6} | {'Rem p.':<6} | {'Modified (Local / Remote)':<35} | {'Title'}")
            print("-" * 135)

            for lf in local_files:
                meta, body = parse_chapter_file(lf)
                chid = meta.get("id")
                if not chid or chid not in remote_chs:
                    print(f"{lf.name:<45} | 🆕 NEW (local)    | {meta.get('pagenum', '?'):<6} | {'—':<6} | {'—':<35} | {meta.get('title', '')}")
                    continue

                r = remote_chs[chid]
                r_body = normalize_text(r.get("content", ""))
                r_mtime = int(r.get("timemodified", 0))
                l_mtime = int(meta.get("timemodified", 0))

                content_changed = (body != r_body)
                title_changed = (meta.get("title") != r.get("title"))
                page_changed = (int(meta.get("pagenum", 0)) != int(r.get("pagenum", 0)))
                sub_changed = (int(meta.get("subchapter", 0)) != int(r.get("subchapter", 0)))
                any_change = content_changed or title_changed or page_changed or sub_changed

                # Conflict detection: remote modified AFTER local was pulled AND local was modified
                is_conflict = (r_mtime > l_mtime and any_change)

                if is_conflict:
                    state_str = "🔥 CONFLICT"
                elif any_change:
                    mod_types = []
                    if content_changed: mod_types.append("content")
                    if title_changed: mod_types.append("title")
                    if page_changed: mod_types.append("pagenum")
                    if sub_changed: mod_types.append("subchapter")
                    state_str = f"✏️ MOD ({','.join(mod_types)})"
                elif r_mtime > l_mtime:
                    state_str = "📥 NEED PULL"
                else:
                    state_str = "✅ In sync"

                mtime_str = f"{format_timestamp(l_mtime)[:16]} / {format_timestamp(r_mtime)[:16]}"
                print(f"{lf.name:<45} | {state_str:<16} | {meta.get('pagenum', 0):<6} | {r.get('pagenum', 0):<6} | {mtime_str:<35} | {meta.get('title', '')}")

    def diff_chapter(self, identifier: str) -> None:
        """Show unified diff between local chapter file and Moodle database."""
        found_file: Path | None = None
        target_id = int(identifier) if identifier.isdigit() else None

        for b_cfg in self.get_book_configs():
            dir_path = self.root_dir / b_cfg["directory"]
            if not dir_path.exists():
                continue
            for f in dir_path.glob("*.html"):
                meta, _ = parse_chapter_file(f)
                if target_id is not None and meta.get("id") == target_id:
                    found_file = f
                    break
                if f.name == identifier or f.stem == identifier or f"_ch{identifier}_" in f.name:
                    found_file = f
                    break
            if found_file:
                break

        if not found_file:
            print(f"❌ Розділ '{identifier}' не знайдено серед локальних файлів")
            return

        meta, local_body = parse_chapter_file(found_file)
        chid = meta.get("id")
        remote_ch = moodle_client.get_book_chapter(chid)
        if not remote_ch or "content" not in remote_ch:
            print(f"❌ Розділ #{chid} не знайдено у базі даних Moodle")
            return

        remote_body = normalize_text(remote_ch.get("content", ""))
        diff = difflib.unified_diff(
            remote_body.splitlines(keepends=True),
            local_body.splitlines(keepends=True),
            fromfile=f"moodle:ch{chid}",
            tofile=f"local:{found_file.name}",
        )
        diff_text = "".join(diff)
        if not diff_text:
            print(f"✅ Вміст [ch {chid}] '{found_file.name}' повністю ідентичний із Moodle DB")
        else:
            print(diff_text)

    # =========================================================================
    # PUSH OPERATIONS (WITH CONFLICT GUARDS & SNAPSHOTS)
    # =========================================================================

    def push_book(
        self,
        book_cfg: dict[str, Any],
        chapter_id: int | None = None,
        force: bool = False,
        dry_run: bool = False,
    ) -> None:
        book_id = book_cfg["book_id"]
        course_id = book_cfg.get("course_id", 6)
        dir_path = self.root_dir / book_cfg["directory"]
        if not dir_path.exists():
            raise FileNotFoundError(f"Directory {dir_path} does not exist")

        local_files = sorted(dir_path.glob("*.html"))
        if chapter_id:
            local_files = [f for f in local_files if f"ch{chapter_id}_" in f.name or parse_chapter_file(f)[0].get("id") == chapter_id]
            if not local_files:
                raise ValueError(f"Chapter #{chapter_id} not found in {dir_path}")

        print(f"🚀 Аналіз змін книги '{book_cfg['name']}' (Book #{book_id})...")

        php_fetch = f"""<?php
define('CLI_SCRIPT', true);
require('/opt/bitnami/moodle/config.php');
global $DB;
$chapters = $DB->get_records('book_chapters', ['bookid' => {book_id}], 'pagenum ASC');
echo json_encode(array_values($chapters), JSON_UNESCAPED_UNICODE);
"""
        remote_chs = {int(c["id"]): c for c in (self._exec(php_fetch) or [])}

        updates: list[dict[str, Any]] = []
        conflicts: list[str] = []

        for lf in local_files:
            meta, body = parse_chapter_file(lf)
            chid = meta.get("id")
            if not chid or chid not in remote_chs:
                print(f"⚠️ Пропущено {lf.name}: розділ відсутній у базі Moodle (використовуйте moodle_sync add-chapter)")
                continue

            r = remote_chs[chid]
            r_body = normalize_text(r.get("content", ""))
            r_mtime = int(r.get("timemodified", 0))
            l_mtime = int(meta.get("timemodified", 0))

            is_changed = (
                body != r_body or
                meta.get("title") != r.get("title") or
                int(meta.get("subchapter", 0)) != int(r.get("subchapter", 0)) or
                int(meta.get("hidden", 0)) != int(r.get("hidden", 0)) or
                int(meta.get("pagenum", 0)) != int(r.get("pagenum", 0))
            )

            if is_changed:
                if r_mtime > l_mtime and not force:
                    conflicts.append(f"[ch {chid}] {lf.name} (змінено в Moodle: {format_timestamp(r_mtime)})")
                else:
                    updates.append({
                        "id": chid,
                        "title": meta.get("title", r["title"]),
                        "pagenum": int(meta.get("pagenum", r["pagenum"])),
                        "subchapter": int(meta.get("subchapter", r.get("subchapter", 0))),
                        "hidden": int(meta.get("hidden", r.get("hidden", 0))),
                        "content": body,
                        "filename": lf.name,
                    })

        if conflicts:
            print(f"🔥 ЗНАЙДЕНО КОНФЛІКТИ ({len(conflicts)} розділів було змінено у Moodle після вашого завантаження):")
            for c in conflicts:
                print(f"  • {c}")
            print("Використовуйте 'diff <id>' для аналізу або запустіть push з прапорцем '--force' для примусового перезапису.")
            return

        if not updates:
            print("✅ Немає змін для відправки: всі локальні файли відповідають базі Moodle.")
            return

        print(f"Знайдено змінених розділів: {len(updates)}")
        for u in updates:
            print(f"  • [ch {u['id']}] {u['filename']} -> '{u['title']}' (стор. {u['pagenum']})")

        if dry_run:
            print("🔍 [DRY-RUN] Зміни перевірено, до бази даних Moodle НЕ записано.")
            return

        # Create pre-push snapshot
        snap = self.create_snapshot(f"pre_push_{book_cfg['name']}", [dir_path])
        if snap:
            print(f"📦 Створено резервний знімок: {snap.name}")

        b64_payload = base64.b64encode(json.dumps(updates, ensure_ascii=False).encode("utf-8")).decode("ascii")
        php_push = f"""<?php
define('CLI_SCRIPT', true);
require('/opt/bitnami/moodle/config.php');
require_once($CFG->dirroot . '/course/lib.php');
global $DB;

$courseid = {course_id};
$items = json_decode(base64_decode('{b64_payload}'), true);
$updated_count = 0;

foreach ($items as $item) {{
    $ch = $DB->get_record('book_chapters', ['id' => $item['id']]);
    if ($ch) {{
        $ch->title = $item['title'];
        $ch->content = $item['content'];
        $ch->contentformat = FORMAT_HTML;
        $ch->pagenum = (int)$item['pagenum'];
        $ch->subchapter = (int)$item['subchapter'];
        $ch->hidden = (int)$item['hidden'];
        $ch->timemodified = time();
        $DB->update_record('book_chapters', $ch);
        $updated_count++;
    }}
}}

rebuild_course_cache($courseid, true);
purge_all_caches();

echo json_encode([
    'status' => 'success',
    'updated_count' => $updated_count,
], JSON_UNESCAPED_UNICODE);
"""
        push_res = self._exec(php_push)
        if push_res and push_res.get("status") == "success":
            print(f"✅ Успішно оновлено {push_res.get('updated_count')} розділів у Moodle! Кеш курсу оновлено.")
            self.pull_book(book_cfg, force=True)
        else:
            print(f"❌ Помилка запису в Moodle: {push_res}")

    def push_glossary(self, gloss_cfg: dict[str, Any], dry_run: bool = False) -> None:
        gloss_id = gloss_cfg["glossary_id"]
        file_path = self.root_dir / gloss_cfg["file"]
        if not file_path.exists():
            raise FileNotFoundError(f"File {file_path} does not exist")

        with open(file_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        entries = data.get("entries", [])
        print(f"🚀 Синхронізація глосарію '{gloss_cfg['name']}' (Glossary #{gloss_id}, статей: {len(entries)})...")

        if dry_run:
            print("🔍 [DRY-RUN] Глосарій перевірено, зміни НЕ застосовано.")
            return

        self.create_snapshot(f"pre_push_{gloss_cfg['name']}", [file_path])

        b64_payload = base64.b64encode(json.dumps(entries, ensure_ascii=False).encode("utf-8")).decode("ascii")
        php_push = f"""<?php
define('CLI_SCRIPT', true);
require('/opt/bitnami/moodle/config.php');
global $DB;

$gid = {gloss_id};
$items = json_decode(base64_decode('{b64_payload}'), true);
$inserted = 0;
$updated = 0;

foreach ($items as $item) {{
    $concept = trim($item['concept']);
    $definition = $item['definition'];
    $usedynalink = isset($item['usedynalink']) ? (int)$item['usedynalink'] : 1;
    $casesensitive = isset($item['casesensitive']) ? (int)$item['casesensitive'] : 0;
    $fullmatch = isset($item['fullmatch']) ? (int)$item['fullmatch'] : 1;
    $aliases = isset($item['aliases']) && is_array($item['aliases']) ? $item['aliases'] : [];

    $entry = null;
    if (!empty($item['id'])) {{
        $entry = $DB->get_record('glossary_entries', ['id' => $item['id'], 'glossaryid' => $gid]);
    }}
    if (!$entry) {{
        $entry = $DB->get_record('glossary_entries', ['concept' => $concept, 'glossaryid' => $gid]);
    }}

    if ($entry) {{
        $entry->concept = $concept;
        $entry->definition = $definition;
        $entry->definitionformat = FORMAT_HTML;
        $entry->usedynalink = $usedynalink;
        $entry->casesensitive = $casesensitive;
        $entry->fullmatch = $fullmatch;
        $entry->timemodified = time();
        $DB->update_record('glossary_entries', $entry);
        $entryid = $entry->id;
        $updated++;
    }} else {{
        $entry = new stdClass();
        $entry->glossaryid = $gid;
        $entry->userid = 2; // admin
        $entry->concept = $concept;
        $entry->definition = $definition;
        $entry->definitionformat = FORMAT_HTML;
        $entry->attachment = '';
        $entry->timecreated = time();
        $entry->timemodified = time();
        $entry->teacherentry = 1;
        $entry->sourceglossaryid = 0;
        $entry->usedynalink = $usedynalink;
        $entry->casesensitive = $casesensitive;
        $entry->fullmatch = $fullmatch;
        $entry->approved = 1;
        $entryid = $DB->insert_record('glossary_entries', $entry);
        $inserted++;
    }}

    // Sync aliases
    $DB->delete_records('glossary_alias', ['entryid' => $entryid]);
    foreach ($aliases as $alias) {{
        $a_text = trim($alias);
        if ($a_text !== '') {{
            $a = new stdClass();
            $a->entryid = $entryid;
            $a->alias = $a_text;
            $DB->insert_record('glossary_alias', $a);
        }}
    }}
}}

purge_all_caches();

echo json_encode([
    'status' => 'success',
    'inserted' => $inserted,
    'updated' => $updated,
], JSON_UNESCAPED_UNICODE);
"""
        push_res = self._exec(php_push)
        if push_res and push_res.get("status") == "success":
            print(f"✅ Успішно синхронізовано глосарій! Додано: {push_res.get('inserted')}, оновлено: {push_res.get('updated')}.")
            self.pull_glossary(gloss_cfg, force=True)
        else:
            print(f"❌ Помилка запису глосарію: {push_res}")

    # =========================================================================
    # ADD CHAPTER HELPER
    # =========================================================================

    def add_chapter(
        self,
        book_name: str,
        title: str,
        subchapter: int = 0,
        after_chapter: int | None = None,
        content: str = "<p>Новий розділ...</p>",
    ) -> None:
        book_cfgs = self.get_book_configs(book_name)
        if not book_cfgs:
            raise ValueError(f"Book '{book_name}' not found")
        b_cfg = book_cfgs[0]
        book_id = b_cfg["book_id"]

        print(f"➕ Створення розділу '{title}' у книзі '{b_cfg['name']}'...")
        res = moodle_client.add_book_chapter(
            book_id=book_id,
            title=title,
            subchapter=subchapter,
            after_chapter_id=after_chapter,
            content=content,
        )
        if res and res.get("status") == "success":
            print(f"✅ Успішно створено розділ #{res['chapter_id']} (стор. {res['pagenum']}) у Moodle!")
            self.pull_book(b_cfg, force=True)
        else:
            print(f"❌ Помилка створення розділу: {res}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Moodle Book & Glossary Sync Tool with Versioning")
    subparsers = parser.add_subparsers(dest="action", help="Action to execute")

    # Pull command
    pull_p = subparsers.add_parser("pull", help="Pull books or glossaries from Moodle")
    pull_p.add_argument("--book", type=str, default=None, help="Book name/ID or 'all' (default: all)")
    pull_p.add_argument("--glossary", type=str, default=None, help="Glossary name/ID or 'all'")
    pull_p.add_argument("--all", action="store_true", help="Pull all configured books and glossaries")
    pull_p.add_argument("--force", action="store_true", help="Force overwrite local files even if modified")
    pull_p.add_argument("--git-commit", action="store_true", help="Automatically commit pulled state to Git")

    # Push command
    push_p = subparsers.add_parser("push", help="Push local changes back to Moodle")
    push_p.add_argument("--book", type=str, default=None, help="Book name/ID to push")
    push_p.add_argument("--chapter", type=int, default=None, help="Specific chapter ID to push")
    push_p.add_argument("--glossary", type=str, default=None, help="Glossary name/ID to push")
    push_p.add_argument("--force", action="store_true", help="Force push even if remote was modified")
    push_p.add_argument("--dry-run", action="store_true", help="Preview changes without writing to Moodle")
    push_p.add_argument("--git-commit", action="store_true", help="Automatically commit pushed changes to Git")

    # Status command
    status_p = subparsers.add_parser("status", help="Show difference between local files and Moodle DB")
    status_p.add_argument("--book", type=str, default=None, help="Book name/ID (default: all)")

    # Diff command
    diff_p = subparsers.add_parser("diff", help="Show unified diff for a chapter")
    diff_p.add_argument("chapter", type=str, help="Chapter ID or filename to compare")

    # Add chapter command
    add_ch_p = subparsers.add_parser("add-chapter", help="Add a new chapter to a book")
    add_ch_p.add_argument("--book", type=str, required=True, help="Book name/ID")
    add_ch_p.add_argument("--title", type=str, required=True, help="Chapter title")
    add_ch_p.add_argument("--subchapter", type=int, choices=[0, 1], default=0, help="0=main, 1=subchapter")
    add_ch_p.add_argument("--after", type=int, default=None, help="Chapter ID to insert after")
    add_ch_p.add_argument("--content", type=str, default="<p>Текст розділу...</p>", help="Initial HTML content")

    # Backups command
    subparsers.add_parser("backups", help="List local timestamped snapshots in .sync_backups/")

    # Rollback command
    roll_p = subparsers.add_parser("rollback", help="Restore local files from a snapshot")
    roll_p.add_argument("snapshot", type=str, help="Snapshot folder name")

    # Commit command
    comm_p = subparsers.add_parser("commit", help="Commit local changes to Git")
    comm_p.add_argument("-m", "--message", type=str, default=None, help="Commit message")

    args = parser.parse_args()
    if not args.action:
        parser.print_help()
        sys.exit(0)

    mgr = MoodleSyncManager()

    if args.action == "pull":
        pull_all = args.all or (args.book is None and args.glossary is None)
        if pull_all or args.book:
            for b in mgr.get_book_configs(args.book):
                mgr.pull_book(b, force=args.force)
        if pull_all or args.glossary:
            for g in mgr.get_glossary_configs(args.glossary):
                mgr.pull_glossary(g, force=args.force)
        if args.git_commit:
            now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
            mgr.git_commit(f"sync(moodle): pull updates from Moodle ({now_str})")

    elif args.action == "push":
        if args.book:
            for b in mgr.get_book_configs(args.book):
                mgr.push_book(b, chapter_id=args.chapter, force=args.force, dry_run=args.dry_run)
        if args.glossary:
            for g in mgr.get_glossary_configs(args.glossary):
                mgr.push_glossary(g, dry_run=args.dry_run)
        if not args.book and not args.glossary:
            for b in mgr.get_book_configs():
                mgr.push_book(b, force=args.force, dry_run=args.dry_run)
            for g in mgr.get_glossary_configs():
                mgr.push_glossary(g, dry_run=args.dry_run)
        if args.git_commit and not args.dry_run:
            now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
            mgr.git_commit(f"sync(moodle): push changes to Moodle ({now_str})")

    elif args.action == "status":
        mgr.status(args.book)

    elif args.action == "diff":
        mgr.diff_chapter(args.chapter)

    elif args.action == "add-chapter":
        mgr.add_chapter(
            book_name=args.book,
            title=args.title,
            subchapter=args.subchapter,
            after_chapter=args.after,
            content=args.content,
        )

    elif args.action == "backups":
        mgr.list_backups()

    elif args.action == "rollback":
        mgr.rollback(args.snapshot)

    elif args.action == "commit":
        msg = args.message or f"sync(book): update content ({datetime.datetime.now().strftime('%Y-%m-%d %H:%M')})"
        mgr.git_commit(msg)


if __name__ == "__main__":
    main()
