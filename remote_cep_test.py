#!/usr/bin/env python3
"""Test raw WebSocket messages to the CEP panel."""

import asyncio
import json
import sys
import websockets

WS_URL = "ws://127.0.0.1:9801"

def load_token():
    """Load the CEP token from the default path."""
    import os
    token_path = os.path.expanduser("~/.premierpro-mcp/cep-token")
    with open(token_path) as f:
        return f.read().strip()

async def send_raw_action(action, params=None):
    """Send a raw action to the CEP panel."""
    token = load_token()
    headers = {"Authorization": f"Bearer {token}"}

    async with websockets.connect(WS_URL, extra_headers=headers) as ws:
        request = {
            "action": action,
            "params": params or {},
            "requestId": "test-001"
        }
        print(f"Sending: {json.dumps(request, indent=2)}")
        await ws.send(json.dumps(request))

        # Wait for response
        try:
            response = await asyncio.wait_for(ws.recv(), timeout=15)
            print(f"Response: {response}")
            return json.loads(response)
        except asyncio.TimeoutError:
            print("TIMEOUT: No response from CEP panel")
            return None

async def main():
    action = sys.argv[1] if len(sys.argv) > 1 else "ping"
    params = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}

    print(f"Testing action: {action}")
    print(f"Params: {params}")
    print()

    result = await send_raw_action(action, params)

    if result:
        if result.get("success"):
            print(f"\nSUCCESS: {json.dumps(result.get('result'), indent=2)}")
        else:
            print(f"\nERROR: {result.get('error')}")

if __name__ == "__main__":
    asyncio.run(main())
