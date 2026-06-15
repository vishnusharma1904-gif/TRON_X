"""
TRON-X File System Agent
─────────────────────────
Search, read, organize, rename, move, delete files.
All destructive ops require explicit 'confirm=True'.
"""
from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from typing import Optional

from src.core.logger import log


async def search_files(
    query: str,
    root: str = ".",
    extensions: Optional[list[str]] = None,
    max_results: int = 50,
) -> dict:
    """
    Search for files by name or content snippet.
    - query: filename substring OR 'content:<text>' prefix for content search
    - root: directory to search (default: cwd)
    - extensions: e.g. ['.py', '.txt'] to filter
    """
    root_path = Path(root).expanduser().resolve()
    is_content_search = query.lower().startswith("content:")
    search_term = query[8:].strip() if is_content_search else query.lower()

    results = []

    def _scan() -> list[dict]:
        hits = []
        for path in root_path.rglob("*"):
            if path.is_dir():
                continue
            if extensions and path.suffix.lower() not in extensions:
                continue

            if is_content_search:
                try:
                    text = path.read_text(errors="ignore")
                    if search_term.lower() in text.lower():
                        # find first occurrence line
                        for i, line in enumerate(text.splitlines(), 1):
                            if search_term.lower() in line.lower():
                                hits.append({
                                    "path": str(path),
                                    "line": i,
                                    "snippet": line.strip()[:120],
                                })
                                break
                except Exception:
                    pass
            else:
                if search_term in path.name.lower():
                    stat = path.stat()
                    hits.append({
                        "path": str(path),
                        "size": stat.st_size,
                        "modified": stat.st_mtime,
                    })

            if len(hits) >= max_results:
                break
        return hits

    loop = asyncio.get_event_loop()
    results = await loop.run_in_executor(None, _scan)
    log.info(f"[files] search '{query}' → {len(results)} hits")
    return {"query": query, "root": str(root_path), "results": results, "count": len(results)}


