# serenitydevserver.py
from startup import initialize_environment

try:
    print("[...] Initiating SerenityDev Secure Boot...")
    initialize_environment()
except RuntimeError as e:
    import sys
    print(f"\n{e}")
    sys.exit(1)

import sys
import asyncio
import threading
import ast
import json
import os
import re
import subprocess
import time
import logging
import hmac
import uuid
import hashlib
import datetime
from typing import Dict, Any, List, Optional, AsyncGenerator
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, BackgroundTasks, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from pydantic import BaseModel
import httpx
import uvicorn
import signal
import cryptography
import difflib
import warnings
warnings.filterwarnings("ignore", category=FutureWarning, module="pynvml")

class SerenityKeyVault:
    """Hardware-bound Key Vault using SHA3-512 & SHAKE-256 (Keccak XOF) multi-factor entropy binding."""

    _cached_entropy: Optional[bytes] = None

    @classmethod
    def get_machine_entropy(cls) -> bytes:
        if cls._cached_entropy is not None:
            return cls._cached_entropy

        components = [str(uuid.getnode()).encode("utf-8")]
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as key:
                guid, _ = winreg.QueryValueEx(key, "MachineGuid")
                if guid:
                    components.append(str(guid).encode("utf-8"))
        except Exception:
            pass

        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        try:
            result = subprocess.run(
                ["wmic", "csproduct", "get", "UUID"],
                capture_output=True,
                text=True,
                timeout=2,
                creationflags=flags
            )
            if result.returncode == 0:
                lines = [line.strip() for line in result.stdout.splitlines() if line.strip() and "UUID" not in line]
                if lines:
                    components.append(lines[0].encode("utf-8"))
        except Exception:
            pass

        combined = b"|".join(components)
        cls._cached_entropy = hashlib.sha3_512(combined).digest()
        return cls._cached_entropy

    @staticmethod
    def get_legacy_entropy() -> bytes:
        return hashlib.sha3_512(str(uuid.getnode()).encode("utf-8")).digest()

    @classmethod
    def get_entropy_candidates(cls) -> List[bytes]:
        candidates = [cls.get_machine_entropy()]
        legacy = cls.get_legacy_entropy()
        if legacy not in candidates:
            candidates.append(legacy)
        return candidates

    @classmethod
    def generate_nonce(cls, entropy: bytes, extra: bytes = b"") -> bytes:
        seed = os.urandom(16) + time.monotonic_ns().to_bytes(8, "big") + entropy + extra
        return hashlib.shake_256(seed).digest(12)

    @classmethod
    def unlock(cls, key_blob: str) -> str:
        if not key_blob:
            return ""
        if not key_blob.startswith("pqc_v1:"):
            raise ValueError("[PQC Error] Key blob is not encrypted with PQC format (pqc_v1:).")

        raw_payload = bytes.fromhex(key_blob[7:])
        if len(raw_payload) < 13:
            raise ValueError("[PQC Error] Key payload too short.")

        nonce = raw_payload[:12]
        ciphertext = raw_payload[12:]
        for entropy in cls.get_entropy_candidates():
            try:
                from cryptography.hazmat.primitives.ciphers.aead import AESGCM
                derived_key = hashlib.shake_256(entropy).digest(32)
                return AESGCM(derived_key).decrypt(nonce, ciphertext, None).decode("utf-8")
            except Exception:
                pass

            try:
                keystream = hashlib.shake_256(entropy + nonce).digest(len(ciphertext))
                plain_bytes = bytes(value ^ key for value, key in zip(ciphertext, keystream))
                text = plain_bytes.decode("utf-8")
                if text and all(32 <= ord(char) <= 126 or char in "\r\n\t" for char in text):
                    return text
            except Exception:
                pass

        raise PermissionError("[Security Breach] Hardware mismatch or corrupted PQC key blob.")

    @classmethod
    def encrypt(cls, plaintext: str) -> str:
        entropy = cls.get_machine_entropy()
        nonce = cls.generate_nonce(entropy)
        plain_bytes = plaintext.encode("utf-8")

        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            derived_key = hashlib.shake_256(entropy).digest(32)
            aesgcm = AESGCM(derived_key)
            ciphertext = aesgcm.encrypt(nonce, plain_bytes, None)
        except ImportError:
            keystream = hashlib.shake_256(entropy + nonce).digest(len(plain_bytes))
            ciphertext = bytes(b ^ k for b, k in zip(plain_bytes, keystream))

        payload = nonce + ciphertext
        return f"pqc_v1:{payload.hex()}"

raw_env_key = os.getenv("LOCAL_API_KEY") or os.getenv("LOCALAPI_KEY") or os.getenv("LOCALAPIKEY") or ""
try:
    LOCAL_API_KEY = SerenityKeyVault.unlock(raw_env_key) if raw_env_key.startswith("pqc_v1") else raw_env_key
except Exception as e:
    LOCAL_API_KEY = raw_env_key
  
# Force working directory to be the directory of this server script
workspace_dir = os.path.dirname(os.path.abspath(__file__))
os.chdir(workspace_dir)

cache_dir = os.path.abspath(os.path.join(workspace_dir, ".serenity_cache"))
cache_subdirs = {
    "TEMP": os.path.join(cache_dir, "temp"),
    "TMP": os.path.join(cache_dir, "temp"),
    "CUDA_CACHE_PATH": os.path.join(cache_dir, "cuda_cache"),
    "TRITON_CACHE_DIR": os.path.join(cache_dir, "triton_cache"),
    "TORCH_EXTENSIONS_DIR": os.path.join(cache_dir, "torch_extensions"),
    "PIP_CACHE_DIR": os.path.join(cache_dir, "pip_cache")
}
for path in set(cache_subdirs.values()):
    os.makedirs(path, exist_ok=True)
for env_var, path in cache_subdirs.items():
    os.environ[env_var] = path

LLAMA_SERVER_BASE = "http://localhost:8080"
LLAMA_SERVER_URL = f"{LLAMA_SERVER_BASE}/v1/completions"
LLAMA_SERVER_CHAT_URL = f"{LLAMA_SERVER_BASE}/v1/chat/completions"
llama_server_process = None

SUPERVISOR_LOW_MODEL = "gemma-4-E4B-it-qat-UD-Q4_K_XL"
SUPERVISOR_HIGH_MODEL = "gemma-4-26B-A4B-it-qat-UD-Q4_K_XL"
ORCHESTRATOR_TURBO_MODEL = "NVIDIA-Nemotron-3.5-Lightning-30B-A3B-MXFP4_MOE"
SUPERVISOR_MODEL = "gemma-4-26B-A4B"
W1_MODEL = "Qwen3.8-27B-UD-Q4_K_XL"  # Reasoning & Architecture
W2_MODEL = "codegemma-7b-it"  # Heavy Code Synthesis
W3_MODEL = "gemma-4-E4B-it-Coder.Q4_K_M"  # Fast Utilities / Scripting / Explanations
W4_MODEL = "gemma4-v2-Q4_K_M"      # Specialized worker
FIM_MODEL = "codegemma-2b"    # Inline Autocomplete

auto_continue_enabled: bool = False  # Unlimited auto-continue iteration toggle
AUTOSWAP_TIMEOUT = 240.0            # Seconds before swapping back to Supervisor VRAM
CONTEXT_WINDOW = int(os.environ.get("SERENITY_CONTEXT_WINDOW", "16384")) # Configurable context window (default 16k)
MAX_RESPONSE_TOKENS = CONTEXT_WINDOW //4  #e.g. 4096 tokens on a 16k window
MAX_RESPONSE_LENGTH = MAX_RESPONSE_TOKENS * 4  # ~16,384 characters

# KV Cache Compression Settings
cache_type_k = "f16"
cache_type_v = "f16"
gpu_layers_override: Optional[int] = None
CURRENT_MODEL = SUPERVISOR_MODEL

# Decoupled Reasoning Strength (Thoughts) and Limit Tiers (Execution bounds)
reasoning_strength: str = "medium"  # "low", "medium", "high", "xhigh"
limit_tier: str = "default"         # "default", "low", "medium", "high", "autonomy"

SERENITY_CONFIG_PATH = os.path.join(workspace_dir, "serenity_config.json")
MEMORY_STORE_PATH = os.path.join(cache_dir, "long_term_memory.json")

