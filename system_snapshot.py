import os
import random
import shutil
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

WATCHED_FOLDERS = [
    Path.home() / "Downloads",
    Path.home() / "Desktop",
    Path.home() / "Documents",
]

SAMPLE_NAME_COUNT = 10

_EXT_CATEGORIES = {
    "image": {".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".svg", ".bmp"},
    "video": {".mp4", ".mov", ".avi", ".mkv", ".m4v", ".wmv"},
    "audio": {".mp3", ".wav", ".aac", ".flac", ".m4a"},
    "archive": {".zip", ".dmg", ".pkg", ".tar", ".gz", ".rar", ".7z"},
    "pdf": {".pdf"},
    "doc": {".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".pages", ".numbers", ".key"},
    "code": {".py", ".js", ".ts", ".json", ".yaml", ".yml", ".sh", ".rb", ".go", ".rs"},
}


def _categorize(ext: str) -> str:
    ext = ext.lower()
    for cat, exts in _EXT_CATEGORIES.items():
        if ext in exts:
            return cat
    return "other"


def _folder_snapshot(path: Path) -> dict:
    if not path.exists():
        return {"exists": False}

    now = datetime.now(timezone.utc).timestamp()
    files = []
    try:
        entries = list(path.iterdir())
    except PermissionError:
        return {"exists": True, "error": "permission denied"}

    subdir_count = sum(1 for e in entries if e.is_dir())
    for e in entries:
        if not e.is_file():
            continue
        try:
            stat = e.stat()
        except OSError:
            continue
        age_days = int((now - stat.st_mtime) / 86400)
        size_mb = stat.st_size / (1024 * 1024)
        files.append({
            "name": e.name,
            "ext": e.suffix.lower(),
            "age_days": age_days,
            "size_mb": round(size_mb, 2),
        })

    if not files:
        return {"exists": True, "file_count": 0, "subdir_count": subdir_count}

    type_counts: dict[str, int] = defaultdict(int)
    for f in files:
        type_counts[_categorize(f["ext"])] += 1

    ages = [f["age_days"] for f in files]
    age_buckets = {
        "0-7d":  sum(1 for a in ages if a <= 7),
        "7-30d": sum(1 for a in ages if 7 < a <= 30),
        "30-90d": sum(1 for a in ages if 30 < a <= 90),
        "90d+":  sum(1 for a in ages if a > 90),
    }

    largest = sorted(files, key=lambda f: -f["size_mb"])[:5]
    largest_files = [{"name": f["name"], "size_mb": f["size_mb"], "age_days": f["age_days"]} for f in largest]

    sample = random.sample(files, min(SAMPLE_NAME_COUNT, len(files)))
    sample_names = [f["name"] for f in sample]

    return {
        "exists": True,
        "file_count": len(files),
        "subdir_count": subdir_count,
        "type_breakdown": dict(type_counts),
        "age_buckets": age_buckets,
        "largest_files": largest_files,
        "sample_names": sample_names,
    }


def _disk_snapshot() -> dict:
    usage = shutil.disk_usage("/")
    used_gb = round(usage.used / (1024 ** 3), 1)
    total_gb = round(usage.total / (1024 ** 3), 1)
    pct = round(usage.used / usage.total * 100, 1)

    # Top-level home dirs by size (best-effort, non-recursive du equivalent)
    largest_dirs = []
    home = Path.home()
    candidates = ["Movies", "Downloads", "Documents", "Library", "Desktop"]
    for name in candidates:
        p = home / name
        if not p.exists():
            continue
        try:
            total = sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
            largest_dirs.append({"path": f"~/{name}", "size_gb": round(total / (1024 ** 3), 2)})
        except (PermissionError, OSError):
            pass
    largest_dirs.sort(key=lambda d: -d["size_gb"])

    return {
        "used_gb": used_gb,
        "total_gb": total_gb,
        "used_pct": pct,
        "largest_dirs": largest_dirs[:5],
    }


def _memory_snapshot() -> dict:
    try:
        import psutil
        vm = psutil.virtual_memory()
        swap = psutil.swap_memory()
        used_gb = round(vm.used / (1024 ** 3), 1)
        total_gb = round(vm.total / (1024 ** 3), 1)
        swap_used_gb = round(swap.used / (1024 ** 3), 1)
        pct = vm.percent
        if pct >= 90:
            pressure = "critical"
        elif pct >= 75:
            pressure = "high"
        elif pct >= 60:
            pressure = "moderate"
        else:
            pressure = "normal"
        return {
            "used_gb": used_gb,
            "total_gb": total_gb,
            "used_pct": pct,
            "swap_used_gb": swap_used_gb,
            "pressure": pressure,
        }
    except ImportError:
        return {"error": "psutil not installed"}


def build() -> dict:
    folders = {}
    for p in WATCHED_FOLDERS:
        folders[f"~/{p.name}"] = _folder_snapshot(p)

    return {
        "disk": _disk_snapshot(),
        "folders": folders,
        "memory": _memory_snapshot(),
    }
