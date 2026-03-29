"""SocketShare TCP client with a simple interactive command-line menu."""

from __future__ import annotations

import argparse
import re
import socket
from pathlib import Path
from typing import Any

from config import BUFFER_SIZE, DEFAULT_HOST, DEFAULT_PORT, DOWNLOADS_PATH
from protocol import (
    ConnectionClosedError,
    ProtocolError,
    receive_file_bytes,
    receive_json,
    send_file_bytes,
    send_json,
)
from utils import (
    compute_sha256,
    ensure_directory,
    format_file_size,
    normalize_text_for_matching,
    remove_file_if_exists,
    safe_filename,
    strip_surrounding_quotes,
    unique_path_for_file,
)


class SocketShareClient:
    """Interactive TCP client used to talk to the SocketShare server."""

    def __init__(self, host: str, port: int, buffer_size: int) -> None:
        self.host = host
        self.port = port
        self.buffer_size = buffer_size
        self.client_socket: socket.socket | None = None
        self.last_listed_files: list[dict[str, Any]] = []

    def connect(self) -> bool:
        """Connect to the server and report success or failure."""

        self.client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

        try:
            self.client_socket.connect((self.host, self.port))
        except OSError as exc:
            print(f"Could not connect to {self.host}:{self.port}: {exc}")
            self.client_socket.close()
            self.client_socket = None
            return False

        print(f"Connected to SocketShare server at {self.host}:{self.port}")
        self.print_help()
        return True

    def run(self) -> None:
        """Main interactive loop for the CLI menu."""

        if self.client_socket is None and not self.connect():
            return

        try:
            while True:
                self.print_menu()
                choice = input("Select an option: ").strip().upper()

                if choice in {"1", "LIST"}:
                    self.list_files()
                elif choice in {"2", "UPLOAD"}:
                    file_path = input("Enter the local file path to upload: ").strip()
                    self.upload_file(file_path)
                elif choice in {"3", "DOWNLOAD"}:
                    filename = input(
                        "Enter the file number or server filename to download: "
                    ).strip()
                    self.download_file(filename)
                elif choice in {"4", "HELP"}:
                    self.print_help()
                elif choice in {"5", "QUIT", "EXIT"}:
                    self.quit()
                    break
                else:
                    print("Invalid option. Type 4 for help.")

                if self.client_socket is None:
                    print("Client session ended because the server connection is no longer active.")
                    break
        except KeyboardInterrupt:
            print("\nKeyboard interrupt received. Closing client.")
            self.quit()

    def list_files(self) -> None:
        """Request the server's file list and display it."""

        response = self.fetch_file_listing()
        if response is None:
            return

        files = response.get("files", [])
        message = response.get("message", "")
        print(message)

        if not files:
            return

        print("Available files:")
        for index, file_info in enumerate(files, start=1):
            filename = str(file_info.get("filename", "unknown"))
            size = int(file_info.get("size", 0))
            print(f"  {index}. {filename} ({format_file_size(size)})")

        print("Tip: For DOWNLOAD, you can enter the file number or the exact filename.")

    def upload_file(self, file_path_text: str) -> None:
        """Upload one local file to the server."""

        cleaned_input = strip_surrounding_quotes(file_path_text)
        local_path = Path(cleaned_input).expanduser()

        if not local_path.is_file():
            print(f"Local file not found: {local_path}")
            return

        file_size = local_path.stat().st_size
        file_hash = compute_sha256(local_path)

        response = self.send_request(
            {
                "type": "UPLOAD",
                "filename": local_path.name,
                "filesize": file_size,
                "sha256": file_hash,
            }
        )

        if response is None:
            return

        if response.get("type") != "READY" or response.get("status") != "OK":
            print(response.get("message", "Server refused the upload."))
            return

        try:
            send_file_bytes(self.require_socket(), local_path, self.buffer_size)
            result = receive_json(self.require_socket())
        except OSError as exc:
            print(f"Could not read the local file for upload: {exc}")
            return
        except (ConnectionClosedError, ProtocolError) as exc:
            self.close_local_socket()
            print(f"Upload failed because the connection was lost: {exc}")
            return

        if result.get("status") != "OK":
            print(result.get("message", "Server reported an upload error."))
            return

        saved_name = str(result.get("filename", local_path.name))
        saved_size = int(result.get("filesize", file_size))
        print(f"Upload complete: {saved_name} ({format_file_size(saved_size)})")
        print(f"Integrity verified by server: {result.get('sha256', 'unknown')}")

    def download_file(self, filename_text: str) -> None:
        """Download one server-side file into the local downloads directory."""

        resolved_filename = self.resolve_server_filename(filename_text)

        if resolved_filename is None:
            return

        try:
            requested_name = safe_filename(resolved_filename)
        except ValueError as exc:
            print(exc)
            return

        response = self.send_request({"type": "DOWNLOAD", "filename": requested_name})
        if response is None:
            return

        if response.get("type") != "DOWNLOAD_READY" or response.get("status") != "OK":
            print(response.get("message", "Server could not start the download."))
            return

        expected_size = response.get("filesize")
        expected_hash = response.get("sha256")
        server_filename = str(response.get("filename", requested_name))

        if not isinstance(expected_size, int) or expected_size < 0:
            print("Server returned an invalid file size.")
            return

        if not isinstance(expected_hash, str) or len(expected_hash) != 64:
            print("Server returned an invalid SHA-256 hash.")
            return

        downloads_directory = ensure_directory(DOWNLOADS_PATH)
        destination_path = unique_path_for_file(downloads_directory, server_filename)

        try:
            received_hash = receive_file_bytes(
                self.require_socket(),
                destination_path,
                expected_size,
                self.buffer_size,
            )
        except (ConnectionClosedError, ProtocolError) as exc:
            self.close_local_socket()
            remove_file_if_exists(destination_path)
            print(f"Download failed because the connection was lost: {exc}")
            return
        except OSError as exc:
            remove_file_if_exists(destination_path)
            print(f"Could not save the downloaded file: {exc}")
            return

        if received_hash != expected_hash:
            remove_file_if_exists(destination_path)
            print("Integrity verification failed after download. The partial file was removed.")
            return

        print(f"Download complete: {destination_path}")
        print(f"Size: {format_file_size(expected_size)}")
        print(f"Integrity verified: {received_hash}")

    def quit(self) -> None:
        """Disconnect from the server cleanly."""

        if self.client_socket is None:
            return

        try:
            send_json(self.client_socket, {"type": "QUIT"})
            response = receive_json(self.client_socket)
            print(response.get("message", "Disconnected from server."))
        except (ConnectionClosedError, ProtocolError):
            print("Connection closed.")
        finally:
            self.close_local_socket()

    def send_request(self, request: dict[str, Any]) -> dict[str, Any] | None:
        """Send one request and return the server's immediate response."""

        try:
            send_json(self.require_socket(), request)
            response = receive_json(self.require_socket())
        except ConnectionClosedError as exc:
            self.close_local_socket()
            print(f"Connection lost: {exc}")
            return None
        except ProtocolError as exc:
            self.close_local_socket()
            print(f"Protocol error: {exc}")
            return None

        if response.get("status") == "ERROR":
            print(response.get("message", "The server returned an error."))

        return response

    def fetch_file_listing(self) -> dict[str, Any] | None:
        """Fetch the server file list and cache it for later download selection."""

        response = self.send_request({"type": "LIST"})
        if response is None or response.get("status") != "OK":
            return response

        files = response.get("files", [])
        if isinstance(files, list):
            self.last_listed_files = files
        else:
            self.last_listed_files = []

        return response

    def resolve_server_filename(self, filename_text: str) -> str | None:
        """Resolve a download target from a number, display line, or filename."""

        cleaned_input = strip_surrounding_quotes(filename_text)

        if not cleaned_input:
            print("Please enter a file number or filename.")
            return None

        files = self.last_listed_files
        if not files:
            response = self.fetch_file_listing()
            if response is None:
                return None
            files = self.last_listed_files

        if re.fullmatch(r"\d+", cleaned_input):
            selection = int(cleaned_input)
            if 1 <= selection <= len(files):
                return str(files[selection - 1].get("filename", ""))

            print(f"File number {selection} is out of range. Run LIST to see valid numbers.")
            return None

        numbered_line_match = re.match(r"^\s*(\d+)\.\s+.+$", cleaned_input)
        if numbered_line_match:
            selection = int(numbered_line_match.group(1))
            if 1 <= selection <= len(files):
                return str(files[selection - 1].get("filename", ""))

            print(f"File number {selection} is out of range. Run LIST to see valid numbers.")
            return None

        if not files:
            return cleaned_input

        display_line_map: dict[str, str] = {}
        normalized_name_map: dict[str, list[str]] = {}

        for index, file_info in enumerate(files, start=1):
            filename = str(file_info.get("filename", ""))
            size = int(file_info.get("size", 0))
            display_line = f"{index}. {filename} ({format_file_size(size)})"
            display_line_map[normalize_text_for_matching(display_line)] = filename
            normalized_name_map.setdefault(
                normalize_text_for_matching(filename), []
            ).append(filename)

        normalized_input = normalize_text_for_matching(cleaned_input)

        if normalized_input in display_line_map:
            return display_line_map[normalized_input]

        matched_filenames = normalized_name_map.get(normalized_input, [])
        if len(matched_filenames) == 1:
            return matched_filenames[0]

        if len(matched_filenames) > 1:
            print("Multiple files matched that name. Please use the file number from LIST.")
            return None

        return cleaned_input

    def require_socket(self) -> socket.socket:
        """Return the active socket or raise a runtime error."""

        if self.client_socket is None:
            raise RuntimeError("Client is not connected to a server.")
        return self.client_socket

    def close_local_socket(self) -> None:
        """Close the local socket object and reset the client state."""

        if self.client_socket is None:
            return

        try:
            self.client_socket.close()
        except OSError:
            pass

        self.client_socket = None

    @staticmethod
    def print_menu() -> None:
        """Display the numbered main menu."""

        print()
        print("SocketShare Menu")
        print("1) LIST files on server")
        print("2) UPLOAD local file to server")
        print("3) DOWNLOAD file from server")
        print("4) HELP")
        print("5) QUIT")

    @staticmethod
    def print_help() -> None:
        """Display supported commands and what they do."""

        print()
        print("Supported commands:")
        print("  1 or LIST     - Show files currently stored on the server")
        print("  2 or UPLOAD   - Upload a local file path to the server")
        print("  3 or DOWNLOAD - Download a server file by number or exact filename")
        print("  4 or HELP     - Show this help message")
        print("  5 or QUIT     - Disconnect from the server")


def parse_arguments() -> argparse.Namespace:
    """Parse command-line arguments for the client."""

    parser = argparse.ArgumentParser(description="Start the SocketShare TCP client.")
    parser.add_argument(
        "--host", default=DEFAULT_HOST, help="Server host address to connect to."
    )
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help="Server TCP port."
    )
    return parser.parse_args()


def main() -> int:
    """Program entry point."""

    arguments = parse_arguments()
    client = SocketShareClient(arguments.host, arguments.port, BUFFER_SIZE)
    client.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
