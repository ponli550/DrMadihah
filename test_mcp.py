import json
import urllib.request

BASE = "http://localhost:8080"

# Get SSE endpoint
req = urllib.request.Request(f"{BASE}/sse", method="GET")
with urllib.request.urlopen(req) as resp:
    for line in resp:
        line = line.decode().strip()
        if line.startswith("data:"):
            endpoint = line[5:].strip()
            print("Endpoint:", endpoint)
            break

# Send initialize
msg = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "1.0"}
    }
}
req = urllib.request.Request(endpoint, data=json.dumps(msg).encode(), headers={"Content-Type": "application/json"}, method="POST")
with urllib.request.urlopen(req) as resp:
    print(resp.read().decode())

# List tools
msg = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
req = urllib.request.Request(endpoint, data=json.dumps(msg).encode(), headers={"Content-Type": "application/json"}, method="POST")
with urllib.request.urlopen(req) as resp:
    data = json.loads(resp.read().decode())
    print("Tools:")
    for tool in data.get("result", {}).get("tools", [])[:10]:
        print(" -", tool["name"])
