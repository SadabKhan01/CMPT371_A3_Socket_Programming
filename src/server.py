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
        # Allow reuse of the address, useful for rapid restarts of the server
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        try:
            self.server_socket.bind((self.host, self.port))  # Bind to specified host/port
            self.server_socket.listen()  # Start listening for incoming connections
            self.server_socket.settimeout(1.0) # Set timeout for accept() to allow shutdown checks
        except OSError as exc:  # Catch network-related errors during setup
            log_event(
                "SERVER",
                f"Could not start the server on {self.host}:{self.port}: {exc}",
            )
            self.server_socket.close() # Ensure socket is closed on failure
            return 1

        self.is_running.set() # Signal that the server is now running
        log_event("SERVER", f"SocketShare server listening on {self.host}:{self.port}")
        log_event("SERVER", f"Upload directory: {STORAGE_PATH}")

        try:
            self.accept_loop()  # Main loop for accepting clients
        except KeyboardInterrupt: # Handle Ctrl+C for graceful shutdown
            log_event("SERVER", "Keyboard interrupt received. Stopping the server.")
        finally:
            self.shutdown() # Ensure shutdown routine is always called

        return 0

    def accept_loop(self) -> None:
        """
        Continuously accepts new client connections in a loop.
        Each new client connection is handled in a separate daemon thread.
        The loop continues until the `is_running` event is cleared (server shutdown).
        """

        while self.is_running.is_set():  # Loop while server is marked as running
            try:
                # Accept a new connection (with timeout for graceful shutdown)
                client_socket, client_address = self.server_socket.accept()
            except socket.timeout:
                continue  # Continue if no connection within timeout
            except OSError:
                break     # Break if socket is closed while accepting

            # Create a new thread for each client to handle concurrently
            client_thread = threading.Thread(
                target=self.handle_client,
                args=(client_socket, client_address),
                daemon=True,  # Daemon threads exit when main program exits
            )
            client_thread.start()
            self.client_threads.append(client_thread) # Keep track of client threads

    def shutdown(self) -> None:
        """
        Shuts down the server gracefully.
        It stops accepting new connections, closes the server socket, and attempts
        to join all client handler threads to ensure they finish.
        """

        self.is_running.clear()  # Signal `accept_loop` to stop

        if self.server_socket is not None:
            try:
                self.server_socket.close()  # Close the listening socket
            except OSError:
                pass # Ignore errors if socket is already closed

        for client_thread in self.client_threads:
            # Wait for each client thread to finish gracefully, with a timeout
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

        with client_socket:  # Ensure socket is closed upon exiting this block
            while True:
                try:
                    request = receive_json(client_socket) # Attempt to receive client request
                except ConnectionClosedError: # Client disconnected gracefully or abruptly
                    log_event("SERVER", f"Client disconnected unexpectedly: {client_label}")
                    break # Exit loop, clean up connection
                except ProtocolError as exc: # Malformed JSON or protocol violation
                    log_event(
                        "SERVER",
                        f"Malformed message from {client_label}. "
                        f"Closing connection: {exc}",
                    )
                    self.send_error(client_socket, str(exc)) # Inform client of error
                    break # Exit loop due to protocol error

                try:
                    # Dispatch request to appropriate handler; determine if connection persists
                    should_continue = self.dispatch_request(
                        client_socket, client_label, request
                    )
                except ConnectionClosedError: # Handler lost connection during processing
                    log_event(
                        "SERVER",
                        f"Connection lost while serving client: {client_label}",
                    )
                    break
                except OSError as exc: # General server-side file I/O error
                    log_event(
                        "SERVER",
                        f"Server-side I/O error while serving {client_label}: {exc}",
                    )
                    break

                if not should_continue: # Handler indicated connection should close
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

        command_type = str(request.get("type", "")).upper().strip() # Standardize command

        if command_type == "LIST":
            self.handle_list(client_socket)
            return True # Keep connection open for further requests

        if command_type == "UPLOAD":
            return self.handle_upload(client_socket, client_label, request)

        if command_type == "DOWNLOAD":
            return self.handle_download(client_socket, client_label, request)

        if command_type == "QUIT":
            # Send a confirmation message before closing
            send_json(
                client_socket,
                {
                    "type": "GOODBYE",
                    "status": "OK",
                    "message": "Disconnected from the SocketShare server.",
                },
            )
            log_event("SERVER", f"Client requested clean disconnect: {client_label}")
            return False # Signal to close the connection

        # Handle unrecognized commands
        self.send_error(client_socket, "Unsupported command received.")
        return True # Keep connection open, client might send valid commands next

    def handle_list(self, client_socket: socket.socket) -> None:
        """
        Handles a client's 'LIST' request by compiling a list of available files
        in the server's storage and sending it back to the client.

        Args:
            client_socket (socket.socket): The socket object for the client connection.
        """

        files = build_file_listing(STORAGE_PATH) # Get sorted list of files
        message = "No files are currently available on the server."

        if files: # Customize message if files are present
            message = f"{len(files)} file(s) available on the server."

        send_json( # Send the file list and status back to the client
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
        except ValueError as exc: # Catch invalid characters/paths in filename
            self.send_error(client_socket, str(exc))
            return True

        expected_size = request.get("filesize")
        expected_hash = request.get("sha256")

        # Validate crucial metadata received from the client
        if not isinstance(expected_size, int) or expected_size < 0:
            self.send_error(client_socket, "UPLOAD requires a "
                                           "non-negative integer filesize.")
            return True

        if not isinstance(expected_hash, str) or len(expected_hash) != 64:
            self.send_error(client_socket, "UPLOAD requires a valid SHA-256 hash.")
            return True

        # Determine a unique and safe path to save the incoming file
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
            # Receive file bytes and compute its hash on the fly
            received_hash = receive_file_bytes(
                client_socket, destination_path, expected_size, self.buffer_size
            )
        except ConnectionClosedError: # Client disconnected during transfer
            remove_file_if_exists(destination_path) # Clean up partial file
            log_event(
                "SERVER",
                f"Upload interrupted because {client_label} disconnected early.",
            )
            return False # Signal to close this client connection
        except OSError as exc: # Problem writing to disk
            remove_file_if_exists(destination_path) # Clean up partial file
            self.send_error(client_socket, f"Server failed to save the upload: {exc}")
            return True

        saved_size = destination_path.stat().st_size

        # Perform final integrity verification using size and hash
        if saved_size != expected_size or received_hash != expected_hash:
            remove_file_if_exists(destination_path) # Remove potentially corrupt file
            self.send_error(client_socket, "Upload failed integrity verification.")
            log_event(
                "SERVER",
                f"Integrity mismatch for uploaded file from {client_label}: "
                f"{original_name}",
            )
            return True

        send_json( # Send success response back to client
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

        log_event( # Log successful upload
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
        except ValueError as exc: # Catch invalid characters/paths in filename
            self.send_error(client_socket, str(exc))
            return True

        file_path = STORAGE_PATH / requested_name # Construct full path to file

        if not file_path.is_file(): # Check if the requested file actually exists
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
            send_file_bytes(client_socket, file_path, self.buffer_size) # Stream file
        except ConnectionClosedError: # Client disconnected during transfer
            log_event(
                "SERVER",
                f"Download interrupted because {client_label} disconnected early.",
            )
            return False # Signal to close this client connection

        log_event( # Log successful download
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
            send_json( # Attempt to send an ERROR message to the client
                client_socket,
                {
                    "type": "ERROR",
                    "status": "ERROR",
                    "message": message,
                },
            )
        except ConnectionClosedError: # Ignore if client already disconnected
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
