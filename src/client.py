"""SocketShare TCP client with a simple interactive command-line menu."""

from __future__ import annotations

import argparse
import re
import socket
from collections.abc import Callable
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
    """
    Interactive TCP client used to communicate with the SocketShare server.
    Manages connections, file listing, uploading, and downloading.
    """

    def __init__(
        self,
        host: str,
        port: int,
        buffer_size: int,
        message_handler: Callable[[str], None] | None = None,
    ) -> None:
        """
        Initializes the SocketShareClient.

        Args:
            host (str): The hostname or IP address of the server to connect to.
            port (int): The port number of the server to connect to.
            buffer_size (int): The size of the buffer used for sending/receiving data.
        """
        self.host = host
        self.port = port
        self.buffer_size = buffer_size
        self.client_socket: socket.socket | None = None
        self.last_listed_files: list[dict[str, Any]] = []
        self.message_handler = message_handler or print

    def emit(self, message: str = "") -> None:
        """Sends a user-facing message to the active UI."""

        self.message_handler(message)

    def set_message_handler(self, message_handler: Callable[[str], None]) -> None:
        """Replaces the active message handler used for user-facing output."""

        self.message_handler = message_handler

    def connect(self, show_help: bool = True) -> bool:
        """
        Establishes a connection to the SocketShare server.

        Returns:
            bool: True if the connection was successful, False otherwise.
        """

        self.client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

        try:
            self.client_socket.connect((self.host, self.port))
        except OSError as exc:  # Catch network-related errors during connection
            self.emit(f"Could not connect to {self.host}:{self.port}: {exc}")
            self.client_socket.close()  # Close the socket on connection failure
            self.client_socket = None    # Reset socket to indicate no active connection
            return False

        self.emit(f"Connected to SocketShare server at {self.host}:{self.port}")
        if show_help:
            self.print_help()
        return True

    def run(self) -> None:
        """
        Main interactive loop for the client's command-line interface.
        Handles user input and dispatches to appropriate methods.
        """

        if self.client_socket is None and not self.connect():
            return  # Exit if initial connection fails

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
                    self.quit()  # Perform graceful disconnect
                    break        # Exit the main loop
                else:
                    self.emit("Invalid option. Type 4 for help.")

                if self.client_socket is None:  # Check if connection was closed
                    self.emit(
                        "Client session ended because the server connection "
                        "is no longer active."
                    )
                    break
        except KeyboardInterrupt:  # Handle Ctrl+C to gracefully close the client
            self.emit("\nKeyboard interrupt received. Closing client.")
            self.quit()

    def list_files(self) -> None:
        """
        Requests the server's file listing and displays it to the user.
        Caches the listed files for potential future download selections.
        """

        response = self.fetch_file_listing()
        if response is None:
            return

        files = response.get("files", [])
        message = response.get("message", "")
        self.emit(message)

        if not files:
            return

        self.emit("Available files:")
        for index, file_info in enumerate(files, start=1):
            filename = str(file_info.get("filename", "unknown"))
            size = int(file_info.get("size", 0))
            self.emit(f"  {index}. {filename} ({format_file_size(size)})")

        self.emit(
            "Tip: For DOWNLOAD, you can enter the file number or the exact filename."
        )

    def upload_file(self, file_path_text: str) -> None:
        """
        Uploads a specified local file to the server.

        Args:
            file_path_text (str): The string path to the local file to be uploaded.
                                  Can include surrounding quotes.
        """

        cleaned_input = strip_surrounding_quotes(file_path_text)
        local_path = Path(cleaned_input).expanduser()

        if not local_path.is_file():  # Ensure the local file exists
            self.emit(f"Local file not found: {local_path}")
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

        # Server must send a "READY" response before file bytes are sent
        if response.get("type") != "READY" or response.get("status") != "OK":
            self.emit(response.get("message", "Server refused the upload."))
            return

        try:
            # Stream file bytes to the server
            send_file_bytes(self.require_socket(), local_path, self.buffer_size)
            # Receive final upload result from server
            result = receive_json(self.require_socket())
        except OSError as exc:  # Handle issues reading the local file
            self.emit(f"Could not read the local file for upload: {exc}")
            return
        except (ConnectionClosedError, ProtocolError) as exc:
            self.close_local_socket()  # Close socket if connection is lost
            self.emit(f"Upload failed because the connection was lost: {exc}")
            return

        if result.get("status") != "OK":  # Server reported an issue
            self.emit(result.get("message", "Server reported an upload error."))
            return

        saved_name = str(result.get("filename", local_path.name))
        saved_size = int(result.get("filesize", file_size))
        self.emit(f"Upload complete: {saved_name} ({format_file_size(saved_size)})")
        self.emit(f"Integrity verified by server: {result.get('sha256', 'unknown')}")

    def download_file(self, filename_text: str) -> None:
        """
        Downloads a file from the server, identified by its name or a listed index.

        Args:
            filename_text (str): The filename or the numbered index from the
                                 last file listing (e.g., "1" or "my_document.txt").
        """

        resolved_filename = self.resolve_server_filename(filename_text)

        if resolved_filename is None:  # Could not resolve input to a server filename
            return

        try:
            requested_name = safe_filename(resolved_filename)
        except ValueError as exc:  # Invalid filename characters
            self.emit(str(exc))
            return

        response = self.send_request({"type": "DOWNLOAD", "filename": requested_name})
        if response is None:
            return

        # Server must send DOWNLOAD_READY before file bytes are sent
        if response.get("type") != "DOWNLOAD_READY" or response.get("status") != "OK":
            self.emit(response.get("message", "Server could not start the download."))
            return

        expected_size = response.get("filesize")
        expected_hash = response.get("sha256")
        server_filename = str(response.get("filename", requested_name))

        # Validate crucial metadata from the server
        if not isinstance(expected_size, int) or expected_size < 0:
            self.emit("Server returned an invalid file size.")
            return

        if not isinstance(expected_hash, str) or len(expected_hash) != 64:
            self.emit("Server returned an invalid SHA-256 hash.")
            return

        downloads_directory = ensure_directory(DOWNLOADS_PATH)
        # Prevent overwriting existing files with the same name
        destination_path = unique_path_for_file(downloads_directory, server_filename)

        try:
            # Receive file bytes and compute hash simultaneously
            received_hash = receive_file_bytes(
                self.require_socket(),
                destination_path,
                expected_size,
                self.buffer_size,
            )
        except (ConnectionClosedError, ProtocolError) as exc:
            self.close_local_socket()
            remove_file_if_exists(destination_path)  # Clean up partial download
            self.emit(f"Download failed because the connection was lost: {exc}")
            return
        except OSError as exc:
            remove_file_if_exists(destination_path)  # Clean up if save failed
            self.emit(f"Could not save the downloaded file: {exc}")
            return

        if received_hash != expected_hash:  # Verify file integrity after download
            remove_file_if_exists(destination_path)  # Remove corrupt file
            self.emit(
                "Integrity verification failed after download. "
                "The partial file was removed."
            )
            return

        self.emit(f"Download complete: {destination_path}")
        self.emit(f"Size: {format_file_size(expected_size)}")
        self.emit(f"Integrity verified: {received_hash}")

    def quit(self) -> None:
        """
        Sends a QUIT request to the server and closes the client's connection cleanly.
        """

        if self.client_socket is None:
            return

        try:
            send_json(self.client_socket, {"type": "QUIT"})  # Send QUIT command
            response = receive_json(self.client_socket)     # Await server's response
            self.emit(response.get("message", "Disconnected from server."))
        except (ConnectionClosedError, ProtocolError):
            # If connection is already closed or protocol error, just acknowledge
            self.emit("Connection closed.")
        finally:
            self.close_local_socket()  # Always ensure local socket is closed

    def send_request(self, request: dict[str, Any]) -> dict[str, Any] | None:
        """
        Sends a JSON request to the server and awaits its immediate JSON response.

        Args:
            request (dict[str, Any]): The dictionary representing the JSON request to send.

        Returns:
            dict[str, Any] | None: The server's JSON response, or None if a
                                   connection or protocol error occurred.
        """

        try:
            send_json(self.require_socket(), request)   
            response = receive_json(self.require_socket()) 
        except ConnectionClosedError as exc:  # Handle unexpected connection loss
            self.close_local_socket()
            self.emit(f"Connection lost: {exc}")
            return None
        except ProtocolError as exc:  # Handle malformed server responses
            self.close_local_socket()
            self.emit(f"Protocol error: {exc}")
            return None

        if response.get("status") == "ERROR":  # Server explicitly reported an error
            self.emit(response.get("message", "The server returned an error."))

        return response

    def fetch_file_listing(self) -> dict[str, Any] | None:
        """
        Fetches the current file listing from the server and caches it.

        Returns:
            dict[str, Any] | None: The server's response containing the file list,
                                   or None if an error occurred.
        """

        response = self.send_request({"type": "LIST"})
        if response is None or response.get("status") != "OK":
            return response

        files = response.get("files", [])
        if isinstance(files, list):
            self.last_listed_files = files  # Cache the list for download selection
        else:
            self.last_listed_files = []     # Reset if response is not a valid list

        return response

    def resolve_server_filename(self, filename_text: str) -> str | None:
        """
        Resolves a server filename from user input, which can be:
        1. A file number from the last 'LIST' command.
        2. A full display line from the 'LIST' command.
        3. An exact filename.
        4. A case-insensitive match against filenames.

        Args:
            filename_text (str): The raw string input from the user.

        Returns:
            str | None: The resolved server filename if found, otherwise None.
        """

        cleaned_input = strip_surrounding_quotes(filename_text)

        if not cleaned_input:  # Handle empty input
            self.emit("Please enter a file number or filename.")
            return None

        files = self.last_listed_files
        if not files:  # If no cached list, try to fetch it
            response = self.fetch_file_listing()
            if response is None:
                return None
            files = self.last_listed_files

        # Attempt to match input as a file number (e.g., "1", "5")
        if re.fullmatch(r"\d+", cleaned_input):
            selection = int(cleaned_input)
            if 1 <= selection <= len(files):
                return str(files[selection - 1].get("filename", ""))

            # Provide feedback if number is out of range
            self.emit(
                f"File number {selection} is out of range. "
                "Run LIST to see valid numbers."
            )
            return None

        # Attempt to match input as a numbered display line (e.g., "1. my_file.txt (10 KB)")
        numbered_line_match = re.match(r"^\s*(\d+)\.\s+.+$", cleaned_input)
        if numbered_line_match:
            selection = int(numbered_line_match.group(1))
            if 1 <= selection <= len(files):
                return str(files[selection - 1].get("filename", ""))

            # Provide feedback if number from line is out of range
            self.emit(
                f"File number {selection} is out of range. "
                "Run LIST to see valid numbers."
            )
            return None

        if not files:  # If still no files, treat input as literal filename
            return cleaned_input

        #
        # Block: Fuzzy matching for filenames or display lines
        # This section handles more flexible matching of user input
        # against the cached file list.
        #
        display_line_map: dict[str, str] = {}    # Map normalized display lines to filenames
        normalized_name_map: dict[str, list[str]] = {} # Map normalized names to original

        for index, file_info in enumerate(files, start=1):
            filename = str(file_info.get("filename", ""))
            size = int(file_info.get("size", 0))
            display_line = f"{index}. {filename} ({format_file_size(size)})"
            # Normalize both for consistent matching
            display_line_map[normalize_text_for_matching(display_line)] = filename
            normalized_name_map.setdefault(
                normalize_text_for_matching(filename), []
            ).append(filename)

        normalized_input = normalize_text_for_matching(cleaned_input)

        if normalized_input in display_line_map:  # Direct match of normalized display line
            return display_line_map[normalized_input]

        matched_filenames = normalized_name_map.get(normalized_input, [])
        if len(matched_filenames) == 1:  # Unique match for normalized filename
            return matched_filenames[0]

        if len(matched_filenames) > 1:  # Ambiguous match, require specific input
            self.emit(
                "Multiple files matched that name. "
                "Please use the file number from LIST."
            )
            return None

        # If no match, assume input is an exact filename for server lookup
        return cleaned_input

    def require_socket(self) -> socket.socket:
        """
        Returns the active socket connection.

        Raises:
            RuntimeError: If the client is not currently connected to a server.

        Returns:
            socket.socket: The active socket object.
        """

        if self.client_socket is None:
            raise RuntimeError("Client is not connected to a server.")
        return self.client_socket

    def close_local_socket(self) -> None:
        """
        Closes the client's local socket connection and resets its state.
        Handles potential OSError during socket closure gracefully.
        """

        if self.client_socket is None:
            return

        try:
            self.client_socket.close()
        except OSError:
            pass

        self.client_socket = None

    def print_menu(self) -> None:
        """
        Displays the main interactive menu options to the user.
        """

        self.emit()
        self.emit("SocketShare Menu")
        self.emit("1) LIST files on server")
        self.emit("2) UPLOAD local file to server")
        self.emit("3) DOWNLOAD file from server")
        self.emit("4) HELP")
        self.emit("5) QUIT")

    def print_help(self) -> None:
        """
        Displays a detailed help message outlining all supported commands and their functions.
        """

        self.emit()
        self.emit("Supported commands:")
        self.emit("  1 or LIST     - Show files currently stored on the server")
        self.emit("  2 or UPLOAD   - Upload a local file path to the server")
        self.emit(
            "  3 or DOWNLOAD - Download a server file by number or exact filename"
        )
        self.emit("  4 or HELP     - Show this help message")
        self.emit("  5 or QUIT     - Disconnect from the server")