class LongTermMemoryManager:
    """Persistent Long-Term Memory Database maintained by agents and users."""
    _lock = threading.Lock()

    @classmethod
    def _load_data(cls) -> Dict[str, Any]:
        with cls._lock:
            if not os.path.exists(MEMORY_STORE_PATH):
                return {}
            try:
                with open(MEMORY_STORE_PATH, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                log_message(f"[Memory Error] Failed to read long_term_memory.json: {e}")
                return {}

    @classmethod
    def _save_data(cls, data: Dict[str, Any]):
        with cls._lock:
            try:
                os.makedirs(os.path.dirname(MEMORY_STORE_PATH), exist_ok=True)
                with open(MEMORY_STORE_PATH, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
            except Exception as e:
                log_message(f"[Memory Error] Failed to save long_term_memory.json: {e}")

    @classmethod
    def store(cls, key: str, category: str, content: str, source: str = "agent") -> Dict[str, Any]:
        data = cls._load_data()
        clean_key = str(key).strip().lower().replace(" ", "_")
        entry = {
            "key": clean_key,
            "category": str(category).strip().lower() if category else "general",
            "content": str(content).strip(),
            "source": source,
            "updated_at": time.time(),
            "created_at": data.get(clean_key, {}).get("created_at", time.time())
        }
        data[clean_key] = entry
        cls._save_data(data)
        log_message(f"[Memory] Stored persistent memory '{clean_key}' under category '{entry['category']}'")
        return entry

    @classmethod
    def query(cls, query_text: str, category: Optional[str] = None) -> List[Dict[str, Any]]:
        data = cls._load_data()
        if not data:
            return []
        q_lower = (query_text or "").strip().lower()
        cat_lower = (category or "").strip().lower() if category else None
        results = []
        for k, v in data.items():
            if cat_lower and v.get("category") != cat_lower:
                continue
            if not q_lower:
                results.append(v)
            else:
                score = 0
                k_match = q_lower in k
                c_match = q_lower in v.get("content", "").lower()
                cat_match = q_lower in v.get("category", "").lower()
                if k_match: score += 5
                if c_match: score += 3
                if cat_match: score += 2
                for word in q_lower.split():
                    if len(word) > 2:
                        if word in k: score += 2
                        if word in v.get("content", "").lower(): score += 1
                if score > 0:
                    results.append((score, v))
        if q_lower:
            results.sort(key=lambda x: x[0], reverse=True)
            return [item[1] for item in results[:15]]
        return list(data.values())

    @classmethod
    def update(cls, key: str, content: str) -> Optional[Dict[str, Any]]:
        data = cls._load_data()
        clean_key = str(key).strip().lower().replace(" ", "_")
        if clean_key not in data:
            return None
        data[clean_key]["content"] = str(content).strip()
        data[clean_key]["updated_at"] = time.time()
        cls._save_data(data)
        log_message(f"[Memory] Updated memory '{clean_key}'")
        return data[clean_key]

    @classmethod
    def delete(cls, key: str) -> bool:
        data = cls._load_data()
        clean_key = str(key).strip().lower().replace(" ", "_")
        if clean_key in data:
            del data[clean_key]
            cls._save_data(data)
            log_message(f"[Memory] Deleted memory '{clean_key}'")
            return True
        return False

    @classmethod
    def purge_all(cls) -> int:
        data = cls._load_data()
        count = len(data)
        cls._save_data({})
        log_message(f"[Memory] Purged all {count} long-term memory entries.")
        return count

    @classmethod
    def get_all(cls) -> List[Dict[str, Any]]:
        data = cls._load_data()
        return sorted(list(data.values()), key=lambda x: x.get("updated_at", 0), reverse=True)

    @classmethod
    def get_context_summary(cls, max_items: int = 6) -> str:
        """Formats top long-term memory facts for agent system prompt context injection."""
        items = cls.get_all()[:max_items]
        if not items:
            return ""
        lines = ["[Long-Term Memory / Persistent Knowledge]"]
        for it in items:
            lines.append(f"- [{it.get('category', 'general').upper()}] {it.get('key')}: {it.get('content')}")
        return "\n".join(lines)

def load_server_config():
    """Loads saved server configuration from serenity_config.json on startup."""
    global cache_type_k, cache_type_v, gpu_layers_override, CONTEXT_WINDOW, auto_continue_enabled
    global CURRENT_MODEL, SUPERVISOR_LOW_MODEL, SUPERVISOR_HIGH_MODEL, ORCHESTRATOR_TURBO_MODEL
    global SUPERVISOR_MODEL, W1_MODEL, W2_MODEL, W3_MODEL, W4_MODEL, FIM_MODEL
    global reasoning_strength, limit_tier
    if not os.path.exists(SERENITY_CONFIG_PATH):
        return
    try:
        with open(SERENITY_CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        cache_type_k = cfg.get("cache_type_k", "f16")
        cache_type_v = cfg.get("cache_type_v", "f16")
        gpu_layers_override = cfg.get("gpu_layers")
        CONTEXT_WINDOW = cfg.get("context_window", 16384)
        auto_continue_enabled = cfg.get("auto_continue", False)
        CURRENT_MODEL = cfg.get("current_model", SUPERVISOR_MODEL)
        reasoning_strength = cfg.get("reasoning_strength", "medium")
        limit_tier = cfg.get("limit_tier", "default")
        roles = cfg.get("roles", {})
        SUPERVISOR_LOW_MODEL = roles.get("supervisor_low", SUPERVISOR_LOW_MODEL)
        SUPERVISOR_HIGH_MODEL = roles.get("supervisor_high", SUPERVISOR_HIGH_MODEL)
        ORCHESTRATOR_TURBO_MODEL = roles.get("orchestrator_turbo", ORCHESTRATOR_TURBO_MODEL)
        SUPERVISOR_MODEL = cfg.get("current_model", SUPERVISOR_MODEL)
        W1_MODEL = roles.get("w1_reasoning", W1_MODEL)
        W2_MODEL = roles.get("w2_code", W2_MODEL)
        W3_MODEL = roles.get("w3_fast", W3_MODEL)
        W4_MODEL = roles.get("w4_specialized", W4_MODEL)
        FIM_MODEL = roles.get("fim", FIM_MODEL)
    except Exception as e:
        print(f"[!] Failed to load serenity_config.json: {e}")

def save_server_config():
    cfg = {
        "cache_type_k": cache_type_k,
        "cache_type_v": cache_type_v,
        "gpu_layers": gpu_layers_override,
        "context_window": CONTEXT_WINDOW,
        "auto_continue": auto_continue_enabled,
        "current_model": CURRENT_MODEL,
        "reasoning_strength": reasoning_strength,
        "limit_tier": limit_tier,
        "roles": {
            "supervisor_low": SUPERVISOR_LOW_MODEL,
            "supervisor_high": SUPERVISOR_HIGH_MODEL,
            "orchestrator_turbo": ORCHESTRATOR_TURBO_MODEL,
            "w1_reasoning": W1_MODEL,
            "w2_code": W2_MODEL,
            "w3_fast": W3_MODEL,
            "w4_specialized": W4_MODEL,
            "fim": FIM_MODEL
        }
    }
    try:
        with open(SERENITY_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        print(f"[!] Failed to save serenity_config.json: {e}")

# --- Security & Validation Helpers ---
# NOTE: Security hardened - Subprocess command execution & path containment module
def validate_path_containment(target_path: str, base_workspace: str, allowed_dirs: Optional[List[str]] = None) -> bool:
    """Validates that target_path stays within base_workspace or user-allowed directories (resolving symlinks and drive casing)."""
    if not target_path:
        return False
    try:
        real_target = os.path.realpath(target_path)
        candidates = [base_workspace] + (allowed_dirs or [])

        for base in candidates:
            if not base:
                continue
            real_base = os.path.realpath(base)

            #Normalize casing for Windows paths
            if sys.platform == "win32":
                rt = os.path.normcase(real_target)
                rb = os.path.normcase(real_base)

            else:
                rt, rb = real_target, real_base

            if rt == rb or rt.startswith(rb + os.sep):        
                return True
        return False
    except Exception:
        return False
        

# Global State Management
inference_lock = asyncio.Lock()
autoswap_timer_task: Optional[asyncio.Task] = None
active_models_list: List[str] = []
model_registry_initialized = False
model_registry_refresh_lock = asyncio.Lock()
orchestrator_logs: List[str] = []
independenttask_count: int = 0  # Tracks concurrent FIM requests
file_edit_backups: Dict[str, Dict[str, Any]] = {}
sessions_history: Dict[str, List[Dict[str, str]]] = {}
server_paused = False
active_system_plan = {"focus": "None", "steps": []}

def log_message(msg: str):
    clean_msg = "".join(c for c in msg if 32 <= ord(c) <= 126 or c in "\n\r\t")
    sys.stdout.write(clean_msg + "\n")
    sys.stdout.flush()
    global orchestrator_logs
    orchestrator_logs.append(clean_msg)
    if len(orchestrator_logs) > 60:
        orchestrator_logs.pop(0)

class CircuitBreaker:
    """Sliding-window circuit breaker for API calls and tool executions (adapted from gork-build xai-circuit-breaker)."""
    def __init__(self, failure_threshold: int = 5, recovery_timeout: float = 30.0):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.failures: List[float] = []
        self.state = "CLOSED"  # CLOSED, OPEN, HALF-OPEN
        self.last_state_change = time.time()

    def record_success(self):
        self.failures.clear()
        self.state = "CLOSED"

    def record_failure(self):
        now = time.time()
        self.failures.append(now)
        self.failures = [t for t in self.failures if now - t <= 60.0]
        if len(self.failures) >= self.failure_threshold:
            self.state = "OPEN"
            self.last_state_change = now

    def allow_request(self) -> bool:
        now = time.time()
        if self.state == "OPEN":
            if now - self.last_state_change > self.recovery_timeout:
                self.state = "HALF-OPEN"
                return True
            return False
        return True

class WorkspaceQueue:
    """Session & workspace-aware prompt queue manager (adapted from gork-build xai-prompt-queue)."""
    def __init__(self):
        self.queues: Dict[str, List[Dict[str, Any]]] = {} # session_id -> list of prompt entries
        self.running_prompts: Dict[str, Optional[str]] = {} # session_id -> running prompt id

    def enqueue(self, session_id: str, prompt_id: str, text: str, kind: str = "user"):
        if session_id not in self.queues:
            self.queues[session_id] = []
        entry = {"id": prompt_id, "text": text, "kind": kind, "timestamp": time.time()}
        self.queues[session_id].append(entry)
        return entry

    def dequeue(self, session_id: str) -> Optional[Dict[str, Any]]:
        if session_id in self.queues and self.queues[session_id]:
            item = self.queues[session_id].pop(0)
            self.running_prompts[session_id] = item["id"]
            return item
        self.running_prompts[session_id] = None
        return None

    def clear_queue(self, session_id: str):
        if session_id in self.queues:
            self.queues[session_id].clear()

    def get_status(self, session_id: str) -> Dict[str, Any]:
        return {
            "session_id": session_id,
            "running_prompt_id": self.running_prompts.get(session_id),
            "pending_count": len(self.queues.get(session_id, []))
        }

def trim_context_tool_pairs(messages: List[Dict[str, str]], max_items: int) -> List[Dict[str, str]]:
    """Context compaction helper (adapted from gork-build xai-grok-compaction).
    Ensures tool calls and tool responses are never split across context window boundaries."""
    if len(messages) <= max_items:
        return messages

    trimmed = messages[-max_items:]
    # If the first message in trimmed is an orphan tool response without its matching call, drop it
    if trimmed and ("System Tool Response" in trimmed[0].get("content", "") or trimmed[0].get("role") == "tool"):
        trimmed = trimmed[1:]
    return trimmed

global_circuit_breaker = CircuitBreaker()
global_workspace_queue = WorkspaceQueue()

class SessionRotationManager:
    idle_minutes: int = 10
    max_age_hours: float = 1.0
    last_client_activity: float = time.time()
    key_created_at: float = time.time()
    rotation_epoch: int = 1
    pending_rotation_notice: bool = False

    @classmethod
    def record_activity(cls):
        cls.last_client_activity = time.time()

    @classmethod
    def rotate(cls, store_and_resume: bool = True) -> Dict[str, Any]:
        global sessions_history
        cls.rotation_epoch += 1
        cls.key_created_at = time.time()
        cls.pending_rotation_notice = True

        cache_path = os.path.join(cache_dir, "session_state.pqc")
        stored_items_count = 0
        if store_and_resume:
            try:
                state = {
                    "sessions_history": sessions_history,
                    "workspace_queues": global_workspace_queue.queues
                }
                encrypted_state = SerenityKeyVault.encrypt(json.dumps(state))
                with open(cache_path, "w", encoding="utf-8") as f:
                    f.write(encrypted_state)

                stored_items_count = len(sessions_history)
            except Exception as e:
                logging.error(f"[Session Rotation] Failed to store session state: {e}")

            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    blob = f.read()
                decrypted_str = SerenityKeyVault.unlock(blob)
                restored = json.loads(decrypted_str)
                if isinstance(restored, dict):
                    if "sessions_history" in restored and isinstance(restored["sessions_history"], dict):
                        sessions_history.update(restored["sessions_history"])
                    if "workspace_queues" in restored and isinstance(restored["workspace_queues"], dict):
                        for k, v in restored["workspace_queues"].items():
                            global_workspace_queue.queues[k] = v
            except Exception as e:
                logging.error(f"[Session Rotation] Failed to restore session state: {e}")

        logging.info(f"[Security] Session rotated to Epoch {cls.rotation_epoch} (store_and_resume={store_and_resume}).")
        return {
            "epoch": cls.rotation_epoch,
            "key_created_at": cls.key_created_at,
            "stored_and_resumed": store_and_resume,
            "stored_sessions_count": stored_items_count,
            "notice": "Session state preserved. Master key rotated successfully."
        }

    @classmethod
    def get_status(cls) -> Dict[str, Any]:
        now = time.time()
        idle_duration = now - cls.last_client_activity
        key_age_seconds = now - cls.key_created_at
        return {
            "rotation_epoch": cls.rotation_epoch,
            "key_created_at": cls.key_created_at,
            "key_age_seconds": round(key_age_seconds, 2),
            "idle_duration_seconds": round(idle_duration, 2),
            "idle_threshold_minutes": cls.idle_minutes,
            "max_age_hours": cls.max_age_hours,
            "pending_rotation_notice": cls.pending_rotation_notice,
            "hardware_binding": "SHA3-512 + SHAKE-256 Multi-Factor (MAC + MachineGuid + BIOS UUID)"
        }

    @classmethod
    def check_downtime_and_rotate(cls):
        now = time.time()
        idle_seconds = now - cls.last_client_activity
        key_age_seconds = now - cls.key_created_at
        if idle_seconds >= cls.idle_minutes * 60 and key_age_seconds >= cls.max_age_hours * 3600:
            cls.rotate(store_and_resume=True)

class SessionRotationMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        SessionRotationManager.record_activity()
        response = await call_next(request)
        return response

import importlib.util
active_llama_model = None
active_llama_model_name = None
llama_cpp_available = False
if importlib.util.find_spec("llama_cpp") is not None:
    try:
        import llama_cpp
        llama_cpp_available = True
    except Exception:
        pass

custom_models_dirs: List[str] = []

def get_candidate_model_dirs() -> List[str]:
    """Returns candidate directories to search for GGUF model files."""
    base_dir = workspace_dir
    dirs = [os.path.join(base_dir, "models")]
    env_paths = os.environ.get("SERENITY_MODELS_PATH") or os.environ.get("MODELS_DIR")
    if env_paths:
        for p in env_paths.replace(";", os.pathsep).split(os.pathsep):
            p = p.strip()
            if p and os.path.exists(p) and p not in dirs:
                dirs.append(p)
    for p in custom_models_dirs:
        if p and os.path.exists(p) and p not in dirs:
            dirs.append(p)

    # 3. Saved custom dirs in models_dirs.json
    dirs_file = os.path.join(base_dir, "models_dirs.json")
    if os.path.exists(dirs_file):
        try:
            with open(dirs_file, "r", encoding="utf-8") as f:
                saved = json.load(f)
                if isinstance(saved, list):
                    for p in saved:
                        p = str(p).strip()
                        if p and os.path.exists(p) and p not in dirs:
                            dirs.append(p)
        except Exception:
            pass
            
    return [os.path.abspath(p) for p in dirs if os.path.isdir(p)]

def add_custom_model_dir(dir_path: str) -> bool:
    """Adds a custom folder path to search for GGUF models and updates models_dirs.json."""
    if not dir_path or not os.path.exists(dir_path):
        return False
    norm_path = os.path.abspath(dir_path)
    if norm_path not in custom_models_dirs:
        custom_models_dirs.append(norm_path)
    base_dir = os.path.dirname(os.path.abspath(__file__))
    dirs_file = os.path.join(base_dir, "models_dirs.json")
    try:
        with open(dirs_file, "w", encoding="utf-8") as f:
            json.dump(custom_models_dirs, f, indent=4)
    except Exception as e:
        log_message(f"[Models] Error saving models_dirs.json: {e}")
    # Refresh installed models
    get_installed_models()
    return True

def resolve_gguf_path(model_name: str) -> Optional[str]:
    if not model_name:
        return None

    base_dir = os.path.dirname(os.path.abspath(__file__))
    
    json_path = os.path.join(base_dir, "models.json")
    if os.path.exists(json_path):
        try:
            with open(json_path, 'r', encoding='utf-8') as f:
                model_map = json.load(f)
                if model_name in model_map:
                    mapped_path = model_map[model_name]
                    if os.path.exists(mapped_path):
                        return mapped_path
        except Exception as e:
            log_message(f"[Models] Error loading models.json: {e}")

    if os.path.exists(model_name):
        return model_name

    target_file = f"{model_name}.gguf" if not model_name.endswith('.gguf') else model_name
    candidate_dirs = get_candidate_model_dirs()

    for models_dir in candidate_dirs:
        if not os.path.exists(models_dir):
            continue
        direct_match = os.path.join(models_dir, target_file)
        if os.path.exists(direct_match):
            return direct_match
        
        for root, _, files in os.walk(models_dir):
            if target_file in files:
                return os.path.join(root, target_file)

    return None

active_llama_server_model_name = None

def unload_llama_server():
    """Stops the llama-server process to free VRAM cleanly without hanging."""
    global llama_server_process, active_llama_server_model_name
    try:
        if llama_server_process is not None:
            log_message("[Llama-Server] Stopping active llama-server process to free VRAM...")
            try:
                llama_server_process.terminate()
                llama_server_process.wait(timeout=2)
            except Exception:
                if llama_server_process is not None:
                    llama_server_process.kill()
            llama_server_process = None
            log_message("[Llama-Server] Successfully terminated.")
    except Exception as e:
        log_message(f"[Llama-Server] Warning killing llama-server: {e}")
        if llama_server_process is not None:
            try:
                llama_server_process.kill()
            except Exception:
                pass
            llama_server_process = None
    finally:
        active_llama_server_model_name = None

def unload_llama_model():
    global active_llama_model, active_llama_model_name
    try:
        if active_llama_model is not None:
            if hasattr(active_llama_model, "close"):
                active_llama_model.close()
            del active_llama_model
            import gc; gc.collect()
            log_message("[Llama-CPP] Direct model offloaded successfully.")
    except Exception as e:
        log_message(f"[Llama-CPP] Warning unloading direct model: {e}")
    finally:
        active_llama_model = None
        active_llama_model_name = None

unload_direct_llama_model = unload_llama_model

def unload_all_models():
    """Release both model backends so a replacement cannot overlap in memory."""
    unload_llama_server()
    with direct_llama_lock:
        unload_llama_model()

async def start_llama_server(model_name: str, n_ctx: int, force_reload: bool = False):
    """Starts the llama-server subprocess if using API fallback."""
    global llama_server_process, active_llama_server_model_name
    
    with direct_llama_lock:
        unload_llama_model()
    if not force_reload and llama_server_process is not None and llama_server_process.poll() is None and active_llama_server_model_name == model_name:
        return
    if force_reload or llama_server_process is None or active_llama_server_model_name != model_name:
        unload_llama_server()
    
    gguf_path = resolve_gguf_path(model_name)
    if not gguf_path:
        raise ValueError(f"Could not resolve GGUF path for model: {model_name}")
        
    try:
        gpu_layers, offload_kqv = calculate_dynamic_gpu_layers(model_name, n_ctx)
        if gpu_layers <= 0:
            gpu_layers = 1
    except Exception as e:
        log_message(f"[Llama-Server] Error calculating GPU layers: {e}. Falling back to 4 layers.")
        gpu_layers = 4
        offload_kqv = True

    log_message(f"[Llama-Server] Starting server for {model_name} from {gguf_path} (n_ctx={n_ctx}, gpu_layers={gpu_layers}, offload_kqv={offload_kqv})...")
    
    import shutil
    llama_bin = os.environ.get("LLAMA_SERVER_BIN") or shutil.which("llama-server") or shutil.which("llama-server.exe")
    if not llama_bin:
        for p in [
            r"C:\Program Files\Android\Android Studio\plugins\gemini\resources\llamacpp\llama-server.exe",
            os.path.expanduser(r"~\Desktop\Desktop\Hub\SerenityPC\temp_zip\llama-server.exe"),
            os.path.expanduser(r"~\llama-server.exe")
        ]:
            if os.path.exists(p):
                llama_bin = p
                break
    if not llama_bin:
        log_message("[Llama-Server] llama-server executable not found in PATH or LLAMA_SERVER_BIN.")
        raise FileNotFoundError("llama-server executable not found in PATH. Set LLAMA_SERVER_BIN or install llama-server.")

    cmd = [
        llama_bin,
        "-m", gguf_path,
        "-c", str(n_ctx),
        "-ngl", str(gpu_layers),
        "-sm", "none",
        "--port", "8080",
        "--host", "127.0.0.1",
        "-fa", "on" # flash attention
    ]
    if "glimmer" in model_name.lower():
        base_model_dir = os.path.dirname(gguf_path)
        chat_template_path = os.path.join(base_model_dir, "chat template.txt")
        if not os.path.exists(chat_template_path) and os.path.exists(r"S:\LLM\META ASI (Muse Glimmer)\chat template.txt"):
            chat_template_path = r"S:\LLM\META ASI (Muse Glimmer)\chat template.txt"
            
        if os.path.exists(chat_template_path):
            cmd.extend(["--chat-template-file", chat_template_path])
    if cache_type_k and cache_type_k != "f16":
        cmd.extend(["--cache-type-k", cache_type_k])
    if cache_type_v and cache_type_v != "f16":
        cmd.extend(["--cache-type-v", cache_type_v])
        
    if not offload_kqv:
        cmd.append("-nkvo")

    
    import subprocess
    try:
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        # Popen capturing stdout and stderr to inspect process health and log errors
        llama_server_process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=flags
        )

        stderr_lines: List[str] = []
        def _drain_stream(stream, sink: Optional[List[str]] = None):
            try:
                for line in iter(stream.readline, ''):
                    if sink is not None and len(sink) < 100:
                        sink.append(line)
            except Exception:
                pass
            finally:
                try:
                    stream.close()
                except Exception:
                    pass

        threading.Thread(target=_drain_stream, args=(llama_server_process.stdout, None), daemon=True).start()
        threading.Thread(target=_drain_stream, args=(llama_server_process.stderr, stderr_lines), daemon=True).start()
        
        # Poll health status to ensure it's ready
        log_message("[Llama-Server] Server process spawned. Polling health check...")
        
        healthy = False
        err_msg = ""
        async with httpx.AsyncClient(timeout=1.0) as client:
            for i in range(45):
                await asyncio.sleep(1.0)

                # Instant check: if process died, break immediately and capture stderr
                if llama_server_process.poll() is not None:
                    err_msg = "".join(stderr_lines).strip() or f"Exited with code {llama_server_process.returncode}"
                    log_message(f"[Llama-Server Error] Process exited prematurely: {err_msg}")
                    break

                try:
                    res = await client.get(f"{LLAMA_SERVER_BASE}/health")
                    if res.status_code == 200:
                        healthy = True
                        break
                except Exception:
                    try:
                        res = await client.get(f"{LLAMA_SERVER_BASE}/v1/models")
                        if res.status_code == 200:
                            healthy = True
                            break
                    except Exception:
                        pass
        
        if healthy:
            log_message("[Llama-Server] Server is healthy and responding.")
            active_llama_server_model_name = model_name
        else:
            if not err_msg:
                if llama_server_process.poll() is not None:
                    err_msg = "".join(stderr_lines).strip() or f"Exited with code {llama_server_process.returncode}"
                else:
                    err_msg = "Health check timed out after 45 seconds"
            log_message(f"[Llama-Server Error] Startup failed: {err_msg}")
            unload_llama_server()
            raise RuntimeError(f"llama-server failed to start: {err_msg}")
    except Exception as e:
        log_message(f"[Llama-Server] Failed to spawn server: {e}")
        unload_llama_server()
        raise RuntimeError(f"Failed to spawn llama-server: {e}")


def get_ggml_type(cache_type_str: str) -> Optional[int]:
    import llama_cpp
    mapping = {
        "f16": llama_cpp.GGML_TYPE_F16,
        "fp16": llama_cpp.GGML_TYPE_F16,
        "q8_0": llama_cpp.GGML_TYPE_Q8_0,
        "q5_1": llama_cpp.GGML_TYPE_Q5_1,
        "q5_0": llama_cpp.GGML_TYPE_Q5_0,
        "q4_0": llama_cpp.GGML_TYPE_Q4_0,
        "turbo4_tcq": 44,
        "turbo3_tcq": 45,
        "turbo2_tcq": 46,
    }
    cleaned = cache_type_str.lower().strip()
    if "q4_k" in cleaned:
        return getattr(llama_cpp, "GGML_TYPE_Q4_K", None)
    if "q8_k" in cleaned:
        return getattr(llama_cpp, "GGML_TYPE_Q8_K", None)
    return mapping.get(cleaned)

MODEL_MAX_CONTEXT_LIMITS = {
    "codegemma": 8192,
    "gemma-2": 8192,
    "gemma-2b": 8192,
    "gemma-7b": 8192,
}

def cap_n_ctx_for_model(model_name: str, requested_n_ctx: int) -> int:
    model_lower = model_name.lower()
    for key, max_limit in MODEL_MAX_CONTEXT_LIMITS.items():
        if key in model_lower:
            return min(requested_n_ctx, max_limit)
    return requested_n_ctx

def get_llama_model(model_name: str, n_ctx: int = 16384, force_reload: bool = False):
    global active_llama_model, active_llama_model_name
    n_ctx = cap_n_ctx_for_model(model_name, n_ctx)

    with direct_llama_lock:
        unload_llama_server()
        if active_llama_model is not None and (force_reload or active_llama_model_name != model_name or active_llama_model.n_ctx() < n_ctx):
            unload_direct_llama_model()
        
        if active_llama_model is None:
            gguf_path = resolve_gguf_path(model_name)
            if not gguf_path:
                raise ValueError(f"Could not resolve GGUF path for model: {model_name}")
            
            from llama_cpp import Llama
            log_message(f"[Llama-CPP] Loading model {model_name} (n_ctx={n_ctx}, K={cache_type_k}, V={cache_type_v})...")
            layers, offload_kqv = calculate_dynamic_gpu_layers(model_name, n_ctx)

            type_k = get_ggml_type(cache_type_k)
            type_v = get_ggml_type(cache_type_v)

            kwargs = {
                "model_path": gguf_path,
                "n_ctx": n_ctx,
                "n_gpu_layers": layers,
                "split_mode": 0, # LLAMA_SPLIT_MODE_NONE (prevents GGML_SCHED_MAX_SPLIT_INPUTS clipping on hybrid offload)
                "flash_attn": offload_kqv,
                "verbose": False
            }
            if type_k is not None:
                kwargs["type_k"] = type_k
            if type_v is not None:
                kwargs["type_v"] = type_v
            if not offload_kqv:
                kwargs["offload_kqv"] = False

            active_llama_model = Llama(**kwargs)

        active_llama_model_name = model_name    
        return active_llama_model

direct_llama_lock = threading.RLock()

async def generate_completion_stream(model_name: str, prompt: str, temperature: float, num_ctx: int, max_tokens: int = -1, stop: Optional[List[str]] = None, min_p: float = 0.05, repeat_penalty: float = 1.05):
    if stop is None:
        stop = ["<turn|>", "<|turn|>", "<bos>", "<eos>"]

    if llama_cpp_available and "glimmer" not in model_name.lower():
        try:
            gguf_path = resolve_gguf_path(model_name)
            if gguf_path:
                queue = asyncio.Queue()
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = asyncio.get_event_loop()
                cancel_event = threading.Event()
                        
                def producer():
                    try:
                        with direct_llama_lock:
                            if cancel_event.is_set():
                                return
                            # Initial model load with current CONTEXT_WINDOW
                            llm = get_llama_model(model_name, CONTEXT_WINDOW)
                            
                            # Pre-check token count and dynamic context expansion with clamped headroom
                            try:
                                import math
                                prompt_tokens = llm.tokenize(prompt.encode("utf-8"))
                                token_count = len(prompt_tokens)
                                
                                headroom = max_tokens if (max_tokens > 0 and max_tokens <= 2048) else 2048
                                needed_ctx = token_count + headroom
                                # Target context rounded up to 2048 chunks
                                target_ctx = math.ceil(needed_ctx / 2048) * 2048
                                target_ctx = max(target_ctx, CONTEXT_WINDOW)
                                capped_ctx = cap_n_ctx_for_model(model_name, target_ctx)

                                if needed_ctx > llm.n_ctx() and llm.n_ctx() < capped_ctx:
                                    log_message(f"[Llama-CPP] Context expansion triggered: {llm.n_ctx()} -> {capped_ctx} (Needed: {needed_ctx})")
                                    llm = get_llama_model(model_name, capped_ctx, force_reload=True)
                            except Exception as token_ex:
                                log_message(f"[Llama-CPP] Token count pre-check notice: {token_ex}")

                            limit_tokens = max_tokens if max_tokens > 0 else None
                            chunks = llm(
                                prompt=prompt,
                                max_tokens=limit_tokens,
                                temperature=temperature,
                                stop=stop,
                                stream=True,
                                min_p=min_p,
                                repeat_penalty=repeat_penalty
                            )
                            for chunk in chunks:
                                if cancel_event.is_set(): 
                                    break
                                if isinstance(chunk, dict) and chunk.get("choices"):
                                    text = chunk["choices"][0].get("text", "")
                                    if text:
                                        loop.call_soon_threadsafe(queue.put_nowait, text)
                    except Exception as ex:
                        log_message(f"[Llama-CPP] Stream Generation Error: {ex}")
                        loop.call_soon_threadsafe(queue.put_nowait, ex)
                    finally:
                        loop.call_soon_threadsafe(queue.put_nowait, None)
                loop.run_in_executor(None, producer)
                try:
                    while True:
                        val = await queue.get()
                        if val is None:
                            break
                        if isinstance(val, Exception):
                            raise val
                        yield val
                except asyncio.CancelledError:
                    cancel_event.set()
                    log_message("[Llama-CPP] Stream cancelled by client connection drop.")
                    raise
                return
        except asyncio.CancelledError:
            log_message("[Llama-CPP] Stream Generation cancelled.")
            raise
        except Exception as e:
            log_message(f"[Llama.cpp Stream Error] Direct Streaming Failed, falling back to Llama-Server API: {e}")        

    try:
        if llama_server_process is None or active_llama_server_model_name != model_name:
            await start_llama_server(model_name, num_ctx)
    except Exception as server_err:
        log_message(f"[Llama-Server Startup Error] {server_err}")
        if llama_cpp_available and resolve_gguf_path(model_name):
            log_message(f"[Fallback] Attempting direct llama_cpp load for '{model_name}'...")
            try:
                llm = get_llama_model(model_name, num_ctx)
                chunks = llm(
                    prompt=prompt,
                    max_tokens=max_tokens if max_tokens > 0 else None,
                    temperature=temperature,
                    stop=stop,
                    stream=True,
                    min_p=min_p,
                    repeat_penalty=repeat_penalty
                )
                for chunk in chunks:
                    if isinstance(chunk, dict) and "choices" in chunk and len(chunk["choices"]) > 0:
                        text = chunk["choices"][0].get("text", "")
                        if text:
                            yield text
                return
            except Exception as direct_err:
                log_message(f"[Fallback Error] Direct llama_cpp load also failed for '{model_name}': {direct_err}")
                yield f"\n\n❌ **Model Error:** Failed to execute model '{model_name}': {direct_err}"
                return
        else:
            yield f"\n\n❌ **Model Error:** Failed to load model '{model_name}'. Please verify model status and context settings."
            return

    payload = {
        "model": model_name,
        "prompt": prompt,
        "stream": True,
        "temperature": temperature,
        "max_tokens": max_tokens if max_tokens > 0 else 2048,
        "stop": stop,
        "min_p": min_p,
        "repeat_penalty": repeat_penalty
    }
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            async with client.stream("POST", LLAMA_SERVER_URL, json=payload) as res:
                async for line in res.aiter_lines():
                    line = line.strip()
                    if line.startswith("data: "):
                        data_str = line[6:]
                        if data_str == "[DONE]":
                            break
                        try:
                            data = json.loads(data_str)
                            if "choices" in data and len(data["choices"]) > 0:
                                chunk = data["choices"][0].get("text", "")
                                if chunk:
                                    yield chunk
                        except Exception:
                            pass
    except asyncio.CancelledError:
        log_message("[Llama-Server Stream] Stream cancelled by client connection drop.")
        raise

async def generate_completion(model_name: str, prompt: str, temperature: float, num_ctx: int, max_tokens: int = -1, stop: Optional[List[str]] = None, min_p: float = 0.05, repeat_penalty: float = 1.05) -> Dict[str, Any]:
    result = []
    try:
        async for chunk in generate_completion_stream(model_name, prompt, temperature, num_ctx, max_tokens, stop, min_p, repeat_penalty):
            result.append(chunk)
    except asyncio.CancelledError:
        log_message(f"[Completion Cancelled] Task cancelled for model {model_name}")
        raise
    return {"response": "".join(result)}

PYTHON_TOOL_STUBS = """AVAILABLE TOOLS (Programmatic Tool Calling):
```python
def create_or_update_plan(steps: list[str], current_focus: str) -> dict:
def list_directory(path: str = ".") -> list[str]:
def read_file(path: str, start_line: int | None = None, end_line: int | None = None) -> str:
def write_file(path: str, content: str) -> str:
def insert_edit_into_file(path: str, target_content: str, new_content: str) -> str:
def replace_string_in_file(path: str, target_content: str, new_content: str) -> str:
def multi_replace_string_in_file(path: str, replacements: list[dict[str, str]]) -> str:
def grep_search(query: str, path: str = ".") -> list[str]:
def run_command(command: str) -> str:
```"""

SUPERVISOR_PROMPT = f"""
Reasoning strength: xhigh.
You are the Orchestrator of a multi-agent system. Your goal is to manage tasks by delegating to specialized workers or using available tools.

AVAILABLE WORKERS:
- gemma-4-26B-A4B-it-qat-UD-Q4_K_XL(Agent): Specializes in complex tasks that require deep reasoning
- gemma-4-E4B-it-qat-UD-Q4_K_XL(Agent): Specializes in fast and efficient reasoning and architecture tasks
- codegemma-2b-it(Agent): Specializes in code synthesis and inline autocomplete tasks
- gemma-4-E4B-it-Coder.Q4_K_M(Agent): Competent, small but suprisingly capable all-around agent
- codegemma-7b-it-f16(Agent): Specializes in heavy code synthesis and complex programming tasks
- gemma-4-v2-Q4_K_M(Agent): Built on the 12B architecture for coding + agentic work — writing code, running commands, using tools, debugging, multi-step technical tasks.

{PYTHON_TOOL_STUBS}

DECISION PROCESS:
1. Analyze the current state and user request.
2. Determine if a tool can solve the problem directly.
3. If not, determine which worker is best suited for the task.
4. Formulate a precise instruction or tool call.

OUTPUT FORMAT:
You MUST respond with a single JSON object or Programmatic Tool Call. Do not include extraneous text.

SCHEMA:
{{
  "action": "call_tool" | "delegate_worker",
  "target": "name_of_tool_or_worker",
  "arguments_or_instructions": {{ "key": "value" }} or "string_instructions",
  "step_summary": "Short summary of the step",
  "reason": "Brief explanation of why this action was chosen"
}}
"""
class QueryRequest(BaseModel):
    prompt: str
    context: str = ""
    model: Optional[str] = None
    session_id: Optional[str] = None
    workspace_dir: Optional[str] = None

class FimRequest(BaseModel):
    prefix: str
    suffix: str
    model: Optional[str] = None


FIM_CONTEXT_CHARS = 16000


def fit_fim_context(prefix: str, suffix: str) -> tuple[str, str]:
    """Keep inline completion prompts within the native context of small FIM models."""
    prefix_budget = min(len(prefix), FIM_CONTEXT_CHARS * 3 // 4)
    suffix_budget = FIM_CONTEXT_CHARS - prefix_budget
    return prefix[-prefix_budget:], suffix[:suffix_budget]

# --- Startup Check & Auto-Registration ---

def get_installed_models() -> List[str]:
    """Scan local modes directory recursively for GGUF files, register them, and maintains a models.json"""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    models = []

    json_path = os.path.join(base_dir, "models.json")
    model_map = {}
    map_updated = False
    
    if os.path.exists(json_path):
        try:
            with open(json_path, 'r', encoding='utf-8') as f:
                model_map = json.load(f)
                for model_name, path in list(model_map.items()):
                    model_name_lower = model_name.lower()
                    if any(tag in model_name_lower for tag in ['mmproj', 'assistant', 'mtp', 'glimmer', 'gemma-3', 'dflash', 'diffusiongemma']):
                        del model_map[model_name]
                        map_updated = True
                        continue
                    if os.path.exists(path):
                        models.append(model_name)
        except Exception as e:
            log_message(f"[Models] Error reading models.json: {e}")

    candidate_dirs = get_candidate_model_dirs()
    for models_dir in candidate_dirs:
        try:
            if not os.path.exists(models_dir):
                if models_dir == os.path.join(base_dir, "models"):
                    os.makedirs(models_dir, exist_ok=True)
                continue
                
            for root, _, files in os.walk(models_dir):
                for f in files:
                    if f.lower().endswith('.gguf'):
                        name = f[:-5]
                        name_lower = name.lower()
                        if any(tag in name_lower for tag in ['mmproj', 'assistant', 'mtp', 'glimmer', 'gemma-3', 'dflash', 'diffusiongemma']):
                            continue
                        abs_path = os.path.abspath(os.path.join(root, f))
                        if name not in models:
                            models.append(name)

                        abs_path = os.path.abspath(os.path.join(root, f))
                        if name not in model_map or model_map[name] != abs_path:
                            model_map[name] = abs_path
                            map_updated = True
        except Exception as e:
            log_message(f"[Models] Error scanning for Models in {models_dir}: {e}")

    if map_updated or not os.path.exists(json_path):
        try:
            with open(json_path, 'w', encoding='utf-8') as f:
                json.dump(model_map, f, indent=4)
        except Exception as e:
            log_message(f"[Models] Error saving models.json: {e}")

    return models

def check_and_register_models():
    """Initializes active model targets by auto-discovering GGUFs."""
    global active_models_list, model_registry_initialized, SUPERVISOR_MODEL, SUPERVISOR_LOW_MODEL, SUPERVISOR_HIGH_MODEL, ORCHESTRATOR_TURBO_MODEL, W1_MODEL, W2_MODEL, W3_MODEL, W4_MODEL, FIM_MODEL
    log_message("[Startup] Scanning for local model files...")
    active_models_list = get_installed_models()
    model_registry_initialized = True
    log_message(f"[Startup] Found models: {active_models_list}")

    # Reload saved user config first so saved assignments take precedence
    load_server_config()

    if active_models_list:
        models = sorted(active_models_list, reverse=True)
        
        main_models = [m for m in models if not any(tag in m.lower() for tag in ['mmproj', 'assistant', 'mtp', 'glimmer', 'gemma-3', 'dflash', 'diffusiongemma'])]
        if not main_models:
            main_models = models
            
        if not FIM_MODEL:
            fim_candidates = [m for m in main_models if '2b' in m.lower() or 'code' in m.lower()]
            FIM_MODEL = fim_candidates[-1] if fim_candidates else main_models[-1]

        if not W2_MODEL:
            w2_candidates = [m for m in main_models if ('7b' in m.lower() or 'code' in m.lower()) and m != FIM_MODEL]
            W2_MODEL = w2_candidates[0] if w2_candidates else main_models[0]

        if not SUPERVISOR_MODEL:
            super_candidates = [m for m in main_models if 'a4b' in m.lower() or '35b' in m.lower() or '26b' in m.lower()]
            SUPERVISOR_MODEL = super_candidates[0] if super_candidates else main_models[0]
            SUPERVISOR_LOW_MODEL = SUPERVISOR_MODEL
            SUPERVISOR_HIGH_MODEL = SUPERVISOR_MODEL
            ORCHESTRATOR_TURBO_MODEL = SUPERVISOR_MODEL
            W1_MODEL = SUPERVISOR_MODEL
            W3_MODEL = super_candidates[1] if len(super_candidates) > 1 else SUPERVISOR_MODEL
            W4_MODEL = super_candidates[2] if len(super_candidates) > 2 else SUPERVISOR_MODEL

        # Re-apply persisted config to guarantee exact user choices remain active
        load_server_config()

        log_message(f"[Startup] Active Supervisor: {SUPERVISOR_MODEL}")
        log_message(f"[Startup] Active Agent (Code): {W2_MODEL}")
        log_message(f"[Startup] Active Agent (FIM): {FIM_MODEL}")

async def autonomous_downtime_session_rotation_loop():
    while True:
        try:
            await asyncio.sleep(60)
            SessionRotationManager.check_downtime_and_rotate()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logging.error(f"[Downtime Rotation] Loop error: {e}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    global active_models_list, model_registry_initialized
    load_server_config()
    active_models_list = get_installed_models()
    model_registry_initialized = True
    log_message(f"[Startup] Model registry initialized: {len(active_models_list)} local GGUF model(s)")
    if active_models_list:
        log_message(f"[Startup] Registered models: {', '.join(active_models_list)}")
    else:
        log_message("[Startup] No local GGUF models found. Check SERENITY_MODELS_PATH or models_dirs.json.")
    yield

app = FastAPI(title="Serenity Orchestrator Core", lifespan=lifespan)
app.add_middleware(SessionRotationMiddleware)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

class PQCEnforcementMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, secret_key: str):
        super().__init__(app)
        self.secret_key = secret_key

    async def dispatch(self, request: Request, call_next) -> Response:
        pqc_signature = request.headers.get("X-PQC-Signature")
        timestamp_str = request.headers.get("X-PQC-Timestamp")

        if pqc_signature or timestamp_str:
            try:
                ts = float(timestamp_str or "0")
                if abs(time.time() - ts) > 30:
                    return JSONResponse(status_code=401, content={"detail": "Request expired / Replay detected."})
                mac_bytes = uuid.getnode().to_bytes(8, byteorder="big")
                expected_sig = hashlib.sha3_512(mac_bytes + str(int(ts)).encode("utf-8") + self.secret_key.encode("utf-8")).hexdigest()
                if not hmac.compare_digest(pqc_signature or "", expected_sig):
                    return JSONResponse(status_code=401, content={"detail": "Cryptographic Identity Mismatch."})
            except Exception as e:
                return JSONResponse(status_code=401, content={"detail": f"PQC Signature Verification Failed: {str(e)}"})

        return await call_next(request)

app.add_middleware(PQCEnforcementMiddleware, secret_key=LOCAL_API_KEY)

import importlib.util
# llama-cpp-python state management
active_llama_model = None
active_llama_model_name = None
llama_cpp_available = False
if importlib.util.find_spec("llama_cpp") is not None:
    try:
        import llama_cpp  # type: ignore
        llama_cpp_available = True
    except Exception:
        pass

# --- GPU Layer Offloading Helpers ---

def get_vram_info(ctx_size: int = 16384) -> dict:
    """Queries GPU VRAM metrics: Total, Free, Used, Self (devserver/llama-server), and Target Usable VRAM with Shared VRAM Guard."""
    total_mb = 0.0
    free_mb = 0.0
    used_mb = 0.0
    self_used_mb = 0.0

    # 1. High precision query via pynvml / nvidia-ml-py
    try:
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", category=FutureWarning)
            import pynvml
            pynvml.nvmlInit()
            handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            mem_info = pynvml.nvmlDeviceGetMemoryInfo(handle)
            total_mb = float(mem_info.total) / (1024 * 1024)
            free_mb = float(mem_info.free) / (1024 * 1024)
            used_mb = float(mem_info.used) / (1024 * 1024)

        target_pids = {os.getpid()}
        if 'llama_server_process' in globals() and llama_server_process and llama_server_process.poll() is None:
            target_pids.add(llama_server_process.pid)

        procs = pynvml.nvmlDeviceGetComputeRunningProcesses(handle) + pynvml.nvmlDeviceGetGraphicsRunningProcesses(handle)
        for proc in procs:
            if proc.pid in target_pids and proc.usedGpuMemory:
                self_used_mb += proc.usedGpuMemory / (1024 * 1024)
    except Exception:
        pass

    # 2. Fallback query via nvidia-smi
    if total_mb <= 0:
        try:
            flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            res = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.total,memory.free,memory.used", "--format=csv,nounits,noheader"],
                capture_output=True, text=True, check=True, creationflags=flags
            )
            lines = res.stdout.strip().split("\n")
            if lines:
                parts = [float(x.strip()) for x in lines[0].split(",")]
                if len(parts) >= 3:
                    total_mb, free_mb, used_mb = parts[0], parts[1], parts[2]
        except Exception:
            pass

    if total_mb <= 0:
        total_mb = 6144.0
        free_mb = 4000.0
        used_mb = 2144.0

    usable_mb = free_mb + self_used_mb
    
    # Shared VRAM Guard:
    # Base CUDA driver buffer from 600 MB to 900-1000MB
    graph_scratch_mb = max(384.0, (ctx_size / 49152.0) * 1024.0)
    safety_headroom_mb = min(2200.0, 950.0 + graph_scratch_mb)
    
    target_vram = max(200.0, usable_mb - safety_headroom_mb)
    target_vram = min(target_vram, total_mb - safety_headroom_mb)

    return {
        "total": total_mb,
        "free": free_mb,
        "used": used_mb,
        "self_used": self_used_mb,
        "headroom": safety_headroom_mb,
        "available_target": target_vram
    }

def get_target_vram_mb() -> float:
    return get_vram_info()["available_target"]


def get_model_info(model_name: str):
    """Estimate model info (layers, size, native context length) from local GGUF or fallback to defaults."""
    gguf_path = resolve_gguf_path(model_name)
    if gguf_path and os.path.exists(gguf_path):
        size = os.path.getsize(gguf_path)
        layers = None
        ctx_len = None
        # Parse block count and context length from GGUF metadata
        try:
            import struct
            with open(gguf_path, "rb") as f:
                header = f.read(128 * 1024)
                if header[:4] == b'GGUF':
                    idx_bc = header.find(b"block_count")
                    if idx_bc != -1:
                        type_offset = idx_bc + len("block_count")
                        val_type = struct.unpack("<I", header[type_offset:type_offset+4])[0]
                        if val_type in (4, 5):  # UINT32 or INT32
                            l_val = struct.unpack("<I" if val_type == 4 else "<i", header[type_offset+4:type_offset+8])[0]
                            if 0 < l_val < 150:
                                layers = l_val

                    idx_ctx = header.find(b"context_length")
                    if idx_ctx != -1:
                        type_offset = idx_ctx + len("context_length")
                        val_type = struct.unpack("<I", header[type_offset:type_offset+4])[0]
                        if val_type in (4, 5):
                            c_val = struct.unpack("<I" if val_type == 4 else "<i", header[type_offset+4:type_offset+8])[0]
                            if c_val >= 512:
                                ctx_len = c_val
                        elif val_type in (8, 10):
                            c_val = struct.unpack("<Q" if val_type == 8 else "<q", header[type_offset+4:type_offset+12])[0]
                            if c_val >= 512:
                                ctx_len = c_val
        except Exception:
            pass

        if layers is None:
            # Fallbacks based on size
            if size > 20 * 1024 * 1024 * 1024:
                layers = 60
            elif size > 10 * 1024 * 1024 * 1024:
                layers = 40
            else:
                layers = 32
        return layers, size, ctx_len
    return 32, int(7e9 * 0.55), None

def calculate_dynamic_gpu_layers(model_name: str, ctx_size: int) -> tuple[int, bool]:
    """
    Calculates safe GPU layer offload and KV cache offloading strategy with Shared VRAM Guard.
    Guards against GGML_SCHED_MAX_SPLIT_INPUTS graph assertion failures and Windows Shared VRAM thrashing.
    """
    global gpu_layers_override
    vram_info = get_vram_info(ctx_size=ctx_size)
    targeted_reserve_vram_mb = vram_info["available_target"]
    total_layers, model_base_vram_bytes, _ = get_model_info(model_name)
    if total_layers == 0:
        total_layers = 32
    
    model_base_vram_mb = model_base_vram_bytes / (1024 * 1024)
    vram_per_layer = model_base_vram_mb / total_layers
    
    # Estimate KV cache requirement
    kv_cache_vram_mb = max(256.0, (ctx_size / 49152.0) * 3150.0)
    
    # Shared VRAM & Graph-Split Guard Strategy:
    # 1. If context is high (>= 16384) or model has SWA (e.g. gemma, gemma-4) or model cannot fit 100% on GPU,
    #    offloading KV cache to GPU while splitting layers causes GGML_SCHED_MAX_SPLIT_INPUTS assertion crashes.
    # 2. Moving KV cache to system RAM preserves GPU memory strictly for layer weights and avoids split input graph explosion.
    if (model_base_vram_mb + kv_cache_vram_mb) <= targeted_reserve_vram_mb:
        # Full model and KV cache fit cleanly within dedicated VRAM
        safe_layers = total_layers
        offload_kqv = True
    elif model_base_vram_mb <= targeted_reserve_vram_mb:
        # Weights fit in VRAM, but KV cache would risk spilling into shared memory / graph pressure
        safe_layers = total_layers
        offload_kqv = False
        log_message(f"[VRAM-GUARD] Model weights fit entirely in VRAM ({model_base_vram_mb:.1f}MB). Retaining KV Cache in RAM to guard against Shared VRAM paging.")
    else:
        # Partial layer offload required
        # For partial offload, ALWAYS keep KV cache in RAM to prevent GGML split graph overflows & VRAM thrash
        offload_kqv = False
        available_weight_vram = targeted_reserve_vram_mb
        safe_layers = int(available_weight_vram // vram_per_layer)
        
        # Guard against micro-splits
        if safe_layers < 2:
            safe_layers = 1
        safe_layers = max(1, min(total_layers, safe_layers))
        log_message(f"[VRAM-GUARD] Partial layer offload mode ({safe_layers}/{total_layers} layers). KV Cache pinned to RAM (offload_kqv=False) to prevent GGML_SCHED_MAX_SPLIT_INPUTS graph assertion crashes.")

    if gpu_layers_override is not None:
        if gpu_layers_override == 0:
            final_layers = 0
            offload_kqv = False
            log_message(f"[VRAM-CONFIG] Explicit CPU-only mode: 0 layers.")
        else:
            requested = gpu_layers_override if (gpu_layers_override > 0 and gpu_layers_override < 90) else safe_layers
            final_layers = min(requested, safe_layers)
            if final_layers < total_layers:
                offload_kqv = False
            log_message(f"[VRAM-CONFIG] User requested {gpu_layers_override} layers. Clamped to safe physical limit: {final_layers}/{total_layers} layers (offload_kqv={offload_kqv}).")
    else:
        final_layers = max(1, min(total_layers, safe_layers))
    
    log_message("--- SHARED VRAM GUARD REPORT ---")
    log_message(f"Model:            {model_name}")
    log_message(f"Total GPU VRAM:   {vram_info['total']:.1f} MiB")
    log_message(f"Free VRAM:        {vram_info['free']:.1f} MiB")
    log_message(f"Self VRAM:        {vram_info['self_used']:.1f} MiB")
    log_message(f"Guard Headroom:   {vram_info['headroom']:.1f} MiB")
    log_message(f"Total Layers:     {total_layers}")
    log_message(f"File Size:        {model_base_vram_mb:.1f} MiB (~{vram_per_layer:.1f} MiB/layer)")
    log_message(f"Est. KV Cache:    {kv_cache_vram_mb:.1f} MiB (Offloaded: {offload_kqv})")
    log_message(f"Target VRAM:      {targeted_reserve_vram_mb:.1f} MiB")
    log_message(f"Action:           Offloading {final_layers}/{total_layers} layers to GPU")
    log_message("--------------------------------")
    return final_layers, offload_kqv

# --- Graceful Fallbacks ---

async def resolve_model(target: str) -> str:
    """Returns the requested model if installed, or falls back to Supervisor."""
    global active_models_list, model_registry_initialized
    if not model_registry_initialized or not active_models_list:
        loop = asyncio.get_event_loop()
        active_models_list = await loop.run_in_executor(None, get_installed_models)
        model_registry_initialized = True

    if not target:
        return SUPERVISOR_MODEL

    # Resolve public role aliases before matching installed model names.
    role_aliases = {
        "serenity-supervisor": SUPERVISOR_MODEL,
        "serenity-supervisor-high": SUPERVISOR_HIGH_MODEL,
        "serenity-supervisor-low": SUPERVISOR_LOW_MODEL,
        "custom-model": SUPERVISOR_MODEL,
        "default": SUPERVISOR_MODEL,
        "copilot-chat": SUPERVISOR_MODEL,
        "gpt-4": SUPERVISOR_MODEL,
        "gpt-4o": SUPERVISOR_MODEL,
        "gpt-4o-mini": W3_MODEL,
        "gpt-3.5-turbo": W3_MODEL,
        "claude-3-5-sonnet": SUPERVISOR_MODEL,
    }
    target_clean = target.strip()
    target_lower = target_clean.lower()
    requested_target = role_aliases.get(target_lower, target_clean)
    req_lower = requested_target.lower()

    # 1. Exact case-insensitive match
    for m in active_models_list:
        if m.lower() == req_lower:
            return m

    # 2. Check if direct file / gguf path resolves
    direct_path = resolve_gguf_path(requested_target)
    if direct_path and os.path.exists(direct_path):
        base_no_ext = os.path.splitext(os.path.basename(direct_path))[0]
        if base_no_ext in active_models_list:
            return base_no_ext
        return requested_target

    # 3. Token-based and Substring scoring
    def extract_tokens(s: str) -> List[str]:
        return [tok for tok in re.split(r'[^a-zA-Z0-9]+', s.lower()) if tok]

    target_tokens = extract_tokens(req_lower)
    
    scored_candidates = []
    for model_name in active_models_list:
        m_lower = model_name.lower()
        if any(tag in m_lower for tag in ['mmproj', 'assistant', 'mtp']):
            continue
        
        m_tokens = extract_tokens(m_lower)
        
        # Calculate token match score
        matches = 0
        for t_tok in target_tokens:
            if any(t_tok == m_tok or m_tok.startswith(t_tok) or t_tok.startswith(m_tok) for m_tok in m_tokens):
                matches += 1
                
        token_ratio = matches / len(target_tokens) if target_tokens else 0.0
        prefix_bonus = 1.0 if m_lower.startswith(req_lower[:min(len(req_lower), 6)]) else 0.0
        total_score = (token_ratio * 10.0) + prefix_bonus
        if token_ratio >= 0.5:
            scored_candidates.append((total_score, matches, model_name))

    if scored_candidates:
        scored_candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
        matched = scored_candidates[0][2]
        log_message(f"[Model Resolver] Fuzzy matched '{target}' -> '{matched}' (score: {scored_candidates[0][0]:.1f})")
        return matched

    log_message(f"[Fallback] Model '{target}' resolved to '{requested_target}' but was not found in active models. Falling back to '{SUPERVISOR_MODEL}'.")
    return SUPERVISOR_MODEL

def strip_thought_blocks(text: str) -> str:
    """Removes thinking/reasoning blocks only, preserving all tool calls and response content."""
    if not text:
        return ""
    orig_text = text
    text = re.sub(r'ễ.*?ễ', '', text, flags=re.DOTALL)
    text = re.sub(r'<\|?(?:thought|think)\|?>.*?(?:</\|?(?:thought|think)\|?>)', '', text, flags=re.DOTALL)
    text = re.sub(r'<\|?channel\|?>?thought.*?(?:<channel\|?>?)', '', text, flags=re.DOTALL)
    
    cleaned = text.strip()
    if not cleaned:
        # Fallback: if all text was stripped, try removing unclosed tags from the end
        unclosed_stripped = orig_text
        unclosed_stripped = re.sub(r'<\|?(?:thought|think)\|?>.*$', '', unclosed_stripped, flags=re.DOTALL)
        unclosed_stripped = re.sub(r'<\|?channel\|?>?thought.*$', '', unclosed_stripped, flags=re.DOTALL)
        cleaned_unclosed = unclosed_stripped.strip()
        if cleaned_unclosed:
            return cleaned_unclosed
        return orig_text.strip()
    return cleaned

def clean_thought_and_whitespace(text: str) -> str:
    """Removes thinking blocks, tool call tags, and leading/trailing blank lines/whitespace for final display."""
    if not text:
        return ""
    text = strip_thought_blocks(text)
    text = _strip_tool_call_tags(text)
    return text.strip()

def normalize_tool_action(obj: Any) -> Optional[Dict[str, Any]]:
    """Normalizes parsed JSON dicts or arrays into standard Serenity tool call structures."""
    if isinstance(obj, list) and len(obj) > 0:
        obj = obj[0]
        
    if isinstance(obj, dict):
        if "action" in obj and "target" in obj:
            return obj
        tool_name = obj.get("tool_name") or obj.get("name") or obj.get("tool") or obj.get("function") or obj.get("target")
        args = obj.get("args") or obj.get("arguments") or obj.get("parameters") or obj.get("arguments_or_instructions") or {}
        reason = obj.get("reason") or obj.get("explanation") or "Parsed tool action"
        summary = obj.get("step_summary") or obj.get("summary") or (f"Execute tool: {tool_name}" if tool_name else "Execute step")
        
        if tool_name:
            action_type = "delegate_worker" if tool_name in ["W1", "W2", "W3", "W4"] else "call_tool"
            return {
                "action": action_type,
                "target": tool_name,
                "arguments_or_instructions": args,
                "step_summary": summary,
                "reason": reason
            }
        if "steps" in obj or "focus" in obj:
            return {
                "action": "call_tool",
                "target": "mcp:filesystem:create_or_update_plan",
                "arguments_or_instructions": obj,
                "step_summary": "Plan updated",
                "reason": "Plan update"
            }
    return None

def _strip_tool_call_tags(text: str) -> str:
    """Strips native tool call tags and tool invocation statements from text using brace-balanced matching."""
    if not text:
        return ""
    
    # Fast check
    if "call:" not in text and "<|tool_call" not in text and "<tool_call" not in text and "tool_call" not in text and "```json" not in text:
        return text

    # Strip XML-style <tool_call>...</tool_call>
    text = re.sub(r'<tool_call>.*?(?:</tool_call>|$)', '', text, flags=re.DOTALL)

    # Pre-pass: Strip markdown code blocks containing tool call JSON
    text = re.sub(r'```json\s*\{\s*["\']action["\']\s*:\s*["\'](?:call_tool|delegate_worker)["\'].*?```', '', text, flags=re.DOTALL)

    result = []
    i = 0
    length = len(text)
    
    # Match prefixes like <|channel>thought <|tool_call>call:func_name or simple call:func_name
    tool_prefix_pattern = re.compile(
        r'(?:<\|?channel\|?>)*\s*(?:<\|?tool_call\|?>?)*\s*call:\s*([^\s{\(]+)\s*',
        re.DOTALL
    )

    while i < length:
        match = tool_prefix_pattern.search(text, i)
        if not match:
            result.append(text[i:])
            break
        
        # Append text prior to the tool call
        result.append(text[i:match.start()])
        
        start_idx = match.end()
        end_idx = start_idx

        # Check for brace args block
        if start_idx < length and text[start_idx] == '{':
            balanced_end = _find_balanced_brace(text, start_idx)
            if balanced_end != -1:
                end_idx = balanced_end + 1
            else:
                # Unbalanced brace - consume to end or next tag
                end_idx = length
        elif start_idx < length and text[start_idx] == '(':
            # Fallback parenthesized args
            paren_close = text.find(')', start_idx)
            if paren_close != -1:
                end_idx = paren_close + 1
            else:
                end_idx = length

        # Consume trailing closing tool_call tags if present
        tail = text[end_idx:]
        end_tag_match = re.match(r'\s*(?:<\|?tool_call\|?>|</?\|?tool_call\|?>)', tail)
        if end_tag_match:
            end_idx += end_tag_match.end()

        stripped_content = text[match.start():end_idx]
        log_message(f"[Tool Filter] Stripped native tool call text ({len(stripped_content)} chars): {stripped_content[:80]}...")

        i = end_idx

    cleaned_text = "".join(result)
    # Secondary pass to strip any orphaned <|tool_call>, <tool_call|>, </tool_call> tags left in stream
    cleaned_text = re.sub(r'<\|?tool_call\|?>|</?\|?tool_call\|?>', '', cleaned_text)
    return cleaned_text

def _find_balanced_brace(text: str, start: int = 0) -> int:
    """Find index of the matching closing brace, skipping braces inside string literals and quotes."""
    if start >= len(text) or text[start] != '{':
        return -1
    depth = 0
    i = start
    in_tq = False     # inside """..."""
    in_tmpl = False   # inside <|"|>...<|"|>
    in_str = False    # inside standard "..."
    str_char = None
    
    while i < len(text):
        if not in_str and text[i:i+3] == '"""' and not in_tmpl:
            in_tq = not in_tq
            i += 3
            continue
        if not in_str and text[i:i+5] == '<|"|>' and not in_tq:
            in_tmpl = not in_tmpl
            i += 5
            continue
            
        if not in_tq and not in_tmpl:
            ch = text[i]
            if in_str:
                if ch == '\\':
                    i += 2  # skip escaped character
                    continue
                elif ch == str_char:
                    in_str = False
                    str_char = None
            else:
                if ch in ('"', "'"):
                    in_str = True
                    str_char = ch
                elif ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        return i
        i += 1
    return -1

def _parse_native_tool_args(args_str: str) -> dict:
    """Parse Gemma-style native tool call args with triple-quote and template-quote delimiters."""
    s = args_str.strip()
    if s.startswith('{'):
        s = s[1:]
    if s.endswith('}'):
        s = s[:-1]
    result = {}
    i = 0
    while i < len(s):
        # Skip whitespace and commas
        while i < len(s) and s[i] in ' \t\n\r,':
            i += 1
        if i >= len(s):
            break
        # Extract key
        colon_idx = s.find(':', i)
        if colon_idx == -1:
            break
        key = s[i:colon_idx].strip()
        i = colon_idx + 1
        # Skip whitespace after colon
        while i < len(s) and s[i] in ' \t\n\r':
            i += 1
        if i >= len(s):
            result[key] = ""
            break
        # Extract value based on delimiter
        if s[i:i+3] == '"""':
            i += 3
            end = s.find('"""', i)
            if end == -1:
                result[key] = s[i:]
                break
            result[key] = s[i:end]
            i = end + 3
        elif s[i:i+5] == '<|"|>':
            i += 5
            end = s.find('<|"|>', i)
            if end == -1:
                result[key] = s[i:]
                break
            result[key] = s[i:end]
            i = end + 5
        elif s[i] == '"':
            i += 1
            val_start = i
            while i < len(s) and s[i] != '"':
                if s[i] == '\\':
                    i += 2
                    continue
                i += 1
            result[key] = s[val_start:i]
            if i < len(s):
                i += 1
        elif s[i] == '{':
            brace_end = _find_balanced_brace(s, i)
            if brace_end == -1:
                result[key] = s[i:]
                break
            nested = s[i:brace_end+1]
            try:
                result[key] = json.loads(nested)
            except Exception:
                result[key] = nested
            i = brace_end + 1
        elif s[i] == '[':
            bracket_depth = 1
            j = i + 1
            while j < len(s) and bracket_depth > 0:
                if s[j] == '[': bracket_depth += 1
                elif s[j] == ']': bracket_depth -= 1
                j += 1
            nested = s[i:j]
            try:
                result[key] = json.loads(nested)
            except Exception:
                result[key] = nested
            i = j
        else:
            val_start = i
            while i < len(s) and s[i] not in ',}\n':
                i += 1
            val = s[val_start:i].strip()
            if val.lower() == 'true':
                result[key] = True
            elif val.lower() == 'false':
                result[key] = False
            else:
                try:
                    result[key] = int(val)
                except ValueError:
                    try:
                        result[key] = float(val)
                    except ValueError:
                        result[key] = val
    return result

def _parse_python_func_call(call_str: str) -> Optional[Dict[str, Any]]:
    """Safely parses a Python function call into tool_name and args dict using AST."""
    try:
        parsed = ast.parse(call_str.strip(), mode='eval')
        if isinstance(parsed.body, ast.Call):
            func_node = parsed.body.func
            func_name = ""
            if isinstance(func_node, ast.Name):
                func_name = func_node.id
            elif isinstance(func_node, ast.Attribute):
                func_name = func_node.attr

            if not func_name or func_name in ["print", "len", "str", "int", "float", "list", "dict", "set"]:
                return None

            args = {}
            for kw in parsed.body.keywords:
                if kw.arg:
                    try:
                        args[kw.arg] = ast.literal_eval(kw.value)
                    except Exception:
                        if isinstance(kw.value, ast.Constant):
                            args[kw.arg] = kw.value.value
                        else:
                            args[kw.arg] = ast.unparse(kw.value)

            # Positional args fallback
            if parsed.body.args and not args:
                pos_vals = []
                for a in parsed.body.args:
                    try:
                        pos_vals.append(ast.literal_eval(a))
                    except Exception:
                        if isinstance(a, ast.Constant):
                            pos_vals.append(a.value)
                        else:
                            pos_vals.append(ast.unparse(a))
                if func_name in ["run_command", "exec", "terminal", "bash", "sh"]:
                    args["command"] = pos_vals[0] if pos_vals else ""
                elif func_name in ["read_file", "view_file", "write_file", "list_directory"]:
                    args["path"] = pos_vals[0] if pos_vals else "."
                elif func_name in ["grep_search", "grep"]:
                    args["query"] = pos_vals[0] if pos_vals else ""
                elif func_name in ["create_or_update_plan"]:
                    args["steps"] = pos_vals[0] if pos_vals else []

            return {
                "action": "call_tool",
                "target": func_name,
                "arguments_or_instructions": args,
                "step_summary": f"PTC tool call: {func_name}",
                "reason": "Parsed from Programmatic Tool Call (PTC)"
            }
    except Exception:
        pass
    return None

def extract_json(text: str) -> Optional[Dict[str, Any]]:
    """Defensively extracts and parses JSON or Programmatic Tool Calls (PTC) from raw LLM output."""
    if not text:
        return None

    decoder = json.JSONDecoder()

    # Pre-pass: Try extracting JSON from markdown code blocks in raw text first
    if "```" in text:
        match = re.search(r'```(?:json)?\s*([\{\[].*?[\}\]])\s*```', text, re.DOTALL)
        if match:
            try:
                obj, _ = decoder.raw_decode(match.group(1).strip())
                norm = normalize_tool_action(obj)
                if norm:
                    return norm
            except Exception:
                pass

    cleaned = strip_thought_blocks(text).strip()
    if not cleaned:
        cleaned = text.strip()

    # 0. Intercept XML-style <tool_call>...</tool_call> wrappers
    if "<tool_call>" in cleaned:
        xml_match = re.search(r'<tool_call>\s*([\s\S]*?)\s*(?:</tool_call>|$)', cleaned)
        if xml_match:
            inner = xml_match.group(1).strip()
            ptc_res = _parse_python_func_call(inner)
            if ptc_res:
                return ptc_res
            try:
                obj, _ = decoder.raw_decode(inner)
                norm = normalize_tool_action(obj)
                if norm:
                    return norm
            except Exception:
                pass

    # 1. Intercept Programmatic Python function calls (PTC - arXiv:2608.06370v1)
    if "```python" in cleaned or "```py" in cleaned:
        py_blocks = re.findall(r'```(?:python|py)\s*([\s\S]*?)\s*```', cleaned)
        for block in py_blocks:
            block_stripped = block.strip()
            try:
                parsed_mod = ast.parse(block_stripped, mode='exec')
                for node in ast.walk(parsed_mod):
                    if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                        call_node = node.value
                        func_name = ""
                        if isinstance(call_node.func, ast.Name):
                            func_name = call_node.func.id
                        elif isinstance(call_node.func, ast.Attribute):
                            func_name = call_node.func.attr
                        if func_name and func_name not in ["print", "len", "str", "int", "float", "list", "dict", "set"]:
                            args = {}
                            for kw in call_node.keywords:
                                if kw.arg:
                                    try:
                                        args[kw.arg] = ast.literal_eval(kw.value)
                                    except Exception:
                                        if isinstance(kw.value, ast.Constant):
                                            args[kw.arg] = kw.value.value
                                        else:
                                            args[kw.arg] = ast.unparse(kw.value)
                            if call_node.args and not args:
                                pos_vals = []
                                for a in call_node.args:
                                    try:
                                        pos_vals.append(ast.literal_eval(a))
                                    except Exception:
                                        if isinstance(a, ast.Constant):
                                            pos_vals.append(a.value)
                                        else:
                                            pos_vals.append(ast.unparse(a))
                                if func_name in ["run_command", "exec", "terminal", "bash", "sh"]:
                                    args["command"] = pos_vals[0] if pos_vals else ""
                                elif func_name in ["read_file", "view_file", "write_file", "list_directory"]:
                                    args["path"] = pos_vals[0] if pos_vals else "."
                                elif func_name in ["grep_search", "grep"]:
                                    args["query"] = pos_vals[0] if pos_vals else ""
                                elif func_name in ["create_or_update_plan"]:
                                    args["steps"] = pos_vals[0] if pos_vals else []
                            return {
                                "action": "call_tool",
                                "target": func_name,
                                "arguments_or_instructions": args,
                                "step_summary": f"PTC tool call: {func_name}",
                                "reason": "Parsed from Programmatic Tool Call (PTC) - full block AST"
                            }
            except SyntaxError:
                pass
            for line in block_stripped.splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    res = _parse_python_func_call(line)
                    if res:
                        return res

    for line in cleaned.splitlines():
        line_s = line.strip()
        if line_s and not line.startswith("#"):
            direct_ptc = _parse_python_func_call(line_s)
            if direct_ptc:
                return direct_ptc

#Fixed regex search (added * to \([^)]*\) for multi-character argument lists)
    known_tools = r'(?:create_or_update_plan|list_directory|read_file|write_file|insert_edit_into_file|replace_string_in_file|multi_replace_string_in_file|grep_search|run_command|mcp:[a-zA-Z0-9_:]+)'
    ptc_match = re.search(rf'({known_tools}\s*\([^)]*\))', cleaned, re.DOTALL)
    if ptc_match:
        res = _parse_python_func_call(ptc_match.group(1))
        if res:
            return res

    # 2. Intercept native tool call syntax (<|tool_call>, <tool_call>, call:...)
    if "<|tool_call" in cleaned or "<tool_call" in cleaned or "call:" in cleaned:
        name_match = re.search(r'(?:<\|?channel\|?>)*\s*(?:<\|?tool_call\|?>?)\s*call:\s*([^\s{\(]+)\s*', cleaned)
        if name_match:
            func_name = name_match.group(1).strip()
            rest = cleaned[name_match.end():]

            if rest.startswith('{'):
                brace_end = _find_balanced_brace(rest)
                if brace_end > 0:
                    args_str = rest[:brace_end + 1]
                    args = _parse_native_tool_args(args_str)
                    return {
                        "action": "call_tool",
                        "target": func_name,
                        "arguments_or_instructions": args,
                        "step_summary": f"Native tool call: {func_name}",
                        "reason": "Parsed from native tool call"
                    }

            simple_match = re.search(r'(\{.*?\})', rest, re.DOTALL)
            if simple_match:
                args_str = simple_match.group(1).strip()
                cleaned_args_str = re.sub(r'<\|"\|>', '"', args_str)
                try:
                    args = json.loads(cleaned_args_str)
                except Exception:
                    args = cleaned_args_str
                return {
                    "action": "call_tool",
                    "target": func_name,
                    "arguments_or_instructions": args,
                    "step_summary": f"Native tool call: {func_name}",
                    "reason": "Parsed from native tool call"
                }
        
        alt_match = re.search(r'(?:<\|?channel\|?>)*\s*(?:<\|?tool_call\|?>?)\s*call:\s*([^\s{\(]+)\((.*?)\)\s*(?:<\|?tool_call\|?>?|<\|?tool_call\|?>|</tool_call>)?', cleaned, re.DOTALL)
        if alt_match:
            func_name = alt_match.group(1).strip()
            args = alt_match.group(2).strip()
            return {
                "action": "call_tool",
                "target": func_name,
                "arguments_or_instructions": args,
                "step_summary": f"Native tool call: {func_name}",
                "reason": "Parsed from native tool call"
            }

    # 3. Try to extract from markdown code blocks in cleaned text
    if "```" in cleaned:
        match = re.search(r'```(?:json)?\s*([\{\[].*?[\}\]])\s*```', cleaned, re.DOTALL)
        if match:
            try:
                obj, _ = decoder.raw_decode(match.group(1).strip())
                norm = normalize_tool_action(obj)
                if norm:
                    return norm
            except Exception:
                pass

    # 4. Iterate over all '{' and '[' matches using raw_decode to parse valid JSON surrounded by text
    for match in re.finditer(r'[\{\[]', cleaned):
        try:
            obj, _ = decoder.raw_decode(cleaned[match.start():])
            norm = normalize_tool_action(obj)
            if norm:
                return norm
        except Exception:
            pass

    return None

# --- Proactive VRAM Autoswap Manager ---

async def reset_autoswap_timer():
    """Resets the inactivity timer."""
    global autoswap_timer_task
    if autoswap_timer_task and not autoswap_timer_task.done():
        autoswap_timer_task.cancel()
    
    autoswap_timer_task = asyncio.create_task(autoswap_timer_worker())

async def autoswap_timer_worker():
    try:
        await asyncio.sleep(AUTOSWAP_TIMEOUT)
        await preload_supervisor()
    except asyncio.CancelledError:
        pass

async def preload_supervisor():
    """Warms up the heavy Supervisor model in VRAM during idle time."""
    async with inference_lock:
        resolved_model_name = await resolve_model(SUPERVISOR_MODEL)
        log_message(f"[Autoswap] Idle timeout. Warm-loading '{resolved_model_name}' into GPU VRAM...")
        try:
            if llama_cpp_available and "glimmer" not in resolved_model_name.lower() and resolve_gguf_path(resolved_model_name):
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, get_llama_model, resolved_model_name, CONTEXT_WINDOW)
            else:
                if llama_server_process is None or active_llama_server_model_name != resolved_model_name:
                    await start_llama_server(resolved_model_name, CONTEXT_WINDOW)
                payload = {
                    "model": resolved_model_name,
                    "prompt": "",
                    "stream": False,
                    "keep_alive": -1
                }
                async with httpx.AsyncClient(timeout=20) as client: await client.post(LLAMA_SERVER_URL, json=payload)
            log_message(f"[Autoswap] Successfully warm-loaded '{resolved_model_name}'.")
        except Exception as e:
            log_message(f"[Autoswap] Warm-load failed: {e}")

def safe_parse_tool_args(payload_data: Any, expected_key: str) -> dict:
    if not payload_data:
        return {}
    if isinstance(payload_data, dict):
        return payload_data
    if isinstance(payload_data, str):
        payload_data = payload_data.strip()
        try:
            parsed = json.loads(payload_data)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
        # Fallback 1: it might be double-wrapped JSON or escaped
        if (payload_data.startswith('"') and payload_data.endswith('"')) or (payload_data.startswith("'") and payload_data.endswith("'")):
            try:
                # Strip outermost quotes and unescape
                inner = json.loads(payload_data)
                if isinstance(inner, dict):
                    return inner
                parsed = json.loads(inner)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                pass
        # Fallback 2: raw value passed directly instead of dict (e.g. "filename.py")
        return {expected_key: payload_data}
    return {}

def parse_read_file_args(payload_data: Any) -> tuple[Optional[str], Optional[int], Optional[int]]:
    args = safe_parse_tool_args(payload_data, "path")
    if not isinstance(args, dict):
        return None, None, None

    file_path = args.get("path") or args.get("file") or args.get("target_file") or args.get("filepath")
    if not file_path or not isinstance(file_path, str):
        return None, None, None

    file_path = file_path.strip()

    start_line = args.get("start_line") or args.get("start") or args.get("startLine") or args.get("from_line") or args.get("from")
    end_line = args.get("end_line") or args.get("end") or args.get("endLine") or args.get("to_line") or args.get("to")
    line_val = args.get("line") or args.get("target_line") or args.get("around_line") or args.get("line_number")
    range_val = args.get("range")

    s_num, e_num = None, None

    if isinstance(range_val, (list, tuple)) and len(range_val) >= 2:
        try:
            s_num, e_num = int(range_val[0]), int(range_val[1])
        except (ValueError, TypeError):
            pass
    elif isinstance(range_val, str) and ("-" in range_val or ":" in range_val):
        m = re.split(r"[-:]", range_val.strip())
        if len(m) >= 2:
            try:
                s_num, e_num = int(m[0]), int(m[1])
            except (ValueError, TypeError):
                pass

    if start_line is not None:
        try:
            s_num = int(start_line)
        except (ValueError, TypeError):
            pass
    if end_line is not None:
        try:
            e_num = int(end_line)
        except (ValueError, TypeError):
            pass

    if line_val is not None and s_num is None:
        try:
            t = int(line_val)
            s_num = max(1, t - 50)
            e_num = t + 50
        except (ValueError, TypeError):
            pass

    # Extract path line suffixes like "file.kt:1520" or "file.kt#L1520-L1580" or "file.kt around line 1520"
    if not os.path.exists(file_path):
        match_range = re.search(r'^(.*?)[#:]L?(\d+)(?:[-:]L?(\d+))?$', file_path, re.IGNORECASE)
        if match_range:
            base_p = match_range.group(1).strip()
            if os.path.exists(base_p):
                file_path = base_p
                if s_num is None:
                    s_num = int(match_range.group(2))
                    if match_range.group(3):
                        e_num = int(match_range.group(3))
                    else:
                        e_num = s_num + 100
        else:
            match_around = re.search(r'^(.*?)\s+(?:around\s+line|lines?|L)\s*(\d+)(?:\s*-\s*(\d+))?', file_path, re.IGNORECASE)
            if match_around:
                base_p = match_around.group(1).strip()
                if os.path.exists(base_p):
                    file_path = base_p
                    if s_num is None:
                        t = int(match_around.group(2))
                        if match_around.group(3):
                            s_num = t
                            e_num = int(match_around.group(3))
                        else:
                            s_num = max(1, t - 50)
                            e_num = t + 50

    return file_path, s_num, e_num

def extract_instructions(payload: Any) -> str:
    if not payload:
        return ""
    if isinstance(payload, str):
        return payload.strip()
    if isinstance(payload, dict):
        for key in ["instructions", "instruction", "task", "prompt", "command", "text", "query"]:
            if key in payload and isinstance(payload[key], str):
                return payload[key].strip()
        if len(payload) == 1:
            val = list(payload.values())[0]
            if isinstance(val, str):
                return val.strip()
        return json.dumps(payload)
    if isinstance(payload, list):
        return "\n".join(str(item) for item in payload)
    return str(payload)

# --- API Endpoints ---

@app.get("/api/models")
async def get_active_models():
    """Returns the dynamically discovered local models with accurate context size and capabilities for the VS Code extension."""
    global active_models_list, model_registry_initialized
    if not model_registry_initialized or not active_models_list:
        active_models_list = get_installed_models()
        model_registry_initialized = True

    def _build_model_entry(m_id: str, display_name: str, family: str, model_target: Optional[str] = None) -> dict:
        target = model_target or m_id
        _, _, native_ctx = get_model_info(target)
        ctx = native_ctx if native_ctx is not None else CONTEXT_WINDOW
        
        m_lower = target.lower()
        has_vision = any(tag in m_lower for tag in ['vision', 'diffusion', 'glimmer', 'vl', 'gemini'])
        has_tools = not any(tag in m_lower for tag in ['fim', 'codegemma-2b'])

        return {
            "id": m_id,
            "name": display_name,
            "family": family,
            "version": "1.0.0",
            "maxInputTokens": ctx,
            "maxOutputTokens": 16384,
            "capabilities": {
                "toolCalling": has_tools,
                "imageInput": has_vision,
                "tools": has_tools,
                "vision": has_vision
            }
        }

    models_to_report = [
        _build_model_entry("serenity-supervisor-high", f"Supervisor - High Mode ({SUPERVISOR_HIGH_MODEL})", "serenity-supervisor", SUPERVISOR_HIGH_MODEL),
        _build_model_entry("serenity-supervisor-low", f"Supervisor - Low Mode ({SUPERVISOR_LOW_MODEL})", "serenity-supervisor", SUPERVISOR_LOW_MODEL),
        _build_model_entry("serenity-supervisor", f"Orchestrator - Turbo Mode ({ORCHESTRATOR_TURBO_MODEL})", "serenity-supervisor", ORCHESTRATOR_TURBO_MODEL),
        _build_model_entry("custom-model", f"Active Custom Model ({CURRENT_MODEL})", "custom", CURRENT_MODEL),
        _build_model_entry("copilot-chat", f"Copilot Model ({CURRENT_MODEL})", "copilot", CURRENT_MODEL),
        _build_model_entry("gpt-4o", f"GPT-4o Alias ({SUPERVISOR_MODEL})", "openai", SUPERVISOR_MODEL),
        _build_model_entry("gpt-4", f"GPT-4 Alias ({SUPERVISOR_MODEL})", "openai", SUPERVISOR_MODEL),
        _build_model_entry("gpt-3.5-turbo", f"Fast Worker ({W3_MODEL})", "openai", W3_MODEL),
        _build_model_entry("claude-3-5-sonnet", f"Claude Sonnet Alias ({SUPERVISOR_MODEL})", "anthropic", SUPERVISOR_MODEL)
    ]

    for model_name in active_models_list:
        models_to_report.append(
            _build_model_entry(model_name, f"{model_name} (Local GGUF)", model_name, model_name)
        )

    return {"models": models_to_report}

# --- Ollama & OpenAI Standard API Compatibility Routes ---

@app.get("/api/tags")
async def get_ollama_tags():
    """Ollama API standard model listing endpoint."""
    models_resp = await get_active_models()
    ollama_models = []
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    for m in models_resp.get("models", []):
        model_id = m.get("id", "serenity-supervisor")
        ollama_models.append({
            "name": model_id,
            "model": model_id,
            "modified_at": now_iso,
            "size": 0,
            "digest": hashlib.sha256(model_id.encode("utf-8")).hexdigest(),
            "details": {
                "parent_model": "",
                "format": "gguf",
                "family": m.get("family", "serenity"),
                "families": [m.get("family", "serenity")],
                "parameter_size": "7B",
                "quantization_level": "Q4_K_M"
            }
        })
    return {"models": ollama_models}

@app.get("/api/version")
async def get_ollama_version():
    """Ollama API standard version endpoint."""
    return {"version": "0.1.30"}

@app.get("/v1/models")
@app.get("/models")
async def get_openai_v1_models():
    """OpenAI API standard model listing endpoint."""
    models_resp = await get_active_models()
    v1_models = []
    now = int(time.time())
    seen = set()
    for m in models_resp.get("models", []):
        m_id = m.get("id", "serenity-supervisor")
        if m_id not in seen:
            seen.add(m_id)
            v1_models.append({
                "id": m_id,
                "object": "model",
                "created": now,
                "owned_by": "local",
                "permission": [],
                "root": m_id,
                "parent": None
            })
    return {"object": "list", "data": v1_models}

@app.get("/v1/models/{model_id:path}")
@app.get("/models/{model_id:path}")
async def get_openai_v1_model_by_id(model_id: str):
    """OpenAI API standard single model metadata endpoint."""
    resolved = await resolve_model(model_id)
    return {
        "id": model_id,
        "object": "model",
        "created": int(time.time()),
        "owned_by": "local",
        "resolved_model": resolved
    }

@app.get("/health")
@app.get("/v1/health")
async def health_check_probe():
    """Health check endpoint for third-party integrations."""
    return {"status": "ok", "server": "SerenityOrchestrator"}

# --- MCP (Model Context Protocol) StreamableHTTP Endpoint ---

MCP_SERVER_INFO = {
    "name": "SerenityDev Secure MCP Server",
    "version": "1.5.0"
}

MCP_TOOLS_DEFINITIONS = [
    {
        "name": "read_file",
        "description": "Read contents of a workspace file with optional line range slicing.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to file"},
                "start_line": {"type": "integer", "description": "1-indexed start line"},
                "end_line": {"type": "integer", "description": "1-indexed end line"}
            },
            "required": ["path"]
        }
    },
    {
        "name": "write_file",
        "description": "Write or overwrite content in a workspace file.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path to target file"},
                "content": {"type": "string", "description": "Content to write"}
            },
            "required": ["path", "content"]
        }
    },
    {
        "name": "list_directory",
        "description": "List files and subdirectories in a directory.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory path"}
            },
            "required": ["path"]
        }
    },
    {
        "name": "grep_search",
        "description": "Search workspace files using regular expression or literal string.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Regex or string search query"},
                "path": {"type": "string", "description": "Directory or file path to search"}
            },
            "required": ["query"]
        }
    },
    {
        "name": "insert_edit_into_file",
        "description": "Insert new content after target content in a workspace file.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Target file path"},
                "target_content": {"type": "string", "description": "Target text after which insertion occurs"},
                "new_content": {"type": "string", "description": "Content to insert"}
            },
            "required": ["path", "target_content", "new_content"]
        }
    },
    {
        "name": "replace_string_in_file",
        "description": "Replace existing string with new content in a workspace file.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Target file path"},
                "target_content": {"type": "string", "description": "Existing text to replace"},
                "new_content": {"type": "string", "description": "Replacement content"}
            },
            "required": ["path", "target_content", "new_content"]
        }
    },
    {
        "name": "run_command",
        "description": "Execute terminal command securely in workspace.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Command string to run"}
            },
            "required": ["command"]
        }
    },
    {
        "name": "store_memory",
        "description": "Store persistent knowledge or decisions in the long-term memory database.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Unique key identifier for the memory"},
                "category": {"type": "string", "description": "Category (architecture, decisions, code_patterns, preferences, general)"},
                "content": {"type": "string", "description": "Detailed persistent content to remember"}
            },
            "required": ["key", "category", "content"]
        }
    },
    {
        "name": "query_memory",
        "description": "Search the persistent long-term memory database for past architectural decisions, patterns, or facts.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query or keyword"},
                "category": {"type": "string", "description": "Optional category filter"}
            },
            "required": ["query"]
        }
    },
    {
        "name": "update_memory",
        "description": "Update an existing long-term memory entry.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Key of the memory to update"},
                "content": {"type": "string", "description": "New content"}
            },
            "required": ["key", "content"]
        }
    },
    {
        "name": "delete_memory",
        "description": "Delete a long-term memory entry by key.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Key of the memory to delete"}
            },
            "required": ["key"]
        }
    }
]

