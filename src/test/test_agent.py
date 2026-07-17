import asyncio
import httpx
import json
import sys

if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8')


async def test_tool_calling():
    print("Testing SerenityDev backend tool calling...")
    url = "http://127.0.0.1:8002/ask_stream"
    payload = {
        "prompt": "List the contents of the current workspace root directory using the mcp:filesystem:list_directory tool.",
        "session_id": "test_session_123",
        "workspace_dir": "."
    }

    try:
        async with httpx.AsyncClient(timeout=600.0, trust_env=False) as client:
            print("Fetching active models from /api/models...")


            models_url = "http://127.0.0.1:8002/api/models"
            
            print("Connecting to SerenityDev server at 127.0.0.1:8002...")
            connected = False
            models_res = None
            for attempt in range(30):
                try:
                    models_res = await client.get(models_url)
                    if models_res.status_code == 200:
                        connected = True
                        break
                except Exception:
                    await asyncio.sleep(1.0)
            
            if not connected:
                print("FAILED: Could not connect to devserver after 30 seconds.")
                return

            print("SUCCESS: /api/models returned:")
            print(json.dumps(models_res.json(), indent=2))

                
            print(f"\nSending request to {url}")
            async with client.stream("POST", url, json=payload) as response:
                print(f"Response status: {response.status_code}")
                if response.status_code != 200:
                    print(f"Error: {await response.aread()}")
                    return
                
                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        data_str = line[6:].strip()
                        try:
                            data = json.loads(data_str)
                            if data.get("type") == "progress":
                                print(f"[Progress] {data.get('text')}")
                            elif data.get("type") == "content":
                                print(f"[Content] {data.get('content')}")
                            elif data.get("type") == "done":
                                routing = data.get("routing", {})
                                print("\n[Done] Routing Info:")
                                print(json.dumps(routing, indent=2))
                                
                                steps = routing.get("steps", [])
                                tool_called = any(s.get("tool") == "mcp:filesystem:list_directory" for s in steps)
                                if tool_called:
                                    print("\nSUCCESS: Tool 'mcp:filesystem:list_directory' was called.")
                                else:
                                    print("\nWARNING: Tool was not called in the routing steps.")
                            elif data.get("type") == "error":
                                print(f"[Error] {data.get('detail')}")
                        except json.JSONDecodeError:
                            pass
            
            print("\nVerifying server status and self-diagnostic indicators...")
            status_res = await client.get("http://127.0.0.1:8002/api/status")

            if status_res.status_code == 200:
                status_data = status_res.json()
                print("SUCCESS: /api/status retrieved.")
                print(f"Server Status: {status_data.get('status')}")
                print(f"GPU Memory: {status_data.get('gpu_memory')}")
                print(f"Model Consolidation: {status_data.get('model_consolidation')}")
                print(f"Current Model: {status_data.get('current_model')}")
            else:
                print(f"FAILED: /api/status returned {status_res.status_code}")
                
    except Exception as e:
        print(f"Test failed to connect or execute: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(test_tool_calling())
