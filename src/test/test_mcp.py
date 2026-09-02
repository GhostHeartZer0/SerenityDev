import os
import sys
import json
import unittest
from fastapi.testclient import TestClient

# Add project root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import serenitydevserver
from serenitydevserver import (
    app,
    LongTermMemoryManager,
    MCP_SERVER_INFO,
    MCP_TOOLS_DEFINITIONS,
    SerenityKeyVault,
    LOCAL_API_KEY
)

class TestMCPLogic(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        LongTermMemoryManager.purge_all()
        self.workspace_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.orig_workspace_dir = os.environ.get("SERENITY_WORKSPACE_DIR")
        os.environ["SERENITY_WORKSPACE_DIR"] = self.workspace_dir
        # Reset environment auth/https flags to defaults
        os.environ["ENFORCE_MCP_AUTH"] = "false"
        os.environ["ENFORCE_MCP_HTTPS"] = "false"

    def tearDown(self):
        LongTermMemoryManager.purge_all()
        if self.orig_workspace_dir is not None:
            os.environ["SERENITY_WORKSPACE_DIR"] = self.orig_workspace_dir
        else:
            os.environ.pop("SERENITY_WORKSPACE_DIR", None)
        os.environ["ENFORCE_MCP_AUTH"] = "false"
        os.environ["ENFORCE_MCP_HTTPS"] = "false"

    def test_mcp_get_handshake(self):
        """Verify GET /mcp handshake returns valid MCP metadata."""
        res = self.client.get("/mcp")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "online")
        self.assertEqual(data["protocol"], "Model Context Protocol (MCP)")
        self.assertEqual(data["transport"], "StreamableHTTP")
        self.assertEqual(data["server"]["name"], MCP_SERVER_INFO["name"])

    def test_mcp_jsonrpc_initialize(self):
        """Verify POST /mcp initialize method."""
        req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05"}
        }
        res = self.client.post("/mcp", json=req)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["jsonrpc"], "2.0")
        self.assertEqual(data["id"], 1)
        self.assertEqual(data["result"]["protocolVersion"], "2024-11-05")
        self.assertIn("tools", data["result"]["capabilities"])
        self.assertTrue(data["result"]["capabilities"]["tools"]["listChanged"])
        self.assertIn("resources", data["result"]["capabilities"])
        self.assertIn("prompts", data["result"]["capabilities"])
        self.assertEqual(data["result"]["serverInfo"]["name"], MCP_SERVER_INFO["name"])

    def test_mcp_jsonrpc_notifications_and_ping(self):
        """Verify notifications/initialized, initialized alias, and ping methods."""
        res = self.client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["result"], {})

        res_alias = self.client.post("/mcp", json={"jsonrpc": "2.0", "method": "initialized"})
        self.assertEqual(res_alias.status_code, 200)
        self.assertEqual(res_alias.json()["result"], {})

        res = self.client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "ping"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["result"], {})

    def test_mcp_tools_list(self):
        """Verify tools/list exposes all expected MCP tools."""
        res = self.client.post("/mcp", json={"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
        self.assertEqual(res.status_code, 200)
        data = res.json()
        tools = data["result"]["tools"]
        tool_names = [t["name"] for t in tools]
        expected_tools = [
            "read_file", "write_file", "list_directory", "grep_search",
            "insert_edit_into_file", "replace_string_in_file", "run_command",
            "store_memory", "query_memory", "update_memory", "delete_memory"
        ]
        for expected in expected_tools:
            self.assertIn(expected, tool_names, f"Tool '{expected}' missing from tools/list")

    def test_mcp_tools_call_list_directory(self):
        """Verify tools/call for list_directory."""
        req = {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "list_directory",
                "arguments": {"path": "."}
            }
        }
        res = self.client.post("/mcp", json=req)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertFalse(data["result"]["isError"])
        content_text = data["result"]["content"][0]["text"]
        self.assertIn("serenitydevserver.py", content_text)

    def test_mcp_tools_call_memory_lifecycle(self):
        """Verify tools/call for store_memory, query_memory, update_memory, delete_memory."""
        # 1. store_memory
        res = self.client.post("/mcp", json={
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {
                "name": "store_memory",
                "arguments": {
                    "key": "mcp_test_key",
                    "category": "architecture",
                    "content": "MCP JSON-RPC 2.0 implementation verification"
                }
            }
        })
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.json()["result"]["isError"])

        # 2. query_memory
        res = self.client.post("/mcp", json={
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {
                "name": "query_memory",
                "arguments": {"query": "verification"}
            }
        })
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.json()["result"]["isError"])
        self.assertIn("mcp_test_key", res.json()["result"]["content"][0]["text"])

        # 3. update_memory
        res = self.client.post("/mcp", json={
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {
                "name": "update_memory",
                "arguments": {
                    "key": "mcp_test_key",
                    "content": "Updated MCP JSON-RPC 2.0 content"
                }
            }
        })
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.json()["result"]["isError"])

        # 4. delete_memory
        res = self.client.post("/mcp", json={
            "jsonrpc": "2.0",
            "id": 8,
            "method": "tools/call",
            "params": {
                "name": "delete_memory",
                "arguments": {"key": "mcp_test_key"}
            }
        })
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.json()["result"]["isError"])

    def test_mcp_tools_call_unknown_tool(self):
        """Verify tools/call with unknown tool returns tool error."""
        res = self.client.post("/mcp", json={
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {
                "name": "non_existent_tool",
                "arguments": {}
            }
        })
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["result"]["isError"])

    def test_mcp_unknown_method(self):
        """Verify unknown JSON-RPC method returns -32601."""
        res = self.client.post("/mcp", json={
            "jsonrpc": "2.0",
            "id": 10,
            "method": "invalid/method"
        })
        self.assertEqual(res.status_code, 404)
        data = res.json()
        self.assertEqual(data["error"]["code"], -32601)

    def test_mcp_auth_enforcement(self):
        """Verify ENFORCE_MCP_AUTH token verification."""
        test_token = "serenity_test_secret_mcp_token_12345"
        os.environ["ENFORCE_MCP_AUTH"] = "true"
        os.environ["SERENITY_MCP_TOKEN"] = test_token

        # 1. No token -> 401
        res = self.client.post("/mcp", json={"jsonrpc": "2.0", "id": 11, "method": "ping"})
        self.assertEqual(res.status_code, 401)

        # 2. Invalid token -> 401
        res = self.client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 11, "method": "ping"},
            headers={"Authorization": "Bearer wrong_token"}
        )
        self.assertEqual(res.status_code, 401)

        # 3. Valid Bearer token in header -> 200
        res = self.client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 11, "method": "ping"},
            headers={"Authorization": f"Bearer {test_token}"}
        )
        self.assertEqual(res.status_code, 200)

        # 4. Valid token in query param -> 200
        res = self.client.post(
            f"/mcp?token={test_token}",
            json={"jsonrpc": "2.0", "id": 11, "method": "ping"}
        )
        self.assertEqual(res.status_code, 200)

        # 5. GET endpoint with optional auth -> 200
        res = self.client.get("/mcp")
        self.assertEqual(res.status_code, 200)

    def test_mcp_https_enforcement(self):
        """Verify ENFORCE_MCP_HTTPS header enforcement."""
        os.environ["ENFORCE_MCP_HTTPS"] = "true"
        # Non-localhost host without https -> 403
        res = self.client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 12, "method": "ping"},
            headers={"Host": "192.168.1.50:8002"}
        )
        self.assertEqual(res.status_code, 403)

        # Proxied https header -> 200
        res = self.client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 12, "method": "ping"},
            headers={"Host": "192.168.1.50:8002", "X-Forwarded-Proto": "https"}
        )
        self.assertEqual(res.status_code, 200)

    def test_mcp_tools_call_parameters_and_stringified(self):
        """Verify tools/call handles 'parameters' alias and stringified JSON arguments."""
        # 1. parameters key
        req1 = {
            "jsonrpc": "2.0",
            "id": 101,
            "method": "tools/call",
            "params": {
                "name": "list_directory",
                "parameters": {"path": "."}
            }
        }
        res1 = self.client.post("/mcp", json=req1)
        self.assertEqual(res1.status_code, 200)
        self.assertFalse(res1.json()["result"]["isError"])

        # 2. stringified JSON arguments
        req2 = {
            "jsonrpc": "2.0",
            "id": 102,
            "method": "tools/call",
            "params": {
                "name": "list_directory",
                "arguments": '{"path": "."}'
            }
        }
        res2 = self.client.post("/mcp", json=req2)
        self.assertEqual(res2.status_code, 200)
        self.assertFalse(res2.json()["result"]["isError"])

    def test_mcp_resources_list_and_read(self):
        """Verify resources/list and resources/read endpoints."""
        # 1. resources/list
        res = self.client.post("/mcp", json={"jsonrpc": "2.0", "id": 201, "method": "resources/list"})
        self.assertEqual(res.status_code, 200)
        data = res.json()
        resources = data["result"]["resources"]
        uris = [r["uri"] for r in resources]
        self.assertIn("workspace://files", uris)
        self.assertIn("workspace://git_status", uris)

        # 2. resources/read for workspace://files
        res_files = self.client.post("/mcp", json={
            "jsonrpc": "2.0",
            "id": 202,
            "method": "resources/read",
            "params": {"uri": "workspace://files"}
        })
        self.assertEqual(res_files.status_code, 200)
        files_data = res_files.json()
        contents = files_data["result"]["contents"]
        self.assertEqual(len(contents), 1)
        self.assertEqual(contents[0]["uri"], "workspace://files")
        self.assertEqual(contents[0]["mimeType"], "application/json")
        parsed_files = json.loads(contents[0]["text"])
        self.assertIn("serenitydevserver.py", parsed_files["files"])

        # 3. resources/read for workspace://git_status
        res_git = self.client.post("/mcp", json={
            "jsonrpc": "2.0",
            "id": 203,
            "method": "resources/read",
            "params": {"uri": "workspace://git_status"}
        })
        self.assertEqual(res_git.status_code, 200)
        git_data = res_git.json()
        contents_git = git_data["result"]["contents"]
        self.assertEqual(len(contents_git), 1)
        self.assertEqual(contents_git[0]["uri"], "workspace://git_status")
        self.assertEqual(contents_git[0]["mimeType"], "text/plain")

        # 4. resources/read for invalid uri
        res_invalid = self.client.post("/mcp", json={
            "jsonrpc": "2.0",
            "id": 204,
            "method": "resources/read",
            "params": {"uri": "workspace://unknown_res"}
        })
        self.assertEqual(res_invalid.status_code, 404)
        self.assertEqual(res_invalid.json()["error"]["code"], -32602)

    def test_native_mcp_helpers(self):
        """Verify get_all_local_ips and start_native_mcp network functions."""
        from start_native_mcp import get_all_local_ips, get_local_ip
        local_ip = get_local_ip()
        self.assertTrue(local_ip)
        all_ips = get_all_local_ips()
        self.assertIn(local_ip, all_ips)

if __name__ == "__main__":
    unittest.main()