async def read_file(path: str, max_chars: int = 8000) -> dict:
    """Read a text file, return its content (truncated if large)."""
    p = Path(path).expanduser().resolve()
    if not p.exists():
        return {"success": False, "error": f"File not found: {path}"}
    try:
        text = p.read_text(errors="replace")
        truncated = len(text) > max_chars
        return {
            "success": True,
            "path": str(p),
            "content": text[:max_chars],
            "total_chars": len(text),
            "truncated": truncated,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


async def list_directory(path: str = ".", show_hidden: bool = False) -> dict:
    """List files and subdirectories in a path."""
    p = Path(path).expanduser().resolve()
    if not p.exists():
        return {"success": False, "error": f"Path not found: {path}"}

    items = []
    for entry in sorted(p.iterdir()):
        if not show_hidden and entry.name.startswith("."):
            continue
        stat = entry.stat()
        items.append({
            "name": entry.name,
            "type": "dir" if entry.is_dir() else "file",
            "size": stat.st_size if entry.is_file() else None,
            "modified": stat.st_mtime,
        })
    return {"path": str(p), "items": items, "count": len(items)}


async def rename_file(src: str, dst: str, confirm: bool = False) -> dict:
    """Rename or move a file. Requires confirm=True."""
    if not confirm:
        return {"success": False, "error": "Set confirm=True to perform rename"}
    src_p = Path(src).expanduser().resolve()
    dst_p = Path(dst).expanduser().resolve()
    if not src_p.exists():
        return {"success": False, "error": f"Source not found: {src}"}
    try:
        shutil.move(str(src_p), str(dst_p))
        log.info(f"[files] Rename: {src_p} → {dst_p}")
        return {"success": True, "from": str(src_p), "to": str(dst_p)}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def copy_file(src: str, dst: str, confirm: bool = False) -> dict:
    """Copy a file. Requires confirm=True."""
    if not confirm:
        return {"success": False, "error": "Set confirm=True to perform copy"}
    src_p = Path(src).expanduser().resolve()
    dst_p = Path(dst).expanduser().resolve()
    if not src_p.exists():
        return {"success": False, "error": f"Source not found: {src}"}
    try:
        dst_p.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src_p), str(dst_p))
        log.info(f"[files] Copy: {src_p} → {dst_p}")
        return {"success": True, "from": str(src_p), "to": str(dst_p)}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def delete_file(path: str, confirm: bool = False) -> dict:
    """Delete a file. Requires confirm=True."""
    if not confirm:
        return {"success": False, "error": "Set confirm=True to delete"}
    p = Path(path).expanduser().resolve()
    if not p.exists():
        return {"success": False, "error": f"Not found: {path}"}
    try:
        if p.is_dir():
            shutil.rmtree(str(p))
        else:
            p.unlink()
        log.info(f"[files] Deleted: {p}")
        return {"success": True, "deleted": str(p)}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def create_file(path: str, content: str = "") -> dict:
    """Create a file with optional content."""
    p = Path(path).expanduser().resolve()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        log.info(f"[files] Created: {p}")
        return {"success": True, "path": str(p)}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def create_directory(path: str) -> dict:
    """Create a directory (and parents)."""
    p = Path(path).expanduser().resolve()
    try:
        p.mkdir(parents=True, exist_ok=True)
        return {"success": True, "path": str(p)}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def get_disk_usage(path: str = ".") -> dict:
    """Return disk usage stats for a path."""
    p = Path(path).expanduser().resolve()
    if not p.exists():
        return {"success": False, "error": f"Not found: {path}"}
    try:
        usage = shutil.disk_usage(str(p))
        return {
            "path": str(p),
            "total_gb": round(usage.total / 1e9, 2),
            "used_gb":  round(usage.used  / 1e9, 2),
            "free_gb":  round(usage.free  / 1e9, 2),
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


# =============================================================================
# Phase 5 additions -- folder analysis, duplicates, batch ops, archives
# =============================================================================
import hashlib
import fnmatch
import zipfile


async def folder_summary(path: str) -> dict:
    """Size breakdown, extension counts, newest/oldest/largest file."""
    p = Path(path).expanduser().resolve()
    if not p.exists():
        return {"success": False, "error": f"Not found: {path}"}

    def _walk():
        total_files = 0
        total_dirs = 0
        total_size = 0
        by_ext: dict[str, int] = {}
        newest = oldest = largest = None

        for entry in p.rglob("*"):
            if entry.is_dir():
                total_dirs += 1
                continue
            try:
                stat = entry.stat()
            except Exception:
                continue
            total_files += 1
            total_size += stat.st_size
            ext = entry.suffix.lower() or "(none)"
            by_ext[ext] = by_ext.get(ext, 0) + 1

            info = {"path": str(entry), "modified": stat.st_mtime}
            if newest is None or stat.st_mtime > newest["modified"]:
                newest = info.copy()
            if oldest is None or stat.st_mtime < oldest["modified"]:
                oldest = info.copy()
            if largest is None or stat.st_size > largest["size_bytes"]:
                largest = {"path": str(entry), "size_bytes": stat.st_size}

        return {
            "success": True,
            "path": str(p),
            "total_files": total_files,
            "total_dirs": total_dirs,
            "total_size_mb": round(total_size / 1e6, 2),
            "by_extension": dict(sorted(by_ext.items(), key=lambda x: x[1], reverse=True)),
            "newest_file": newest,
            "oldest_file": oldest,
            "largest_file": largest,
        }

    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(None, _walk)
    except Exception as e:
        return {"success": False, "error": str(e)}


async def find_duplicates(root: str, extensions: Optional[list[str]] = None) -> dict:
    """MD5-based duplicate detection. Returns groups of identical files."""
    root_p = Path(root).expanduser().resolve()
    if not root_p.exists():
        return {"success": False, "error": f"Not found: {root}"}

    def _scan():
        hashes: dict[str, list[str]] = {}
        for entry in root_p.rglob("*"):
            if not entry.is_file():
                continue
            if extensions and entry.suffix.lower() not in extensions:
                continue
            try:
                md5 = hashlib.md5(entry.read_bytes()).hexdigest()
                hashes.setdefault(md5, []).append(str(entry))
            except Exception:
                continue

        groups = []
        wasted = 0
        for h, paths in hashes.items():
            if len(paths) < 2:
                continue
            size = Path(paths[0]).stat().st_size
            wasted += size * (len(paths) - 1)
            groups.append({"hash": h, "files": paths, "size_bytes": size})

        groups.sort(key=lambda g: g["size_bytes"] * len(g["files"]), reverse=True)
        return {"groups": groups, "total_groups": len(groups), "wasted_bytes": wasted}

    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(None, _scan)
    except Exception as e:
        return {"success": False, "error": str(e)}


async def rename_batch(
    root: str, pattern: str, template: str, confirm: bool = False
) -> dict:
    """Glob-pattern batch rename with template substitution. confirm=False = dry run."""
    root_p = Path(root).expanduser().resolve()
    if not root_p.exists():
        return {"success": False, "error": f"Not found: {root}"}

    matches = sorted(
        f for f in root_p.iterdir()
        if f.is_file() and fnmatch.fnmatch(f.name, pattern)
    )
    if not matches:
        return {"success": True, "count": 0, "renamed": [], "dry_run": not confirm}

    pairs = []
    for n, src in enumerate(matches, 1):
        new_name = (
            template
            .replace("{n}", str(n))
            .replace("{name}", src.stem)
            .replace("{ext}", src.suffix)
        )
        dst = root_p / new_name
        pairs.append((src, dst))

    # Collision check before touching anything
    targets = [dst for _, dst in pairs]
    for dst in targets:
        if dst.exists() and dst not in [src for src, _ in pairs]:
            return {"success": False, "error": f"Target already exists: {dst}"}

    plan = [{"from": str(s), "to": str(d)} for s, d in pairs]

    if not confirm:
        return {"dry_run": True, "count": len(plan), "renames": plan}

    def _do():
        done = []
        for src, dst in pairs:
            src.rename(dst)
            log.info(f"[files] Batch rename: {src.name} -> {dst.name}")
            done.append({"from": str(src), "to": str(dst)})
        return done

    loop = asyncio.get_event_loop()
    try:
        renamed = await loop.run_in_executor(None, _do)
        return {"success": True, "count": len(renamed), "renamed": renamed}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def organize_folder(root: str, confirm: bool = False) -> dict:
    """Sort files in root into category subfolders by extension."""
    _CATEGORIES = {
        "Images":    {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".svg"},
        "Videos":    {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv"},
        "Audio":     {".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a"},
        "Documents": {".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".txt", ".md"},
        "Code":      {".py", ".js", ".ts", ".html", ".css", ".json", ".yaml", ".toml"},
        "Archives":  {".zip", ".tar", ".gz", ".rar", ".7z"},
    }

    def _ext_to_category(ext: str) -> str:
        for cat, exts in _CATEGORIES.items():
            if ext in exts:
                return cat
        return "Other"

    root_p = Path(root).expanduser().resolve()
    if not root_p.exists():
        return {"success": False, "error": f"Not found: {root}"}

    files = [
        f for f in root_p.iterdir()
        if f.is_file() and not f.name.startswith(".")
    ]

    plan = [
        {
            "from": str(f),
            "to": str(root_p / _ext_to_category(f.suffix.lower()) / f.name),
            "category": _ext_to_category(f.suffix.lower()),
        }
        for f in files
    ]

    if not confirm:
        return {"dry_run": True, "count": len(plan), "moves": plan}

    def _do():
        done = []
        for item in plan:
            src = Path(item["from"])
            dst = Path(item["to"])
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            log.info(f"[files] Organize: {src.name} -> {item['category']}/")
            done.append(item)
        return done

    loop = asyncio.get_event_loop()
    try:
        moved = await loop.run_in_executor(None, _do)
        return {"success": True, "count": len(moved), "moved": moved}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def create_archive(sources: list[str], dest: str, confirm: bool = False) -> dict:
    """Zip files/folders into dest. confirm=False = dry run."""
    resolved = []
    for s in sources:
        p = Path(s).expanduser().resolve()
        if not p.exists():
            return {"success": False, "error": f"Source not found: {s}"}
        if p.is_dir():
            for f in p.rglob("*"):
                if f.is_file():
                    resolved.append(f)
        else:
            resolved.append(p)

    if not confirm:
        return {"dry_run": True, "files_to_archive": [str(f) for f in resolved], "count": len(resolved), "dest": dest}

    dest_p = Path(dest).expanduser().resolve()

    def _zip():
        dest_p.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(dest_p, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for f in resolved:
                zf.write(f, arcname=f.name)
        return dest_p.stat().st_size

    loop = asyncio.get_event_loop()
    try:
        size = await loop.run_in_executor(None, _zip)
        log.info(f"[files] Archive created: {dest_p} ({len(resolved)} files)")
        return {"success": True, "dest": str(dest_p), "files_added": len(resolved), "size_bytes": size}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def extract_archive(src: str, dest: str, confirm: bool = False) -> dict:
    """Extract a .zip archive. confirm=False = list contents only."""
    src_p = Path(src).expanduser().resolve()
    if not src_p.exists():
        return {"success": False, "error": f"Not found: {src}"}
    if not zipfile.is_zipfile(src_p):
        return {"success": False, "error": f"Not a valid zip file: {src}"}

    def _list():
        with zipfile.ZipFile(src_p, "r") as zf:
            return zf.namelist()

    def _extract():
        dest_p = Path(dest).expanduser().resolve()
        dest_p.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(src_p, "r") as zf:
            names = zf.namelist()
            zf.extractall(str(dest_p))
        return names, str(dest_p)

    loop = asyncio.get_event_loop()
    try:
        if not confirm:
            names = await loop.run_in_executor(None, _list)
            return {"dry_run": True, "files": names, "count": len(names)}
        names, dest_str = await loop.run_in_executor(None, _extract)
        log.info(f"[files] Extracted {len(names)} files to {dest_str}")
        return {"success": True, "dest": dest_str, "extracted": len(names)}
    except Exception as e:
        return {"success": False, "error": str(e)}
