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
    """
    Sends a length-prefixed JSON control message over the socket.
    The message is first converted to a JSON string, then encoded to bytes.
    Its length is packed into a 4-byte big-endian header.

    Args:
        sock (socket.socket): The socket connected to the peer.
        message (dict[str, Any]): The dictionary to be sent as a JSON message.

    Raises:
        ConnectionClosedError: If sending data to the peer fails unexpectedly.
    """

    payload = json.dumps(message).encode("utf-8")
    header = struct.pack("!I", len(payload))

    try:
        sock.sendall(header)
        sock.sendall(payload)
    except OSError as exc:
        raise ConnectionClosedError("Failed to send data to the peer.") from exc


def receive_json(sock: socket.socket) -> dict[str, Any]:
    """
    Receives and decodes one length-prefixed JSON control message from the socket.
    It first reads a 4-byte header to determine the message length, then reads
    the payload, and finally decodes it from UTF-8 to a JSON dictionary.

    Args:
        sock (socket.socket): The socket connected to the peer.

    Returns:
        dict[str, Any]: The decoded JSON message as a dictionary.

    Raises:
        ConnectionClosedError: If the connection closes unexpectedly while receiving data.
        ProtocolError: If the received message length is invalid, the JSON is malformed,
                       or the decoded message is not a dictionary.
    """

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
    """
    Receives an exact number of bytes from the socket.
    This function blocks until `total_bytes` are received or an error occurs.

    Args:
        sock (socket.socket): The socket connected to the peer.
        total_bytes (int): The exact number of bytes to receive.

    Returns:
        bytes: The received bytes.

    Raises:
        ProtocolError: If `total_bytes` is negative.
        ConnectionClosedError: If the peer disconnects before all bytes are received,
                               or if an OSError occurs during reception.
    """

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
    """
    Streams the content of a local file to the peer over the socket in chunks.

    Args:
        sock (socket.socket): The socket connected to the peer.
        file_path (Path): The path to the local file to be sent.
        buffer_size (int): The size of each chunk to read from the file and send.

    Raises:
        ConnectionClosedError: If an OSError occurs while sending file data.
    """

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
    """
    Receives file bytes from the socket and writes them to a specified destination path.
    Computes the SHA-256 hash of the received data for integrity verification.

    Args:
        sock (socket.socket): The socket connected to the peer.
        destination_path (Path): The local path where the received file will be saved.
        expected_size (int): The total number of bytes expected to be received for the file.
        buffer_size (int): The size of each chunk to receive and write.

    Returns:
        str: The hexadecimal SHA-256 hash of the received file content.

    Raises:
        ProtocolError: If `expected_size` is negative.
        ConnectionClosedError: If the peer closes the connection unexpectedly during reception.
    """

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
