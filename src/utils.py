"""Shared helper functions for logging, hashing, and file/path handling."""

from __future__ import annotations

import hashlib
import unicodedata
from datetime import datetime
from pathlib import Path


def ensure_directory(path: Path) -> Path:
    """
    Ensures that a given directory path exists. If the directory does not exist,
    it is created along with any necessary parent directories.

    Args:
        path (Path): The pathlib.Path object representing the directory to ensure.

    Returns:
        Path: The pathlib.Path object of the ensured directory.
    """

    path.mkdir(parents=True, exist_ok=True)
    return path


def compute_sha256(file_path: Path, chunk_size: int = 65_536) -> str:
    """
    Computes the SHA-256 hash of a file located on disk.
    Reads the file in chunks to efficiently handle large files.

    Args:
        file_path (Path): The pathlib.Path object of the file to hash.
        chunk_size (int): The size of chunks (in bytes) to read from the file.
                          Defaults to 65,536 bytes (64 KB).

    Returns:
        str: The hexadecimal representation of the file's SHA-256 hash.
    """

    digest = hashlib.sha256() # Initialize SHA-256 hash object

    with file_path.open("rb") as file_handle: # Open file in binary read mode
        while True:
            chunk = file_handle.read(chunk_size) # Read file in specified chunk sizes
            if not chunk:
                break # Exit loop when end of file is reached
            digest.update(chunk) # Update the hash with the current chunk

    return digest.hexdigest() # Return the hexadecimal representation of the hash


def format_file_size(num_bytes: int) -> str:
    """
    Converts a raw byte count into a human-readable string representation
    using appropriate units (B, KB, MB, GB, TB).

    Args:
        num_bytes (int): The number of bytes to format.

    Returns:
        str: A human-readable string (e.g., "1.23 MB", "500 B").
    """

    size = float(num_bytes)
    units = ["B", "KB", "MB", "GB", "TB"] # Define standard units

    for unit in units:
        # Check if size is less than 1024 or if it's the largest unit
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}" # Return integer for bytes
            return f"{size:.2f} {unit}"     # Return with 2 decimal places for larger units
        size /= 1024 # Convert to next larger unit

    return f"{num_bytes} B" # Fallback if loop finishes unexpectedly


def safe_filename(filename: str) -> str:
    """
    Extracts a safe filename from a given path, preventing directory traversal
    by returning only the final path component.

    Args:
        filename (str): The potentially unsafe filename string.

    Returns:
        str: A safe filename string suitable for file system operations.

    Raises:
        ValueError: If the cleaned filename is empty or represents a directory
                    traversal attempt (e.g., ".", "..").
    """

    clean_name = Path(filename).name.strip() # Extract only the filename component

    # Prevent path traversal attempts by checking for reserved names
    if clean_name in {"", ".", ".."}:
        raise ValueError("A valid filename is required.")

    return clean_name


def strip_surrounding_quotes(text: str) -> str:
    """
    Removes a single matching pair of surrounding single or double quotes
    from the given string, if present. Leading/trailing whitespace is also stripped.

    Args:
        text (str): The input string, potentially with surrounding quotes.

    Returns:
        str: The string with one layer of surrounding quotes removed, if they existed.
    """

    cleaned_text = text.strip() # Remove leading/trailing whitespace first

    # Check if the string has at least two characters,
    # starts and ends with the same quote, and is a quote
    if (
        len(cleaned_text) >= 2
        and cleaned_text[0] == cleaned_text[-1]
        and cleaned_text[0] in {'"', "'"}
    ):
        return cleaned_text[1:-1].strip() # Remove quotes and re-strip whitespace

    return cleaned_text


def normalize_text_for_matching(text: str) -> str:
    """
    Normalizes a string for robust matching, typically for user input.
    It performs the following operations:
    1. Applies Unicode Normalization Form KC (NFKC) to handle compatible characters.
    2. Replaces all whitespace characters with standard spaces.
    3. Collapses multiple spaces into a single space and strips leading/trailing spaces.

    Args:
        text (str): The input string to normalize.

    Returns:
        str: The normalized string, suitable for case-insensitive and
             whitespace-agnostic comparisons.
    """

    normalized_text = unicodedata.normalize("NFKC", text) # Apply Unicode normalization
    # Replace all whitespace characters (tabs, newlines, etc.) with single spaces
    normalized_spaces = "".join(
        " " if character.isspace() else character for character in normalized_text
    )
    # Split by space and join to collapse multiple spaces into one, then strip
    return " ".join(normalized_spaces.split())


def unique_path_for_file(directory: Path, filename: str) -> Path:
    """
    Generates a unique file path within a specified directory.
    If a file with the given filename already exists, it appends a counter
    (e.g., "filename_1.txt", "filename_2.txt") to create a non-conflicting path.

    Args:
        directory (Path): The target directory where the file will be saved.
        filename (str): The desired filename.

    Returns:
        Path: A pathlib.Path object representing a unique, non-existent file path.
    """

    ensure_directory(directory) # Ensure the target directory exists
    candidate = directory / filename # Start with the desired filename

    if not candidate.exists():
        return candidate # If name is unique, use it

    base_path = Path(filename)
    counter = 1

    while True: # Loop to find an available name
        # Append counter before suffix: filename_1.ext, filename_2.ext
        candidate = directory / f"{base_path.stem}_{counter}{base_path.suffix}"
        if not candidate.exists():
            return candidate # Found a unique name
        counter += 1


def build_file_listing(directory: Path) -> list[dict[str, int | str]]:
    """
    Builds a list of visible files (not starting with '.') in a given directory,
    sorted alphabetically by filename.

    Args:
        directory (Path): The pathlib.Path object of the directory to scan.

    Returns:
        list[dict[str, int | str]]: A list of dictionaries, where each dictionary
                                    contains 'filename' (str) and 'size' (int) for a file.
    """

    ensure_directory(directory)
    files: list[dict[str, int | str]] = []

    # Iterate through directory contents, sorted by lowercase name
    for path in sorted(directory.iterdir(), key=lambda item: item.name.lower()):
        # Only include regular files and exclude hidden files (starting with '.')
        if path.is_file() and not path.name.startswith("."):
            files.append({"filename": path.name, "size": path.stat().st_size})

    return files


def remove_file_if_exists(file_path: Path) -> None:
    """
    Deletes a file from the file system if it exists.
    Handles FileNotFoundError gracefully, so no error is raised if the file
    is already absent.

    Args:
        file_path (Path): The pathlib.Path object of the file to remove.
    """

    try:
        file_path.unlink() # Attempt to delete the file
    except FileNotFoundError: # If file doesn't exist, do nothing
        pass


def format_endpoint(address: tuple[str, int]) -> str:
    """
    Formats a socket address (host, port) tuple into a human-readable string.

    Args:
        address (tuple[str, int]): A tuple containing the host IP/name (str)
                                   and port number (int).

    Returns:
        str: A formatted string like "host:port".
    """

    host, port = address
    return f"{host}:{port}"


def log_event(source: str, message: str) -> None:
    """
    Prints a timestamped log message to the console.

    Args:
        source (str): The source of the log event (e.g., "CLIENT", "SERVER").
        message (str): The main content of the log message.
    """

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] [{source}] {message}")
