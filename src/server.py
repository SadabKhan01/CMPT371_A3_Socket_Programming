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
    """
    Threaded TCP file-transfer server that handles multiple clients concurrently.
    Each client connection is managed in a separate thread.
    """

    def __init__(self, host: str, port: int, buffer_size: int) -> None:
        """
        Initializes the SocketShareServer.

        Args:
            host (str): The hostname or IP address the server will bind to.
            port (int): The TCP port number the server will listen on.
            buffer_size (int): The size of the buffer used for sending/receiving data.
        """
        self.host = host
        self.port = port
        self.buffer_size = buffer_size
        self.server_socket: socket.socket | None = None
        self.is_running = threading.Event()
        self.client_threads: list[threading.Thread] = []

    def start(self) -> int:
        """
        Starts the server, binds to the specified host and port, and begins listening
        for incoming client connections. This method also handles graceful shutdown
        on KeyboardInterrupt.

        Returns:
            int: An exit code (0 for success, 1 for failure to start).
        """

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
        """
        Continuously accepts new client connections in a loop.
        Each new client connection is handled in a separate daemon thread.
        The loop continues until the `is_running` event is cleared (server shutdown).
        """

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
        """
        Shuts down the server gracefully.
        It stops accepting new connections, closes the server socket, and attempts
        to join all client handler threads to ensure they finish.
        """

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
        """
        Handles incoming requests from a single client. This method runs in a
        dedicated thread for each connected client. It continuously receives
        JSON requests and dispatches them to appropriate handlers until the
        client disconnects or sends a QUIT command.

        Args:
            client_socket (socket.socket): The socket object for the client connection.
            client_address (tuple[str, int]): The IP address and port of the connected client.
        """

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
        """
        Routes an incoming client request to the appropriate handler method based on
        the 'type' field in the request.

        Args:
            client_socket (socket.socket): The socket object for the client connection.
            client_label (str): A string identifier for the client (e.g., "IP:Port").
            request (dict[str, Any]): The parsed JSON request from the client.

        Returns:
            bool: True if the client connection should remain open, False if it
                  should be closed (e.g., after a QUIT command).
        """

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
        """
        Handles a client's 'LIST' request by compiling a list of available files
        in the server's storage and sending it back to the client.

        Args:
            client_socket (socket.socket): The socket object for the client connection.
        """

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
        """
        Handles a client's 'UPLOAD' request. It receives file metadata, sends a
        'READY' signal, then receives the file bytes. After reception, it performs
        integrity verification using file size and SHA-256 hash.

        Args:
            client_socket (socket.socket): The socket object for the client connection.
            client_label (str): A string identifier for the client.
            request (dict[str, Any]): The 'UPLOAD' request dictionary containing
                                     filename, filesize, and SHA-256 hash.

        Returns:
            bool: True if the client connection should remain open, False if an
                  unrecoverable error or disconnection occurred.
        """

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
        """
        Handles a client's 'DOWNLOAD' request. It locates the requested file,
        sends metadata (filename, size, SHA-256) to the client, and then
        streams the file's bytes over the socket.

        Args:
            client_socket (socket.socket): The socket object for the client connection.
            client_label (str): A string identifier for the client.
            request (dict[str, Any]): The 'DOWNLOAD' request dictionary containing
                                     the requested filename.

        Returns:
            bool: True if the client connection should remain open, False if an
                  unrecoverable error or disconnection occurred during the transfer.
        """

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
        """
        Sends an error response back to the client if the connection is still active.
        This is used to inform the client about issues encountered on the server.

        Args:
            client_socket (socket.socket): The socket object for the client connection.
            message (str): The error message to send to the client.
        """

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
    """
    Parses command-line arguments provided to the server script.

    Returns:
        argparse.Namespace: An object containing the parsed arguments,
                            e.g., host and port.
    """

    parser = argparse.ArgumentParser(description="Start the SocketShare TCP server.")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Host address to bind to.")
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help="TCP port number to listen on."
    )
    return parser.parse_args()


def main() -> int:
    """
    Program entry point for the SocketShare server.
    Parses arguments, initializes the server, and starts its listening loop.

    Returns:
        int: Exit code of the program (0 for success, 1 for failure).
    """

    arguments = parse_arguments()
    server = SocketShareServer(arguments.host, arguments.port, BUFFER_SIZE)
    return server.start()


if __name__ == "__main__":
    raise SystemExit(main())
