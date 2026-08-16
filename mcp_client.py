import json
import urllib.request
import threading
import queue
import time

BASE = "http://localhost:8080"

# Global response queue
responses = queue.Queue()
endpoint_event = threading.Event()
endpoint_url = [None]


def sse_listener():
    req = urllib.request.Request(f"{BASE}/sse", method="GET", headers={"Accept": "text/event-stream"})
    with urllib.request.urlopen(req) as resp:
        for line in resp:
            line = line.decode().strip()
            if not line:
                continue
            if line.startswith("event: endpoint"):
                continue
            if line.startswith("data:"):
                data = line[5:].strip()
                if endpoint_url[0] is None:
                    endpoint_url[0] = data
                    endpoint_event.set()
                else:
                    try:
                        msg = json.loads(data)
                        responses.put(msg)
                    except json.JSONDecodeError:
                        pass


def send_message(msg):
    endpoint_event.wait()
    req = urllib.request.Request(
        endpoint_url[0],
        data=json.dumps(msg).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        return resp.read().decode()


# Start SSE listener
threading.Thread(target=sse_listener, daemon=True).start()

# Initialize
init_msg = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "umcares-pipeline", "version": "1.0"},
    },
}
print(send_message(init_msg))

# List tools
tools_msg = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
print(send_message(tools_msg))

# Ping Premiere
ping_msg = {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "ping_premiere", "arguments": {}}}
print(send_message(ping_msg))

# Wait a bit for responses
for _ in range(5):
    try:
        print(responses.get(timeout=0.5))
    except queue.Empty:
        break
