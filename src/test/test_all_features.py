import asyncio
import os
import sys
import unittest

# Add project root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import serenitydevserver
from serenitydevserver import (
    LongTermMemoryManager,
    dispatch_tool_call,
    load_server_config,
    save_server_config,
    ConfigUpdate,
    update_config,
    get_config,
    get_all_memories,
    store_memory_endpoint,
    delete_memory_endpoint,
    purge_all_memories_endpoint,
    MemoryStoreRequest,
    clear_all_sessions
)
from fastapi import BackgroundTasks

class TestSerenityOverhaul(unittest.TestCase):
    def setUp(self):
        LongTermMemoryManager.purge_all()

    def tearDown(self):
        LongTermMemoryManager.purge_all()

    def test_ltm_crud(self):
        # 1. Store memory
        entry = LongTermMemoryManager.store("test_key", "architecture", "Use FastAPI backend for orchestration")
        self.assertEqual(entry["key"], "test_key")
        self.assertEqual(entry["category"], "architecture")

        # 2. Query memory
        results = LongTermMemoryManager.query("FastAPI")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["key"], "test_key")

        # 3. Update memory
        updated = LongTermMemoryManager.update("test_key", "Use FastAPI backend on port 8002")
        self.assertIsNotNone(updated)
        self.assertIn("8002", updated["content"])

        # 4. Context summary injection
        summary = LongTermMemoryManager.get_context_summary()
        self.assertIn("ARCHITECTURE", summary)
        self.assertIn("test_key", summary)

        # 5. Delete memory
        deleted = LongTermMemoryManager.delete("test_key")
        self.assertTrue(deleted)
        self.assertEqual(len(LongTermMemoryManager.get_all()), 0)

    def test_tool_dispatch_memory(self):
        # Dispatch store_memory
        res = dispatch_tool_call("store_memory", {
            "key": "arch_pattern",
            "category": "architecture",
            "content": "Decoupled thought reasoning from execution limits"
        })
        self.assertFalse(res["is_error"])
        self.assertEqual(res["status"], "success")

        # Dispatch query_memory
        q_res = dispatch_tool_call("query_memory", {"query": "Decoupled"})
        self.assertFalse(q_res["is_error"])
        self.assertIn("arch_pattern", q_res["raw_output"])

        # Dispatch update_memory
        u_res = dispatch_tool_call("update_memory", {
            "key": "arch_pattern",
            "content": "Decoupled thought reasoning with subagent budgets"
        })
        self.assertFalse(u_res["is_error"])

        # Dispatch delete_memory
        d_res = dispatch_tool_call("delete_memory", {"key": "arch_pattern"})
        self.assertFalse(d_res["is_error"])

    def test_config_endpoints_async(self):
        async def run_async_tests():
            bg = BackgroundTasks()
            # Test decoupled reasoning strength and limit tiers
            cfg_update = ConfigUpdate(
                reasoning_strength="xhigh",
                limit_tier="autonomy",
                gpu_layers=42,
                cache_type_k="turbo4_tcq",
                context_window=32768
            )
            res = await update_config(cfg_update, bg)
            self.assertEqual(res["reasoning_strength"], "xhigh")
            self.assertEqual(res["limit_tier"], "autonomy")
            self.assertTrue(res["auto_continue"])  # Autonomy enables auto-continue
            self.assertEqual(res["gpu_layers"], 42)
            self.assertEqual(res["context_window"], 32768)

            cfg = await get_config()
            self.assertEqual(cfg["reasoning_strength"], "xhigh")
            self.assertEqual(cfg["limit_tier"], "autonomy")

            # Test Memory REST endpoints
            store_res = await store_memory_endpoint(MemoryStoreRequest(
                key="db_schema",
                category="decisions",
                content="Use JSON storage for LTM"
            ))
            self.assertEqual(store_res["status"], "stored")

            all_mems = await get_all_memories()
            self.assertEqual(all_mems["count"], 1)

            del_res = await delete_memory_endpoint("db_schema")
            self.assertEqual(del_res["status"], "deleted")

            # Test session clear
            clear_res = await clear_all_sessions()
            self.assertEqual(clear_res["status"], "cleared")

        asyncio.run(run_async_tests())

if __name__ == "__main__":
    unittest.main()
