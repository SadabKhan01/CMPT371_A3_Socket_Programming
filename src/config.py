"""Central configuration values for the SocketShare project."""

from pathlib import Path

# Resolve all important folders relative to the assignment root so the project
# is easy to run after cloning the repository anywhere on disk.
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Default network settings for local testing on one machine.
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5001

# A 4 KB buffer keeps the code simple and is large enough for smooth local demos.
BUFFER_SIZE = 4096

# Paths used by the server and client for storing transferred files.
STORAGE_PATH = PROJECT_ROOT / "storage" / "uploads"
DOWNLOADS_PATH = PROJECT_ROOT / "downloads"

