"""Shared helper functions for logging, hashing, and file/path handling."""

from __future__ import annotations

import hashlib
import unicodedata
from datetime import datetime
from pathlib import Path


def ensure_directory(path: Path) -> Path:
    """Create a directory if it does not already exist."""

    path.mkdir(parents=True, exist_ok=True)
    return path


def compute_sha256(file_path: Path, chunk_size: int = 65_536) -> str:
    """Compute the SHA-256 hash of a file on disk."""

    digest = hashlib.sha256()

    with file_path.open("rb") as file_handle:
        while True:
            chunk = file_handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)

    return digest.hexdigest()


def format_file_size(num_bytes: int) -> str:
    """Convert a raw byte count into a human-readable size string."""

    size = float(num_bytes)
    units = ["B", "KB", "MB", "GB", "TB"]

    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.2f} {unit}"
        size /= 1024

    return f"{num_bytes} B"


def safe_filename(filename: str) -> str:
    """Keep only the final filename component to avoid path traversal."""

    clean_name = Path(filename).name.strip()

    if clean_name in {"", ".", ".."}:
        raise ValueError("A valid filename is required.")

    return clean_name


def strip_surrounding_quotes(text: str) -> str:
    """Remove one matching pair of wrapping quotes from user input."""

    cleaned_text = text.strip()

    if (
        len(cleaned_text) >= 2
        and cleaned_text[0] == cleaned_text[-1]
        and cleaned_text[0] in {'"', "'"}
    ):
        return cleaned_text[1:-1].strip()

    return cleaned_text


def normalize_text_for_matching(text: str) -> str:
    """Normalize user-entered text so Unicode whitespace mismatches do not matter."""

    normalized_text = unicodedata.normalize("NFKC", text)
    normalized_spaces = "".join(
        " " if character.isspace() else character for character in normalized_text
    )
    return " ".join(normalized_spaces.split())


def unique_path_for_file(directory: Path, filename: str) -> Path:
    """Return a non-conflicting file path by appending a counter if needed."""

    ensure_directory(directory)
    candidate = directory / filename

    if not candidate.exists():
        return candidate

    base_path = Path(filename)
    counter = 1

    while True:
        candidate = directory / f"{base_path.stem}_{counter}{base_path.suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def build_file_listing(directory: Path) -> list[dict[str, int | str]]:
    """Return a sorted list of visible files in a directory."""

    ensure_directory(directory)
    files: list[dict[str, int | str]] = []

    for path in sorted(directory.iterdir(), key=lambda item: item.name.lower()):
        if path.is_file() and not path.name.startswith("."):
            files.append({"filename": path.name, "size": path.stat().st_size})

    return files


def remove_file_if_exists(file_path: Path) -> None:
    """Delete a file if it exists, ignoring missing-file errors."""

    try:
        file_path.unlink()
    except FileNotFoundError:
        pass


def format_endpoint(address: tuple[str, int]) -> str:
    """Format a socket address for readable logs."""

    host, port = address
    return f"{host}:{port}"


def log_event(source: str, message: str) -> None:
    """Print a timestamped log message."""

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] [{source}] {message}")
