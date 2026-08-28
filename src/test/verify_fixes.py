import asyncio
import json
import httpx
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

BASE_URL = "http://127.0.0.1:8002"

async def main():
    print("=== Testing SerenityDev Server ===")
    async with httpx.AsyncClient(timeout=60.0) as client:
        # 1. Test /health and /api/status
        print("\n--- 1. Health & Status Check ---")
        h_res = await client.get(f"{BASE_URL}/health")
        print(f"/health response: {h_res.status_code} -> {h_res.json()}")
        assert h_res.status_code == 200

        s_res = await client.get(f"{BASE_URL}/api/status")
        print(f"/api/status response: {s_res.status_code} -> status={s_res.json().get('status')}, active_model={s_res.json().get('current_model')}")
        assert s_res.status_code == 200

        # 2. Test /api/models and /v1/models (VS Code Copilot local endpoint compatibility)
        print("\n--- 2. Models Endpoint Check ---")
        m_res = await client.get(f"{BASE_URL}/api/models")
        models_data = m_res.json()
        models_list = models_data.get("models", [])
        model_count = len(models_list)
        print(f"/api/models returned {model_count} models.")
        assert model_count >= 20, f"Expected >= 20 models, got {model_count}"
        sample_ids = [m["id"] for m in models_list[:5]]
        print(f"Sample /api/models IDs: {sample_ids}")

        v1_res = await client.get(f"{BASE_URL}/v1/models")
        v1_data = v1_res.json()
        v1_list = v1_data.get("data", [])
        print(f"/v1/models returned {len(v1_list)} models.")
        assert len(v1_list) >= 20

        # 3. Test Small Model Resolution & Direct Llama-CPP Streaming (gemma-4-e2b-q2)
        print("\n--- 3. Testing Direct Llama-CPP Streaming for 'gemma-4-e2b-q2' ---")
        chat_req = {
            "model": "gemma-4-e2b-q2",
            "messages": [{"role": "user", "content": "Respond with the single word 'CONFIRMED'."}],
            "stream": True,
            "temperature": 0.1,
            "max_tokens": 16
        }
        tokens_received = []
        async with client.stream("POST", f"{BASE_URL}/v1/chat/completions", json=chat_req) as response:
            assert response.status_code == 200, f"Stream returned {response.status_code}"
            assert "text/event-stream" in response.headers.get("content-type", "")
            print(f"Response headers validated: Content-Type = {response.headers.get('content-type')}")
            
            async for raw_line in response.aiter_lines():
                line = raw_line.strip()
                if not line:
                    continue
                if line == "data: [DONE]":
                    print("\nReceived [DONE] delimiter.")
                    break
                if line.startswith("data: "):
                    payload_json = line[6:]
                    chunk = json.loads(payload_json)
                    delta = chunk["choices"][0]["delta"].get("content", "")
                    if delta:
                        tokens_received.append(delta)
                        sys.stdout.write(delta)
                        sys.stdout.flush()

        full_output = "".join(tokens_received)
        print(f"\nStream output: '{full_output.strip()}'")
        assert len(tokens_received) > 0, "No tokens received from stream"

        # 4. Test Orchestration /ask_stream tool calling
        print("\n--- 4. Testing /ask_stream Orchestration ---")
        ask_req = {
            "prompt": "List files in the current workspace using directory listing.",
            "session_id": "test_verification"
        }
        ask_events = []
        async with client.stream("POST", f"{BASE_URL}/ask_stream", json=ask_req) as response:
            assert response.status_code == 200
            async for raw_line in response.aiter_lines():
                line = raw_line.strip()
                if line.startswith("data: "):
                    event = json.loads(line[6:])
                    ask_events.append(event)
                    if event.get("type") == "progress":
                        print(f"Progress event: {event.get('text')}")
                    elif event.get("type") == "content":
                        sys.stdout.write(event.get("content", ""))
                        sys.stdout.flush()
                    elif event.get("type") == "done":
                        print(f"\nDone event received: {event.get('routing')}")

        print("\n=== ALL VERIFICATIONS PASSED SUCCESSFULLY ===")

if __name__ == "__main__":
    asyncio.run(main())
