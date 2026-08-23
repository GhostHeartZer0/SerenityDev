import unittest
import sys
import os
import uuid
import hashlib

# Add root directory to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

# Generate valid hardware sealed key for testing environment
def _gen_test_key():
    mac = str(uuid.getnode()).encode('utf-8')
    entropy = hashlib.sha3_512(mac).digest()
    seed = os.urandom(16) + entropy
    nonce = hashlib.shake_256(seed).digest(12)
    plain_bytes = b"test_secret_key"
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        derived_key = hashlib.shake_256(entropy).digest(32)
        aesgcm = AESGCM(derived_key)
        ciphertext = aesgcm.encrypt(nonce, plain_bytes, None)
    except Exception:
        keystream = hashlib.shake_256(entropy + nonce).digest(len(plain_bytes))
        ciphertext = bytes(b ^ k for b, k in zip(plain_bytes, keystream))
    return f"pqc_v1:{(nonce + ciphertext).hex()}"

os.environ["LOCAL_API_KEY"] = _gen_test_key()

from serenitydevserver import extract_json, _parse_python_func_call

class TestPTCParsing(unittest.TestCase):
    def test_direct_python_func_call(self):
        code = 'read_file(path="serenitydevserver.py", start_line=10, end_line=50)'
        res = extract_json(code)
        self.assertIsNotNone(res)
        self.assertEqual(res["action"], "call_tool")
        self.assertEqual(res["target"], "read_file")
        self.assertEqual(res["arguments_or_instructions"]["path"], "serenitydevserver.py")
        self.assertEqual(res["arguments_or_instructions"]["start_line"], 10)
        self.assertEqual(res["arguments_or_instructions"]["end_line"], 50)

    def test_python_code_block_ptc(self):
        text = """Here is the tool call to list the files:
```python
list_directory(path="./src")
```
"""
        res = extract_json(text)
        self.assertIsNotNone(res)
        self.assertEqual(res["action"], "call_tool")
        self.assertEqual(res["target"], "list_directory")
        self.assertEqual(res["arguments_or_instructions"]["path"], "./src")

    def test_run_command_ptc(self):
        text = 'run_command(command="pytest src/test")'
        res = extract_json(text)
        self.assertIsNotNone(res)
        self.assertEqual(res["action"], "call_tool")
        self.assertEqual(res["target"], "run_command")
        self.assertEqual(res["arguments_or_instructions"]["command"], "pytest src/test")

    def test_multi_replace_ptc(self):
        text = 'multi_replace_string_in_file(path="a.txt", replacements=[{"target": "old", "replacement": "new"}])'
        res = extract_json(text)
        self.assertIsNotNone(res)
        self.assertEqual(res["action"], "call_tool")
        self.assertEqual(res["target"], "multi_replace_string_in_file")
        self.assertEqual(res["arguments_or_instructions"]["path"], "a.txt")
        self.assertEqual(len(res["arguments_or_instructions"]["replacements"]), 1)

    def test_json_fallback(self):
        text = '{"action": "call_tool", "target": "read_file", "arguments_or_instructions": {"path": "test.py"}}'
        res = extract_json(text)
        self.assertIsNotNone(res)
        self.assertEqual(res["target"], "read_file")

    def test_native_tag_fallback(self):
        text = '<|tool_call>call:read_file{path: "test.py"}<tool_call|>'
        res = extract_json(text)
        self.assertIsNotNone(res)
        self.assertEqual(res["target"], "read_file")

if __name__ == "__main__":
    unittest.main()