def _extra_workspace_dirs() -> List[str]:
    raw = os.environ.get("SERENITY_WORKSPACE_DIR", "")
    paths = []
    if raw:
        paths.extend([path.strip() for path in raw.replace(";", os.pathsep).split(os.pathsep) if path.strip()])
    # Auto-allow adjacent Serenity workspace paths
    user_home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    candidates = [
        os.path.join(user_home, "Documents", "SerenityPC"),
        os.path.join(user_home, "Documents", "SerenityDev"),
        os.path.join(user_home, "SerenityDev"),
        os.path.join(user_home, "Desktop", "Hub")
    ]
    for c in candidates:
        if os.path.exists(c) and c not in paths:
            paths.append(os.path.abspath(c))
    return paths

def get_primary_workspace_dir(req_workspace: Optional[str] = None) -> str:
    """Returns the effective workspace directory: request workspace > SERENITY_WORKSPACE_DIR > server directory."""
    if req_workspace and isinstance(req_workspace, str) and req_workspace.strip():
        rw = os.path.abspath(req_workspace.strip())
        if os.path.exists(rw):
            return rw
    extra = _extra_workspace_dirs()
    if extra:
        for d in extra:
            if os.path.exists(d):
                return os.path.abspath(d)
        return os.path.abspath(extra[0])
    return os.path.abspath(workspace_dir)

