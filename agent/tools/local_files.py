import logging
import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from agents import function_tool

logger = logging.getLogger(__name__)

DENY_SUBSTRINGS = (
    ".ssh/", ".aws/", ".gnupg/", ".config/", "Library/",
    "id_rsa", "credentials", "secret", "token",
    ".env", ".kdbx", ".key", ".pem",
)
MAX_READ_BYTES = 200_000


def _search_root() -> Path | None:
    raw = os.environ.get("LOCAL_SEARCH_ROOT")
    if not raw:
        return None
    try:
        root = Path(raw).expanduser().resolve()
    except OSError:
        return None
    return root if root.is_dir() else None


def _is_allowed(path: Path, root: Path) -> bool:
    try:
        resolved = path.resolve()
        resolved.relative_to(root)
    except (ValueError, OSError):
        return False
    s = str(resolved)
    return not any(d in s for d in DENY_SUBSTRINGS)


def _read_text(p: Path) -> str:
    if p.suffix.lower() == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            return f"Refused: pypdf not installed; cannot read PDF {p}."
        try:
            reader = PdfReader(str(p))
        except Exception as e:
            return f"Refused: failed to open PDF {p}: {e}"
        chunks: list[str] = []
        total = 0
        for page in reader.pages:
            try:
                text = page.extract_text() or ""
            except Exception:
                text = ""
            chunks.append(text)
            total += len(text)
            if total >= MAX_READ_BYTES:
                break
        return "\n".join(chunks)[:MAX_READ_BYTES]
    data = p.read_bytes()[:MAX_READ_BYTES]
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return f"Refused: {p} is not a UTF-8 text file."


def local_file_tools() -> list[Any]:
    """Return Spotlight-backed search/read tools, or [] if LOCAL_SEARCH_ROOT is unset."""
    root = _search_root()
    if root is None:
        return []

    @function_tool
    def search_local_files(query: str) -> list[dict]:
        """Spotlight-search the user's local files for paths matching `query`.

        `query` is a Spotlight query string — plain words match filename + content,
        or use raw mdfind syntax (e.g. 'kind:pdf "Q3 invoice"',
        'kMDItemFSName == "*.md"'). Searches only inside the configured
        LOCAL_SEARCH_ROOT, with sensitive paths (ssh/aws keys, .env, etc.)
        filtered out. Returns up to 20 results with path, size, and modified time.
        Use read_local_file(path) to read a specific file.
        """
        try:
            out = subprocess.run(
                ["mdfind", "-onlyin", str(root), query],
                capture_output=True, text=True, timeout=10,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            return [{"error": f"mdfind failed: {e}"}]
        results = []
        for line in out.stdout.splitlines():
            p = Path(line)
            if not _is_allowed(p, root) or not p.is_file():
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            results.append({
                "path": str(p),
                "size": st.st_size,
                "modified": datetime.fromtimestamp(st.st_mtime).isoformat(),
            })
            if len(results) >= 20:
                break
        return results

    @function_tool
    def read_local_file(path: str) -> str:
        """Read a local file (text or PDF) and return up to 200KB of text.
        Only paths inside LOCAL_SEARCH_ROOT and not on the deny-list are allowed.
        Use search_local_files first to find the path.
        """
        p = Path(path).expanduser()
        if not _is_allowed(p, root) or not p.is_file():
            return f"Refused: {path} is outside the allowed root or is on the deny-list."
        try:
            return _read_text(p)
        except Exception as e:
            logger.exception("read_local_file failed for %s", path)
            return f"Refused: failed to read {path}: {e}"

    return [search_local_files, read_local_file]
