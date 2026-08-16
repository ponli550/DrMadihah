#!/usr/bin/env python3
"""Raw WebSocket client to test CEP panel directly."""

import socket
import json
import hashlib
import base64
import os
import sys

WS_HOST = "127.0.0.1"
WS_PORT = 9801

def load_token():
    token_path = os.path.expanduser("~/.premierpro-mcp/cep-token")
    with open(token_path) as f:
        return f.read().strip()

def websocket_connect(host, port, path="/", headers=None):
    """Establish a WebSocket connection."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(10)
    sock.connect((host, port))

    # Generate WebSocket key
    key = base64.b64encode(os.urandom(16)).decode()

    # Build HTTP upgrade request
    request_lines = [
        f"GET {path} HTTP/1.1",
        f"Host: {host}:{port}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Key: {key}",
        "Sec-WebSocket-Version: 13",
    ]

    if headers:
        for name, value in headers.items():
            request_lines.append(f"{name}: {value}")

    request = "\r\n".join(request_lines) + "\r\n\r\n"
    sock.sendall(request.encode())

    # Read response
    response = b""
    while b"\r\n\r\n" not in response:
        chunk = sock.recv(1024)
        if not chunk:
            raise ConnectionError("Connection closed during handshake")
        response += chunk

    # Verify upgrade
    status_line = response.split(b"\r\n")[0].decode()
    if "101" not in status_line:
        raise ConnectionError(f"WebSocket upgrade failed: {status_line}")

    return sock

def websocket_send(sock, message):
    """Send a WebSocket text message."""
    data = message.encode("utf-8")
    length = len(data)

    # Frame header
    frame = bytearray()
    frame.append(0x81)  # FIN + text opcode

    # Mask bit set
    if length < 126:
        frame.append(0x80 | length)
    elif length < 65536:
        frame.append(0x80 | 126)
        frame.extend(length.to_bytes(2, "big"))
    else:
        frame.append(0x80 | 127)
        frame.extend(length.to_bytes(8, "big"))

    # Masking key
    mask = os.urandom(4)
    frame.extend(mask)

    # Masked payload
    masked = bytearray(data)
    for i in range(len(masked)):
        masked[i] ^= mask[i % 4]
    frame.extend(masked)

    sock.sendall(frame)

def websocket_recv(sock):
    """Receive a WebSocket message."""
    # Read frame header
    header = sock.recv(2)
    if len(header) < 2:
        raise ConnectionError("Connection closed")

    opcode = header[0] & 0x0F
    masked = (header[1] & 0x80) != 0
    length = header[1] & 0x7F

    # Extended length
    if length == 126:
        length = int.from_bytes(sock.recv(2), "big")
    elif length == 127:
        length = int.from_bytes(sock.recv(8), "big")

    # Mask
    if masked:
        mask = sock.recv(4)

    # Payload
    payload = b""
    while len(payload) < length:
        chunk = sock.recv(min(8192, length - len(payload)))
        if not chunk:
            raise ConnectionError("Connection closed during read")
        payload += chunk

    # Unmask if needed
    if masked:
        unmasked = bytearray(payload)
        for i in range(len(unmasked)):
            unmasked[i] ^= mask[i % 4]
        payload = bytes(unmasked)

    if opcode == 0x1:  # Text
        return payload.decode("utf-8")
    elif opcode == 0x9:  # Ping
        return None  # Ignore pings
    else:
        return None

def main():
    action = sys.argv[1] if len(sys.argv) > 1 else "ping"
    params = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}

    token = load_token()
    print(f"Token: {token[:8]}...")
    print(f"Connecting to ws://{WS_HOST}:{WS_PORT}...")

    try:
        sock = websocket_connect(
            WS_HOST, WS_PORT,
            headers={"Authorization": f"Bearer {token}"}
        )
        print("Connected!")

        # Send ping action
        request = {
            "action": action,
            "params": params,
            "requestId": "test-001"
        }
        message = json.dumps(request)
        print(f"Sending: {message}")
        websocket_send(sock, message)

        # Receive response
        response = websocket_recv(sock)
        print(f"Response: {response}")

        sock.close()

    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