def resolve_workspace_path(path_val: Optional[str], req_workspace: Optional[str] = None) -> Optional[str]:
    """Resolves relative and absolute file/directory paths against workspace roots."""
    if not path_val or not isinstance(path_val, str):
        return None
    p = path_val.strip()
    if not p:
        return None
    if os.path.isabs(p):
        return os.path.abspath(p)

    primary = get_primary_workspace_dir(req_workspace)
    cand = os.path.abspath(os.path.join(primary, p))
    if os.path.exists(cand):
        return cand

    for extra_dir in _extra_workspace_dirs():
        extra_cand = os.path.abspath(os.path.join(extra_dir, p))
        if os.path.exists(extra_cand):
            return extra_cand

    server_cand = os.path.abspath(os.path.join(workspace_dir, p))
    if os.path.exists(server_cand):
        return server_cand

    return cand

def is_path_allowed(file_path: Optional[str], req_workspace: Optional[str] = None) -> bool:
    """Restricts filesystem tools to the workspace, dynamic environment paths, model directories."""
    if not file_path or not isinstance(file_path, str):
        return False
    try:
        resolved = resolve_workspace_path(file_path, req_workspace) or file_path
        abs_path = os.path.abspath(resolved)
        base_name = os.path.basename(abs_path).lower()
        if base_name == ".env" or base_name.startswith(".env.") or ".env" in base_name:
            return False

        allowed = get_candidate_model_dirs() + _extra_workspace_dirs()
        if req_workspace and isinstance(req_workspace, str) and req_workspace.strip():
            allowed.append(os.path.abspath(req_workspace.strip()))

        return validate_path_containment(abs_path, workspace_dir, allowed_dirs=allowed)
    except Exception:
        return False

def dispatch_tool_call(target: str, payload_data: Any, mode: str = "agent", step_num: int = 1, workspace_dir: Optional[str] = None) -> Dict[str, Any]:
    target_raw = (target or "").strip()
    target_norm = target_raw.lower()
    core = target_norm.split(":")[-1].strip()

    if core in ["read_file", "read", "view_file", "cat"]:
        normalized_target = "mcp:filesystem:read_file"
    elif core in ["grep_search", "grep", "search", "search_files"]:
        normalized_target = "mcp:filesystem:grep_search"
    elif core in ["list_directory", "list_dir", "ls"]:
        normalized_target = "mcp:filesystem:list_directory"
    elif core in ["write_file", "write", "create_file"]:
        normalized_target = "mcp:filesystem:write_file"
    elif core in ["insert_edit_into_file", "insert"]:
        normalized_target = "mcp:filesystem:insert_edit_into_file"
    elif core in ["replace_string_in_file", "replace"]:
        normalized_target = "mcp:filesystem:replace_string_in_file"
    elif core in ["multi_replace_string_in_file", "multi_replace"]:
        normalized_target = "mcp:filesystem:multi_replace_string_in_file"
    elif core in ["create_or_update_plan", "update_plan", "plan", "create_plan"]:
        normalized_target = "mcp:plan:create_or_update_plan"    
    elif core in ["run_command", "exec", "terminal", "execute", "bash", "sh", "shell", "run"]:
        normalized_target = "mcp:filesystem:run_command"
    elif core in ["store_memory", "save_memory", "remember"]:
        normalized_target = "mcp:memory:store_memory"
    elif core in ["query_memory", "search_memory", "recall_memory", "recall"]:
        normalized_target = "mcp:memory:query_memory"
    elif core in ["update_memory", "modify_memory"]:
        normalized_target = "mcp:memory:update_memory"
    elif core in ["delete_memory", "forget_memory", "remove_memory"]:
        normalized_target = "mcp:memory:delete_memory"
    else:
        normalized_target = target_raw

    is_edit_tool = normalized_target in ["mcp:filesystem:write_file", "mcp:filesystem:insert_edit_into_file", "mcp:filesystem:replace_string_in_file", "mcp:filesystem:multi_replace_string_in_file"]        
    if mode in ["explore", "plan"] and is_edit_tool:
        err_msg = f"[System Tool Error: Action blocked. You are in {mode.upper()} mode, which is read-only. Modifying files is forbidden.]"
        log_message(f"[Constraint Blocked] Blocked write tool {normalized_target} in {mode} mode.")
        return {
            "tool_context": f"\n\n{err_msg}\n",
            "raw_output": err_msg,
            "is_error": True,
            "proof_tag": None,
            "backup_id": None,
            "status": "error",
            "details": f"blocked by read-only mode ({mode})",
            "target_norm": normalized_target,
            "file_path": None
        }   

    if normalized_target == "mcp:filesystem:run_command":
        try:
            args = safe_parse_tool_args(payload_data, "command")
            command = args.get("command") if isinstance(args, dict) else (payload_data if isinstance(payload_data, str) else None)
            if command:
                if not is_command_allowed(command):
                    err_msg = f"[System Tool Error: Command execution blocked by security policy: '{command}']"
                    log_message(f"[Constraint Blocked] Blocked command execution: {command}")
                    return {
                        "tool_context": f"\n\n{err_msg}\n",
                        "raw_output": err_msg,
                        "is_error": True,
                        "proof_tag": None,
                        "backup_id": None,
                        "status": "error",
                        "details": "Blocked by security policy",
                        "target_norm": normalized_target,
                        "file_path": None
                    }
                full_cmd = f"powershell -NoProfile -Command \"{command}\""
                sub_env = {k: v for k, v in os.environ.items() if k not in ["LOCAL_API_KEY", "OPENAI_API_KEY", "OPENAIAPI_KEY"]}
                flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
                cmd_cwd = get_primary_workspace_dir(workspace_dir)
                result = subprocess.run(full_cmd, shell=True, capture_output=True, text=True, timeout=30, env=sub_env, cwd=cmd_cwd, creationflags=flags)
                stdout = result.stdout[:2000] if result.stdout else ""
                stderr = result.stderr[:2000] if result.stderr else ""
                is_err = result.returncode != 0
                out_text = f"Command Exited with {result.returncode}\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}"
                tool_ctx = f"\n\n[System Tool Response: {out_text}]\n"
                log_message(f"[Tool Success] Executed command: {command}")
                return {
                    "tool_context": tool_ctx,
                    "raw_output": stdout + stderr,
                    "is_error": is_err,
                    "proof_tag": None,
                    "backup_id": None,
                    "status": "success" if not is_err else "error",
                    "details": f"Exit {result.returncode}",
                    "target_norm": normalized_target,
                    "file_path": None
                }
            else:
                return {
                    "tool_context": "\n\n[System Tool Error: No command provided]\n",
                    "raw_output": "Error: No command provided",
                    "is_error": True,
                    "proof_tag": None,
                    "backup_id": None,
                    "status": "error",
                    "details": "No command provided",
                    "target_norm": normalized_target,
                    "file_path": None
                }
        except subprocess.TimeoutExpired:
            return {
                "tool_context": "\n\n[System Tool Error: Command timed out after 30 seconds]\n",
                "raw_output": "Error: Command timed out after 30 seconds",
                "is_error": True,
                "proof_tag": None,
                "backup_id": None,
                "status": "error",
                "details": "Timed out after 30s",
                "target_norm": normalized_target,
                "file_path": None
            }
        except Exception as e:
            return {
                "tool_context": f"\n\n[System Tool Error: Command failed: {e}]\n",
                "raw_output": f"Error: Command failed: {e}",
                "is_error": True,
                "proof_tag": None,
                "backup_id": None,
                "status": "error",
                "details": f"Error: {e}",
                "target_norm": normalized_target,
                "file_path": None
            }

    elif normalized_target == "mcp:filesystem:list_directory":
        try:
            args = safe_parse_tool_args(payload_data, "path")
            raw_dir = (
                args.get("path") or args.get("directory") or args.get("dir") or "."
            ) if isinstance(args, dict) else (payload_data if isinstance(payload_data, str) else ".")
            dir_path = resolve_workspace_path(raw_dir, workspace_dir)

            if not dir_path or not is_path_allowed(dir_path, workspace_dir):
                return {
                    "tool_context": f"\n\n[System Tool Error: Access to '{raw_dir}' is restricted or path missing.]\n",
                    "raw_output": f"Error: Access restricted or path missing: '{raw_dir}'",
                    "is_error": True,
                    "proof_tag": None,
                    "backup_id": None,
                    "status": "error",
                    "details": "Access restricted",
                    "target_norm": normalized_target,
                    "file_path": raw_dir
                }
            if not os.path.exists(dir_path):
                return {
                    "tool_context": f"\n\n[System Tool Error: Directory '{raw_dir}' not found.]\n",
                    "raw_output": f"Error: Directory not found: '{raw_dir}'",
                    "is_error": True,
                    "proof_tag": None,
                    "backup_id": None,
                    "status": "error",
                    "details": "Directory not found",
                    "target_norm": normalized_target,
                    "file_path": raw_dir
                }
            if not os.path.isdir(dir_path):
                return {
                    "tool_context": f"\n\n[System Tool Error: '{raw_dir}' is not a directory.]\n",
                    "raw_output": f"Error: Not a directory: '{raw_dir}'",
                    "is_error": True,
                    "proof_tag": None,
                    "backup_id": None,
                    "status": "error",
                    "details": "Not a directory",
                    "target_norm": normalized_target,
                    "file_path": raw_dir
                }
            entries: List[str] = []
            with os.scandir(dir_path) as it:
                for entry in sorted(it, key=lambda e: (not e.is_dir(), e.name.lower())):
                    try:
                        if entry.is_dir():
                            entries.append(f"dir:  {entry.name}/")
                        else:
                            size = entry.stat().st_size
                            entries.append(f"file: {entry.name} ({size} bytes)")
                    except Exception:
                        entries.append(f"unknown: {entry.name}")
                        
            total = len(entries)
            if total > 200:
                entries = entries[:200] + [f"... truncated, {total} total entries"]

            out = "\n".join(entries) if entries else "(empty_directory)"
            display_dir = raw_dir if raw_dir != "." else os.path.basename(dir_path) or dir_path
            tool_ctx = f"\n\n[System Tool Response: Directory listing for '{display_dir}' ({total} entries)]\n{out}\n"
            log_message(f"[Tool Success] Listed directory: {dir_path} ({total} entries)")
            return {
                "tool_context": tool_ctx,
                "raw_output": out,
                "is_error": False,
                "proof_tag": None,
                "backup_id": None,
                "status": "success",
                "details": f"{total} entries",
                "target_norm": normalized_target,
                "file_path": dir_path
            }
        except Exception as e:
            return {
                "tool_context": f"\n\n[System Tool Error: Failed to list directory: {e}]\n",
                "raw_output": f"Error: {e}",
                "is_error": True,
                "proof_tag": None,
                "backup_id": None,
                "status": "error",
                "details": "List failed",
                "target_norm": normalized_target,
                "file_path": None
            }

    elif normalized_target == "mcp:filesystem:grep_search":
        try:
            args = safe_parse_tool_args(payload_data, "query")
            query = (args.get("query") or args.get("pattern") or args.get("search") or "") if isinstance(args, dict) else (payload_data if isinstance(payload_data, str) else "")
            raw_search = (args.get("path") or args.get("directory") or ".") if isinstance(args, dict) else "."
            search_path = resolve_workspace_path(raw_search, workspace_dir)
            if not query:
                return {
                    "tool_context": "\n\n[System Tool Error: No search query provided]\n",
                    "raw_output": "Error: Missing query",
                    "is_error": True,
                    "proof_tag": None,
                    "backup_id": None,
                    "status": "error",
                    "details": "Missing query",
                    "target_norm": normalized_target,
                    "file_path": None
                }
            if not search_path or not is_path_allowed(search_path, workspace_dir):
                return {
                    "tool_context": f"\n\n[System Tool Error: Search path not allowed: {raw_search}]\n",
                    "raw_output": f"Error: Path not allowed: {raw_search}",
                    "is_error": True,
                    "proof_tag": None,
                    "backup_id": None,
                    "status": "error",
                    "details": "Path not allowed",
                    "target_norm": normalized_target,
                    "file_path": None
                }
            if not os.path.exists(search_path):
                return {
                    "tool_context": f"\n\n[System Tool Error: Search path does not exist: {raw_search}]\n",
                    "raw_output": f"Error: Path does not exist: {raw_search}",
                    "is_error": True,
                    "proof_tag": None,
                    "backup_id": None,
                    "status": "error",
                    "details": "Path not found",
                    "target_norm": normalized_target,
                    "file_path": None
                }
            matches = []
            primary_ws = get_primary_workspace_dir(workspace_dir)
            if os.path.isfile(search_path):
                try:
                    with open(search_path, "r", encoding="utf-8", errors="ignore") as f:
                        for line_idx, line in enumerate(f, start=1):
                            if query.lower() in line.lower():
                                rel_p = os.path.relpath(search_path, primary_ws)
                                matches.append(f"{rel_p}:{line_idx}: {line.strip()[:140]}")
                                if len(matches) >= 50:
                                    break
                except Exception:
                    pass
            else:
                for root, dirs, files in os.walk(search_path):
                    dirs[:] = [d for d in dirs if d not in [".git", ".serenity_cache", "node_modules", "__pycache__", ".venv", "venv"]]
                    for fname in files:
                        full_p = os.path.join(root, fname)
                        if is_path_allowed(full_p, workspace_dir):
                            try:
                                if os.path.getsize(full_p) > 2_000_000:
                                    continue
                                with open(full_p, "r", encoding="utf-8", errors="ignore") as f:
                                    for line_idx, line in enumerate(f, start=1):
                                        if query.lower() in line.lower():
                                            rel_p = os.path.relpath(full_p, primary_ws)
                                            matches.append(f"{rel_p}:{line_idx}: {line.strip()[:140]}")
                                            if len(matches) >= 50:
                                                break
                            except Exception:
                                pass
                    if len(matches) >= 50:
                        break
            out_text = "\n".join(matches) if matches else f"No matches found for '{query}'."
            tool_ctx = f"\n\n[System Tool Response: Grep search for '{query}']\n{out_text}\n"
            return {
                "tool_context": tool_ctx,
                "raw_output": out_text,
                "is_error": False,
                "proof_tag": None,
                "backup_id": None,
                "status": "success",
                "details": f"Found {len(matches)} matches",
                "target_norm": normalized_target,
                "file_path": search_path
            }
        except Exception as e:
            return {
                "tool_context": f"\n\n[System Tool Error: Grep search failed: {e}]\n",
                "raw_output": f"Error: {e}",
                "is_error": True,
                "proof_tag": None,
                "backup_id": None,
                "status": "error",
                "details": "Grep failed",
                "target_norm": normalized_target,
                "file_path": None
            }

    elif normalized_target == "mcp:plan:create_or_update_plan":
        try:
            args = safe_parse_tool_args(payload_data, "steps")
            steps = args.get("steps", []) if isinstance(args, dict) else []
            focus = args.get("current_focus", "In progress") if isinstance(args, dict) else "In progress"
            global active_system_plan
            active_system_plan = {"focus": focus, "steps": steps}
            plan_str = f"Plan Focus: {focus}\n" + "\n".join([f"- {s}" for s in steps])
            tool_ctx = f"\n\n[System Tool Response: Plan updated successfully]\n{plan_str}\n"
            return {
                "tool_context": tool_ctx,
                "raw_output": plan_str,
                "is_error": False,
                "proof_tag": None,
                "backup_id": None,
                "status": "success",
                "details": f"Plan updated ({len(steps)} steps)",
                "target_norm": normalized_target,
                "file_path": None
            }
        except Exception as e:
            return {
                "tool_context": f"\n\n[System Tool Error: Plan update failed: {e}]\n",
                "raw_output": f"Error: {e}",
                "is_error": True,
                "proof_tag": None,
                "backup_id": None,
                "status": "error",
                "details": "Plan error",
                "target_norm": normalized_target,
                "file_path": None
            }

    elif normalized_target == "mcp:filesystem:read_file":
        try:
            raw_path, start_line, end_line = parse_read_file_args(payload_data)
            file_path = resolve_workspace_path(raw_path, workspace_dir)
            if not file_path or not is_path_allowed(file_path, workspace_dir):
                return {
                    "tool_context": f"\n\n[System Tool Error: Access to '{raw_path}' is restricted or path missing.]\n",
                    "raw_output": f"Error: Access restricted or path missing: '{raw_path}'",
                    "is_error": True,
                    "proof_tag": None,
                    "backup_id": None,
                    "status": "error",
                    "details": "Access restricted",
                    "target_norm": normalized_target,
                    "file_path": raw_path
                }
            if not os.path.exists(file_path):
                return {
                    "tool_context": f"\n\n[System Tool Error: File '{raw_path}' was not found.]\n",
                    "raw_output": f"Error: File not found: '{raw_path}'",
                    "is_error": True,
                    "proof_tag": None,
                    "backup_id": None,
                    "status": "error",
                    "details": "File not found",
                    "target_norm": normalized_target,
                    "file_path": raw_path
                }
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                file_contents = f.read()

            file_lines = file_contents.splitlines()
            total_lines = len(file_lines)

            if start_line is not None and end_line is not None:
                s = max(1, int(start_line))
                e = min(total_lines, int(end_line))
                sliced_lines = [f"{idx}: {line}" for idx, line in enumerate(file_lines[s-1:e], start=s)]
                response_content = "\n".join(sliced_lines)
                tool_ctx = f"\n\n[System Tool Response: Contents of file '{raw_path}' (Lines {s}-{e} of {total_lines})]\n{response_content}\n"
            else:
                if total_lines > 150:
                    sliced_lines = [f"{idx}: {line}" for idx, line in enumerate(file_lines[:100], start=1)]
                    response_content = "\n".join(sliced_lines)
                    warn_msg = f"\n... [File too large ({total_lines} lines). Auto-truncated to first 100 lines. Use 'read_file' with 'start_line' and 'end_line' or 'grep_search' to target code.] ...\n"
                    tool_ctx = f"\n\n[System Tool Response: Contents of file '{raw_path}' (First 100 lines of {total_lines})]\n{response_content}{warn_msg}"
                else:
                    sliced_lines = [f"{idx}: {line}" for idx, line in enumerate(file_lines, start=1)]
                    response_content = "\n".join(sliced_lines)
                    tool_ctx = f"\n\n[System Tool Response: Contents of file '{raw_path}']\n{response_content}\n"

            log_message(f"[Tool Success] Read {len(file_contents)} characters from: {file_path}")
            return {
                "tool_context": tool_ctx,
                "raw_output": response_content,
                "is_error": False,
                "proof_tag": None,
                "backup_id": None,
                "status": "success",
                "details": f"Read {total_lines} lines",
                "target_norm": normalized_target,
                "file_path": file_path
            }
        except Exception as e:
            return {
                "tool_context": f"\n\n[System Tool Error: Failed to read file: {e}]\n",
                "raw_output": f"Error: Failed to read file: {e}",
                "is_error": True,
                "proof_tag": None,
                "backup_id": None,
                "status": "error",
                "details": "Failed to read file",
                "target_norm": normalized_target,
                "file_path": None
            }

    elif normalized_target == "mcp:filesystem:write_file":
        try:
            args = safe_parse_tool_args(payload_data, "path")
            raw_path = (args.get("path") or args.get("file") or args.get("filepath") or args.get("target_file")) if isinstance(args, dict) else None
            content = (args.get("content") or args.get("code") or args.get("text") or args.get("data") or args.get("body") or "") if isinstance(args, dict) else ""
            if not isinstance(content, str):
                content = str(content)
            file_path = resolve_workspace_path(raw_path, workspace_dir)
            if not file_path or not is_path_allowed(file_path, workspace_dir):
                return {
                    "tool_context": "\n\n[System Tool Error: Access restricted or path missing for write_file.]\n",
                    "raw_output": "Error: Access restricted or path missing for write_file",
                    "is_error": True,
                    "proof_tag": None,
                    "backup_id": None,
                    "status": "error",
                    "details": "Access restricted",
                    "target_norm": normalized_target,
                    "file_path": raw_path
                }
            b_id, old_c = create_edit_backup(file_path)
            parent_dir = os.path.dirname(file_path)
            if parent_dir and not os.path.exists(parent_dir):
                os.makedirs(parent_dir, exist_ok=True)
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(content)
            dels, adds = calculate_diff_counts(old_c, content)
            rel_name = os.path.basename(file_path)
            proof_tag = f"edited:{rel_name}-{dels}+{adds}"
            tool_ctx = f"\n\n[System Tool Response: Successfully wrote {len(content)} characters to '{raw_path}']\nPROOF: {proof_tag} (backup:{b_id})\n"
            log_message(f"[Tool Success] Wrote file: {file_path} ({proof_tag})")
            return {
                "tool_context": tool_ctx,
                "raw_output": f"Successfully wrote {len(content)} bytes to '{raw_path}'",
                "is_error": False,
                "proof_tag": proof_tag,
                "backup_id": b_id,
                "status": "success",
                "details": f"{proof_tag} (backup:{b_id})",
                "target_norm": normalized_target,
                "file_path": file_path
            }
        except Exception as e:
            return {
                "tool_context": f"\n\n[System Tool Error: Failed to write file: {e}]\n",
                "raw_output": f"Error: Failed to write file: {e}",
                "is_error": True,
                "proof_tag": None,
                "backup_id": None,
                "status": "error",
                "details": "Failed to write file",
                "target_norm": normalized_target,
                "file_path": None
            }

    elif normalized_target in ["mcp:filesystem:insert_edit_into_file", "mcp:filesystem:replace_string_in_file"]:
        try:
            args = safe_parse_tool_args(payload_data, "path")
            raw_path = (args.get("path") or args.get("file") or args.get("filepath") or args.get("target_file")) if isinstance(args, dict) else None
            target_content = (args.get("target_content") or args.get("search_string") or args.get("search_text") or args.get("old_content") or args.get("target") or "") if isinstance(args, dict) else ""
            new_content = (args.get("new_content") or args.get("new_string") or args.get("replace_string") or args.get("code_to_insert") or args.get("replacement") or args.get("code") or "") if isinstance(args, dict) else ""
            file_path = resolve_workspace_path(raw_path, workspace_dir)
            if not file_path or not is_path_allowed(file_path, workspace_dir):
                return {
                    "tool_context": f"\n\n[System Tool Error: Access to '{raw_path}' is restricted for security.]\n",
                    "raw_output": f"Error: Path access restricted: '{raw_path}'",
                    "is_error": True,
                    "proof_tag": None,
                    "backup_id": None,
                    "status": "error",
                    "details": "Access restricted",
                    "target_norm": normalized_target,
                    "file_path": raw_path
                }
            if not os.path.exists(file_path):
                return {
                    "tool_context": f"\n\n[System Tool Error: File '{raw_path}' not found.]\n",
                    "raw_output": f"Error: File not found: '{raw_path}'",
                    "is_error": True,
                    "proof_tag": None,
                    "backup_id": None,
                    "status": "error",
                    "details": "File not found",
                    "target_norm": normalized_target,
                    "file_path": raw_path
                }
            b_id, old_c = create_edit_backup(file_path)
            if target_content in old_c:
                if normalized_target.endswith("insert_edit_into_file"):
                    modified = old_c.replace(target_content, target_content + "\n" + new_content, 1)
                    action_word = "inserted content in"
                else:
                    modified = old_c.replace(target_content, new_content, 1)
                    action_word = "replaced content in"
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(modified)
                dels, adds = calculate_diff_counts(old_c, modified)
                rel_name = os.path.basename(file_path)
                proof_tag = f"edited:{rel_name}-{dels}+{adds}"
                tool_ctx = f"\n\n[System Tool Response: Successfully {action_word} '{raw_path}']\nPROOF: {proof_tag} (backup:{b_id})\n"
                log_message(f"[Tool Success] {action_word}: {file_path} ({proof_tag})")
                return {
                    "tool_context": tool_ctx,
                    "raw_output": f"Successfully {action_word} '{raw_path}'",
                    "is_error": False,
                    "proof_tag": proof_tag,
                    "backup_id": b_id,
                    "status": "success",
                    "details": f"{proof_tag} (backup:{b_id})",
                    "target_norm": normalized_target,
                    "file_path": file_path
                }
            else:
                tool_ctx = f"\n\n[System Tool Error: target_content not found in '{raw_path}']\n"
                log_message(f"[Tool Warning] Target content not found in {file_path}")
                return {
                    "tool_context": tool_ctx,
                    "raw_output": f"Error: target_content not found in '{raw_path}'",
                    "is_error": True,
                    "proof_tag": None,
                    "backup_id": None,
                    "status": "warning",
                    "details": "Target content not found",
                    "target_norm": normalized_target,
                    "file_path": file_path
                }
        except Exception as e:
            return {
                "tool_context": f"\n\n[System Tool Error: Edit failed: {e}]\n",
                "raw_output": f"Error: Edit failed: {e}",
                "is_error": True,
                "proof_tag": None,
                "backup_id": None,
                "status": "error",
                "details": "Edit failed",
                "target_norm": normalized_target,
                "file_path": None
            }

    elif normalized_target == "mcp:filesystem:multi_replace_string_in_file":
        try:
            args = safe_parse_tool_args(payload_data, "path")
            raw_path = (args.get("path") or args.get("file") or args.get("filepath") or args.get("target_file")) if isinstance(args, dict) else None
            replacements = args.get("replacements", []) if isinstance(args, dict) else []
            file_path = resolve_workspace_path(raw_path, workspace_dir)
            if not file_path or not is_path_allowed(file_path, workspace_dir):
                return {
                    "tool_context": f"\n\n[System Tool Error: Access to '{raw_path}' is restricted for security.]\n",
                    "raw_output": f"Error: Path access restricted: '{raw_path}'",
                    "is_error": True,
                    "proof_tag": None,
                    "backup_id": None,
                    "status": "error",
                    "details": "Access restricted",
                    "target_norm": normalized_target,
                    "file_path": raw_path
                }
            if not os.path.exists(file_path):
                return {
                    "tool_context": f"\n\n[System Tool Error: File '{raw_path}' not found.]\n",
                    "raw_output": f"Error: File not found: '{raw_path}'",
                    "is_error": True,
                    "proof_tag": None,
                    "backup_id": None,
                    "status": "error",
                    "details": "File not found",
                    "target_norm": normalized_target,
                    "file_path": raw_path
                }
            b_id, old_c = create_edit_backup(file_path)
            modified = old_c
            applied = 0
            for r in replacements:
                if isinstance(r, dict):
                    t = r.get("target_content") or r.get("search_string") or r.get("old_content") or ""
                    n = r.get("new_content") or r.get("new_string") or r.get("replace_string") or ""
                    if t and t in modified:
                        modified = modified.replace(t, n, 1)
                        applied += 1
            if applied > 0:
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(modified)
                dels, adds = calculate_diff_counts(old_c, modified)
                rel_name = os.path.basename(file_path)
                proof_tag = f"edited:{rel_name}-{dels}+{adds}"
                tool_ctx = f"\n\n[System Tool Response: Successfully applied {applied} replacements in '{raw_path}']\nPROOF: {proof_tag} (backup:{b_id})\n"
                log_message(f"[Tool Success] Multi-replaced in: {file_path} ({proof_tag})")
                return {
                    "tool_context": tool_ctx,
                    "raw_output": f"Successfully applied {applied} replacements in '{raw_path}'",
                    "is_error": False,
                    "proof_tag": proof_tag,
                    "backup_id": b_id,
                    "status": "success",
                    "details": f"{proof_tag} (backup:{b_id})",
                    "target_norm": normalized_target,
                    "file_path": file_path
                }
            else:
                tool_ctx = f"\n\n[System Tool Error: No replacement targets found in '{raw_path}']\n"
                return {
                    "tool_context": tool_ctx,
                    "raw_output": f"Error: No replacement targets found in '{raw_path}'",
                    "is_error": True,
                    "proof_tag": None,
                    "backup_id": None,
                    "status": "warning",
                    "details": "No targets matched",
                    "target_norm": normalized_target,
                    "file_path": file_path
                }
        except Exception as e:
            return {
                "tool_context": f"\n\n[System Tool Error: Multi-replace failed: {e}]\n",
                "raw_output": f"Error: Multi-replace failed: {e}",
                "is_error": True,
                "proof_tag": None,
                "backup_id": None,
                "status": "error",
                "details": "Multi-replace failed",
                "target_norm": normalized_target,
                "file_path": None
            }

    elif normalized_target == "mcp:memory:store_memory":
        try:
            args = safe_parse_tool_args(payload_data, "content")
            key = args.get("key") or f"mem_{int(time.time()*1000)}"
            category = args.get("category") or "general"
            content = args.get("content") or (payload_data if isinstance(payload_data, str) else "")
            if not content:
                return {
                    "tool_context": "\n\n[System Tool Error: Missing content to store in memory]\n",
                    "raw_output": "Error: Missing content",
                    "is_error": True,
                    "proof_tag": None,
                    "backup_id": None,
                    "status": "error",
                    "details": "Missing content",
                    "target_norm": normalized_target,
                    "file_path": None
                }
            entry = LongTermMemoryManager.store(key, category, content)
            tool_ctx = f"\n\n[System Tool Response: Stored persistent long-term memory under key '{entry['key']}' ({entry['category']})]\n"
            return {
                "tool_context": tool_ctx,
                "raw_output": f"Memory stored: {entry['key']}",
                "is_error": False,
                "proof_tag": None,
                "backup_id": None,
                "status": "success",
                "details": f"Stored '{entry['key']}' ({entry['category']})",
                "target_norm": normalized_target,
                "file_path": None
            }
        except Exception as e:
            return {
                "tool_context": f"\n\n[System Tool Error: Failed to store memory: {e}]\n",
                "raw_output": f"Error: {e}",
                "is_error": True,
                "proof_tag": None,
                "backup_id": None,
                "status": "error",
                "details": f"Error: {e}",
                "target_norm": normalized_target,
                "file_path": None
            }

    elif normalized_target == "mcp:memory:query_memory":
        try:
            args = safe_parse_tool_args(payload_data, "query")
            query_text = (args.get("query") or args.get("search") or "") if isinstance(args, dict) else (payload_data if isinstance(payload_data, str) else "")
            category = args.get("category") if isinstance(args, dict) else None
            results = LongTermMemoryManager.query(query_text, category)
            if not results:
                tool_ctx = f"\n\n[System Tool Response: No long-term memory entries found matching '{query_text}']\n"
                out_str = "No matching memories found."
            else:
                lines = [f"Found {len(results)} long-term memory entries:"]
                for it in results:
                    lines.append(f"• [{it.get('category','').upper()}] {it.get('key')}: {it.get('content')}")
                out_str = "\n".join(lines)
                tool_ctx = f"\n\n[System Tool Response: {out_str}]\n"
            return {
                "tool_context": tool_ctx,
                "raw_output": out_str,
                "is_error": False,
                "proof_tag": None,
                "backup_id": None,
                "status": "success",
                "details": f"{len(results)} memories found",
                "target_norm": normalized_target,
                "file_path": None
            }
        except Exception as e:
            return {
                "tool_context": f"\n\n[System Tool Error: Failed to query memory: {e}]\n",
                "raw_output": f"Error: {e}",
                "is_error": True,
                "proof_tag": None,
                "backup_id": None,
                "status": "error",
                "details": f"Error: {e}",
                "target_norm": normalized_target,
                "file_path": None
            }

    elif normalized_target == "mcp:memory:update_memory":
        try:
            args = safe_parse_tool_args(payload_data, "content")
            key = args.get("key") or ""
            content = args.get("content") or ""
            if not key or not content:
                return {
                    "tool_context": "\n\n[System Tool Error: Both 'key' and 'content' are required to update memory]\n",
                    "raw_output": "Error: Missing key or content",
                    "is_error": True,
                    "proof_tag": None,
                    "backup_id": None,
                    "status": "error",
                    "details": "Missing key or content",
                    "target_norm": normalized_target,
                    "file_path": None
                }
            res = LongTermMemoryManager.update(key, content)
            if res:
                tool_ctx = f"\n\n[System Tool Response: Successfully updated memory '{key}']\n"
                return {
                    "tool_context": tool_ctx,
                    "raw_output": f"Updated memory: {key}",
                    "is_error": False,
                    "proof_tag": None,
                    "backup_id": None,
                    "status": "success",
                    "details": f"Updated '{key}'",
                    "target_norm": normalized_target,
                    "file_path": None
                }
            else:
                tool_ctx = f"\n\n[System Tool Error: Memory key '{key}' not found to update]\n"
                return {
                    "tool_context": tool_ctx,
                    "raw_output": f"Memory key not found: {key}",
                    "is_error": True,
                    "proof_tag": None,
                    "backup_id": None,
                    "status": "warning",
                    "details": "Key not found",
                    "target_norm": normalized_target,
                    "file_path": None
                }
        except Exception as e:
            return {
                "tool_context": f"\n\n[System Tool Error: Update memory failed: {e}]\n",
                "raw_output": f"Error: {e}",
                "is_error": True,
                "proof_tag": None,
                "backup_id": None,
                "status": "error",
                "details": f"Error: {e}",
                "target_norm": normalized_target,
                "file_path": None
            }

    elif normalized_target == "mcp:memory:delete_memory":
        try:
            args = safe_parse_tool_args(payload_data, "key")
            key = (args.get("key") or (payload_data if isinstance(payload_data, str) else "")).strip()
            if not key:
                return {
                    "tool_context": "\n\n[System Tool Error: Key required to delete memory]\n",
                    "raw_output": "Error: Missing key",
                    "is_error": True,
                    "proof_tag": None,
                    "backup_id": None,
                    "status": "error",
                    "details": "Missing key",
                    "target_norm": normalized_target,
                    "file_path": None
                }
            deleted = LongTermMemoryManager.delete(key)
            if deleted:
                tool_ctx = f"\n\n[System Tool Response: Deleted memory entry '{key}']\n"
                return {
                    "tool_context": tool_ctx,
                    "raw_output": f"Deleted memory: {key}",
                    "is_error": False,
                    "proof_tag": None,
                    "backup_id": None,
                    "status": "success",
                    "details": f"Deleted '{key}'",
                    "target_norm": normalized_target,
                    "file_path": None
                }
            else:
                tool_ctx = f"\n\n[System Tool Response: Memory entry '{key}' was not found]\n"
                return {
                    "tool_context": tool_ctx,
                    "raw_output": f"Not found: {key}",
                    "is_error": False,
                    "proof_tag": None,
                    "backup_id": None,
                    "status": "warning",
                    "details": "Key not found",
                    "target_norm": normalized_target,
                    "file_path": None
                }
        except Exception as e:
            return {
                "tool_context": f"\n\n[System Tool Error: Delete memory failed: {e}]\n",
                "raw_output": f"Error: {e}",
                "is_error": True,
                "proof_tag": None,
                "backup_id": None,
                "status": "error",
                "details": f"Error: {e}",
                "target_norm": normalized_target,
                "file_path": None
            }

    else:
        err_msg = f"[System Tool Error: Tool '{target_raw}' is unknown or not supported]"
        return {
            "tool_context": f"\n\n{err_msg}\n",
            "raw_output": err_msg,
            "is_error": True,
            "proof_tag": None,
            "backup_id": None,
            "status": "error",
            "details": f"Unknown tool: {target_raw}",
            "target_norm": normalized_target,
            "file_path": None
        }