def parse_arguments() -> argparse.Namespace:
    """
    Parses command-line arguments provided to the client script.

    Returns:
        argparse.Namespace: An object containing the parsed arguments,
                            e.g., host and port.
    """

    parser = argparse.ArgumentParser(description="Start the SocketShare TCP client.")
    parser.add_argument(
        "--host", default=DEFAULT_HOST, help="Server host address to connect to."
    )
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help="Server TCP port."
    )
    parser.add_argument(
        "--ui",
        choices=("auto", "tui", "classic"),
        default="auto",
        help="Choose the client interface mode. Defaults to auto.",
    )
    return parser.parse_args()


def main() -> int:
    """
    Program entry point for the SocketShare client.
    Parses arguments, initializes the client, and starts its main loop.

    Returns:
        int: Exit code of the program (0 for success).
    """

    arguments = parse_arguments()
    client = SocketShareClient(arguments.host, arguments.port, BUFFER_SIZE)
    if arguments.ui in {"auto", "tui"}:
        try:
            from client_tui import TerminalUiUnavailableError, run_terminal_ui

            run_terminal_ui(client)
            return 0
        except TerminalUiUnavailableError as exc:
            if arguments.ui == "tui":
                print(f"Terminal UI unavailable: {exc}")
                print("Falling back to the classic menu.")

    client.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
