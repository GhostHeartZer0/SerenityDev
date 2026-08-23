import os
import sys

workspace_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if workspace_dir not in sys.path:
    sys.path.insert(0, workspace_dir)

def test_endpoints():
    from fastapi.testclient import TestClient
    from serenitydevserver import app, CONTEXT_WINDOW, cache_type_k, cache_type_v

    client = TestClient(app)

    # 1. Test /api/models reporting
    res = client.get("/api/models")
    assert res.status_code == 200, f"/api/models failed: {res.text}"
    models_data = res.json()
    assert "models" in models_data, "No 'models' field in /api/models"
    assert len(models_data["models"]) > 0, "Empty models list"
    first_model = models_data["models"][0]
    assert "id" in first_model
    assert "name" in first_model
    assert "family" in first_model
    assert "maxInputTokens" in first_model
    assert first_model["maxInputTokens"] >= 2048
    assert "capabilities" in first_model
    assert "toolCalling" in first_model["capabilities"]
    assert "imageInput" in first_model["capabilities"]
    assert "tools" in first_model["capabilities"]
    assert "vision" in first_model["capabilities"]
    print("PASS: /api/models reporting validated with context size & capabilities")

    # 2. Test /api/config GET & POST
    cfg_get = client.get("/api/config")
    assert cfg_get.status_code == 200
    cfg_data = cfg_get.json()
    assert "context_window" in cfg_data
    assert "gpu_layers" in cfg_data
    assert "cache_type_k" in cfg_data
    assert "cache_type_v" in cfg_data
    assert "roles" in cfg_data
    assert "auto_continue" in cfg_data
    assert "supervisor_low" in cfg_data["roles"]
    assert "supervisor_high" in cfg_data["roles"]
    assert "orchestrator_turbo" in cfg_data["roles"]
    print("PASS: /api/config GET validated with roles & auto_continue")

    cfg_post = client.post("/api/config", json={
        "cache_type_k": "q4_0",
        "cache_type_v": "q4_0",
        "context_window": 8192,
        "gpu_layers": 16,
        "auto_continue": True,
        "roles": {
            "supervisor_low": "test-low-model",
            "supervisor_high": "test-high-model",
            "w2_code": "test-code-model"
        }
    })
    assert cfg_post.status_code == 200
    post_data = cfg_post.json()
    assert post_data.get("cache_type_k") == "q4_0"
    assert post_data.get("cache_type_v") == "q4_0"
    assert post_data.get("context_window") == 8192
    assert post_data.get("gpu_layers") == 16
    assert post_data.get("auto_continue") is True
    assert post_data.get("roles", {}).get("supervisor_low") == "test-low-model"
    assert post_data.get("roles", {}).get("supervisor_high") == "test-high-model"
    assert post_data.get("roles", {}).get("w2_code") == "test-code-model"
    print("PASS: /api/config POST update validated with roles & auto_continue")

    # 3. Test /api/control/unload & /api/unload
    unload_res = client.post("/api/control/unload")
    assert unload_res.status_code == 200
    assert unload_res.json().get("status") == "unloaded"
    print("PASS: /api/control/unload validated")

    # 4. Test /api/restart
    restart_res = client.post("/api/restart")
    assert restart_res.status_code == 200
    assert "status" in restart_res.json()
    print("PASS: /api/restart validated")

    print("\nAll SerenityDev control, role model assignment, and auto-continue tests passed!")

if __name__ == "__main__":
    test_endpoints()