def verify_mcp_auth(request: Request, optional: bool = False):
    """Verifies PQC Bearer token if ENFORCE_MCP_AUTH is enabled."""
    if os.environ.get("ENFORCE_MCP_AUTH", "false").lower() in ("true", "1", "yes"):
        expected_token = os.environ.get("SERENITY_MCP_TOKEN")
        if not expected_token:
            expected_token = SerenityKeyVault.get_machine_entropy().hex()[:32]
        
        auth_header = request.headers.get("authorization", "").strip()
        bearer_header = request.headers.get("bearer", "").strip()
        api_key_header = request.headers.get("x-api-key", "").strip()
        token_param = request.query_params.get("token", "").strip()
        
        provided_token = ""
        if auth_header.lower().startswith("bearer "):
            provided_token = auth_header[7:].strip()
        elif auth_header:
            provided_token = auth_header
        elif bearer_header:
            provided_token = bearer_header
        elif api_key_header:
            provided_token = api_key_header
        elif token_param:
            provided_token = token_param
            
        if not provided_token and optional:
            return

        valid_tokens = [expected_token]
        if LOCAL_API_KEY:
            valid_tokens.append(LOCAL_API_KEY)
        if raw_env_key:
            valid_tokens.append(raw_env_key)

        matched = any(hmac.compare_digest(provided_token, v) for v in valid_tokens if v)
        if not provided_token or not matched:
            raise HTTPException(status_code=401, detail="Unauthorized: Invalid or missing MCP Bearer Token")

def execute_mcp_tool_call(tool_name: str, args: dict) -> tuple[str, bool]:
    """Executes MCP tool call securely via central dispatch engine, returning (result_text, is_error)."""
    ws = args.get("workspace_dir") or args.get("workspace") if isinstance(args, dict) else None
    res = dispatch_tool_call(tool_name, args, mode="agent", step_num=1, workspace_dir=ws)
    return res["raw_output"] or res["tool_context"].strip(), res["is_error"]

@app.api_route("/mcp", methods=["GET", "POST"])
async def mcp_streamable_http_endpoint(request: Request):
    """StreamableHTTP Model Context Protocol (MCP) endpoint for Google AI Edge Gallery & external clients."""
    # HTTPS Enforcement
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme).lower()
    host = request.headers.get("host", "").lower().split(":")[0]
    enforce_https = os.environ.get("ENFORCE_MCP_HTTPS", "true").lower() in ("true", "1", "yes")

    if enforce_https and scheme != "https" and host not in ("localhost", "127.0.0.1", "::1"):
        raise HTTPException(status_code=403, detail="HTTPS required for MCP endpoints. Request must use https:// or proxy header X-Forwarded-Proto: https")

    # Optional Bearer / API Key header check
    verify_mcp_auth(request, optional=(request.method == "GET"))

    if request.method == "GET":
        return JSONResponse({
            "status": "online",
            "protocol": "Model Context Protocol (MCP)",
            "transport": "StreamableHTTP",
            "server": MCP_SERVER_INFO
        })

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"jsonrpc": "2.0", "error": {"code": -32700, "message": "Parse error"}, "id": None})

    req_id = body.get("id")
    method = body.get("method")
    params = body.get("params", {})

    log_message(f"[MCP Endpoint] Received method '{method}' (id: {req_id})")

    if method == "initialize":
        return JSONResponse({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": params.get("protocolVersion", "2024-11-05"),
                "capabilities": {
                    "tools": {}
                },
                "serverInfo": MCP_SERVER_INFO
            }
        })

    elif method == "notifications/initialized":
        return JSONResponse({"jsonrpc": "2.0", "result": {}})

    elif method == "ping":
        return JSONResponse({"jsonrpc": "2.0", "id": req_id, "result": {}})

    elif method == "tools/list":
        return JSONResponse({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "tools": MCP_TOOLS_DEFINITIONS
            }
        })

    elif method == "tools/call":
        tool_name = params.get("name", "")
        tool_args = params.get("arguments", {})
        result_text, is_error = execute_mcp_tool_call(tool_name, tool_args)
        return JSONResponse({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": result_text
                    }
                ],
                "isError": is_error
            }
        })

    else:
        return JSONResponse(status_code=404, content={
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {
                "code": -32601,
                "message": f"Method '{method}' not found"
            }
        })

SSE_HEADERS = {
    "Content-Type": "text/event-stream; charset=utf-8",
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "Transfer-Encoding": "chunked",
    "X-Accel-Buffering": "no"
}

class OpenAIChatCompletionRequest(BaseModel):
    model: Optional[str] = None
    messages: List[Dict[str, Any]] = []
    stream: Optional[bool] = False
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None

