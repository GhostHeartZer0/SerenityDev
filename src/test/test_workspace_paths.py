# test_workspace_paths.py
import unittest
import os
import tempfile
import shutil
from serenitydevserver import (
    resolve_workspace_path,
    get_primary_workspace_dir,
    is_path_allowed,
    dispatch_tool_call
)

class TestWorkspacePaths(unittest.TestCase):
    def setUp(self):
        self.temp_ws = tempfile.mkdtemp(prefix="serenity_test_ws_")
        self.test_file = os.path.join(self.temp_ws, "sample.py")
        with open(self.test_file, "w", encoding="utf-8") as f:
            f.write("def sample():\n    return 42\n")

    def tearDown(self):
        shutil.rmtree(self.temp_ws, ignore_errors=True)

    def test_get_primary_workspace_dir_from_request(self):
        ws = get_primary_workspace_dir(self.temp_ws)
        self.assertEqual(os.path.normcase(ws), os.path.normcase(self.temp_ws))

    def test_resolve_workspace_path_relative(self):
        resolved = resolve_workspace_path("sample.py", req_workspace=self.temp_ws)
        self.assertIsNotNone(resolved)
        self.assertEqual(os.path.normcase(resolved), os.path.normcase(self.test_file))

    def test_is_path_allowed_in_request_workspace(self):
        self.assertTrue(is_path_allowed("sample.py", req_workspace=self.temp_ws))
        self.assertTrue(is_path_allowed(self.test_file, req_workspace=self.temp_ws))

    def test_read_file_tool_dispatch_with_workspace_dir(self):
        res = dispatch_tool_call("read_file", {"path": "sample.py"}, workspace_dir=self.temp_ws)
        self.assertFalse(res["is_error"])
        self.assertIn("def sample():", res["raw_output"])

    def test_list_directory_tool_dispatch_with_workspace_dir(self):
        res = dispatch_tool_call("list_directory", {"path": "."}, workspace_dir=self.temp_ws)
        self.assertFalse(res["is_error"])
        self.assertIn("sample.py", res["raw_output"])

    def test_grep_search_tool_dispatch_with_workspace_dir(self):
        res = dispatch_tool_call("grep_search", {"query": "sample", "path": "."}, workspace_dir=self.temp_ws)
        self.assertFalse(res["is_error"])
        self.assertIn("sample.py", res["raw_output"])

    def test_write_file_tool_dispatch_with_workspace_dir(self):
        res = dispatch_tool_call("write_file", {"path": "created.txt", "content": "hello serenity"}, workspace_dir=self.temp_ws)
        self.assertFalse(res["is_error"])
        created_path = os.path.join(self.temp_ws, "created.txt")
        self.assertTrue(os.path.exists(created_path))
        with open(created_path, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "hello serenity")

if __name__ == "__main__":
    unittest.main()
