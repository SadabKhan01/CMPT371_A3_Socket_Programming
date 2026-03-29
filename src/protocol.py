"""Helpers for the SocketShare application protocol.

The project uses a simple and reliable framing strategy:
1. Every control message is encoded as JSON.
2. A 4-byte big-endian integer is sent first to describe the JSON length.
3. Raw file bytes are sent only after both sides agree on the file metadata.

This approach is beginner-friendly because it avoids guessing where one message
ends and the next one begins.
"""

from __future__ import annotations

import hashlib
import json
import socket
import struct
from pathlib import Path
from typing import Any

HEADER_LENGTH_SIZE = 4
MAX_JSON_MESSAGE_SIZE = 1_048_576


class ProtocolError(Exception):
    """Raised when a peer sends malformed protocol data."""


class ConnectionClosedError(ConnectionError):
    """Raised when the other side closes the socket unexpectedly."""


def send_json(sock: socket.socket, message: dict[str, Any]) -> None:
    """Send one length-prefixed JSON control message."""

    payload = json.dumps(message).encode("utf-8")
    header = struct.pack("!I", len(payload))

    try:
        sock.sendall(header)
        sock.sendall(payload)
    except OSError as exc:
        raise ConnectionClosedError("Failed to send data to the peer.") from exc


def receive_json(sock: socket.socket) -> dict[str, Any]:
    """Read one length-prefixed JSON control message from the socket."""

    header = receive_exactly(sock, HEADER_LENGTH_SIZE)
    message_length = struct.unpack("!I", header)[0]

    if message_length <= 0 or message_length > MAX_JSON_MESSAGE_SIZE:
        raise ProtocolError(
            f"Invalid JSON message length received: {message_length} bytes."
        )

    payload = receive_exactly(sock, message_length)

    try:
        message = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("Received malformed JSON data.") from exc

    if not isinstance(message, dict):
        raise ProtocolError("Protocol messages must decode into JSON objects.")

    return message


def receive_exactly(sock: socket.socket, total_bytes: int) -> bytes:
    """Read an exact number of bytes or raise an error if the peer disconnects."""

    if total_bytes < 0:
        raise ProtocolError("Cannot receive a negative number of bytes.")

    chunks = bytearray()

    while len(chunks) < total_bytes:
        try:
            chunk = sock.recv(total_bytes - len(chunks))
        except OSError as exc:
            raise ConnectionClosedError("Failed while receiving socket data.") from exc

        if not chunk:
            raise ConnectionClosedError("The peer closed the connection.")

        chunks.extend(chunk)

    return bytes(chunks)


def send_file_bytes(
    sock: socket.socket, file_path: Path, buffer_size: int
) -> None:
    """Stream a file to the peer in chunks."""

    with file_path.open("rb") as file_handle:
        while True:
            chunk = file_handle.read(buffer_size)
            if not chunk:
                break

            try:
                sock.sendall(chunk)
            except OSError as exc:
                raise ConnectionClosedError(
                    "Failed while sending file data."
                ) from exc


def receive_file_bytes(
    sock: socket.socket, destination_path: Path, expected_size: int, buffer_size: int
) -> str:
    """Receive a file from the socket and return its SHA-256 hash."""

    if expected_size < 0:
        raise ProtocolError("File size cannot be negative.")

    remaining_bytes = expected_size
    digest = hashlib.sha256()

    with destination_path.open("wb") as file_handle:
        while remaining_bytes > 0:
            chunk = receive_exactly(sock, min(buffer_size, remaining_bytes))
            file_handle.write(chunk)
            digest.update(chunk)
            remaining_bytes -= len(chunk)

    return digest.hexdigest()
