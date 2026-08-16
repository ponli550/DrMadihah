import json
import urllib.request
import threading
import time

BASE = "http://localhost:8080"
endpoint_event = threading.Event()
endpoint_url = [None]
responses = []


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
                        responses.append(msg)
                        print("SSE:", msg.get("id"), msg.get("result", {}).get("content", "")[:200] if msg.get("result") else msg)
                    except Exception:
                        pass


def send_message(msg):
    endpoint_event.wait()
    req = urllib.request.Request(
        endpoint_url[0],
        data=json.dumps(msg).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.read().decode()


threading.Thread(target=sse_listener, daemon=True).start()

# Initialize
print(send_message({
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "umcares-pipeline", "version": "1.0"},
    },
}))

# Ping Premiere
print(send_message({
    "jsonrpc": "2.0",
    "id": 2,
    "method": "tools/call",
    "params": {"name": "ping_premiere", "arguments": {}},
}))

# Get project state
print(send_message({
    "jsonrpc": "2.0",
    "id": 3,
    "method": "tools/call",
    "params": {"name": "premiere_get_project_state", "arguments": {}},
}))

time.sleep(2)
for r in responses:
    print("RESPONSE:", r)