@app.post("/v1/chat/completions")
@app.post("/chat/completions")
async def openai_chat_completions(req: OpenAIChatCompletionRequest, http_request: Request):
    target_model = req.model or SUPERVISOR_MODEL
    resolved_model = await resolve_model(target_model)
    
    # Build prompt directly from multi-turn messages
    formatted_prompt = ""
    for msg in req.messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join([c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"])
        formatted_prompt += f"<|turn>{role}\n{content}\n<turn|>\n"
    formatted_prompt += "<|turn>model\n<|channel>thought\n"

    req_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created_ts = int(time.time())

    if req.stream:
        async def openai_direct_stream():
            thought_filter = StreamingThoughtFilter()
            try:
                async for chunk in generate_completion_stream(
                    resolved_model, formatted_prompt,
                    temperature=req.temperature if req.temperature is not None else 0.2,
                    num_ctx=CONTEXT_WINDOW,
                    max_tokens=req.max_tokens if req.max_tokens is not None else -1,
                ):
                    if await http_request.is_disconnected():
                        log_message("[OpenAI Stream] Client disconnected.")
                        break

                    for item in thought_filter.feed(chunk):
                        delta_text = item.get("content", "")
                        if delta_text:
                            payload = {
                                "id": req_id,
                                "object": "chat.completion.chunk",
                                "created": created_ts,
                                "model": target_model,
                                "choices": [{"index": 0, "delta": {"content": delta_text}, "finish_reason": None}]
                            }
                            yield f"data: {json.dumps(payload)}\n\n"

                for item in thought_filter.flush():
                    delta_text = item.get("content", "")
                    if delta_text:
                        payload = {
                            "id": req_id,
                            "object": "chat.completion.chunk",
                            "created": created_ts,
                            "model": target_model,
                            "choices": [{"index": 0, "delta": {"content": delta_text}, "finish_reason": None}]
                        }
                        yield f"data: {json.dumps(payload)}\n\n"

                yield f"data: {json.dumps({'id': req_id, 'object': 'chat.completion.chunk', 'created': created_ts, 'model': target_model, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]})}\n\n"
            except asyncio.CancelledError:
                log_message("[OpenAI Stream] Client aborted request.")
                return
            yield "data: [DONE]\n\n"

        return StreamingResponse(openai_direct_stream(), media_type="text/event-stream", headers=SSE_HEADERS)

    else:
        last_user_msg = ""
        for m in reversed(req.messages):
            if m.get("role") == "user":
                c = m.get("content", "")
                last_user_msg = c if isinstance(c, str) else str(c)
                break
        if not last_user_msg:
            last_user_msg = "Hello"

        query_req = QueryRequest(prompt=last_user_msg, model=target_model, session_id="openai_chat_sync")
        answer_parts = []
        try:
            async for event in run_orchestration(query_req, http_request):
                if isinstance(event, dict) and event.get("type") == "content":
                    answer_parts.append(event.get("content", ""))
        except Exception as e:
            log_message(f"[OpenAI Chat Error] {e}")

        full_answer = "".join(answer_parts)
        return {
            "id": req_id,
            "object": "chat.completion",
            "created": created_ts,
            "model": target_model,
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": full_answer
                },
                "finish_reason": "stop"
            }],
            "usage": {
                "prompt_tokens": max(1, len(formatted_prompt) // 4),
                "completion_tokens": max(1, len(full_answer) // 4),
                "total_tokens": max(2, (len(formatted_prompt) + len(full_answer)) // 4)
            }
        }

@app.post("/v1/responses")
@app.post("/responses")
async def openai_responses_endpoint(http_request: Request):
    """OpenAI Responses API compatibility adapter for IDE clients (Android Studio, VS Code, JetBrains)."""
    try:
        body = await http_request.json()
    except Exception:
        body = {}

    target_model = body.get("model") or SUPERVISOR_MODEL
    stream = body.get("stream", False)
    
    prompt_text = ""
    input_val = body.get("input") or body.get("messages") or body.get("prompt") or body.get("instructions")
    
    if isinstance(input_val, str):
        prompt_text = input_val
    elif isinstance(input_val, list):
        parts = []
        for item in input_val:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                role = item.get("role", "user")
                content = item.get("content") or item.get("text") or item.get("value") or ""
                if isinstance(content, list):
                    content = " ".join([c.get("text", "") for c in content if isinstance(c, dict) and "text" in c])
                parts.append(f"{role.capitalize()}: {content}")
        prompt_text = "\n".join(parts)

    if not prompt_text:
        prompt_text = body.get("user_prompt") or "Hello"

    query_req = QueryRequest(
        prompt=prompt_text,
        model=target_model,
        session_id="responses_api_session"
    )

    req_id = f"resp-{uuid.uuid4().hex[:12]}"
    created_ts = int(time.time())

    if stream:
        async def responses_stream_generator():
            try:
                init_event = {
                    "type": "response.created",
                    "response": {
                        "id": req_id,
                        "object": "realtime.response",
                        "status": "in_progress",
                        "model": target_model
                    }
                }
                yield f"data: {json.dumps(init_event)}\n\n"

                async for event in run_orchestration(query_req, http_request):
                    if isinstance(event, dict):
                        if event.get("type") == "content":
                            text_content = event.get("content", "")
                            delta_event = {
                                "type": "response.text.delta",
                                "response_id": req_id,
                                "delta": text_content
                            }
                            yield f"data: {json.dumps(delta_event)}\n\n"
                            
                            chat_chunk = {
                                "id": req_id,
                                "object": "chat.completion.chunk",
                                "created": created_ts,
                                "model": target_model,
                                "choices": [{
                                    "index": 0,
                                    "delta": {"content": text_content},
                                    "finish_reason": None
                                }]
                            }
                            yield f"data: {json.dumps(chat_chunk)}\n\n"

                done_event = {
                    "type": "response.done",
                    "response": {
                        "id": req_id,
                        "status": "completed"
                    }
                }
                yield f"data: {json.dumps(done_event)}\n\n"
                yield "data: [DONE]\n\n"
            except asyncio.CancelledError:
                log_message("[Responses Stream] Request cancelled by client disconnect.")
                return
            except Exception as e:
                log_message(f"[Responses Stream Error] {e}")
                yield "data: [DONE]\n\n"

        return StreamingResponse(responses_stream_generator(), media_type="text/event-stream", headers=SSE_HEADERS)

    else:
        answer_parts = []
        try:
            async for event in run_orchestration(query_req, http_request):
                if isinstance(event, dict) and event.get("type") == "content":
                    answer_parts.append(event.get("content", ""))
        except Exception as e:
            log_message(f"[Responses API Error] {e}")

        full_answer = "".join(answer_parts)
        return {
            "id": req_id,
            "object": "response",
            "created": created_ts,
            "model": target_model,
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "text",
                            "text": full_answer
                        }
                    ]
                }
            ],
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": full_answer
                },
                "finish_reason": "stop"
            }],
            "usage": {
                "prompt_tokens": max(1, len(prompt_text) // 4),
                "completion_tokens": max(1, len(full_answer) // 4),
                "total_tokens": max(2, (len(prompt_text) + len(full_answer)) // 4)
            }
        }

@app.post("/v1/completions")
@app.post("/completions")
async def openai_text_completions(http_request: Request):
    """OpenAI standard legacy text completion endpoint compatibility adapter."""
    try:
        body = await http_request.json()
    except Exception:
        body = {}

    target_model = body.get("model") or SUPERVISOR_MODEL
    stream = body.get("stream", False)
    prompt_text = body.get("prompt", "Hello")
    if isinstance(prompt_text, list):
        prompt_text = "\n".join(str(p) for p in prompt_text)

    query_req = QueryRequest(
        prompt=str(prompt_text),
        model=target_model,
        session_id="text_completion_session"
    )

    req_id = f"cmpl-{uuid.uuid4().hex[:12]}"
    created_ts = int(time.time())

    if stream:
        async def text_stream_generator():
            try:
                async for event in run_orchestration(query_req, http_request):
                    if isinstance(event, dict) and event.get("type") == "content":
                        chunk = {
                            "id": req_id,
                            "object": "text_completion",
                            "created": created_ts,
                            "model": target_model,
                            "choices": [{
                                "text": event.get("content", ""),
                                "index": 0,
                                "logprobs": None,
                                "finish_reason": None
                            }]
                        }
                        yield f"data: {json.dumps(chunk)}\n\n"
            except asyncio.CancelledError:
                return
            except Exception as e:
                log_message(f"[Text Stream Error] {e}")
            yield f"data: {json.dumps({'id': req_id, 'object': 'text_completion', 'created': created_ts, 'model': target_model, 'choices': [{'text': '', 'index': 0, 'finish_reason': 'stop'}]})}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(text_stream_generator(), media_type="text/event-stream", headers=SSE_HEADERS)

    else:
        answer_parts = []
        try:
            async for event in run_orchestration(query_req, http_request):
                if isinstance(event, dict) and event.get("type") == "content":
                    answer_parts.append(event.get("content", ""))
        except Exception as e:
            log_message(f"[Text Completion Error] {e}")

        full_answer = "".join(answer_parts)
        return {
            "id": req_id,
            "object": "text_completion",
            "created": created_ts,
            "model": target_model,
            "choices": [{
                "text": full_answer,
                "index": 0,
                "logprobs": None,
                "finish_reason": "stop"
            }],
            "usage": {
                "prompt_tokens": max(1, len(prompt_text) // 4),
                "completion_tokens": max(1, len(full_answer) // 4),
                "total_tokens": max(2, (len(prompt_text) + len(full_answer)) // 4)
            }
        }

@app.delete("/api/session/{session_id}")
async def delete_session(session_id: str):
    if session_id in sessions_history:
        del sessions_history[session_id]
        return {"status": "success", "message": f"Session {session_id} deleted."}
    return {"status": "error", "message": "Session not found."}

COMMAND_RULES = [
    # Blacklisted rules first (if any match, block it!)
    (r"^git(\s+(-C\s+\S+|--no-pager))*\s+log\b.*\s--output(=|\s|$)", False),
    (r"^git(\s+(-C\s+\S+|--no-pager))*\s+branch\b.*\s-(d|D|m|M|-delete|-force)\b", False),
    (r"^column\b.*\s-c\s+[0-9]{4,}", False),
    (r"^date\b.*\s(-s|--set)\b", False),
    (r"^find\b.*\s-(delete|exec|execdir|fprint|fprintf|fls|ok|okdir)\b", False),
    (r"^rg\b.*\s(--pre|--hostname-bin)\b", False),
    (r"^sed\b.*\s(-[a-zA-Z]*(e|f)[a-zA-Z]*|--expression|--file)\b", False),
    (r"^sed\b.*s\/.*\/.*\/[ew]", False),
    (r"^sed\b.*;W", False),
    (r"^sort\b.*\s-(o|S)\b", False),
    (r"^tree\b.*\s-o\b", False),
    (r"^rm\b", False),
    (r"^rmdir\b", False),
    (r"^del\b", False),
    (r"^Remove-Item\b", False),
    (r"^ri\b", False),
    (r"^rd\b", False),
    (r"^erase\b", False),
    (r"^dd\b", False),
    (r"^ps\b", False),
    (r"^top\b", False),
    (r"^Stop-Process\b", False),
    (r"^spps\b", False),
    (r"^curl\b", False),
    (r"^wget\b", False),
    (r"\.env\b", False),
    (r"\b\.env", False),
    (r"^python(\.exe)?\s+-c\b", False),
    (r"^py\s+-c\b", False),
    (r"^python(\.exe)?(\s+-i\b|\s*$)", False),
    (r"^py(\s+-i\b|\s*$)", False),

    # Whitelisted rules
    (r"^cd(\s+|$)", True),
    (r"^echo(\s+|$)", True),
    (r"^ls(\s+|$)", True),
    (r"^dir(\s+|$)", True),
    (r"^pwd(\s+|$)", True),
    (r"^cat(\s+|$)", True),
    (r"^head(\s+|$)", True),
    (r"^tail(\s+|$)", True),
    (r"^findstr(\s+|$)", True),
    (r"^wc(\s+|$)", True),
    (r"^tr(\s+|$)", True),
    (r"^cut(\s+|$)", True),
    (r"^cmp(\s+|$)", True),
    (r"^which(\s+|$)", True),
    (r"^basename(\s+|$)", True),
    (r"^dirname(\s+|$)", True),
    (r"^realpath(\s+|$)", True),
    (r"^readlink(\s+|$)", True),
    (r"^stat(\s+|$)", True),
    (r"^file(\s+|$)", True),
    (r"^od(\s+|$)", True),
    (r"^du(\s+|$)", True),
    (r"^df(\s+|$)", True),
    (r"^sleep(\s+|$)", True),
    (r"^nl(\s+|$)", True),
    (r"^grep(\s+|$)", True),
    (r"^column(\s+|$)", True),
    (r"^date(\s+|$)", True),
    (r"^find(\s+|$)", True),
    (r"^rg(\s+|$)", True),
    (r"^sed(\s+|$)", True),
    (r"^sort(\s+|$)", True),
    (r"^tree(\s+|$)", True),
    (r"^xxd(\s+|$)", True),
    (r"^xxd\b(\s+-\S+)*\s+[^-\s]\\S*$", True),
    (r"^git(\s+(-C\s+\S+|--no-pager))*\s+status\b", True),
    (r"^git(\s+(-C\s+\S+|--no-pager))*\s+log\b", True),
    (r"^git(\s+(-C\s+\S+|--no-pager))*\s+show\b", True),
    (r"^git(\s+(-C\s+\S+|--no-pager))*\s+diff\b", True),
    (r"^git(\s+(-C\s+\S+|--no-pager))*\s+ls-files\b", True),
    (r"^git(\s+(-C\s+\S+|--no-pager))*\s+grep\b", True),
    (r"^git(\s+(-C\s+\S+|--no-pager))*\s+branch\b", True),
    (r"^docker\s+(ps|images|info|version|inspect|logs|top|stats|port|diff|search|events)\b", True),
    (r"^docker\s+(container|image|network|volume|context|system)\s+(ls|ps|inspect|history|show|df|info)\b", True),
    (r"^docker\s+compose\s+(ps|ls|top|logs|images|config|version|port|events)\b", True),
    (r"^Get-ChildItem(\s+|$)", True),
    (r"^type(\s+|$)", True),
    (r"^Get-Content(\s+|$)", True),
    (r"^Get-Date(\s+|$)", True),
    (r"^Get-Random(\s+|$)", True),
    (r"^Get-Location(\s+|$)", True),
    (r"^Set-Location(\s+|$)", True),
    (r"^Write-Host(\s+|$)", True),
    (r"^Write-Output(\s+|$)", True),
    (r"^Out-String(\s+|$)", True),
    (r"^Split-Path(\s+|$)", True),
    (r"^Join-Path(\s+|$)", True),
    (r"^Start-Sleep(\s+|$)", True),
    (r"^Where-Object(\s+|$)", True),
    (r"^Select-(Object|String|Xml)\b", True),
    (r"^Measure-(Object|Command)\b", True),
    (r"^Compare-Object\b", True),
    (r"^Format-(Table|List|Wide|Custom|String)\b", True),
    (r"^Sort-Object\b", True),
    (r"^npm\s+(ls|list|outdated|view|info|show|explain|why|root|prefix|bin|search|doctor|fund|repo|bugs|docs|home|help(-search)?)\b", True),
    (r"^npm\s+config\s+(list|get)\b", True),
    (r"^npm\s+pkg\s+get\b", True),
    (r"^npm\s+audit$", True),
    (r"^npm\s+cache\s+verify\b", True),
    (r"^yarn\s+(list|outdated|info|why|bin|help|versions)\b", True),
    (r"^yarn\s+licenses\b", True),
    (r"^yarn\s+audit\b(?!.*\bfix\b)", True),
    (r"^yarn\s+config\s+(list|get)\b", True),
    (r"^yarn\s+cache\s+dir\b", True),
    (r"^pnpm\s+(ls|list|outdated|why|root|bin|doctor)\b", True),
    (r"^pnpm\s+licenses\b", True),
    (r"^pnpm\s+audit\b(?!.*\bfix\b)", True),
    (r"^pnpm\s+config\s+(list|get)\b", True),
    (r"^npm\s+ci\b", True),
    (r"^yarn\s+install\s+--frozen-lockfile\b", True),
    (r"^pnpm\s+install\s+--frozen-lockfile\b", True),
    (r"^python(\.exe)?\s+(--version|-V)\b", True),
    (r"^py\s+(--version|-V)\b", True),
    (r"^python(\.exe)?\s+-m\s+pip\s+(list|check)\b", True),
    (r"^py\s+-m\s+pip\s+(list|check)\b", True),
    (r"^python(\.exe)?\s+[A-Za-z0-9_\-\.\/\\\\]+\.py(\s+.*)?$", True),
    (r"^py\s+[A-Za-z0-9_\-\.\/\\\\]+\.py(\s+.*)?$", True),
    (r"^python(\.exe)?\s+-m\s+[A-Za-z0-9_\-\.]+(\s+.*)?$", True),
    (r"^py\s+-m\s+[A-Za-z0-9_\-\.]+(\s+.*)?$", True),
    (r"^pytest\b", True)
]

def split_command_statements(command_line: str) -> List[str]:
    try:
        pattern = r'(".*?"|\'.*?\'|&&|\|\||;|\|)'
        parts = re.split(pattern, command_line)
        statements = []
        current = []
        for part in parts:
            if part in ["&&", "||", ";", "|"]:
                stmt = "".join(current).strip()
                if stmt:
                    statements.append(stmt)
                current = []
            else:
                current.append(part)
        stmt = "".join(current).strip()
        if stmt:
            statements.append(stmt)
        return statements
    except Exception:
        return [command_line.strip()]

def is_command_allowed(command_line: str) -> bool:
    statements = split_command_statements(command_line)
    if not statements:
        return False
    for stmt in statements:
        stmt = stmt.strip()
        if not stmt:
            continue
        for pattern, allowed in COMMAND_RULES:
            if not allowed:
                if re.search(pattern, stmt, re.I):
                    log_message(f"[Command Filter] Blocked command statement '{stmt}' by blacklist pattern '{pattern}'")
                    return False
        matched_whitelist = False
        for pattern, allowed in COMMAND_RULES:
            if allowed:
                if re.search(pattern, stmt, re.I):
                    matched_whitelist = True
                    break
        if not matched_whitelist:
            log_message(f"[Command Filter] Blocked command statement '{stmt}' (no matching whitelist pattern)")
            return False
    return True


file_edit_backups: Dict[str, Dict[str, Any]] = {}

def create_edit_backup(file_path: str) -> tuple:
    old_content = ""
    if os.path.exists(file_path):
        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                old_content = f.read()
        except Exception:
            pass
    backup_id = f"bak_{uuid.uuid4().hex[:8]}"
    file_edit_backups[backup_id] = {
        "file_path": os.path.abspath(file_path),
        "content": old_content,
        "timestamp": time.time()
    }
    return backup_id, old_content

def calculate_diff_counts(old_content: str, new_content: str) -> tuple:
    old_lines = old_content.splitlines()
    new_lines = new_content.splitlines()
    diff = list(difflib.unified_diff(old_lines, new_lines, lineterm=""))
    dels = sum(1 for line in diff if line.startswith("-") and not line.startswith("---"))
    adds = sum(1 for line in diff if line.startswith("+") and not line.startswith("+++"))
    return dels, adds


class StreamingThoughtFilter:
    def __init__(self, start_in_thought: bool = True, max_buffer_limit: int = 16384):
        self.buffer = ""
        self.in_thought = start_in_thought
        self.thought_started = start_in_thought
        self.initial_start_in_thought = start_in_thought
        self.max_buffer_limit = max_buffer_limit
        self.start_tags = [
            "<|channel>thought", "<|channel|>thought", "<channel>thought",
            "<think>", "<|think|>", "<thought>", "<|thought|>"
        ]
        self.end_tags = [
            "<channel|>", "<|channel|>", "<|turn|>", "<turn|>",
            "</think>", "</|think|>",
            "</thought>", "</|thought|>"
        ]
        self.lookahead_tags = [
            "<|channel", "<channel", "<think", "<thought", "</think", "</thought", "<|turn", "<turn", "<|tool_call", "<tool_call", "call:", "<tool_call|>", "<|tool_call|>", "</tool_call>"
        ]
        self.has_seen_explicit_thought_tag = False        

    def _is_incomplete_tool_call(self, text: str) -> bool:
        if not text:
            return False
        if re.search(r'(?:<\|?tool_call\|?>?|<\|?channel\|?>*|<tool_call\b|\bcall:\s*[a-zA-Z0-9_:]*|\bcall:[^\s{\(]*)$', text.strip(), re.DOTALL):
            return True
        if "<tool_call>" in text and "</tool_call>" not in text:
            return True
        match = re.search(r'(?:<\|?channel\|?>)*\s*(?:<\|?tool_call\|?>?)\s*call:\s*([^\s{\(]+)\s*', text, re.DOTALL)
        if not match:
            if re.search(r'```json\s*\{\s*["\']action["\']\s*:\s*["\']call_tool["\']', text, re.DOTALL) and not text.rstrip().endswith("```"):
                return True
            return False
        start_idx = match.end()
        if start_idx < len(text) and text[start_idx] == '{':
            brace_end = _find_balanced_brace(text, start_idx)
            if brace_end == -1:
                return True
            tail = text[brace_end + 1:]
            if "<|tool_call" in text and not re.search(r'<\|?tool_call\|?>|</?\|?tool_call\|?>', tail):
                return True
            return False
        elif start_idx < len(text) and text[start_idx] == '(':
            paren_close = text.find(')', start_idx)
            if paren_close == -1:
                return True
            tail = text[paren_close + 1:]
            if "<|tool_call" in text and not re.search(r'<\|?tool_call\|?>|</?\|?tool_call\|?>', tail):
                return True
            return False
        return True

    def _has_trailing_partial_tag(self, text: str) -> bool:
        stripped = text.rstrip()
        if not stripped:
            return False
        for tag in self.lookahead_tags + self.start_tags + self.end_tags:
            for i in range(2, min(len(tag), 15)):
                if stripped.endswith(tag[:i]):
                    return True
        return False

    def feed(self, chunk: str) -> List[Dict[str, Any]]:
        thought_chunk, content_chunk = self.feed_demux(chunk)
        if content_chunk:
            return [{"type": "content", "content": content_chunk}]
        return []        

    def feed_demux(self, chunk: str) -> tuple[str, str]:
        self.buffer += chunk
        thought_out = ""
        content_out = ""

        if self.in_thought and not self.has_seen_explicit_thought_tag:
            stripped_buf = self.buffer.lstrip()
            if (stripped_buf.startswith("❌") or 
                stripped_buf.startswith("Error:") or 
                stripped_buf.startswith("### ") or 
                stripped_buf.startswith("```") or
                stripped_buf.startswith("def ") or
                stripped_buf.startswith("import ")):
                self.in_thought = False
                self.thought_started = False

        if not self.thought_started:
            for tag in self.start_tags:
                idx = self.buffer.find(tag)
                if idx != -1:
                    pre_content = self.buffer[:idx]
                    if pre_content:
                        content_out += self.clean_tool_tags(pre_content)
                    self.in_thought = True
                    self.thought_started = True
                    self.has_seen_explicit_thought_tag = True
                    self.buffer = self.buffer[idx + len(tag):]
                    break
            if not self.thought_started:
                if len(self.buffer) > 25 and not self._has_trailing_partial_tag(self.buffer):
                    if self._is_incomplete_tool_call(self.buffer) and len(self.buffer) < self.max_buffer_limit:
                        return ("", "")
                    flushed = self.clean_tool_tags(self.buffer)
                    self.buffer = ""
                    content_out += flushed
                    return (thought_out, content_out)

        if self.in_thought:
            for end_tag in self.end_tags:
                idx = self.buffer.find(end_tag)
                if idx != -1:
                    thought_text = self.buffer[:idx]
                    if thought_text:
                        thought_out += thought_text
                    self.in_thought = False
                    self.buffer = self.buffer[idx + len(end_tag):]
                    if self._is_incomplete_tool_call(self.buffer) and len(self.buffer) < self.max_buffer_limit:
                        return (thought_out, "")
                    if self._has_trailing_partial_tag(self.buffer):
                        return (thought_out, "")
                    flushed = self.clean_tool_tags(self.buffer)
                    self.buffer = ""
                    content_out += flushed
                    return (thought_out, content_out)

            return (thought_out, "")
        else:
            if self._is_incomplete_tool_call(self.buffer) and len(self.buffer) < self.max_buffer_limit:
                return (thought_out, "")
            if self._has_trailing_partial_tag(self.buffer):
                return (thought_out, "")
            flushed = self.clean_tool_tags(self.buffer)
            self.buffer = ""
            content_out += flushed
            return (thought_out, content_out)

    def clean_tool_tags(self, text: str) -> str:
        return _strip_tool_call_tags(text)

    def flush_remaining(self) -> str:
        if self.in_thought:
            cleaned = self.clean_tool_tags(self.buffer).strip()
            self.buffer = ""
            if not self.has_seen_explicit_thought_tag:
                return cleaned
            # If the model finished without closing <channel|>, strip thought markers and return valid text
            synthesized = strip_thought_blocks(cleaned).strip()
            return synthesized if synthesized else cleaned
        flushed = self.clean_tool_tags(self.buffer)
        self.buffer = ""   
        return flushed
    
    def flush(self) -> List[Dict[str, Any]]:
        rem= self.flush_remaining()
        if rem:
            return [{"type": "content", "content": rem}]
        return[]

# --- Custom Model Mapping Helpers ---
MAX_HISTORY_TURNS = 10

def is_standalone_model(model_id: str) -> bool:
    return model_id not in ["serenity-supervisor", "serenity-supervisor-high", "serenity-supervisor-low"]

def map_custom_model_id(model_id: str) -> str:
    if model_id in ["serenity-supervisor", "serenity-supervisor-high", "serenity-supervisor-low"]:
        return SUPERVISOR_MODEL
    return model_id

def validate_workspace_path(path_value: str) -> str:
    if not isinstance(path_value, str):
        raise ValueError("Workspace path must be a string")
    if "\x00" in path_value:
        raise ValueError("Workspace path contains invalid characters")

    raw = path_value.strip()
    if not raw or raw in (".", ".."):
        raise ValueError("Workspace path is empty or invalid")

    normalized = os.path.normpath(raw)
    parts = normalized.replace("\\", "/").split("/")
    if ".." in parts:
        raise ValueError("Workspace path traversal '..' is not allowed")

    return normalized

async def run_orchestration(request: QueryRequest, http_request: Request) -> AsyncGenerator[Dict[str, Any], None]:
    session_id = request.session_id or "default_session"
    requested_model = request.model or CURRENT_MODEL or SUPERVISOR_MODEL
    mapped_model = await resolve_model(requested_model)
    resolved_model = resolve_gguf_path(mapped_model) or mapped_model
    
    if session_id not in sessions_history:
        sessions_history[session_id] = []
            
    # Format conversation history fitting dynamically within context budget
    history_str = "\n".join(
        f"User: {h.get('prompt', '')}\nAssistant: {h.get('answer', '')}"
        for h in sessions_history[session_id]
    )

    # 1. Reasoning Strength Directives
    reasoning_directives = {
        "low": "Keep internal reasoning minimal and ultra-concise. Focus directly on execution.",
        "medium": "Perform methodical step-by-step reasoning before tool calls or final output.",
        "high": "Conduct deep, rigorous architectural reasoning, verifying all edge cases and dependencies.",
        "xhigh": "Exhaustively analyze multi-perspective architecture, edge cases, safety boundaries, and validation steps."
    }
    active_reasoning_prompt = reasoning_directives.get(reasoning_strength, reasoning_directives["medium"])

    # 2. Limit Tier Bounds
    limit_tier_caps = {
        "low": 8,
        "default": 16,
        "medium": 25,
        "high": 50,
        "autonomy": 1000
    }
    is_autonomy = (limit_tier == "autonomy") or auto_continue_enabled
    max_loops = 1000 if is_autonomy else limit_tier_caps.get(limit_tier, 16)

    # 3. Persistent Long-Term Memory Injection
    ltm_summary = LongTermMemoryManager.get_context_summary(max_items=5)
    ltm_section = f"\n{ltm_summary}\n" if ltm_summary else ""

    current_prompt = f"""<|turn>system
<|think|>
You are SerenityDev Orchestrator. You solve coding queries autonomously using tools.
Reasoning Guidance: {active_reasoning_prompt}
{ltm_section}
{PYTHON_TOOL_STUBS}
When calling a tool, execute Python function call syntax in a ```python block or JSON object.
<turn|>
<|turn>user
History:
{history_str}

User Request: {request.prompt}
<turn|>
<|turn>model
<|channel>thought
"""

    loop_count = 0
    executed_calls = set()
    accumulated_answer = []
    routing_steps: List[Dict[str, Any]] = []

    while loop_count < max_loops:
        if await http_request.is_disconnected():
            log_message("[Supervisor] Orchestration cancelled by client disconnect.")
            break

        thought_filter = StreamingThoughtFilter()
        raw_chunks = []
        model_content = []

        async for chunk in generate_completion_stream(resolved_model, current_prompt, temperature=0.2, num_ctx=CONTEXT_WINDOW):
            if await http_request.is_disconnected():
                log_message("[Supervisor] Stream aborted by client connection drop.")
                break
            raw_chunks.append(chunk)
            content = thought_filter.feed(chunk)
            if content:
                model_content.extend(event.get("content", "") for event in content)

        model_content.extend(
            event.get("content", "") for event in thought_filter.flush()
            if event.get("type") == "content"
        )

        raw_output = "".join(raw_chunks)
        accumulated_answer.append(raw_output)
        
        parsed_tool = extract_json(raw_output)
        if parsed_tool and isinstance(parsed_tool, dict):
            action = parsed_tool.get("action")
            target = parsed_tool.get("target", "")

            # 1. Subagent Delegation Pathway ("Offload then load")
            if action == "delegate_worker" or target in ["W1", "W2", "W3", "W4"]:
                worker_map = {
                    "W1": W1_MODEL,  # Reasoning & Architecture
                    "W2": W2_MODEL,  # Heavy Code Synthesis
                    "W3": W3_MODEL,  # Fast Utilities / Scripting / Explanations
                    "W4": W4_MODEL   # Specialized worker
                }
                
                subagent_model = worker_map.get(target, target)
                subagent_instructions = extract_instructions(parsed_tool.get("arguments_or_instructions", ""))
                
                yield {"type": "progress", "text": f"🤖 Delegating subtask to `{target}` ({subagent_model}) [Offload context -> Execute]..."}
                
                # Subagents receive independent execution budget (bypass parent limit)
                subagent_max_loops = 100 if is_autonomy else 15
                sub_loop = 0
                subagent_prompt = f"<|turn>system\nYou are a specialized Serenity subagent ({target}). Accomplish the task directly:\n{PYTHON_TOOL_STUBS}\n<turn|>\n<|turn>user\nTask: {subagent_instructions}\n<turn|>\n<|turn>model\n<|channel>thought\n"
                sub_accumulated = []

                while sub_loop < subagent_max_loops:
                    sub_chunks = []
                    sub_tf = StreamingThoughtFilter()
                    async for sub_chunk in generate_completion_stream(subagent_model, subagent_prompt, temperature=0.2, num_ctx=CONTEXT_WINDOW):
                        if await http_request.is_disconnected():
                            break
                        sub_chunks.append(sub_chunk)
                        c = sub_tf.feed(sub_chunk)
                        if c:
                            for ev in c:
                                yield ev
                    
                    for ev in sub_tf.flush():
                        if ev.get("type") == "content":
                            yield ev

                    sub_raw = "".join(sub_chunks)
                    sub_accumulated.append(sub_raw)

                    sub_tool = extract_json(sub_raw)
                    if sub_tool and isinstance(sub_tool, dict) and isinstance(sub_tool.get("target"), str) and sub_tool.get("target") not in ["W1", "W2", "W3", "W4"]:
                        s_target: str = sub_tool["target"]
                        s_payload = sub_tool.get("arguments_or_instructions", {})
                        s_res = dispatch_tool_call(s_target, s_payload, step_num=sub_loop + 1, workspace_dir=request.workspace_dir)
                        subagent_prompt += f"\n{sub_raw}\n<|turn|>\n<|turn>user\n[Tool Response]\n{s_res['tool_context']}\n<turn|>\n<|turn>model\n<|channel>thought\n"
                        sub_loop += 1
                    else:
                        break

                handoff_report = clean_thought_and_whitespace("".join(sub_accumulated)).strip() or strip_thought_blocks("".join(sub_accumulated)).strip()
                yield {"type": "progress", "text": f"📥 Subagent `{target}` completed. Loading HandoffReport to Supervisor..."}
                
                # Load handoff report back into supervisor context
                current_prompt += f"\n{raw_output}\n<|turn|>\n<|turn>user\n[HandoffReport from {target}]\n{handoff_report}\n<turn|>\n<|turn>model\n<|channel>thought\n"
                loop_count += 1
                continue

            # 2. Tool Calling Pathway
            elif target:
                payload = parsed_tool.get("arguments_or_instructions", {})
                yield {"type": "progress", "text": f"⚙️ Executing tool `{target}`..."}
                res = dispatch_tool_call(target, payload, step_num=loop_count + 1, workspace_dir=request.workspace_dir)
                tool_context = res["tool_context"]
                routing_steps.append({
                    "step": loop_count + 1,
                    "tool": target,
                    "details": res.get("details", ""),
                    "proof": res.get("proof_tag")
                })
                progress_detail = res.get("details", res.get("status", "completed"))
                yield {"type": "progress", "text": f"✅ Tool `{target}` completed: {progress_detail}"}

                current_prompt += f"\n{raw_output}\n<|turn|>\n<|turn>user\n[System Tool Response]\n{tool_context}\n<|turn|>\n<|turn>model\n<|channel>thought\n"
                loop_count += 1
            else:
                yielded_any = False
                for content in model_content:
                    if content:
                        yield {"type": "content", "content": content}
                        yielded_any = True
                if not yielded_any:
                    fallback_text = clean_thought_and_whitespace(raw_output).strip()
                    if fallback_text:
                        yield {"type": "content", "content": fallback_text}
                    else:
                        stripped_thought = strip_thought_blocks(raw_output).strip()
                        yield {"type": "content", "content": stripped_thought or raw_output}
                break
        else:
            yielded_any = False
            for content in model_content:
                if content:
                    yield {"type": "content", "content": content}
                    yielded_any = True
            if not yielded_any:
                fallback_text = clean_thought_and_whitespace(raw_output).strip()
                if fallback_text:
                    yield {"type": "content", "content": fallback_text}
                else:
                    stripped_thought = strip_thought_blocks(raw_output).strip()
                    yield {"type": "content", "content": stripped_thought or raw_output}
            break

    final_text = "".join(accumulated_answer)
    sessions_history[session_id].append({"prompt": request.prompt, "answer": final_text})
    yield {"type": "done", "routing": {
        "worker": mapped_model,
        "steps": routing_steps,
        "step_count": loop_count
    }}

@app.post("/ask_stream")
async def ask_serenity_stream(request: QueryRequest, http_request: Request):
    if server_paused:
        raise HTTPException(status_code=503, detail="Server is paused.")

    async def event_generator():
        async for event in run_orchestration(request, http_request):
            yield f"data: {json.dumps(event)}\n\n"
    return StreamingResponse(event_generator(), media_type="text/event-stream", headers=SSE_HEADERS)

@app.post("/ask")
async def ask_serenity(request: QueryRequest, http_request: Request):
    answer_parts: List[str] = []
    routing_info: Dict[str, Any] = {}
    async for event in run_orchestration(request, http_request):
        if event.get("type") == "content":
            answer_parts.append(event.get("content", ""))
        elif event.get("type") == "done":
            routing_info = event.get("routing", {})

    return {"answer": "".join(answer_parts), "routing": routing_info}

@app.post("/fim")
async def autocomplete_fim(request: FimRequest):
    """
    Decoupled Autoreplacer (FIM). High-speed autocomplete.
    """
    global server_paused
    if server_paused:
        return {"completion": ""}

    global independenttask_count

    independenttask_count += 1 
    log_message(f"[FIM] Independent Request Started (Queue Depth: {independenttask_count})")

    try:
        global autoswap_timer_task
        if autoswap_timer_task and not autoswap_timer_task.done():
            autoswap_timer_task.cancel()

        resolved_fim_model = await resolve_model(request.model or FIM_MODEL)
        prefix, suffix = fit_fim_context(request.prefix, request.suffix)
        fim_prompt = f"<pre>{prefix}<suf>{suffix}<mid>"

        res = await generate_completion(
            model_name=resolved_fim_model,
            prompt=fim_prompt,
            temperature=0.0,
            num_ctx=CONTEXT_WINDOW,
            max_tokens=128,
            stop=["<pre>", "<suf>", "<mid>"]
        )
        completion_raw = res.get('response', '')
        completion = clean_thought_and_whitespace(completion_raw)

    except Exception as e:
        log_message(f"[FIM Error] Autocomplete failed: {e}")
        completion = ""
    finally:
        independenttask_count -= 1 
        log_message(f"[FIM] Independent Request Completed. Active count: {independenttask_count}")

    return {"completion": completion}

# --- Control Panel Endpoints ---

@app.post("/api/control/pause")
async def pause_server():
    global server_paused
    server_paused = True
    log_message("[Control] SerenityDev Server PAUSED.")
    return {"status": "paused"}

@app.post("/api/control/resume")
async def resume_server():
    global server_paused
    server_paused = False
    log_message("[Control] SerenityDev Server RESUMED.")
    return {"status": "online"}

@app.post("/api/control/unload")
@app.post("/api/unload")
async def unload_models_endpoint():
    """Explicitly unloads active llama-server and direct llama-cpp-python models to immediately free VRAM."""
    log_message("[Control] Unload models command received. Clearing VRAM...")
    unload_all_models()
    return {"status": "unloaded", "message": "Active model offloaded. VRAM freed successfully."}

# --- Config Management ---

class ConfigUpdate(BaseModel):
    current_model: Optional[str] = None
    cache_type_k: Optional[str] = None
    cache_type_v: Optional[str] = None
    context_window: Optional[int] = None
    gpu_layers: Optional[int] = None
    auto_continue: Optional[bool] = None
    reasoning_strength: Optional[str] = None
    limit_tier: Optional[str] = None
    custom_models_dir: Optional[str] = None
    custom_models_dirs: Optional[List[str]] = None
    roles: Optional[Dict[str, str]] = None
    supervisor_low_model: Optional[str] = None
    supervisor_high_model: Optional[str] = None
    orchestrator_turbo_model: Optional[str] = None
    supervisor_model: Optional[str] = None
    w1_model: Optional[str] = None
    w2_model: Optional[str] = None
    w3_model: Optional[str] = None
    w4_model: Optional[str] = None
    fim_model: Optional[str] = None

@app.get("/api/config")
async def get_config():
    installed = get_installed_models()
    return {
        "current_model": CURRENT_MODEL,
        "supervisor_model": SUPERVISOR_MODEL,
        "supervisor_low_model": SUPERVISOR_LOW_MODEL,
        "supervisor_high_model": SUPERVISOR_HIGH_MODEL,
        "orchestrator_turbo_model": ORCHESTRATOR_TURBO_MODEL,
        "w1_model": W1_MODEL,
        "w2_model": W2_MODEL,
        "w3_model": W3_MODEL,
        "w4_model": W4_MODEL,
        "fim_model": FIM_MODEL,
        "auto_continue": auto_continue_enabled,
        "reasoning_strength": reasoning_strength,
        "limit_tier": limit_tier,
        "roles": {
            "supervisor_low": SUPERVISOR_LOW_MODEL,
            "supervisor_high": SUPERVISOR_HIGH_MODEL,
            "orchestrator_turbo": ORCHESTRATOR_TURBO_MODEL,
            "w1_reasoning": W1_MODEL,
            "w2_code": W2_MODEL,
            "w3_fast": W3_MODEL,
            "w4_specialized": W4_MODEL,
            "fim": FIM_MODEL
        },
        "available_models": installed,
        "cache_type_k": cache_type_k,
        "cache_type_v": cache_type_v,
        "context_window": CONTEXT_WINDOW,
        "gpu_layers": gpu_layers_override,
        "custom_models_dirs": get_candidate_model_dirs()
    }

@app.post("/api/config")
async def update_config(config: ConfigUpdate, background_tasks: BackgroundTasks):
    global CURRENT_MODEL, cache_type_k, cache_type_v, CONTEXT_WINDOW, gpu_layers_override, auto_continue_enabled
    global SUPERVISOR_LOW_MODEL, SUPERVISOR_HIGH_MODEL, ORCHESTRATOR_TURBO_MODEL, SUPERVISOR_MODEL, W1_MODEL, W2_MODEL, W3_MODEL, W4_MODEL, FIM_MODEL
    global reasoning_strength, limit_tier
    
    if config.custom_models_dir is not None:
        if add_custom_model_dir(config.custom_models_dir):
            log_message(f"[Config] Added custom model folder: {config.custom_models_dir}")
        else:
            log_message(f"[Config Error] Custom model folder not found: {config.custom_models_dir}")
    if config.custom_models_dirs is not None:
        for d in config.custom_models_dirs:
            add_custom_model_dir(d)
        log_message(f"[Config] Updated custom model folders: {config.custom_models_dirs}")
    if config.auto_continue is not None:
        auto_continue_enabled = config.auto_continue
        log_message(f"[Config] Auto-continue set to: {auto_continue_enabled}")
    if config.reasoning_strength is not None:
        rs = config.reasoning_strength.lower().strip()
        if rs in ["low", "medium", "high", "xhigh"]:
            reasoning_strength = rs
            log_message(f"[Config] Reasoning strength set to: {reasoning_strength}")
    if config.limit_tier is not None:
        lt = config.limit_tier.lower().strip()
        if lt in ["default", "low", "medium", "high", "autonomy"]:
            limit_tier = lt
            if limit_tier == "autonomy":
                auto_continue_enabled = True
            log_message(f"[Config] Limit tier set to: {limit_tier} (auto_continue={auto_continue_enabled})")
    if config.cache_type_k is not None:
        cache_type_k = config.cache_type_k
        log_message(f"[Config] Key cache type (K) set to: {cache_type_k}")
    if config.cache_type_v is not None:
        cache_type_v = config.cache_type_v
        log_message(f"[Config] Value cache type (V) set to: {cache_type_v}")
    if config.context_window is not None and config.context_window > 0:
        CONTEXT_WINDOW = config.context_window
        log_message(f"[Config] Context window set to: {CONTEXT_WINDOW}")
    if config.gpu_layers is not None:
        gpu_layers_override = config.gpu_layers if config.gpu_layers >= 0 else None
        log_message(f"[Config] GPU layers override set to: {gpu_layers_override}")

    # Process Role Model Assignments
    if config.roles is not None:
        for role_k, model_v in config.roles.items():
            if not model_v:
                continue
            r_k = role_k.lower()
            if r_k in ["supervisor_low", "low"]:
                SUPERVISOR_LOW_MODEL = model_v
            elif r_k in ["supervisor_high", "high"]:
                SUPERVISOR_HIGH_MODEL = model_v
            elif r_k in ["orchestrator_turbo", "turbo", "orchestrator"]:
                ORCHESTRATOR_TURBO_MODEL = model_v
            elif r_k in ["w1_reasoning", "w1", "reasoning"]:
                W1_MODEL = model_v
            elif r_k in ["w2_code", "w2", "code"]:
                W2_MODEL = model_v
            elif r_k in ["w3_fast", "w3", "fast"]:
                W3_MODEL = model_v
            elif r_k in ["w4_specialized", "w4"]:
                W4_MODEL = model_v
            elif r_k in ["fim", "autocomplete"]:
                FIM_MODEL = model_v
        log_message(f"[Config] Updated roles mapping: {config.roles}")

    if config.supervisor_low_model is not None:
        SUPERVISOR_LOW_MODEL = config.supervisor_low_model
    if config.supervisor_high_model is not None:
        SUPERVISOR_HIGH_MODEL = config.supervisor_high_model
    if config.orchestrator_turbo_model is not None:
        ORCHESTRATOR_TURBO_MODEL = config.orchestrator_turbo_model
    if config.supervisor_model is not None:
        SUPERVISOR_MODEL = config.supervisor_model
    if config.w1_model is not None:
        W1_MODEL = config.w1_model
    if config.w2_model is not None:
        W2_MODEL = config.w2_model
    if config.w3_model is not None:
        W3_MODEL = config.w3_model
    if config.w4_model is not None:
        W4_MODEL = config.w4_model
    if config.fim_model is not None:
        FIM_MODEL = config.fim_model
        
    needs_reload = (
        config.current_model is not None or
        config.cache_type_k is not None or
        config.cache_type_v is not None or
        config.context_window is not None or
        config.gpu_layers is not None
    )
    if needs_reload:
        resolved = await resolve_model(config.current_model if config.current_model is not None else CURRENT_MODEL)
        if config.current_model is not None:
            CURRENT_MODEL = resolved
            log_message(f"[Config] Current active model set to: {CURRENT_MODEL}")
        
        # Warm-load/preload the model in background to apply cache and context settings
        async def run_preload():
            async with inference_lock:
                log_message(f"[Config] Warm-loading model '{resolved}' (ctx={CONTEXT_WINDOW}, gpu_layers={gpu_layers_override}, K={cache_type_k}, V={cache_type_v})...")
                try:
                    if llama_cpp_available and resolve_gguf_path(resolved):
                        loop = asyncio.get_event_loop()
                        await loop.run_in_executor(None, get_llama_model, resolved, CONTEXT_WINDOW, needs_reload)
                    else:
                        if llama_server_process is None or active_llama_server_model_name != resolved or needs_reload:
                            await start_llama_server(resolved, CONTEXT_WINDOW, force_reload=needs_reload)
                        payload = {"model": resolved, "prompt": "", "stream": False, "keep_alive": -1}
                        async with httpx.AsyncClient(timeout=25) as client:
                            await client.post(LLAMA_SERVER_URL, json=payload)
                    log_message(f"[Config] Successfully warm-loaded '{resolved}'.")
                except Exception as e:
                    log_message(f"[Config] Dynamic preload failed for '{resolved}': {e}")
        background_tasks.add_task(run_preload)

    # Persist config to disk after every update
    save_server_config()
        
    return {
        "status": "success",
        "current_model": CURRENT_MODEL,
        "auto_continue": auto_continue_enabled,
        "reasoning_strength": reasoning_strength,
        "limit_tier": limit_tier,
        "roles": {
            "supervisor_low": SUPERVISOR_LOW_MODEL,
            "supervisor_high": SUPERVISOR_HIGH_MODEL,
            "orchestrator_turbo": ORCHESTRATOR_TURBO_MODEL,
            "w1_reasoning": W1_MODEL,
            "w2_code": W2_MODEL,
            "w3_fast": W3_MODEL,
            "w4_specialized": W4_MODEL,
            "fim": FIM_MODEL
        },
        "cache_type_k": cache_type_k,
        "cache_type_v": cache_type_v,
        "context_window": CONTEXT_WINDOW,
        "gpu_layers": gpu_layers_override
    }

# --- Long-Term Memory API Endpoints ---

class MemoryStoreRequest(BaseModel):
    key: str
    category: Optional[str] = "general"
    content: str

@app.get("/api/memory")
async def get_all_memories(category: Optional[str] = None):
    """Returns all stored long-term memories with optional category filtering."""
    memories = LongTermMemoryManager.query("", category)
    return {
        "memories": memories,
        "count": len(memories)
    }

@app.post("/api/memory")
async def store_memory_endpoint(req: MemoryStoreRequest):
    """Stores or updates a long-term memory entry."""
    entry = LongTermMemoryManager.store(req.key, req.category or "general", req.content, source="user")
    return {"status": "stored", "memory": entry}

@app.delete("/api/memory/{key}")
async def delete_memory_endpoint(key: str):
    """Deletes a specific long-term memory entry by key."""
    success = LongTermMemoryManager.delete(key)
    return {"status": "deleted" if success else "not_found", "key": key}

@app.delete("/api/memory")
async def purge_all_memories_endpoint():
    """Purges the entire long-term memory database."""
    count = LongTermMemoryManager.purge_all()
    return {"status": "purged", "purged_count": count}

@app.delete("/api/session/clear")
@app.post("/api/session/clear")
async def clear_all_sessions():
    """Purges ephemeral current session memories across all sessions."""
    global sessions_history
    sessions_history.clear()
    log_message("[Session] Purged all ephemeral session memories.")
    return {"status": "cleared", "message": "All session memories purged."}

class RevertEditRequest(BaseModel):
    backup_id: str

@app.post("/api/edit/revert")
async def revert_file_edit(req: RevertEditRequest):
    if req.backup_id not in file_edit_backups:
        raise HTTPException(status_code=404, detail="Backup ID not found.")
    b = file_edit_backups.pop(req.backup_id)
    with open(b["file_path"], "w", encoding="utf-8") as f:
        f.write(b["content"])
    log_message(f"[Revert] File '{b['file_path']}' reverted successfully.")
    return {"status": "reverted", "backup_id": req.backup_id}

@app.post("/api/edit/keep")
async def keep_file_edit(req: RevertEditRequest):
    if req.backup_id not in file_edit_backups:
        file_edit_backups.pop(req.backup_id)
    return {"status": "kept", "backup_id": req.backup_id}

# --- Web UI & Dashboard Endpoints ---


cached_gpu_memory = None

def query_nvidia_smi_sync():
    global cached_gpu_memory
    try:
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", category=FutureWarning)
            import pynvml # type: ignore
            pynvml.nvmlInit()
            if pynvml.nvmlDeviceGetCount() > 0:
                handle = pynvml.nvmlDeviceGetHandleByIndex(0)
                mem_info = pynvml.nvmlDeviceGetMemoryInfo(handle)
                cached_gpu_memory = {
                    "used": round(int(mem_info.used) / (1024.0 * 1024.0 * 1024.0), 2),
                    "total": round(int(mem_info.total) / (1024.0 * 1024.0 * 1024.0), 2),
                    "unit": "GiB"
                }
                return cached_gpu_memory
    except Exception:
        pass

    try:
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        res = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total", "--format=csv,nounits,noheader"],
            capture_output=True, text=True, timeout=1.5, creationflags=flags
        )
        if res.returncode == 0 and res.stdout:
            parts = res.stdout.strip().split(",")
            if len(parts) == 2:
                cached_gpu_memory = {
                    "used": round(float(parts[0].strip()) / 1024.0, 2), 
                    "total": round(float(parts[1].strip()) / 1024.0, 2),
                    "unit": "GiB"
                }
                return cached_gpu_memory
    except Exception:
        pass
    return cached_gpu_memory

@app.get("/api/status")
async def get_status():
    installed = get_installed_models()
    loaded_vram = []
    if llama_server_process is not None and llama_server_process.poll() is None:
        try:
            async with httpx.AsyncClient(timeout=1.5) as client:
                res = await client.get(f"{LLAMA_SERVER_BASE}/v1/models")
                if res.status_code == 200:
                    loaded_vram = [{"name": m["id"]} for m in res.json().get("data", [])]
        except Exception:
            pass

    gpu_memory = None
    try:
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", category=FutureWarning)
            import pynvml
            pynvml.nvmlInit()
            if pynvml.nvmlDeviceGetCount() > 0:
                handle = pynvml.nvmlDeviceGetHandleByIndex(0)
                mem_info = pynvml.nvmlDeviceGetMemoryInfo(handle)
                gpu_memory = {
                    "used": round(int(mem_info.used) / (1024.0 * 1024.0 * 1024.0), 2),
                    "total": round(int(mem_info.total) / (1024.0 * 1024.0 * 1024.0), 2),
                    "unit": "GiB"
                }
    except Exception:
        pass

    if gpu_memory is None:
        gpu_memory = cached_gpu_memory

    targets = [
        {"name": ORCHESTRATOR_TURBO_MODEL, "gguf": "gemma-4-26B-A4B-it-qat-UD-Q4_K_XL.gguf", "dir": None},
        {"name": SUPERVISOR_HIGH_MODEL, "gguf": "gemma-4-26B_q4_0-it.gguf", "dir": None},
        {"name": SUPERVISOR_LOW_MODEL, "gguf": "gemma-4-E4B-it-qat-UD-Q4_K_XL.gguf", "dir": None},
        {"name": W1_MODEL, "gguf": "Qwen3.8-27B-UD-Q4_K_XL.gguf", "dir": None},
        {"name": W2_MODEL, "gguf": "codegemma-7b-it-f16.gguf", "dir": None},
        {"name": W3_MODEL, "gguf": "gemma-4-E4B-it-Coder.Q4_K_M.gguf", "dir": None},
        {"name": W4_MODEL, "gguf": "NVIDIA-Nemotron-3.5-Lightning-30B-A3B-MXFP4_MOE.gguf", "dir": None},
        {"name": FIM_MODEL, "gguf": "codegemma-2b-f16.gguf", "dir": None}
    ]

    registry_status = []
    for t in targets:
        name = t["name"]
        registered = any(m.startswith(name) or name.startswith(m) for m in installed)

        source_present = False
        source_type = "Missing"
        resolved_path = resolve_gguf_path(name)
        if resolved_path and os.path.exists(resolved_path):
            source_present = True
            source_type = "GGUF File"
        elif t["dir"] and os.path.exists(t["dir"]):
            source_present = True
            source_type = "Safetensors Dir"

        registry_status.append({
            "name": name,
            "registered": registered,
            "source_present": source_present,
            "source_type": source_type
        })

    return {
        "status": "paused" if server_paused else "online",
        "lock_active": inference_lock.locked(),
        "independenttask_count": independenttask_count,
        "loaded_vram": loaded_vram,
        "registry": registry_status,
        "logs": orchestrator_logs[-50:],
        "gpu_memory": gpu_memory,
        "current_model": CURRENT_MODEL,
        "cache_type_k": cache_type_k,
        "cache_type_v": cache_type_v,
        "context_window": CONTEXT_WINDOW,
        "gpu_layers": gpu_layers_override,
        "auto_continue": auto_continue_enabled,
        "reasoning_strength": reasoning_strength,
        "limit_tier": limit_tier,
        "memory_count": len(LongTermMemoryManager._load_data())
    }

@app.post("/api/register")
async def trigger_registration(background_tasks: BackgroundTasks):
    """Triggers background scanning and registration."""
    background_tasks.add_task(check_and_register_models)
    return {"message": "Model scanning and registration loop started in background."}

@app.post("/api/preload/{model_name}")
async def trigger_preload(model_name: str, background_tasks: BackgroundTasks):
    """Queues background preloading."""
    resolved = await resolve_model(model_name)
    
    async def run_preload():
        async with inference_lock:
            log_message(f"[UI Control] Pre-loading model '{resolved}'...")
            try:
                if llama_cpp_available and resolve_gguf_path(resolved):
                    loop = asyncio.get_event_loop()
                    await loop.run_in_executor(None, get_llama_model, resolved, CONTEXT_WINDOW)
                else:
                    if llama_server_process is None or active_llama_server_model_name != resolved:
                        await start_llama_server(resolved, CONTEXT_WINDOW)
                    payload = {"model": resolved, "prompt": "", "stream": False, "keep_alive": -1}
                    async with httpx.AsyncClient(timeout=25) as client:
                        await client.post(LLAMA_SERVER_URL, json=payload)
                log_message(f"[UI Control] Loaded '{resolved}' successfully.")
            except Exception as e:
                log_message(f"[UI Control] Preload failed for '{resolved}': {e}")

    background_tasks.add_task(run_preload)
    return {"message": f"Preloading request for '{resolved}' queued."}

@app.post("/api/shutdown")
async def shutdown_server():
    log_message("[Server] Shutdown initiated via Web UI control panel.")
    async def kill_process():
        await asyncio.sleep(1)
        os._exit(0)
    asyncio.create_task(kill_process())
    return {"message": "Shutdown command received. The server is shutting down..."}

@app.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    """Serves the highly responsive, feature-rich glassmorphic Orchestrator Playground Dashboard."""
    html_content = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>SerenityDev Agent Console</title>
    <!-- Tailwind CSS -->
    <script src="https://cdn.tailwindcss.com"></script>
    <!-- Google Fonts -->
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=Outfit:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
    <!-- Marked Markdown & Prism Code Styling -->
    <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
    <style>
        body {
            font-family: 'Inter', sans-serif;
            background: radial-gradient(circle at 50% 10%, #150d2c 0%, #080614 70%, #020204 100%);
        }
        h1, h2, h3, h4 {
            font-family: 'Outfit', sans-serif;
        }
        .glass-card {
            background: rgba(22, 16, 45, 0.45);
            backdrop-filter: blur(16px);
            -webkit-backdrop-filter: blur(16px);
            border: 1px solid rgba(255, 255, 255, 0.08);
            box-shadow: 0 12px 42px 0 rgba(0, 0, 0, 0.5);
            transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
        }
        .glass-card:hover {
            border-color: rgba(147, 51, 234, 0.25);
            box-shadow: 0 12px 42px 0 rgba(147, 51, 234, 0.12);
        }
        .glow-cyan {
            box-shadow: 0 0 18px rgba(6, 182, 212, 0.35);
        }
        .glow-purple {
            box-shadow: 0 0 18px rgba(168, 85, 247, 0.35);
        }
        .custom-scrollbar::-webkit-scrollbar {
            width: 6px;
            height: 6px;
        }
        .custom-scrollbar::-webkit-scrollbar-track {
            background: rgba(0, 0, 0, 0.2);
            border-radius: 4px;
        }
        .custom-scrollbar::-webkit-scrollbar-thumb {
            background: rgba(255, 255, 255, 0.12);
            border-radius: 4px;
        }
        .custom-scrollbar::-webkit-scrollbar-thumb:hover {
            background: rgba(168, 85, 247, 0.45);
        }
        @keyframes pulse-slow {
            0%, 100% { opacity: 1; transform: scale(1); }
            50% { opacity: 0.6; transform: scale(0.96); }
        }
        .pulsing-indicator {
            animation: pulse-slow 2.5s infinite ease-in-out;
        }
        .neon-border-active {
            border-color: rgba(168, 85, 247, 0.6);
            box-shadow: 0 0 15px rgba(168, 85, 247, 0.2);
        }
    </style>
</head>
<body class="text-slate-100 min-h-screen pb-12 custom-scrollbar">
    <!-- Header -->
    <header class="border-b border-white/5 py-4 mb-6 bg-black/35 backdrop-blur-md sticky top-0 z-40">
        <div class="max-w-7xl mx-auto px-6 flex justify-between items-center">
            <div class="flex items-center gap-3">
                <div class="w-9 h-9 rounded-xl bg-gradient-to-tr from-purple-600 to-cyan-400 flex items-center justify-center glow-purple">
                    <span class="text-white font-bold text-xl">S</span>
                </div>
                <div>
                    <h1 class="text-lg font-bold tracking-tight bg-clip-text text-transparent bg-gradient-to-r from-white to-purple-200">SerenityDev Orchestrator</h1>
                    <p class="text-[10px] text-cyan-400 font-mono tracking-widest uppercase">Autonomous Tool-Wielding Core</p>
                </div>
            </div>
            
            <div class="flex items-center gap-4">
                <div class="relative group">
                    <div id="connectionStatus" class="flex items-center gap-2 px-3 py-1.5 rounded-full bg-emerald-500/10 border border-emerald-500/25 cursor-pointer hover:bg-emerald-500/20 transition-colors">
                        <span class="w-2.5 h-2.5 rounded-full bg-emerald-400 pulsing-indicator glow-cyan"></span>
                        <span class="text-xs font-semibold text-emerald-400 uppercase tracking-wider">SYSTEM ACTIVE</span>
                        <span class="text-xs opacity-65 group-hover:opacity-100">▼</span>
                    </div>
                    <div class="absolute right-0 mt-2 w-52 bg-slate-900 border border-white/10 rounded-xl shadow-2xl opacity-0 invisible group-hover:opacity-100 group-hover:visible transition-all duration-200 z-50">
                        <div class="py-1">
                            <button onclick="sendControlCommand('restart')" class="w-full text-left px-4 py-2.5 text-xs hover:bg-purple-600/20 hover:text-purple-300 transition-colors flex items-center gap-2">
                                <span>🔄</span> Soft Reset State
                            </button>
                            <button onclick="sendControlCommand('clear_logs')" class="w-full text-left px-4 py-2.5 text-xs hover:bg-cyan-600/20 hover:text-cyan-300 transition-colors flex items-center gap-2">
                                <span>🗑️</span> Clear Terminal Logs
                            </button>
                            <div class="border-t border-white/5 my-1"></div>
                            <button onclick="sendControlCommand('shutdown')" class="w-full text-left px-4 py-2.5 text-xs text-rose-400 hover:bg-rose-950/30 hover:text-rose-300 transition-colors flex items-center gap-2">
                                <span>⏹️</span> Shutdown Server
                            </button>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    </header>

    <!-- Main Grid Content -->
    <main class="max-w-7xl mx-auto px-6 grid grid-cols-1 lg:grid-cols-12 gap-6">
        
        <!-- Left Side (8 Columns): Interactive Playground & Config -->
        <div class="lg:col-span-8 space-y-6">
            
            <!-- Dynamic Configuration Panel -->
            <div class="glass-card p-5 rounded-2xl">
                <div class="flex flex-wrap items-center justify-between gap-4 mb-4">
                    <div>
                        <h2 class="text-sm font-bold font-mono tracking-wider text-purple-300 uppercase">Routing & Model Configuration</h2>
                        <p class="text-xs text-slate-400">Manage active inference engine and dynamic VRAM caching</p>
                    </div>
                </div>

                <div class="grid grid-cols-1 md:grid-cols-2 gap-4 pt-3 border-t border-white/5">
                    <div>
                        <label class="block text-xs font-mono text-slate-400 mb-1.5">Active Target Model</label>
                        <select id="activeModelSelect" onchange="updateActiveModel()" class="w-full bg-slate-950/80 border border-white/10 rounded-lg px-3 py-2 text-xs font-mono text-slate-200 focus:outline-none focus:border-purple-500">
                            <option value="gemma-4-E4B-it-Coder.Q4_K_M">Gemma-4 (E4B-it-Coder) [Active Coder]</option>
                            <option value="gemma-4-26B-A4B">Gemma-4 (26B-A4B) [Supervisor / W1]</option>
                            <option value="codegemma-7b-it">CodeGemma (7B-it) [W2 Synthesizer]</option>
                        </select>
                    </div>
                    <div>
                        <label class="block text-xs font-mono text-slate-400 mb-1.5">Autoswap State</label>
                        <div class="bg-black/35 border border-white/5 rounded-lg px-3 py-2 text-xs flex justify-between items-center">
                            <span class="text-slate-400">Supervisor VRAM Swap</span>
                            <span class="font-mono text-cyan-400 font-semibold" id="autoswapStatus">Initialized (4m timeout)</span>
                        </div>
                    </div>
                </div>

                <div class="grid grid-cols-1 md:grid-cols-2 gap-4 pt-3 mt-3 border-t border-white/5">
                    <div>
                        <label class="block text-xs font-mono text-slate-400 mb-1.5">KV Cache Key (K)</label>
                        <select id="cacheTypeKSelect" onchange="updateCacheCompression()" class="w-full bg-slate-950/80 border border-white/10 rounded-lg px-3 py-2 text-xs font-mono text-slate-200 focus:outline-none focus:border-purple-500">
                            <option value="f16">fp16 (Default)</option>
                            <option value="q8_0">q8_0 (8-bit quantization)</option>
                            <option value="q5_1">q5_1 (5-bit quantization)</option>
                            <option value="q5_0">q5_0 (5-bit quantization)</option>
                            <option value="q4_0">q4_0 (4-bit quantization)</option>
                            <option value="turbo4_tcq">turbo4_tcq</option>
                            <option value="turbo3_tcq">turbo3_tcq</option>
                            <option value="turbo2_tcq">turbo2_tcq</option>
                        </select>
                    </div>
                    <div>
                        <label class="block text-xs font-mono text-slate-400 mb-1.5">KV Cache Value (V)</label>
                        <select id="cacheTypeVSelect" onchange="updateCacheCompression()" class="w-full bg-slate-950/80 border border-white/10 rounded-lg px-3 py-2 text-xs font-mono text-slate-200 focus:outline-none focus:border-purple-500">
                            <option value="f16">fp16 (Default)</option>
                            <option value="q8_0">q8_0 (8-bit quantization)</option>
                            <option value="q5_1">q5_1 (5-bit quantization)</option>
                            <option value="q5_0">q5_0 (5-bit quantization)</option>
                            <option value="q4_0">q4_0 (4-bit quantization)</option>
                            <option value="turbo4_tcq">turbo4_tcq</option>
                            <option value="turbo3_tcq">turbo3_tcq</option>
                            <option value="turbo2_tcq">turbo2_tcq</option>
                        </select>
                    </div>
                </div>
            </div>

            <!-- Interactive Live Playground (Chat Panel) -->
            <div class="glass-card rounded-2xl flex flex-col h-[520px] overflow-hidden">
                <div class="px-5 py-3.5 bg-black/40 border-b border-white/5 flex justify-between items-center">
                    <div class="flex items-center gap-2">
                        <span class="w-2.5 h-2.5 rounded-full bg-purple-500 glow-purple"></span>
                        <span class="font-semibold text-xs tracking-wider text-slate-300">SERENITY DEV AGENT PLAYGROUND</span>
                    </div>
                    <span class="text-[9px] font-mono bg-purple-900/30 border border-purple-500/25 px-2 py-0.5 rounded text-purple-300 uppercase tracking-widest">Interactive API</span>
                </div>
                
                <!-- Chat Log Area -->
                <div id="chatLog" class="flex-1 p-5 overflow-y-auto custom-scrollbar space-y-4">
                    <div class="flex gap-3">
                        <div class="w-8 h-8 rounded-lg bg-purple-600 flex items-center justify-center text-xs font-bold shrink-0">🤖</div>
                        <div class="bg-slate-900/60 border border-white/5 rounded-xl px-4 py-3 text-xs max-w-[85%] text-slate-200">
                            <p class="font-semibold text-purple-300 mb-1">SerenityDev Agent Core</p>
                            Greetings! I am equipped with real-time autonomous workspace tools: `list_directory`, `read_file`, `write_file`, `insert_edit_into_file`, `replace_string_in_file`, `multi_replace_string_in_file`, and `grep_search`. I can autonomously investigate context and execute edits across the project. How can I help you program today?
                        </div>
                    </div>
                </div>

                <!-- Live Thinking & Tool Execution Progress -->
                <div id="thinkingTimeline" class="hidden px-5 py-3 bg-slate-950/80 border-t border-b border-white/5 space-y-2">
                    <div class="flex items-center gap-2">
                        <div class="w-3.5 h-3.5 rounded-full border-2 border-purple-500 border-t-transparent animate-spin"></div>
                        <span class="text-[11px] font-mono text-purple-300 font-bold uppercase tracking-wider">Agent Thinking & Tool Execution pipeline active...</span>
                    </div>
                    <div id="timelineSteps" class="space-y-1.5 pl-5 text-[10px] font-mono text-slate-400">
                        <!-- Filled by JS -->
                    </div>
                </div>

                <!-- Chat Input -->
                <div class="p-4 bg-slate-950/60 border-t border-white/5 flex gap-3">
                    <input type="text" id="chatInput" placeholder="Ask Serenity to research files, write code, or explain features..." onkeydown="handleInputKey(event)" class="flex-1 bg-slate-900 border border-white/10 rounded-xl px-4 py-2.5 text-xs text-slate-100 placeholder-slate-500 focus:outline-none focus:border-purple-500 focus:ring-1 focus:ring-purple-500">
                    <button id="sendBtn" onclick="sendPlaygroundQuery()" class="px-5 py-2.5 rounded-xl bg-purple-600 hover:bg-purple-500 text-xs font-bold text-white transition flex items-center gap-1.5 glow-purple">
                        <span>Send</span> ➡️
                    </button>
                </div>
            </div>

            <!-- Model Registry & Library status -->
            <div class="glass-card p-5 rounded-2xl">
                <div class="flex justify-between items-center mb-4">
                    <div>
                        <h2 class="text-sm font-bold font-mono tracking-wider text-cyan-300 uppercase">Target Model Registry</h2>
                        <p class="text-[11px] text-slate-400">Available local source configurations</p>
                    </div>
                    <button onclick="triggerRegisterAll()" id="btnRegisterAll" class="px-3.5 py-1.5 text-xs font-bold rounded-lg bg-purple-600/35 border border-purple-500/35 text-purple-200 hover:bg-purple-600 transition">
                        Scan & Register Missing
                    </button>
                </div>

                <div class="overflow-x-auto custom-scrollbar">
                    <table class="w-full text-left border-collapse min-w-[600px]">
                        <thead>
                            <tr class="border-b border-white/5 text-slate-400 text-[10px] uppercase tracking-wider font-mono">
                                <th class="pb-2 font-semibold">Orchestrator Role</th>
                                <th class="pb-2 font-semibold">Target Model</th>
                                <th class="pb-2 font-semibold">Workspace Source</th>
                                <th class="pb-2 font-semibold">Library State</th>
                                <th class="pb-2 font-semibold text-right">Actions</th>
                            </tr>
                        </thead>
                        <tbody id="registryTableBody" class="divide-y divide-white/5 text-xs">
                            <!-- Filled dynamically -->
                        </tbody>
                    </table>
                </div>
            </div>

        </div>

        <!-- Right Side (4 Columns): Real-time Diagnostic Indicators & Live Logs -->
        <div class="lg:col-span-4 space-y-6">
            
            <!-- VRAM & GPU Status Panel -->
            <div class="glass-card p-5 rounded-2xl space-y-4">
                <h2 class="text-sm font-bold font-mono tracking-wider text-cyan-300 uppercase">Hardware & VRAM Footprint</h2>
                
                <!-- System VRAM progress -->
                <div class="space-y-2">
                    <div class="flex justify-between text-xs font-mono">
                        <span class="text-slate-400">GPU VRAM Resident</span>
                        <span class="text-cyan-400 font-bold" id="gpuMemoryVal">Detecting...</span>
                    </div>
                    <div class="w-full h-2 rounded-full bg-slate-800 overflow-hidden">
                        <div id="gpuMemoryBar" class="h-full bg-gradient-to-r from-cyan-400 to-purple-500 transition-all duration-500" style="width: 0%"></div>
                    </div>
                </div>

                <!-- Llama-Server Active VRAM Loader -->
                <div class="pt-3 border-t border-white/5">
                    <span class="text-[10px] font-mono text-slate-400 block mb-2 uppercase">VRAM Resident Models (Llama-Server API)</span>
                    <div id="vramContainer" class="space-y-2">
                        <div class="py-4 flex flex-col items-center justify-center border border-dashed border-white/10 rounded-xl bg-black/25">
                            <span class="text-xl mb-1">💤</span>
                            <span class="text-[10px] font-semibold text-slate-500 tracking-wide uppercase">VRAM Resident: NONE</span>
                        </div>
                    </div>
                </div>
            </div>

            <!-- Active Concurrency Lock -->
            <div class="glass-card p-5 rounded-2xl flex items-center justify-between">
                <div>
                    <h3 class="text-xs font-mono font-bold text-slate-400 uppercase tracking-widest">Inference Lock</h3>
                    <p class="text-[10px] text-slate-500 mt-0.5" id="lockSubtitle">Ready for immediate pipeline execution.</p>
                </div>
                <div id="lockBadge" class="px-3 py-1.5 rounded-xl bg-emerald-500/10 border border-emerald-500/25 text-emerald-400 font-mono font-bold text-xs flex items-center gap-1.5">
                    <span class="w-2 h-2 rounded-full bg-emerald-400 pulsing-indicator"></span> 🔓 FREE
                </div>
            </div>

            <!-- Quick Warm Load Controls -->
            <div class="glass-card p-5 rounded-2xl space-y-3">
                <h3 class="text-xs font-mono font-bold text-slate-400 uppercase tracking-widest">Warm-Load Model Library</h3>
                <div class="grid grid-cols-1 gap-2">
                    <button onclick="triggerPreload('gemma-4-26B-A4B')" class="py-2 text-[11px] font-mono font-semibold rounded-lg bg-black/40 border border-white/5 hover:border-purple-500/40 hover:bg-purple-600/15 transition text-left px-3 flex justify-between items-center">
                        <span>Preload Supervisor</span> <span class="text-slate-500">26B</span>
                    </button>
                    <button onclick="triggerPreload('qwen3.6-35B-A3B')" class="py-2 text-[11px] font-mono font-semibold rounded-lg bg-black/40 border border-white/5 hover:border-purple-500/40 hover:bg-purple-600/15 transition text-left px-3 flex justify-between items-center">
                        <span>Preload Explainer (W3)</span> <span class="text-slate-500">35B</span>
                    </button>
                </div>
            </div>

            <!-- Live Mono Terminal Logs -->
            <div class="glass-card rounded-2xl overflow-hidden flex flex-col h-[320px]">
                <div class="px-4 py-3 bg-black/40 border-b border-white/5 flex justify-between items-center">
                    <div class="flex items-center gap-2">
                        <span class="w-2 h-2 rounded-full bg-cyan-400"></span>
                        <span class="font-mono text-[10px] font-semibold tracking-wider text-slate-300 uppercase">Live Pipeline Output</span>
                    </div>
                    <span class="text-[9px] font-mono bg-white/10 px-2 py-0.5 rounded text-slate-400 uppercase">Activity</span>
                </div>
                
                <div id="terminalConsole" class="flex-1 p-4 bg-black/60 font-mono text-[11px] text-cyan-400/90 overflow-y-auto custom-scrollbar space-y-2 flex flex-col justify-end">
                    <!-- Logs populated here -->
                </div>
            </div>

        </div>

    </main>

    <!-- Modal countdown popup -->
    <div id="countdownModal" class="fixed inset-0 z-50 hidden flex items-center justify-center bg-black/90 backdrop-blur-md">
        <div class="text-center">
            <div class="w-16 h-16 rounded-full border-4 border-rose-500/20 border-t-rose-500 animate-spin mx-auto mb-6"></div>
            <h3 class="text-xl font-bold text-rose-400 mb-1">Shutting Down</h3>
            <p id="countdownText" class="text-slate-400 font-mono text-xs">Terminating server and port binding on 8002...</p>
        </div>
    </div>

    <!-- Script Block -->
    <script>
        let logsLength = 0;
        let isConsolidationUpdating = false;

        async function fetchStatus() {
            try {
                const res = await fetch("/api/status");
                const data = await res.json();

                // 1. Update GPU memory card
                const memVal = document.getElementById("gpuMemoryVal");
                const memBar = document.getElementById("gpuMemoryBar");
                if (data.gpu_memory) {
                    const pct = ((data.gpu_memory.used / data.gpu_memory.total) * 100).toFixed(0);
                    memVal.textContent = `${data.gpu_memory.used.toFixed(1)} / ${data.gpu_memory.total.toFixed(1)} ${data.gpu_memory.unit}`;
                    memBar.style.width = `${pct}%`;
                } else {
                    // Fallback to active loaded VRAM sum
                    let activeVramGb = 0;
                    const loadedVram = Array.isArray(data.loaded_vram) ? data.loaded_vram : [];
                    if (loadedVram.length > 0) {
                        loadedVram.forEach(m => {
                            activeVramGb += (m.size_vram / 1024 / 1024 / 1024);
                        });
                    }
                    if (activeVramGb > 0) {
                        memVal.textContent = `${activeVramGb.toFixed(1)} GB (Resident)`;
                        memBar.style.width = `${Math.min((activeVramGb / 24) * 100, 100).toFixed(0)}%`;
                    } else {
                        memVal.textContent = "Standby (CPU Mode)";
                        memBar.style.width = "4%";
                    }
                }

                // 2. Concurrency lock UI
                const lockBadge = document.getElementById("lockBadge");
                const lockSub = document.getElementById("lockSubtitle");
                if (data.lock_active) {
                    lockBadge.className = "px-3 py-1.5 rounded-xl bg-purple-600/10 border border-purple-500/25 text-purple-400 font-mono font-bold text-xs flex items-center gap-1.5";
                    lockBadge.innerHTML = `<span class="w-2 h-2 rounded-full bg-purple-400 pulsing-indicator glow-purple"></span> 🔒 ACTIVE`;
                    lockSub.textContent = "Pipeline executing active inference request...";
                } else {
                    lockBadge.className = "px-3 py-1.5 rounded-xl bg-emerald-500/10 border border-emerald-500/25 text-emerald-400 font-mono font-bold text-xs flex items-center gap-1.5";
                    lockBadge.innerHTML = `<span class="w-2 h-2 rounded-full bg-emerald-400 pulsing-indicator"></span> 🔓 FREE`;
                    lockSub.textContent = "Ready for immediate pipeline execution.";
                }

                // 3. Model & Cache UI Switch Sync (prevent infinite triggers)
                if (!isConsolidationUpdating) {
                    const modelSel = document.getElementById("activeModelSelect");
                    if (modelSel && data.current_model) {
                        modelSel.value = data.current_model;
                    }
                    
                    if (data.cache_type_k) {
                        document.getElementById("cacheTypeKSelect").value = data.cache_type_k;
                    }
                    if (data.cache_type_v) {
                        document.getElementById("cacheTypeVSelect").value = data.cache_type_v;
                    }
                }

                // 4. Update VRAM resident models
                updateVramUI(data.loaded_vram);

                // 5. Update Model Registry status
                updateRegistryUI(data.registry);

                // 6. Update logs
                updateLogsUI(data.logs);

            } catch (err) {
                console.error("Status fetching failed:", err);
            }
        }

        function updateVramUI(models) {
            const container = document.getElementById("vramContainer");
            if (!models || models.length === 0) {
                container.innerHTML = `
                    <div class="py-4 flex flex-col items-center justify-center border border-dashed border-white/10 rounded-xl bg-black/25">
                        <span class="text-xl mb-1">💤</span>
                        <span class="text-[10px] font-semibold text-slate-500 tracking-wide uppercase">VRAM Resident: NONE</span>
                    </div>
                `;
                return;
            }
            
            let html = "";
            models.forEach(m => {
                const gbSize = (m.size_vram / 1024 / 1024 / 1024).toFixed(2);
                html += `
                    <div class="p-3 rounded-xl bg-purple-950/20 border border-purple-500/15 flex justify-between items-center text-xs">
                        <div>
                            <span class="text-[9px] bg-purple-500/20 border border-purple-500/30 px-1.5 py-0.5 rounded font-mono text-purple-300 uppercase tracking-wider">Loaded</span>
                            <h4 class="font-bold text-slate-200 mt-1.5 font-mono">${m.name.split(':')[0]}</h4>
                        </div>
                        <div class="text-right">
                            <span class="text-sm font-bold text-cyan-400 font-mono">${gbSize}</span>
                            <span class="text-[10px] text-slate-500 font-mono">GB</span>
                        </div>
                    </div>
                `;
            });
            container.innerHTML = html;
        }

        function updateRegistryUI(registry) {
            const tbody = document.getElementById("registryTableBody");
            let html = "";
            
            const roleMap = {
                "gemma-4-26B-A4B": "Supervisor / W1 Tier",
                "codegemma-7b-it": "W2 Coding Worker",
                "qwen3.6-35B-A3B": "W3 Explainer Tier",
                "codegemma-2b": "Autocompleter (FIM)"
            };

            registry.forEach(r => {
                const role = roleMap[r.name] || "Worker";
                const regBadge = r.registered 
                    ? `<span class="px-2 py-0.5 text-[9px] font-bold rounded-full bg-emerald-500/10 border border-emerald-500/20 text-emerald-400 uppercase font-mono">OK</span>`
                    : `<span class="px-2 py-0.5 text-[9px] font-bold rounded-full bg-rose-500/10 border border-rose-500/20 text-rose-400 uppercase font-mono">UNREG</span>`;
                
                const srcBadge = r.source_present 
                    ? `<span class="text-slate-300 font-mono text-[10px]">💾 ${r.source_type}</span>`
                    : `<span class="text-slate-500 font-mono text-[10px]">🚫 Missing</span>`;

                const btnPreload = r.registered
                    ? `<button onclick="triggerPreload('${r.name}')" class="px-2 py-1 text-[10px] rounded bg-black/40 border border-white/10 hover:border-purple-500 hover:text-white transition font-mono">Warm Load</button>`
                    : `<button disabled class="px-2 py-1 text-[10px] rounded bg-black/10 border border-white/5 text-slate-600 cursor-not-allowed font-mono">Warm Load</button>`;

                const btnRegister = !r.registered && r.source_present
                    ? `<button onclick="triggerRegisterSingle()" class="px-2 py-1 text-[10px] rounded bg-purple-600 text-white font-mono hover:bg-purple-500 transition">Register</button>`
                    : ``;

                html += `
                    <tr class="hover:bg-white/5 transition">
                        <td class="py-3 font-semibold text-slate-200">${role}</td>
                        <td class="py-3 font-mono text-slate-400 text-[11px]">${r.name}</td>
                        <td class="py-3">${srcBadge}</td>
                        <td class="py-3">${regBadge}</td>
                        <td class="py-3 text-right space-x-1.5">${btnRegister}${btnPreload}</td>
                    </tr>
                `;
            });
            tbody.innerHTML = html;
        }

        function updateLogsUI(logs) {
            const consoleBox = document.getElementById("terminalConsole");
            if (!logs || logs.length === 0) {
                consoleBox.innerHTML = `<div class="text-slate-500 italic text-center py-6">Waiting for activity logs...</div>`;
                return;
            }
            if (logs.length !== logsLength) {
                logsLength = logs.length;
                let html = "";
                logs.forEach(l => {
                    let line = l;
                    if (line.includes("[Orchestrator]")) line = `<span class="text-cyan-400 font-bold">${line}</span>`;
                    else if (line.includes("[Supervisor]")) line = `<span class="text-purple-400 font-bold">${line}</span>`;
                    else if (line.includes("[Worker]")) line = `<span class="text-amber-400 font-mono">${line}</span>`;
                    else if (line.includes("Successfully")) line = `<span class="text-emerald-400">${line}</span>`;
                    else if (line.includes("Error") || line.includes("failed")) line = `<span class="text-rose-400 font-semibold">${line}</span>`;
                    html += `<div>${line}</div>`;
                });
                consoleBox.innerHTML = html;
                consoleBox.scrollTop = consoleBox.scrollHeight;
            }
        }

        // Live Chat Playground Implementation
        async function sendPlaygroundQuery() {
            const input = document.getElementById("chatInput");
            const sendBtn = document.getElementById("sendBtn");
            const text = input.value.trim();
            if (!text) return;

            // Append User Message to Playground
            appendChatMessage("User", text);
            input.value = "";
            input.disabled = true;
            sendBtn.disabled = true;

            // Trigger timeline loader
            const timeline = document.getElementById("thinkingTimeline");
            const stepsContainer = document.getElementById("timelineSteps");
            timeline.classList.remove("hidden");
            stepsContainer.innerHTML = `<div>🟢 Initializing Routing Pipeline...</div>`;

            // Start polling timeline status during inference
            const timelineTimer = setInterval(async () => {
                try {
                    const statusRes = await fetch("/api/status");
                    const statusData = await statusRes.json();
                    if (statusData.logs && statusData.logs.length > 0) {
                        let html = "";
                        statusData.logs.slice(-5).forEach(l => {
                            html += `<div>⚙️ ${l}</div>`;
                        });
                        stepsContainer.innerHTML = html;
                    }
                } catch (e) {}
            }, 800);

            try {
                const res = await fetch("/ask", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ prompt: text, session_id: "playground" })
                });
                
                clearInterval(timelineTimer);
                timeline.classList.add("hidden");

                if (res.ok) {
                    const data = await res.json();
                    appendChatMessage("Serenity", data.answer, data.routing);
                } else {
                    appendChatMessage("Serenity", "❌ Error: Failed to generate agentic solution from worker pool.");
                }
            } catch (err) {
                clearInterval(timelineTimer);
                timeline.classList.add("hidden");
                appendChatMessage("Serenity", "❌ Error: Host API became completely unreachable.");
            } finally {
                input.disabled = false;
                sendBtn.disabled = false;
                input.focus();
            }
        }

        function appendChatMessage(sender, text, routing = null) {
            const chatLog = document.getElementById("chatLog");
            const bubble = document.createElement("div");
            bubble.className = "flex gap-3";
            const messageText = typeof text === "string" ? text : "No response was returned by the model.";
            
            const isUser = (sender === "User");
            const avatar = isUser ? "👤" : "🤖";
            const senderName = isUser ? "You" : "SerenityDev";
            const nameColor = isUser ? "text-cyan-400" : "text-purple-300";

            let routingBox = "";
            if (routing && typeof routing === "object") {
                const steps = Array.isArray(routing.steps) ? routing.steps : [];
                const stepCount = steps.length;
                let stepDetails = "";
                if (steps.length > 0) {
                    stepDetails = `<div class="mt-2 pl-4 border-l border-white/5 space-y-1 text-[10px] font-mono text-slate-400">`;
                    steps.forEach(s => {
                        stepDetails += `<div>🛠️ Step ${s.step}: <code>${s.tool}</code> ➡️ <em>${s.details}</em></div>`;
                    });
                    stepDetails += `</div>`;
                }

                routingBox = `
                    <div class="mt-3 p-3 rounded-lg bg-black/40 border border-white/5 text-[10px] font-mono text-slate-300">
                        <div class="flex justify-between items-center mb-1 text-purple-300 font-bold uppercase tracking-wider">
                            <span>🗺️ System Routing Details</span>
                            <span class="text-[9px] text-cyan-400">${routing.review_badge}</span>
                        </div>
                        <div>Target Worker: <span class="text-cyan-400 font-semibold">${routing.worker || "unknown"}</span> (${typeof routing.worker_model === "string" ? routing.worker_model.split(':')[0] : "unknown"})</div>
                        <div>Reasoning: <span class="italic">"${routing.reason || "No routing explanation was returned."}"</span></div>
                        ${stepDetails}
                    </div>
                `;
            }

            // HTML content with basic markdown rendering for premium markdown answers
            const formattedText = marked.parse ? marked.parse(messageText) : messageText.replace(/\n/g, "<br/>");

            bubble.innerHTML = `
                <div class="w-8 h-8 rounded-lg ${isUser ? 'bg-cyan-600' : 'bg-purple-600'} flex items-center justify-center text-xs font-bold shrink-0">${avatar}</div>
                <div class="bg-slate-900/60 border border-white/5 rounded-xl px-4 py-3 text-xs max-w-[85%] text-slate-200">
                    <p class="font-semibold ${nameColor} mb-1.5">${senderName}</p>
                    <div class="prose prose-invert prose-xs leading-relaxed max-w-none">${formattedText}</div>
                    ${routingBox}
                </div>
            `;
            chatLog.appendChild(bubble);
            chatLog.scrollTop = chatLog.scrollHeight;
        }

        function handleInputKey(event) {
            if (event.key === "Enter") {
                sendPlaygroundQuery();
            }
        }

        // Update Active Model API
        async function updateActiveModel() {
            isConsolidationUpdating = true;
            const selectVal = document.getElementById("activeModelSelect").value;
            try {
                await fetch("/api/config", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ current_model: selectVal })
                });
            } catch (err) {}
            isConsolidationUpdating = false;
        }

        // Update KV cache compression API
        async function updateCacheCompression() {
            isConsolidationUpdating = true;
            const kVal = document.getElementById("cacheTypeKSelect").value;
            const vVal = document.getElementById("cacheTypeVSelect").value;
            try {
                await fetch("/api/config", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ cache_type_k: kVal, cache_type_v: vVal })
                });
            } catch (err) {}
            isConsolidationUpdating = false;
        }
 
         // Quick action command handler
        async function sendControlCommand(command) {
            if (command === 'shutdown') {
                const confirmed = confirm("⚠️ Shutdown SerenityDev Orchestrator process?\nThis releases port 8002 completely.");
                if (!confirmed) return;
            }
            try {
                const res = await fetch(`/api/${command}`, { method: "POST" });
                if (command === 'shutdown') {
                    document.getElementById("countdownModal").classList.remove("hidden");
                    setTimeout(() => {
                        window.close();
                    }, 2500);
                }
            } catch (err) {}
        }

        async function triggerPreload(model) {
            try {
                await fetch(`/api/preload/${model}`, { method: "POST" });
            } catch (err) {}
        }

        async function triggerRegisterAll() {
            const btn = document.getElementById("btnRegisterAll");
            btn.disabled = true;
            btn.innerText = "Scanning...";
            try {
                await fetch("/api/register", { method: "POST" });
            } catch (err) {}
            setTimeout(() => {
                btn.disabled = false;
                btn.innerText = "Scan & Register Missing";
            }, 3000);
        }

        // Initial setup
        fetchStatus();
        setInterval(fetchStatus, 1000);
    </script>
