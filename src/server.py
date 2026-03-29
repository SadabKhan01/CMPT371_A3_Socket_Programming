"""SocketShare TCP server.

This server accepts multiple clients concurrently using threads. Each client can
list files, upload a file to the server, download a file from the server, or
disconnect cleanly.
"""

from __future__ import annotations

import argparse
import socket
import threading
from pathlib import Path
from typing import Any

from config import BUFFER_SIZE, DEFAULT_HOST, DEFAULT_PORT, STORAGE_PATH
from protocol import (
    ConnectionClosedError,
    ProtocolError,
    receive_file_bytes,
    receive_json,
    send_file_bytes,
    send_json,
)
from utils import (
    build_file_listing,
    compute_sha256,
    ensure_directory,
    format_endpoint,
    format_file_size,
    log_event,
    remove_file_if_exists,
    safe_filename,
    unique_path_for_file,
)


class SocketShareServer:
    """Threaded TCP file-transfer server."""

    def __init__(self, host: str, port: int, buffer_size: int) -> None:
        self.host = host
        self.port = port
        self.buffer_size = buffer_size
        self.server_socket: socket.socket | None = None
        self.is_running = threading.Event()
        self.client_threads: list[threading.Thread] = []

    def start(self) -> int:
        """Start listening for client connections."""

        ensure_directory(STORAGE_PATH)
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        try:
            self.server_socket.bind((self.host, self.port))
            self.server_socket.listen()
            self.server_socket.settimeout(1.0)
        except OSError as exc:
            log_event(
                "SERVER",
                f"Could not start the server on {self.host}:{self.port}: {exc}",
            )
            self.server_socket.close()
            return 1

        self.is_running.set()
        log_event("SERVER", f"SocketShare server listening on {self.host}:{self.port}")
        log_event("SERVER", f"Upload directory: {STORAGE_PATH}")

        try:
            self.accept_loop()
        except KeyboardInterrupt:
            log_event("SERVER", "Keyboard interrupt received. Stopping the server.")
        finally:
            self.shutdown()

        return 0

    def accept_loop(self) -> None:
        """Accept new clients until the server is stopped."""

        while self.is_running.is_set():
            try:
                client_socket, client_address = self.server_socket.accept()
            except socket.timeout:
                continue
            except OSError:
                break

            client_thread = threading.Thread(
                target=self.handle_client,
                args=(client_socket, client_address),
                daemon=True,
            )
            client_thread.start()
            self.client_threads.append(client_thread)

    def shutdown(self) -> None:
        """Stop the listening socket and allow worker threads to finish."""

        self.is_running.clear()

        if self.server_socket is not None:
            try:
                self.server_socket.close()
            except OSError:
                pass

        for client_thread in self.client_threads:
            client_thread.join(timeout=1.0)

        log_event("SERVER", "Server shutdown complete.")

    def handle_client(
        self, client_socket: socket.socket, client_address: tuple[str, int]
    ) -> None:
        """Serve one client until it disconnects or sends QUIT."""

        client_label = format_endpoint(client_address)
        log_event("SERVER", f"Client connected: {client_label}")

        with client_socket:
            while True:
                try:
                    request = receive_json(client_socket)
                except ConnectionClosedError:
                    log_event("SERVER", f"Client disconnected unexpectedly: {client_label}")
                    break
                except ProtocolError as exc:
                    log_event(
                        "SERVER",
                        f"Malformed message from {client_label}. Closing connection: {exc}",
                    )
                    self.send_error(client_socket, str(exc))
                    break

                try:
                    should_continue = self.dispatch_request(
                        client_socket, client_label, request
                    )
                except ConnectionClosedError:
                    log_event(
                        "SERVER",
                        f"Connection lost while serving client: {client_label}",
                    )
                    break
                except OSError as exc:
                    log_event(
                        "SERVER",
                        f"Server-side I/O error while serving {client_label}: {exc}",
                    )
                    break

                if not should_continue:
                    break

        log_event("SERVER", f"Connection closed: {client_label}")

    def dispatch_request(
        self, client_socket: socket.socket, client_label: str, request: dict[str, Any]
    ) -> bool:
        """Route a parsed client request to the correct handler."""

        command_type = str(request.get("type", "")).upper().strip()

        if command_type == "LIST":
            self.handle_list(client_socket)
            return True

        if command_type == "UPLOAD":
            return self.handle_upload(client_socket, client_label, request)

        if command_type == "DOWNLOAD":
            return self.handle_download(client_socket, client_label, request)

        if command_type == "QUIT":
            send_json(
                client_socket,
                {
                    "type": "GOODBYE",
                    "status": "OK",
                    "message": "Disconnected from the SocketShare server.",
                },
            )
            log_event("SERVER", f"Client requested clean disconnect: {client_label}")
            return False

        self.send_error(client_socket, "Unsupported command received.")
        return True

    def handle_list(self, client_socket: socket.socket) -> None:
        """Send the current list of uploaded files to the client."""

        files = build_file_listing(STORAGE_PATH)
        message = "No files are currently available on the server."

        if files:
            message = f"{len(files)} file(s) available on the server."

        send_json(
            client_socket,
            {
                "type": "LIST_RESPONSE",
                "status": "OK",
                "files": files,
                "message": message,
            },
        )

    def handle_upload(
        self, client_socket: socket.socket, client_label: str, request: dict[str, Any]
    ) -> bool:
        """Receive a file upload, verify it, and store it on the server."""

        try:
            original_name = safe_filename(str(request.get("filename", "")))
        except ValueError as exc:
            self.send_error(client_socket, str(exc))
            return True

        expected_size = request.get("filesize")
        expected_hash = request.get("sha256")

        if not isinstance(expected_size, int) or expected_size < 0:
            self.send_error(client_socket, "UPLOAD requires a non-negative integer filesize.")
            return True

        if not isinstance(expected_hash, str) or len(expected_hash) != 64:
            self.send_error(client_socket, "UPLOAD requires a valid SHA-256 hash.")
            return True

        destination_path = unique_path_for_file(STORAGE_PATH, original_name)

        # Tell the client when the server is ready so both sides stay in sync
        # before raw file bytes start flowing over the socket.
        send_json(
            client_socket,
            {
                "type": "READY",
                "status": "OK",
                "filename": destination_path.name,
                "message": "Server ready to receive file bytes.",
            },
        )

        try:
            received_hash = receive_file_bytes(
                client_socket, destination_path, expected_size, self.buffer_size
            )
        except ConnectionClosedError:
            remove_file_if_exists(destination_path)
            log_event(
                "SERVER",
                f"Upload interrupted because {client_label} disconnected early.",
            )
            return False
        except OSError as exc:
            remove_file_if_exists(destination_path)
            self.send_error(client_socket, f"Server failed to save the upload: {exc}")
            return True

        saved_size = destination_path.stat().st_size

        if saved_size != expected_size or received_hash != expected_hash:
            remove_file_if_exists(destination_path)
            self.send_error(client_socket, "Upload failed integrity verification.")
            log_event(
                "SERVER",
                f"Integrity mismatch for uploaded file from {client_label}: {original_name}",
            )
            return True

        send_json(
            client_socket,
            {
                "type": "UPLOAD_RESULT",
                "status": "OK",
                "filename": destination_path.name,
                "filesize": saved_size,
                "sha256": received_hash,
                "message": "Upload completed and integrity verified on the server.",
            },
        )

        log_event(
            "SERVER",
            (
                f"Stored upload from {client_label}: {destination_path.name} "
                f"({format_file_size(saved_size)})"
            ),
        )
        return True

    def handle_download(
        self, client_socket: socket.socket, client_label: str, request: dict[str, Any]
    ) -> bool:
        """Send a requested file to the client."""

        try:
            requested_name = safe_filename(str(request.get("filename", "")))
        except ValueError as exc:
            self.send_error(client_socket, str(exc))
            return True

        file_path = STORAGE_PATH / requested_name

        if not file_path.is_file():
            self.send_error(client_socket, f"Server file not found: {requested_name}")
            return True

        file_size = file_path.stat().st_size
        file_hash = compute_sha256(file_path)

        # The header gives the client everything it needs to prepare its local
        # destination file and later verify integrity after the raw bytes arrive.
        send_json(
            client_socket,
            {
                "type": "DOWNLOAD_READY",
                "status": "OK",
                "filename": file_path.name,
                "filesize": file_size,
                "sha256": file_hash,
                "message": "Server is sending the requested file.",
            },
        )

        try:
            send_file_bytes(client_socket, file_path, self.buffer_size)
        except ConnectionClosedError:
            log_event(
                "SERVER",
                f"Download interrupted because {client_label} disconnected early.",
            )
            return False

        log_event(
            "SERVER",
            (
                f"Sent file to {client_label}: {file_path.name} "
                f"({format_file_size(file_size)})"
            ),
        )
        return True

    def send_error(self, client_socket: socket.socket, message: str) -> None:
        """Send an error response if the connection is still usable."""

        try:
            send_json(
                client_socket,
                {
                    "type": "ERROR",
                    "status": "ERROR",
                    "message": message,
                },
            )
        except ConnectionClosedError:
            pass


def parse_arguments() -> argparse.Namespace:
    """Parse command-line arguments for the server."""

    parser = argparse.ArgumentParser(description="Start the SocketShare TCP server.")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Host address to bind to.")
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help="TCP port number to listen on."
    )
    return parser.parse_args()


def main() -> int:
    """Program entry point."""

    arguments = parse_arguments()
    server = SocketShareServer(arguments.host, arguments.port, BUFFER_SIZE)
    return server.start()


if __name__ == "__main__":
    raise SystemExit(main())