</body>
</html>
"""
    return HTMLResponse(content=html_content)

def free_port(port: int = 8002):
    """Checks if the target port is occupied on Windows or Linux/macOS and terminates stale occupying processes to prevent WinError 10048."""
    print(f"[Port Initializer] Scanning port {port}...")

    if os.name == 'nt':  # Windows
        try:
            flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            cmd = f"netstat -ano | findstr LISTENING | findstr :{port}"
            res = subprocess.run(cmd, shell=True, capture_output=True, text=True, creationflags=flags)
            if res.returncode == 0 and res.stdout:
                lines = res.stdout.strip().split('\n')
                killed_any = False
                for line in lines:
                    parts = line.strip().split()
                    if len(parts) >= 5:
                        try:
                            pid = int(parts[-1])
                            if pid != os.getpid() and pid > 0:
                                print(f"[Port Initializer] Port {port} is occupied by PID {pid}. Terminating stale process...")
                                subprocess.run(f"taskkill /F /PID {pid}", shell=True, capture_output=True, creationflags=flags)
                                killed_any = True
                        except Exception:
                            pass
                if killed_any:
                    time.sleep(1.0)
        except Exception as e:
            print(f"[Port Initializer] Failed to free port on Windows: {e}")
    else:  # Linux / macOS
        try:
            cmd = f"lsof -t -i:{port}"
            res = subprocess.run(cmd, shell=True, capture_output=True, text=True)
            if res.returncode == 0 and res.stdout:
                pids = res.stdout.strip().split('\n')
                for pid_str in pids:
                    try:
                        pid = int(pid_str.strip())
                        if pid != os.getpid() and pid > 0:
                            print(f"[Port Initializer] Port {port} is occupied by PID {pid}. Terminating stale process...")
                            os.kill(pid, signal.SIGKILL)
                    except Exception:
                        pass
        except Exception as e:
            print(f"[Port Initializer] Failed to free port on Unix: {e}")

class RestartRequest(BaseModel):
    model: Optional[str] = None

@app.post("/api/restart")
async def restart_server(background_tasks: BackgroundTasks, request: Optional[RestartRequest] = None):
    """Soft reset: clears logs and resets internal counters. Optionally changes active model."""
    global orchestrator_logs, independenttask_count, CURRENT_MODEL, sessions_history
    log_message("[Server] Soft restart initiated. Clearing state...")
    orchestrator_logs = []
    independenttask_count = 0
    sessions_history.clear()
    
    loop = asyncio.get_event_loop()
    def unload_all_models():
        if llama_server_process is None or llama_server_process.poll() is not None:
            return
        log_message("[Server] Unloading active models from Llama-Server VRAM...")
        try:
            installed = get_installed_models()
            for m in installed:
                httpx.post(LLAMA_SERVER_URL, json={"model": m, "keep_alive": 0}, timeout=5)
            log_message("[Server] Successfully unloaded Llama-Server models.")
        except Exception as e:
            log_message(f"[Server] Error unloading Llama-Server models: {e}")
            
    await loop.run_in_executor(None, unload_all_models)
    
    if request:
        if request.model is not None:
            resolved = await resolve_model(request.model)
            CURRENT_MODEL = resolved
            log_message(f"[Server] Active model changed to: {resolved}")
            if background_tasks:
                async def run_preload():
                    async with inference_lock:
                        log_message(f"[Server Preload] Warm-loading model '{resolved}'...")
                        try:
                            if llama_cpp_available and resolve_gguf_path(resolved):
                                loop = asyncio.get_event_loop()
                                await loop.run_in_executor(None, get_llama_model, resolved, CONTEXT_WINDOW, True)
                            else:
                                if llama_server_process is None or active_llama_server_model_name != resolved:
                                    await start_llama_server(resolved, CONTEXT_WINDOW)
                                payload = {"model": resolved, "prompt": "", "stream": False, "keep_alive": -1}
                                async with httpx.AsyncClient(timeout=25) as client: await client.post(LLAMA_SERVER_URL, json=payload)
                            log_message(f"[Server Preload] Successfully loaded '{resolved}' into memory.")
                        except Exception as e:
                            log_message(f"[Server Preload] Preload failed for '{resolved}': {e}")
                background_tasks.add_task(run_preload)

    log_message("[Server] State reset complete. Ready for new requests.")
    return {
        "status": "soft reset complete",
        "current_model": CURRENT_MODEL
    }

@app.post("/api/clear_logs")
async def clear_logs():
    """Clears the orchestrator activity logs."""
    global orchestrator_logs
    log_message("[Server] Clearing activity logs...")
    log_count = len(orchestrator_logs)
    orchestrator_logs = []
    return {"status": f"logs cleared", "cleared_count": log_count}

@app.post("/api/session/rotate")
async def trigger_session_rotate(store_and_resume: bool = True):
    """Triggers session key rotation, encrypting and preserving state, then resuming context seamlessly."""
    res = SessionRotationManager.rotate(store_and_resume=store_and_resume)
    return JSONResponse(content={
        "status": "success",
        "message": "Session rotated successfully with state preserved",
        "details": res
    })

@app.get("/api/session/status")
async def get_session_status():
    """Returns security health, multi-factor hardware binding state, rotation epoch, and downtime timer metrics."""
    return JSONResponse(content=SessionRotationManager.get_status())



@app.post("/shutdown")
async def shutdown():
    """Endpoint to gracefully shut down the server."""
    log_message("[Server] Shutdown command received. The server is shutting down gracefully...")
    async def kill_process():
        await asyncio.sleep(0.5)
        os.kill(os.getpid(), signal.SIGTERM)

    asyncio.create_task(kill_process())
    return {"message": "Shutdown command received. The server is shutting down..."}

class EndpointFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return record.getMessage().find("GET /api/status") == -1

if __name__ == "__main__":
    import logging
    logging.getLogger("uvicorn.access").addFilter(EndpointFilter())
    load_server_config()
    free_port(8002)
    print("[DevServer] Startng SerenityDev...")
    uvicorn.run(app, host="127.0.0.1", port=8002, reload=False)