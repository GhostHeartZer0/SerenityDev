# serenitydevserver.py
import asyncio
import json
import os
import re
import subprocess
import sys
import time
import logging
from typing import Dict, Any, List, Optional
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, BackgroundTasks, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel
import httpx
import uvicorn
import signal

# Force working directory to be the directory of this server script
workspace_dir = os.path.dirname(os.path.abspath(__file__))
os.chdir(workspace_dir)

# Localize TEMP/TMP and compiler cache paths to bypass Windows Smart App Control blocks
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

# Configuration Constants
LLAMA_SERVER_BASE = "http://localhost:8080"
LLAMA_SERVER_URL = f"{LLAMA_SERVER_BASE}/v1/completions"
LLAMA_SERVER_CHAT_URL = f"{LLAMA_SERVER_BASE}/v1/chat/completions"
llama_server_process = None

# Model Mapping Config
SUPERVISOR_MODEL = "gemma-4-26B-A4B"
W1_MODEL = "gemma-4-26B-A4B"       # Reasoning & Architecture
W2_MODEL = "codegemma-7b-it"         # Heavy Code Synthesis
W3_MODEL = "qwen3.6-35B-A3B"           # Fast Utilities / Scripting / Explanations
W4_MODEL = "qwen3.6-27B"           # Additional specialized worker
FIM_MODEL = "codegemma-2b"         # Inline Autocomplete

AUTOSWAP_TIMEOUT = 240.0            # Seconds before swapping back to Supervisor VRAM
CONTEXT_WINDOW = int(os.environ.get("SERENITY_CONTEXT_WINDOW", "16384")) # Configurable context window (default 16k)
MAX_RESPONSE_LENGTH = 1200          # Character limit for responses to Copilot Chat (~300 tokens, safe limit for VS Code Copilot)

# KV Cache Compression Settings
cache_type_k = "f16"
cache_type_v = "f16"

# Global State Management
inference_lock = asyncio.Lock()
autoswap_timer_task: Optional[asyncio.Task] = None
active_models_list: List[str] = []
orchestrator_logs: List[str] = []
independenttask_count: int = 0  # Tracks concurrent FIM requests
active_system_plan = {"focus": "None", "steps": []}

CURRENT_MODEL = SUPERVISOR_MODEL
model_consolidation = True
sessions_history = {} # session_id -> list of message context dicts
server_paused = False

# --- Architectural Insights from gork-build ---

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
        # Prune failures outside 60s sliding window
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
        entry = {
            "id": prompt_id,
            "text": text,
            "kind": kind,
            "timestamp": time.time()
        }
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

# Global instances
global_circuit_breaker = CircuitBreaker()
global_workspace_queue = WorkspaceQueue()

# llama-cpp-python state management
active_llama_model = None
active_llama_model_name = None
llama_cpp_available = False
try:
    import llama_cpp
    llama_cpp_available = True
except ImportError:
    pass

def resolve_gguf_path(model_name: str) -> Optional[str]:
    if not model_name:
        return None
        
    base_dir = os.path.dirname(os.path.abspath(__file__))
    
    # 1. Check dynamic JSON directory mapping
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
            log_message(f"[Models] Error reading models.json: {e}")

    # 2. Check absolute path directly
    if os.path.exists(model_name):
        return model_name
        
    # 3. Check local models directory
    models_dir = os.path.join(base_dir, "models")
    target_file = f"{model_name}.gguf" if not model_name.endswith('.gguf') else model_name
    
    if os.path.exists(os.path.join(models_dir, target_file)):
        return os.path.join(models_dir, target_file)
        
    for root, _, files in os.walk(models_dir):
        if target_file in files:
            return os.path.join(root, target_file)
            
    return None

active_llama_server_model_name = None

def unload_llama_server():
    """Stops the llama-server process to free VRAM."""
    global llama_server_process, active_llama_server_model_name
    try:
        if llama_server_process is not None:
            log_message("[Llama-Server] Stopping active llama-server process to free VRAM...")
            llama_server_process.terminate()
            llama_server_process.wait(timeout=5)
            llama_server_process = None
            log_message("[Llama-Server] Successfully terminated.")
    except Exception as e:
        log_message(f"[Llama-Server] Warning killing llama-server: {e}")
        if llama_server_process is not None:
            llama_server_process.kill()
            llama_server_process = None
    finally:
        active_llama_server_model_name = None

def unload_llama_model():
    """Explicitly unloads the direct llama-cpp-python model to clear VRAM."""
    global active_llama_model, active_llama_model_name
    if active_llama_model is not None:
        log_message(f"[Llama-CPP] Unloading model '{active_llama_model_name}' first...")
        active_llama_model = None
        active_llama_model_name = None
        import gc
        gc.collect()
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass
        log_message("[Llama-CPP] Direct model offloaded successfully.")

async def start_llama_server(model_name: str, n_ctx: int):
    """Starts the llama-server subprocess if using API fallback."""
    global llama_server_process, active_llama_server_model_name
    
    unload_llama_server() # This will kill existing server if any
    
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
    
    cmd = [
        "llama-server",
        "-m", gguf_path,
        "-c", str(n_ctx),
        "-ngl", str(gpu_layers),
        "--port", "8080",
        "--host", "127.0.0.1",
        "-fa" # flash attention
    ]
    if cache_type_k and cache_type_k != "f16":
        cmd.extend(["--cache-type-k", cache_type_k])
    if cache_type_v and cache_type_v != "f16":
        cmd.extend(["--cache-type-v", cache_type_v])
        
    if not offload_kqv:
        cmd.append("-nkvo")

    
    import subprocess
    try:
        # Popen to run in background
        llama_server_process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        
        # Poll health status to ensure it's ready
        log_message("[Llama-Server] Server process spawned. Polling health check...")
        import urllib.request
        import urllib.error
        
        healthy = False
        for i in range(45):
            await asyncio.sleep(1.0)

            try:
                req = urllib.request.Request(f"{LLAMA_SERVER_BASE}/health")
                with urllib.request.urlopen(req, timeout=1.0) as response:
                    if response.status == 200:
                        healthy = True
                        break
            except Exception:
                try:
                    req = urllib.request.Request(f"{LLAMA_SERVER_BASE}/v1/models")
                    with urllib.request.urlopen(req, timeout=1.0) as response:
                        if response.status == 200:
                            healthy = True
                            break
                except Exception:
                    pass
        
        if healthy:
            log_message("[Llama-Server] Server is healthy and responding.")
            active_llama_server_model_name = model_name
        else:
            log_message("[Llama-Server] Warning: Server failed health check after 45 seconds, proceeding anyway.")
            active_llama_server_model_name = model_name
    except Exception as e:
        log_message(f"[Llama-Server] Failed to spawn server: {e}")
        llama_server_process = None
        active_llama_server_model_name = None


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

def get_llama_model(model_name: str, n_ctx: int):
    global active_llama_model, active_llama_model_name
    if active_llama_model is not None:
        if active_llama_model_name != model_name or active_llama_model.n_ctx() < n_ctx:
            unload_llama_model()
    
    unload_llama_server()
    
    if active_llama_model is None:
        gguf_path = resolve_gguf_path(model_name)
        if not gguf_path:
            raise ValueError(f"Could not resolve GGUF path for model: {model_name}")
            
        try:
            gpu_layers, offload_kqv = calculate_dynamic_gpu_layers(model_name, n_ctx)
            if gpu_layers <= 0:
                gpu_layers = 1
        except Exception as e:
            log_message(f"[Llama-CPP] Error calculating GPU layers: {e}. Falling back to 1 layer.")
            gpu_layers = 1
            offload_kqv = False
            
        log_message(f"[Llama-CPP] Loading {model_name} from {gguf_path} (n_ctx={n_ctx}, gpu_layers={gpu_layers}) with KV cache compression K={cache_type_k}, V={cache_type_v}...")
        from llama_cpp import Llama
        active_llama_model = Llama(
            model_path=gguf_path,
            n_ctx=n_ctx,
            n_gpu_layers=gpu_layers,
            flash_attn=True,
            offload_kqv=offload_kqv,
            n_threads=8,
            verbose=False,
            type_k=get_ggml_type(cache_type_k),
            type_v=get_ggml_type(cache_type_v)
        )

        active_llama_model_name = model_name
    return active_llama_model

async def generate_completion_stream(model_name: str, prompt: str, temperature: float, num_ctx: int, max_tokens: int = -1, stop: Optional[List[str]] = None, min_p: float = 0.05, repeat_penalty: float = 1.05):
    if stop is None:
        stop = ["<turn|>", "<|turn|>", "<bos>", "<eos>"]
    if llama_cpp_available:
        try:
            gguf_path = resolve_gguf_path(model_name)
            if gguf_path:
                queue = asyncio.Queue()
                loop = asyncio.get_event_loop()
                def producer():
                    try:
                        llm = get_llama_model(model_name, num_ctx)
                        # Dynamically check if prompt length exceeds current context window
                        try:
                            prompt_tokens = llm.tokenize(prompt.encode('utf-8'))
                            token_count = len(prompt_tokens)
                            # Give a safety buffer (default to 2048 if max_tokens is -1 or None)
                            headroom = max_tokens if max_tokens > 0 else 2048
                            required_ctx = token_count + headroom
                            if required_ctx > llm.n_ctx():
                                log_message(f"[Llama-CPP] Prompt tokens ({token_count}) + headroom ({headroom}) exceeds loaded context limit ({llm.n_ctx()}). Dynamic reloading to n_ctx={required_ctx}...")
                                llm = get_llama_model(model_name, required_ctx)
                        except Exception as token_ex:
                            log_message(f"[Llama-CPP] Error during token count pre-check: {token_ex}")

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
                            if not isinstance(chunk, dict):
                                continue
                            choices = chunk.get("choices")
                            if not isinstance(choices, list) or len(choices) == 0:
                                continue
                            first_choice = choices[0]
                            if not isinstance(first_choice, dict):
                                continue
                            text = first_choice.get("text", "")
                            if text:
                                loop.call_soon_threadsafe(queue.put_nowait, text)
                    except Exception as ex:
                        loop.call_soon_threadsafe(queue.put_nowait, ex)
                    finally:
                        loop.call_soon_threadsafe(queue.put_nowait, None)
                loop.run_in_executor(None, producer)
                while True:
                    val = await queue.get()
                    if val is None:
                        break
                    if isinstance(val, Exception):
                        raise val
                    yield val
                return
        except Exception as e:
            log_message(f"[Llama-CPP Stream Error] Direct streaming failed, falling back to Llama-Server API: {e}")

    # Start or ensure llama-server is running for this model
    if llama_server_process is None or active_llama_server_model_name != model_name:
        await start_llama_server(model_name, num_ctx)



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

async def generate_completion(model_name: str, prompt: str, temperature: float, num_ctx: int, max_tokens: int = -1, stop: Optional[List[str]] = None, min_p: float = 0.05, repeat_penalty: float = 1.05) -> Dict[str, Any]:
    result = []
    async for chunk in generate_completion_stream(model_name, prompt, temperature, num_ctx, max_tokens, stop, min_p, repeat_penalty):
        result.append(chunk)
    return {"response": "".join(result)}

SUPERVISOR_PROMPT = """
You are the Orchestrator of a multi-agent system. Your goal is to manage tasks by delegating to specialized workers or using available tools.

AVAILABLE WORKERS:
- Worker 1: Specialized in [Worker 1 Description]
- Worker 2: Specialized in [Worker 2 Description]
- Worker 3: Specialized in [Worker 3 Description]
- Worker 4: Specialized in [Worker 4 Description]

AVAILABLE TOOLS:
- [Tool Name]: [Tool Description]

DECISION PROCESS:
1. Analyze the current state and user request.
2. Determine if a tool can solve the problem directly.
3. If not, determine which worker is best suited for the task.
4. Formulate a precise instruction or tool call.

OUTPUT FORMAT:
You MUST respond with a single JSON object. Do not include any text before or after the JSON.

SCHEMA:
{
  "action": "call_tool" | "delegate_worker",
  "target": "name_of_tool_or_worker",
  "tool_arguments": { "key": "value" }, // Only if action is 'call_tool'. Otherwise null.
  "instructions": "Detailed instructions for the worker", // Only if action is 'delegate_worker'. Otherwise null.
  "reasoning": "Brief explanation of why this action was chosen"
}

EXAMPLES:

Example 1: Calling a tool
{
  "action": "call_tool",
  "target": "read_file",
  "tool_arguments": { "path": "config.json" },
  "instructions": null,
  "reasoning": "I need to check the configuration file to proceed."
}

Example 2: Delegating to a worker
{
  "action": "delegate_worker",
  "target": "Worker 1",
  "tool_arguments": null,
  "instructions": "Analyze the logs in /var/log/syslog and summarize errors.",
  "reasoning": "Worker 1 is the expert in log analysis."
}
"""
class QueryRequest(BaseModel):
    prompt: str
    context: str = ""
    model: str = SUPERVISOR_MODEL
    session_id: Optional[str] = None
    workspace_dir: Optional[str] = None

class FimRequest(BaseModel):
    prefix: str
    suffix: str
    model: str = FIM_MODEL

# --- Activity Log Helper ---

def log_message(msg: str):
    """Prints a message to console and appends it to the orchestrator log buffer, filtering out non-ASCII to prevent Windows console crashes."""
    clean_msg = "".join(c for c in msg if 32 <= ord(c) <= 126 or c in "\n\r\t")
    print(clean_msg)
    global orchestrator_logs
    orchestrator_logs.append(clean_msg)
    if len(orchestrator_logs) > 55:
        orchestrator_logs.pop(0)

# --- Startup Check & Auto-Registration ---

def get_installed_models() -> List[str]:
    """Scan local models directory recursively for .gguf files, register them, and maintain models.json."""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    models = []
    
    json_path = os.path.join(base_dir, "models.json")
    model_map = {}
    map_updated = False
    
    # 1. Load existing models.json map
    if os.path.exists(json_path):
        try:
            with open(json_path, 'r', encoding='utf-8') as f:
                model_map = json.load(f)
                for model_name, path in list(model_map.items()):
                    model_name_lower = model_name.lower()
                    # Filter out MTP, assistant, and mmproj files
                    if any(tag in model_name_lower for tag in ['mmproj', 'assistant', 'mtp']):
                        del model_map[model_name]
                        map_updated = True
                        continue
                    if os.path.exists(path):
                        models.append(model_name)
        except Exception as e:
            log_message(f"[Models] Error reading models.json: {e}")

    # 2. Scan local models directory and update map
    models_dir = os.path.join(base_dir, "models")
    try:
        if not os.path.exists(models_dir):
            os.makedirs(models_dir, exist_ok=True)
            
        for root, _, files in os.walk(models_dir):
            for f in files:
                if f.endswith('.gguf'):
                    name = f[:-5]
                    name_lower = name.lower()
                    # Filter out MTP, assistant, and mmproj files
                    if any(tag in name_lower for tag in ['mmproj', 'assistant', 'mtp']):
                        continue
                    if name not in models:
                        models.append(name) # remove .gguf extension
                    
                    # Ensure absolute path is in the map
                    abs_path = os.path.abspath(os.path.join(root, f))
                    if name not in model_map or model_map[name] != abs_path:
                        model_map[name] = abs_path
                        map_updated = True
    except Exception as e:
        log_message(f"[Startup Error] Failed to list models: {e}")
        
    # 3. Save updated map back to models.json if changes occurred
    if map_updated or not os.path.exists(json_path):
        try:
            with open(json_path, 'w', encoding='utf-8') as f:
                json.dump(model_map, f, indent=4)
        except Exception as e:
            log_message(f"[Models] Error saving models.json: {e}")

    return models

def check_and_register_models():
    """Initializes active model targets by auto-discovering GGUFs."""
    global active_models_list, SUPERVISOR_MODEL, W1_MODEL, W2_MODEL, W3_MODEL, W4_MODEL, FIM_MODEL
    log_message("[Startup] Scanning for local model files...")
    active_models_list = get_installed_models()
    log_message(f"[Startup] Found models: {active_models_list}")
    
    if active_models_list:
        models = sorted(active_models_list, reverse=True)
        
        main_models = [m for m in models if not any(tag in m.lower() for tag in ['mmproj', 'assistant', 'mtp'])]
        if not main_models:
            main_models = models
            
        fim_candidates = [m for m in main_models if '2b' in m.lower() or 'code' in m.lower()]
        FIM_MODEL = fim_candidates[-1] if fim_candidates else main_models[-1]
        
        w2_candidates = [m for m in main_models if ('7b' in m.lower() or 'code' in m.lower()) and m != FIM_MODEL]
        W2_MODEL = w2_candidates[0] if w2_candidates else main_models[0]
        
        super_candidates = [m for m in main_models if 'a4b' in m.lower() or '35b' in m.lower() or '26b' in m.lower()]
        SUPERVISOR_MODEL = super_candidates[0] if super_candidates else main_models[0]
        W1_MODEL = SUPERVISOR_MODEL
        W3_MODEL = super_candidates[1] if len(super_candidates) > 1 else SUPERVISOR_MODEL
        W4_MODEL = super_candidates[2] if len(super_candidates) > 2 else SUPERVISOR_MODEL
        
        log_message(f"[Startup] Auto-Assigned Supervisor: {SUPERVISOR_MODEL}")
        log_message(f"[Startup] Auto-Assigned W2 (Code): {W2_MODEL}")
        log_message(f"[Startup] Auto-Assigned FIM: {FIM_MODEL}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Run model check synchronously to avoid race conditions with early requests
    check_and_register_models()
    yield

app = FastAPI(
    title="Serenity Orchestrator",
    description="Multi-agent local orchestrator featuring Hierarchical Supervisor routing and Autoreplacer (FIM) autocomplete management.",
    lifespan=lifespan
)

# --- GPU Layer Offloading Helpers ---

def get_target_vram_mb() -> float:
    try:
        res = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,nounits,noheader"],
            capture_output=True, text=True, check=True
        )
        lines = res.stdout.strip().split("\n")
        if lines:
            free = float(lines[0])
            # Leave 600MB safety headroom to prevent Shared VRAM paging
            return max(500.0, free - 600.0)
    except Exception:
        pass
    return 4000.0  # Fallback: assume 4.0GB available


def get_model_info(model_name: str):
    """Estimate model info from local GGUF or fallback to defaults."""
    gguf_path = resolve_gguf_path(model_name)
    if gguf_path and os.path.exists(gguf_path):
        size = os.path.getsize(gguf_path)
        # Parse block count from GGUF metadata
        try:
            import struct
            with open(gguf_path, "rb") as f:
                header = f.read(64 * 1024)
                if header[:4] == b'GGUF':
                    idx = header.find(b"block_count")
                    if idx != -1:
                        type_offset = idx + len("block_count")
                        val_type = struct.unpack("<I", header[type_offset:type_offset+4])[0]
                        if val_type in (4, 5):  # UINT32 or INT32
                            layers = struct.unpack("<I" if val_type == 4 else "<i", header[type_offset+4:type_offset+8])[0]
                            if 0 < layers < 150:
                                return layers, size
        except Exception:
            pass
        # Fallbacks based on size
        if size > 20 * 1024 * 1024 * 1024:
            return 60, size
        elif size > 10 * 1024 * 1024 * 1024:
            return 40, size
        return 32, size
    return 32, int(7e9 * 0.55)

def calculate_dynamic_gpu_layers(model_name: str, ctx_size: int) -> tuple[int, bool]:
    targeted_reserve_vram_mb = get_target_vram_mb()
    total_layers, model_base_vram_bytes = get_model_info(model_name)
    if total_layers == 0:
        total_layers = 32
    
    model_base_vram_mb = model_base_vram_bytes / (1024 * 1024)
    vram_per_layer = model_base_vram_mb / total_layers
    
    if ctx_size <= 49152:
        kv_cache_vram_mb = 3150.0
    else:
        kv_cache_vram_mb = (ctx_size / 49152) * 3150.0
        
    available_weight_vram = targeted_reserve_vram_mb - kv_cache_vram_mb
    offload_kqv = True
    
    if available_weight_vram <= 0:
        log_message(f"[DYNAMIC AUTO-OFFLOAD] Cache footprint ({kv_cache_vram_mb:.1f}MB) saturates VRAM. Moving KV Cache to RAM to preserve GPU layers.")
        available_weight_vram = targeted_reserve_vram_mb
        offload_kqv = False
        
    safe_layers = int(available_weight_vram // vram_per_layer)
    final_layers = max(1, min(total_layers, safe_layers))
    
    log_message("--- DYNAMIC VRAM REPORT ---")
    log_message(f"Model:            {model_name}")
    log_message(f"Total Layers:     {total_layers}")
    log_message(f"File Size:        {model_base_vram_mb:.1f} MiB (~{vram_per_layer:.1f} MiB/layer)")
    log_message(f"Est. KV Cache:    {kv_cache_vram_mb:.1f} MiB (Offloaded: {offload_kqv})")
    log_message(f"Target VRAM:      {targeted_reserve_vram_mb:.1f} MiB")
    log_message(f"Action:           Offloading {final_layers}/{total_layers} layers to GPU")
    log_message("----------------------------")
    return final_layers, offload_kqv

# --- Graceful Fallbacks ---

async def resolve_model(target: str) -> str:
    """Returns the requested model if installed, or falls back to Supervisor."""
    global active_models_list
    if not active_models_list:
        loop = asyncio.get_event_loop()
        active_models_list = await loop.run_in_executor(None, get_installed_models)
    
    # Prioritize main models over mmproj/assistant/mtp when doing prefix matching
    main_candidates = []
    other_candidates = []
    for model_name in active_models_list:
        if model_name.startswith(target) or target.startswith(model_name):
            if any(tag in model_name.lower() for tag in ['mmproj', 'assistant', 'mtp']):
                other_candidates.append(model_name)
            else:
                main_candidates.append(model_name)
                
    if main_candidates:
        return main_candidates[0]
    if other_candidates:
        return other_candidates[0]
            
    log_message(f"[Fallback] Model '{target}' not found. Falling back to '{SUPERVISOR_MODEL}'.")
    return SUPERVISOR_MODEL

def clean_thought_and_whitespace(text: str) -> str:
    """Removes thinking blocks and leading/trailing blank lines/whitespace."""
    if not text:
        return ""
    orig_text = text
    text = re.sub(r'ễ.*?ễ', '', text, flags=re.DOTALL)
    text = re.sub(r'<thought>.*?(?:</thought>)', '', text, flags=re.DOTALL)
    text = re.sub(r'<think>.*?(?:</think>)', '', text, flags=re.DOTALL)
    text = re.sub(r'<\|channel\>thought.*?(?:<channel\|\>)', '', text, flags=re.DOTALL)
    
    cleaned = text.strip()
    if not cleaned:
        # Fallback: if all text was stripped, try removing unclosed tags from the end
        unclosed_stripped = orig_text
        unclosed_stripped = re.sub(r'<thought>.*$', '', unclosed_stripped, flags=re.DOTALL)
        unclosed_stripped = re.sub(r'<think>.*$', '', unclosed_stripped, flags=re.DOTALL)
        unclosed_stripped = re.sub(r'<\|channel\>thought.*$', '', unclosed_stripped, flags=re.DOTALL)
        cleaned_unclosed = unclosed_stripped.strip()
        if cleaned_unclosed:
            return cleaned_unclosed
        return orig_text.strip()
    return cleaned

def extract_json(text: str) -> Optional[Dict[str, Any]]:
    """Defensively extracts and parses JSON from raw LLM output."""
    cleaned = clean_thought_and_whitespace(text).strip()
    if not cleaned or ('{' not in cleaned and '<|tool_call' not in cleaned):
        return None

    # 0. Intercept native <|tool_call> syntax
    if "<|tool_call" in cleaned:
        match = re.search(r'<\|tool_call\|>\s*call:\s*([^\s{]+)\s*(\{.*?\})\s*<tool_call\|>', cleaned, re.DOTALL)
        if match:
            func_name = match.group(1).strip()
            args_str = match.group(2).strip()
            try:
                args = json.loads(args_str)
            except Exception:
                args = args_str
            return {
                "action": "call_tool",
                "target": func_name,
                "arguments_or_instructions": args,
                "step_summary": f"Native tool call: {func_name}",
                "reason": "Parsed from <|tool_call>"
            }
        
        # Fallback for weird syntaxes like <|tool_call>call:run_task(task_id="npm: 3")<tool_call|>
        alt_match = re.search(r'<\|tool_call\|>\s*call:\s*([^\(]+)\((.*?)\)\s*<tool_call\|>', cleaned, re.DOTALL)
        if alt_match:
            func_name = alt_match.group(1).strip()
            args = alt_match.group(2).strip()
            return {
                "action": "call_tool",
                "target": func_name,
                "arguments_or_instructions": args,
                "step_summary": f"Native tool call: {func_name}",
                "reason": "Parsed from <|tool_call>"
            }

    # 1. Try to extract from markdown code blocks first
    if "```" in cleaned:
        match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', cleaned, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1).strip())
            except json.JSONDecodeError:
                pass

    # 2. If it starts/ends with braces, try to parse it directly
    if cleaned.startswith('{') and cleaned.endswith('}'):
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass

    # 3. Try to locate the first '{' and last '}' and parse the substring
    start = cleaned.find('{')
    end = cleaned.rfind('}')
    if start != -1 and end != -1 and end > start:
        if not (start == 0 and end == len(cleaned) - 1):
            try:
                return json.loads(cleaned[start:end+1].strip())
            except json.JSONDecodeError:
                pass

    # 4. As a last resort, try parsing the whole cleaned string
    if not (cleaned.startswith('{') and cleaned.endswith('}')):
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
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
            if llama_cpp_available and resolve_gguf_path(resolved_model_name):
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
    """Returns the dynamically discovered local models in the format expected by the VS Code extension."""
    global active_models_list
    if not active_models_list:
        active_models_list = get_installed_models()

    models_to_report = [
        {
            "id": "serenity-supervisor-high",
            "name": f"Serenity: Supervisor - High Mode ({SUPERVISOR_MODEL})",
            "family": "serenity-supervisor",
            "version": "1.0.0",
            "maxInputTokens": 120000,
            "maxOutputTokens": 16384,
            "capabilities": {"toolCalling": True, "imageInput": False}
        },
        {
            "id": "serenity-supervisor-low",
            "name": f"Serenity: Supervisor - Low Mode ({SUPERVISOR_MODEL})",
            "family": "serenity-supervisor",
            "version": "1.0.0",
            "maxInputTokens": 120000,
            "maxOutputTokens": 16384,
            "capabilities": {"toolCalling": True, "imageInput": False}
        },
        {
            "id": "serenity-supervisor",
            "name": f"Serenity: Orchestrator ({SUPERVISOR_MODEL}) [Default/High]",
            "family": "serenity-supervisor",
            "version": "1.0.0",
            "maxInputTokens": 120000,
            "maxOutputTokens": 16384,
            "capabilities": {"toolCalling": True, "imageInput": False}
        }
    ]

    for model_name in active_models_list:
        gguf_path = resolve_gguf_path(model_name)
        if gguf_path and os.path.exists(gguf_path):
            models_to_report.append({
                "id": model_name,
                "name": f"Serenity: {model_name} (Worker)",
                "family": model_name,
                "version": "1.0.0",
                "maxInputTokens": 120000,
                "maxOutputTokens": 16384,
                "capabilities": {"toolCalling": True, "imageInput": False}
            })

    return {"models": models_to_report}

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
    (r"^pnpm\s+install\s+--frozen-lockfile\b", True)
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

class StreamingThoughtFilter:
    def __init__(self):
        self.buffer = ""
        self.in_thought = False
        self.thought_started = False
        
    def feed(self, chunk: str) -> str:
        self.buffer += chunk
        
        if not self.thought_started:
            for tag in ["<|channel>thought", "<think>", "<thought>"]:
                if tag in self.buffer:
                    self.in_thought = True
                    self.thought_started = True
                    break
            if not self.thought_started and len(self.buffer) > 50:
                flushed = self.buffer
                self.buffer = ""
                return flushed
                
        if self.in_thought:
            for end_tag in ["<channel|>", "</think>", "</thought>"]:
                idx = self.buffer.find(end_tag)
                if idx != -1:
                    self.in_thought = False
                    self.buffer = self.buffer[idx + len(end_tag):]
                    flushed = self.buffer
                    self.buffer = ""
                    return flushed
            return ""
        else:
            flushed = self.buffer
            self.buffer = ""
            return flushed

    def flush_remaining(self) -> str:
        if self.in_thought:
            return ""
        return self.buffer

# --- Custom Model Mapping Helpers ---
MAX_HISTORY_TURNS = 10

def is_standalone_model(model_id: str) -> bool:
    return model_id not in ["serenity-supervisor", "serenity-supervisor-high", "serenity-supervisor-low"]

def map_custom_model_id(model_id: str) -> str:
    if model_id in ["serenity-supervisor", "serenity-supervisor-high", "serenity-supervisor-low"]:
        return SUPERVISOR_MODEL
    return model_id

async def run_orchestration(request: QueryRequest, http_request: Request):
    """
    Unified Orchestrator pipeline generator.
    Handles both standalone direct execution and full multi-agent supervisor routing.
    """
    global autoswap_timer_task, model_consolidation, CURRENT_MODEL, sessions_history, active_system_plan
    
    if autoswap_timer_task and not autoswap_timer_task.done():
        autoswap_timer_task.cancel()

    session_id = request.session_id
    if not session_id:
        client_host = http_request.client.host if http_request.client else "unknown"
        session_id = f"session_{client_host.replace('.', '_')}"

    is_programmatic = (session_id == 'native_lm_picker')

    if not is_programmatic:
        yield {"type": "progress", "text": "Initializing SerenityDev routing pipeline..."}
        await asyncio.sleep(0.01)

    async with inference_lock:
        if request.workspace_dir and os.path.exists(request.workspace_dir):
            try:
                os.chdir(request.workspace_dir)
                log_message(f"[Orchestrator] Changed working directory to: {request.workspace_dir}")
            except Exception as e:
                log_message(f"[Orchestrator Error] Failed to change directory: {e}")

        # --- Path A: Standalone Execution ---
        if is_standalone_model(request.model):
            mapped_model = map_custom_model_id(request.model)
            resolved_model = await resolve_model(mapped_model)
            log_message(f"[Orchestrator] Standalone execution requested for model: {request.model} ({resolved_model})")
            
            if not is_programmatic:
                yield {"type": "progress", "text": f"Pre-loading standalone model weights..."}
                await asyncio.sleep(0.01)

            # Session history
            session_context = ""
            if session_id not in sessions_history:
                sessions_history[session_id] = []
            history = sessions_history[session_id]
            if history:
                session_context = "\n--- CONVERSATION HISTORY ---\n" + "\n".join(
                    f"User: {turn['prompt']}\nSerenityDev Suggestion: {turn['answer']}"
                    for turn in history
                ) + "\n-----------------------------\n"

            standalone_prompt = f"""<bos><|turn>system
<|think|>
You are SerenityDev running in standalone mode for model {request.model}.
Provide a clean, direct, production-ready response in Markdown format. Avoid system markers.

PONYTAIL LAZINESS LADDER:
Before writing code, stop at the first rung that holds:
1. Does this need to exist? (YAGNI) -> skip it.
2. Already in codebase? -> reuse it, don't rewrite.
3. Stdlib does it? -> use it.
4. Native platform feature? -> use it.
5. Installed dependency? -> use it.
6. One line? -> one line.
7. Only then: minimum that works (without compromising safety or validation).
Never compromise on security, input validation, or error handling.
<turn|>
<|turn>user
<context>
{session_context}{request.context}
</context>

<user_request>
{request.prompt}
</user_request>
<turn|>
<|turn>model
<|channel>thought
"""
            if not is_programmatic:
                yield {"type": "progress", "text": f"Worker generating response..."}
                await asyncio.sleep(0.01)

            # Stream direct completion
            try:
                full_text_accumulated = []
                async for chunk in generate_completion_stream(resolved_model, standalone_prompt, temperature=0.2, num_ctx=CONTEXT_WINDOW):
                    full_text_accumulated.append(chunk)
                    yield {"type": "content", "content": chunk}
                
                clean_answer = clean_thought_and_whitespace("".join(full_text_accumulated))
                if session_id:
                    sessions_history[session_id].append({
                        "prompt": request.prompt,
                        "answer": clean_answer
                    })
                    if len(sessions_history[session_id]) > MAX_HISTORY_TURNS:
                        sessions_history[session_id].pop(0)

                yield {
                    "type": "done",
                    "routing": {
                        "supervisor": "N/A",
                        "worker": request.model,
                        "worker_model": resolved_model,
                        "reason": "Standalone model selected",
                        "review_badge": "N/A",
                        "steps": []
                    }
                }
            except Exception as e:
                yield {"type": "error", "detail": f"Standalone execution failed: {str(e)}"}
            return

        # --- Path B: Multi-Agent Supervisor Routing Pipeline ---
        # Parse slash commands if present
        mode = "agent"
        raw_prompt = request.prompt.strip()
        matched_cmd = None
        if raw_prompt.startswith(("/explore", "/exolore")):
            mode = "explore"
            matched_cmd = "/explore" if raw_prompt.startswith("/explore") else "/exolore"
        elif raw_prompt.startswith("/plan"):
            mode = "plan"
            matched_cmd = "/plan"
        elif raw_prompt.startswith("/execute"):
            mode = "execute"
            matched_cmd = "/execute"
        elif raw_prompt.startswith("/agent"):
            mode = "agent"
            matched_cmd = "/agent"

        if matched_cmd:
            cleaned_prompt = raw_prompt[len(matched_cmd):].strip()
            request.prompt = cleaned_prompt if cleaned_prompt else "List or summarize the codebase structures"
            log_message(f"[Orchestrator] Slash Command Detected: {matched_cmd}. Mode: {mode}. Cleaned Prompt: {request.prompt}")

        mode_instructions = ""
        if mode == "explore":
            mode_instructions = (
                "\nCRITICAL EXPLORE MODE CONSTRAINT:\n"
                "- You are running in read-only EXPLORE mode. You are restricted to read-only tools: "
                "mcp:filesystem:list_directory, mcp:filesystem:read_file, and mcp:filesystem:grep_search.\n"
                "- Do NOT modify any files. Do NOT use write_file, insert_edit_into_file, replace_string_in_file, multi_replace_string_in_file.\n"
                "- You must NOT make any changes. Explore the codebase to understand the query, then delegate to W1 to summarize the findings.\n"
            )
        elif mode == "plan":
            mode_instructions = (
                "\nCRITICAL PLAN MODE CONSTRAINT:\n"
                "- You are running in PLAN mode. Your sole objective is to formulate a plan to address the prompt.\n"
                "- You may read files to understand context, but you must NOT write, edit, or modify any files.\n"
                "- Once the plan is ready, delegate to W1 to present the details of the plan to the user.\n"
            )
        elif mode == "execute":
            mode_instructions = (
                "\nCRITICAL EXECUTE MODE:\n"
                "- You are running in EXECUTE mode. Your objective is to implement changes to the codebase.\n"
                "- You have full, direct access to compile/test via 'mcp:terminal:run_command', modify files using edit/write tools directly, or delegate code generation tasks to specialist workers as needed.\n"
            )
        elif mode == "agent":
            mode_instructions = (
                "\nCRITICAL AGENT MODE:\n"
                "- You are running in AGENT mode. Act as an autonomous coordinator.\n"
                "- You can execute any tools directly (compiling, file edits, etc.) or delegate tasks to specialized worker agents on an as-needed basis.\n"
            )

        if not is_programmatic:
            yield {"type": "progress", "text": "Resolving active model weights..."}
            await asyncio.sleep(0.01)

        session_context = ""
        if session_id not in sessions_history:
            sessions_history[session_id] = []
        
        history = sessions_history[session_id]
        if history:
            session_context = "\n--- CONVERSATION HISTORY ---\n" + "\n".join(
                f"User: {turn['prompt']}\nSerenityDev Suggestion: {turn['answer']}"
                for turn in history
            ) + "\n-----------------------------\n"
        
        request.context = f"{session_context}{request.context}"

        # Resolve models
        if model_consolidation:
            supervisor_res = await resolve_model(str(CURRENT_MODEL))
            w1_res = supervisor_res
            w2_res = supervisor_res
            w3_res = supervisor_res
            w4_res = supervisor_res
            log_message(f"[Orchestrator] Consolidated Models Enabled -> Using '{supervisor_res}' for all phases.")
        else:
            supervisor_res = await resolve_model(SUPERVISOR_MODEL)
            w1_res = await resolve_model(W1_MODEL)
            w2_res = await resolve_model(W2_MODEL)
            w3_res = await resolve_model(W3_MODEL)
            w4_res = await resolve_model(W4_MODEL)
            log_message(f"[Orchestrator] Resolved Models -> Supervisor/W1: {supervisor_res}, W2: {w2_res}, W3: {w3_res}, W4: {w4_res}")

        # Determine High / Low Mode
        is_low_mode = (request.model == "serenity-supervisor-low")
        max_tool_loops = 3 if is_low_mode else 6
        supervisor_temp = 0.1 if is_low_mode else 0.5
        mode_explanation_prompt = (
            "- You are running in Low-Resource Efficiency Mode. Output minimal explanations. Keep 'reason' and 'step_summary' fields short, concise, and direct. Focus on token savings and speed.\n"
            if is_low_mode else
            "- You are running in High-Capacity Reasoning Mode. Explain your thoughts fully. Detail your plan, rationale, and validation checks in 'reason' and 'step_summary' to ensure maximum accuracy.\n"
        )

        # Phase 1: Autonomous Multi-Turn Tool Loop
        tool_loop_count = 0
        agent_steps = []
        worker_id = "W1"
        instructions = ""
        reason = "Standard delegation flow"
        
        supervisor_context = request.context
        worker_context = ""
        worker_tool_responses = []

        while tool_loop_count < max_tool_loops:
            routing_prompt = f"""<bos><|turn>system
<|think|>
You are the SerenityDev Hierarchical Supervisor (Gemma-4 Core). You run a multi-agent coding pipeline.
Your immediate objective is to analyze the user's request, establish a multi-step plan, and pull the exact content of files required.

CRITICAL CONTEXT RULES:
- If the task requires changing or understanding code in existing files, you MUST first search for or read those files using 'read_file', 'grep_search', or 'list_directory' to enrich your context. Do NOT delegate to workers or execute code writes without first reading the target files.
- Minimize context bloat. Only read files directly related to the user request.
- Always create or update your plan before executing code writes.
- Enforce the Ponytail Laziness Ladder on all Worker agents (YAGNI, reuse codebase, stdlib, native platform, installed dependency, one line, minimum works) to keep code minimal.
{mode_instructions}
{mode_explanation_prompt}

AVAILABLE TOOLS:
- mcp:filesystem:create_or_update_plan
  Args: {{"steps": ["string"], "current_focus": "string"}}
  Description: Validates or adjusts the execution plan.
- mcp:filesystem:list_directory
  Args: {{}}
  Description: Lists all files in the current workspace root.
- mcp:filesystem:read_file
  Args: {{"path": "relative_path", "start_line": int, "end_line": int}}
  Description: Reads code from a file. If the file is large, start_line and end_line (1-indexed, inclusive) must be specified to read target regions and prevent context bloat.
- mcp:filesystem:write_file
  Args: {{"path": "relative_path", "content": "full_file_content"}}
  Description: Writes or overwrites code.
- mcp:filesystem:insert_edit_into_file
  Args: {{"path": "relative_path", "target_content": "code_to_find", "new_content": "code_to_insert"}}
  Description: Inserts new content in place of target content in a file.
- mcp:filesystem:replace_string_in_file
  Args: {{"path": "relative_path", "target_content": "code_to_find", "new_content": "replacement_code"}}
  Description: Replaces target content with new content in a file.
- mcp:filesystem:multi_replace_string_in_file
  Args: {{"path": "relative_path", "replacements": [{{"target": "exact_string_to_find", "replacement": "replacement_string"}}]}}
  Description: Replaces multiple specific non-adjacent or adjacent text blocks in a file by locating the exact 'target' string and replacing it with the 'replacement' string. Use this instead of write_file to avoid context limits.
- mcp:filesystem:grep_search
  Args: {{"query": "search_term"}}
  Description: Searches for text patterns across project files.
- mcp:terminal:run_command
  Args: {{"command": "string"}}
  Description: Runs a shell command in the workspace and returns stdout/stderr. Use this to run tests, linters, or build scripts.

DECISION RULE:
1. If you haven't formulated a plan or need to look up file locations, call 'create_or_update_plan' or 'list_directory'.
2. If the request involves modifying or checking files, you MUST call 'read_file' or 'grep_search' to read the files first.
3. Only delegate to workers or execute writes after you have gathered and inspected the required file contents.
4. You have full direct access to all execution tools. You can choose to:
   - Edit, write, or modify code directly using file editing tools (write_file, insert_edit_into_file, replace_string_in_file, multi_replace_string_in_file).
   - Compile, build, lint, or run tests directly using 'mcp:terminal:run_command'.
   - Delegate any part of the task to a specialist worker agent (W1, W2, W3, W4) with clear instructions.
5. Choose the path (direct tool usage vs worker delegation) that is most accurate, safe, and efficient for the current task.

Workers:
- W1 (Gemma 26B MOE): Complex architecture, multi-step debugging.
- W2 (CodeGemma 7b): Direct code synthesis and precise file edits.
- W3 (Qwen 35B): Fast explanations and shell scripts.
- W4 (Qwen 27B): Specialized coding routines.

You must respond with a JSON object matching this schema exactly:
{{
  "action": "call_tool" | "delegate_worker",
  "target": "mcp:filesystem:create_or_update_plan" | "mcp:filesystem:list_directory" | "mcp:filesystem:read_file" | "mcp:filesystem:write_file" | "mcp:filesystem:insert_edit_into_file" | "mcp:filesystem:replace_string_in_file" | "mcp:filesystem:multi_replace_string_in_file" | "mcp:filesystem:grep_search" | "mcp:terminal:run_command" | "W1" | "W2" | "W3" | "W4",
  "arguments_or_instructions": {{"key": "value"}} or "string_instructions",
  "step_summary": "A short summary of what was learned or done in this step, to be added to the timeline.",
  "reason": "Explain your tactical thinking."
}}
CRITICAL: Respond ONLY with raw JSON matching the schema exactly. Do NOT use <|tool_call> tags. No markdown wrappers.
<turn|>
<|turn>user
<context>
{supervisor_context}
</context>

<user_request>
{request.prompt}
</user_request>
<turn|>
<|turn>model
<|channel>thought
"""
            if not is_programmatic:
                yield {"type": "progress", "text": f"Supervisor routing phase (turn {tool_loop_count + 1})..."}
                await asyncio.sleep(0.01)

            decision = None
            retry_count = 0
            max_retries = 2
            current_routing_prompt = routing_prompt
            routing_raw = ""

            while retry_count <= max_retries:
                try:
                    res = await generate_completion(supervisor_res, current_routing_prompt, supervisor_temp + (0.05 * retry_count), CONTEXT_WINDOW, min_p=0.05, repeat_penalty=1.05)
                    routing_raw = res.get('response', '')
                    decision = extract_json(routing_raw)
                    if decision and "action" in decision and "target" in decision:
                        break
                    else:
                        raise ValueError("Parsed JSON is missing 'action' or 'target' keys, or is not valid JSON.")
                except Exception as e:
                    retry_count += 1
                    if retry_count > max_retries:
                        log_message(f"[Supervisor Self-Healing] Failed after {max_retries} retries. Falling back to W1. Error: {e}")
                        break
                    log_message(f"[Supervisor Self-Healing] Attempt {retry_count} failed: {e}. Retrying with error details...")
                    current_routing_prompt = routing_prompt + f"\n\n[SYSTEM ERROR: Your previous response was invalid and could not be parsed. Error: {str(e)}.\nYour previous raw response was:\n{routing_raw}\n\nPlease correct this and output ONLY a valid JSON object matching the schema exactly. Do not wrap in markdown or add text before/after.]\n"

            if not decision or "action" not in decision:
                decision = {
                    "action": "delegate_worker",
                    "target": "W1",
                    "arguments_or_instructions": "Provide direct code solutions.",
                    "reason": "Default fallback triggered due to routing or parsing failure after retries."
                }

            action = decision.get("action", "delegate_worker")
            target = decision.get("target", "W1")
            payload_data = decision.get("arguments_or_instructions", "")
            reason = decision.get("reason", "Standard execution flow")
            step_summary = decision.get("step_summary", reason)


            if action == "delegate_worker":
                worker_id = target if target in ["W1", "W2", "W3", "W4"] else "W1"
                instructions = extract_instructions(payload_data)
                if mode == "explore":
                    instructions = f"[EXPLORE MODE - READ ONLY] {instructions}"
                elif mode == "plan":
                    instructions = f"[PLAN MODE - DO NOT MODIFY FILES] {instructions}"
                break

            if not is_programmatic:
                yield {"type": "progress", "text": f"⚙️ Supervisor executing tool {target}..."}
                await asyncio.sleep(0.01)

            # Execute tool code
            tool_context = ""
            full_tool_context = None

            # Hardened constraint check for read-only modes
            if mode in ["explore", "plan"] and target in [
                "mcp:filesystem:write_file",
                "mcp:filesystem:insert_edit_into_file",
                "mcp:filesystem:replace_string_in_file",
                "mcp:filesystem:multi_replace_string_in_file"
            ]:
                tool_context = f"\n\n[System Tool Error: Action blocked. You are in {mode.upper()} mode, which is read-only. Modifying files is forbidden.]\n"
                log_message(f"[Constraint Blocked] Blocked write tool {target} in {mode} mode.")
                agent_steps.append({
                    "step": tool_loop_count + 1,
                    "tool": target,
                    "status": "error",
                    "details": "Blocked by read-only mode"
                })

            if target == "mcp:terminal:run_command":
                try:
                    args = safe_parse_tool_args(payload_data, "command")
                    command = args.get("command")
                    if command:
                        if not is_command_allowed(command):
                            tool_context = f"\n\n[System Tool Error: Command execution blocked by security policy: '{command}']\n"
                            log_message(f"[Constraint Blocked] Blocked command execution: {command}")
                            agent_steps.append({
                                "step": tool_loop_count + 1,
                                "tool": f"run_command: {command}",
                                "status": "error",
                                "details": "Blocked by security policy"
                            })
                        else:
                            # Wrap command in PowerShell to support standard aliases like 'ls'
                            full_cmd = f"powershell -NoProfile -Command \"{command}\""
                            result = subprocess.run(full_cmd, shell=True, capture_output=True, text=True, timeout=30)
                        stdout = result.stdout[:2000]
                        stderr = result.stderr[:2000]
                        tool_context = f"\n\n[System Tool Response: Command Exited with {result.returncode}]\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}\n"
                        log_message(f"[Tool Success] Executed command: {command}")
                        agent_steps.append({
                            "step": tool_loop_count + 1,
                            "tool": f"run_command: {command}",
                            "status": "success" if result.returncode == 0 else "error",
                            "details": f"Exit {result.returncode}"
                        })
                    else:
                        tool_context = f"\n\n[System Tool Error: No command provided]\n"
                except subprocess.TimeoutExpired:
                    tool_context = f"\n\n[System Tool Error: Command timed out after 30 seconds]\n"
                except Exception as e:
                    tool_context = f"\n\n[System Tool Error: Command failed: {e}]\n"

            elif target == "mcp:filesystem:create_or_update_plan":
                try:
                    args = safe_parse_tool_args(payload_data, "steps")
                    steps = args.get("steps", [])
                    focus = args.get("current_focus", "Unknown")
                    active_system_plan = {"focus": focus, "steps": steps}
                    tool_context = f"\n[System: Execution plan successfully synchronized. Current focus: {focus}. Proceed to next step.]\n"
                    log_message(f"[Tool Success] Plan state updated locally with {len(steps)} steps.")
                    agent_steps.append({
                        "step": tool_loop_count + 1,
                        "tool": "create_or_update_plan",
                        "status": "success",
                        "details": f"Plan focus: {focus}"
                    })
                except Exception as e:
                    tool_context = f"\n\n[System Tool Error: Failed to update plan: {str(e)}]\n"

            elif target == "mcp:filesystem:list_directory":
                try:
                    files = os.listdir(".")
                    file_list = []
                    for f in files:
                        if f.startswith(".") or f == "__pycache__":
                            continue
                        is_dir = os.path.isdir(f)
                        size = os.path.getsize(f) if not is_dir else 0
                        file_list.append(f"{f} ({'Dir' if is_dir else f'{size} bytes'})")
                    tool_context = f"\n\n[System Tool Response: Workspace Files]\n" + "\n".join(file_list) + "\n"
                    log_message(f"[Tool Success] Listed directory. Found {len(file_list)} entries.")
                    agent_steps.append({
                        "step": tool_loop_count + 1,
                        "tool": "list_directory",
                        "status": "success",
                        "details": step_summary
                    })
                except Exception as e:
                    tool_context = f"\n\n[System Tool Error: Failed to list directory: {str(e)}]\n"
                    log_message(f"[Tool Error] List directory failed: {e}")
                    agent_steps.append({
                        "step": tool_loop_count + 1,
                        "tool": "list_directory",
                        "status": "error",
                        "details": str(e)
                    })

            elif target == "mcp:filesystem:read_file":
                try:
                    args = safe_parse_tool_args(payload_data, "path")
                    file_path = args.get("path") if isinstance(args, dict) else None
                    start_line = args.get("start_line") if isinstance(args, dict) else None
                    end_line = args.get("end_line") if isinstance(args, dict) else None
                    if file_path and os.path.exists(file_path):
                        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                             file_contents = f.read()
                        
                        file_lines = file_contents.splitlines()
                        total_lines = len(file_lines)
                        
                        if start_line is not None and end_line is not None:
                            try:
                                s = max(1, int(start_line))
                                e = min(total_lines, int(end_line))
                                sliced_lines = [f"{idx}: {line}" for idx, line in enumerate(file_lines[s-1:e], start=s)]
                                response_content = "\n".join(sliced_lines)
                                full_tool_context = f"\n\n[System Tool Response: Contents of file '{file_path}' (Lines {s}-{e} of {total_lines})]\n{response_content}\n"
                            except Exception as ex:
                                raise ValueError(f"Invalid line range parameters: {ex}")
                        else:
                            # Auto-truncation threshold for files without explicit ranges
                            if len(file_lines) > 150:
                                sliced_lines = [f"{idx}: {line}" for idx, line in enumerate(file_lines[:100], start=1)]
                                response_content = "\n".join(sliced_lines)
                                warn_msg = f"\n... [File too large ({len(file_lines)} lines). Auto-truncated to first 100 lines to prevent bloat. Use 'read_file' with 'start_line' and 'end_line' or use 'grep_search' to locate target code.] ...\n"
                                full_tool_context = f"\n\n[System Tool Response: Contents of file '{file_path}' (First 100 lines of {total_lines})]\n{response_content}{warn_msg}"
                            else:
                                sliced_lines = [f"{idx}: {line}" for idx, line in enumerate(file_lines, start=1)]
                                response_content = "\n".join(sliced_lines)
                                full_tool_context = f"\n\n[System Tool Response: Contents of file '{file_path}']\n{response_content}\n"
                        
                        tool_context = full_tool_context
                        log_message(f"[Tool Success] Read {len(file_contents)} characters from: {file_path}")
                        agent_steps.append({
                            "step": tool_loop_count + 1,
                            "tool": f"read_file: {file_path}",
                            "status": "success",
                            "details": step_summary
                        })
                    else:
                        tool_context = f"\n\n[System Tool Error: File '{file_path}' was not found in the workspace.]\n"
                        log_message(f"[Tool Error] File not found: {file_path}")
                        agent_steps.append({
                            "step": tool_loop_count + 1,
                            "tool": f"read_file: {file_path}",
                            "status": "error",
                            "details": "File not found"
                        })
                except Exception as e:
                    tool_context = f"\n\n[System Tool Error: Failed to read file: {str(e)}]\n"
                    log_message(f"[Tool Error] Read file failed: {e}")
                    agent_steps.append({
                        "step": tool_loop_count + 1,
                        "tool": "read_file",
                        "status": "error",
                        "details": str(e)
                    })

            elif target == "mcp:filesystem:insert_edit_into_file":
                try:
                    args = safe_parse_tool_args(payload_data, "path")
                    file_path = args.get("path") if isinstance(args, dict) else None
                    target_content = args.get("target_content") if isinstance(args, dict) else ""
                    new_content = args.get("new_content") if isinstance(args, dict) else ""
                    if file_path and os.path.exists(file_path):
                        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                            content = f.read()
                        if target_content in content:
                            modified = content.replace(target_content, target_content + "\n" + new_content, 1)
                            with open(file_path, "w", encoding="utf-8") as f:
                                f.write(modified)
                            tool_context = f"\n\n[System Tool Response: Successfully inserted content in '{file_path}']\n"
                            log_message(f"[Tool Success] Inserted content in: {file_path}")
                            agent_steps.append({
                                "step": tool_loop_count + 1,
                                "tool": f"insert_edit_into_file: {file_path}",
                                "status": "success",
                                "details": step_summary
                            })
                        else:
                            tool_context = f"\n\n[System Tool Error: target_content not found in '{file_path}']\n"
                            log_message(f"[Tool Warning] insert_edit_into_file target_content not found in {file_path}")
                            agent_steps.append({
                                "step": tool_loop_count + 1,
                                "tool": f"insert_edit_into_file: {file_path}",
                                "status": "warning",
                                "details": "Target content not found"
                            })
                    else:
                        tool_context = f"\n\n[System Tool Error: File '{file_path}' not found]\n"
                        agent_steps.append({
                            "step": tool_loop_count + 1,
                            "tool": f"insert_edit_into_file: {file_path}",
                            "status": "error",
                            "details": "File not found"
                        })
                except Exception as e:
                    tool_context = f"\n\n[System Tool Error: Failed to insert content: {str(e)}]\n"
                    log_message(f"[Tool Error] Insert content failed: {e}")
                    agent_steps.append({
                        "step": tool_loop_count + 1,
                        "tool": "insert_edit_into_file",
                        "status": "error",
                        "details": str(e)
                    })

            elif target == "mcp:filesystem:replace_string_in_file":
                try:
                    args = safe_parse_tool_args(payload_data, "path")
                    file_path = args.get("path") if isinstance(args, dict) else None
                    target_content = args.get("target_content") if isinstance(args, dict) else ""
                    new_content = args.get("new_content") if isinstance(args, dict) else ""
                    if file_path and os.path.exists(file_path):
                        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                            content = f.read()
                        if target_content in content:
                            modified = content.replace(target_content, new_content, 1)
                            with open(file_path, "w", encoding="utf-8") as f:
                                f.write(modified)
                            tool_context = f"\n\n[System Tool Response: Successfully replaced content in '{file_path}']\n"
                            log_message(f"[Tool Success] Replaced content in: {file_path}")
                            agent_steps.append({
                                "step": tool_loop_count + 1,
                                "tool": f"replace_string_in_file: {file_path}",
                                "status": "success",
                                "details": step_summary
                            })
                        else:
                            tool_context = f"\n\n[System Tool Error: target_content not found in '{file_path}']\n"
                            log_message(f"[Tool Warning] replace_string_in_file target_content not found in {file_path}")
                            agent_steps.append({
                                "step": tool_loop_count + 1,
                                "tool": f"replace_string_in_file: {file_path}",
                                "status": "warning",
                                "details": "Target content not found"
                            })
                    else:
                        tool_context = f"\n\n[System Tool Error: File '{file_path}' not found]\n"
                        agent_steps.append({
                            "step": tool_loop_count + 1,
                            "tool": f"replace_string_in_file: {file_path}",
                            "status": "error",
                            "details": "File not found"
                        })
                except Exception as e:
                    tool_context = f"\n\n[System Tool Error: Failed to replace content: {str(e)}]\n"
                    log_message(f"[Tool Error] Replace content failed: {e}")
                    agent_steps.append({
                        "step": tool_loop_count + 1,
                        "tool": "replace_string_in_file",
                        "status": "error",
                        "details": str(e)
                    })

            elif target == "mcp:filesystem:write_file":
                try:
                    args = safe_parse_tool_args(payload_data, "path")
                    file_path = args.get("path") if isinstance(args, dict) else None
                    content = args.get("content") if isinstance(args, dict) else ""
                    if not isinstance(content, str):
                        content = ""
                    if file_path:
                        with open(file_path, "w", encoding="utf-8") as f:
                            f.write(content)
                        tool_context = f"\n\n[System Tool Response: Successfully wrote {len(content)} characters to '{file_path}']\n"
                        log_message(f"[Tool Success] Wrote file: {file_path}")
                        agent_steps.append({
                            "step": tool_loop_count + 1,
                            "tool": f"write_file: {file_path}",
                            "status": "success",
                            "details": step_summary
                        })
                    else:
                        tool_context = f"\n\n[System Tool Error: File path was not provided for writing.]\n"
                        log_message(f"[Tool Error] Write file failed: path not provided")
                        agent_steps.append({
                            "step": tool_loop_count + 1,
                            "tool": "write_file",
                            "status": "error",
                            "details": "No path provided"
                        })
                except Exception as e:
                    tool_context = f"\n\n[System Tool Error: Failed to write file: {str(e)}]\n"
                    log_message(f"[Tool Error] Write file failed: {e}")
                    agent_steps.append({
                        "step": tool_loop_count + 1,
                        "tool": "write_file",
                        "status": "error",
                        "details": str(e)
                    })

            elif target == "mcp:filesystem:multi_replace_string_in_file":
                try:
                    args = safe_parse_tool_args(payload_data, "path")
                    file_path = args.get("path") if isinstance(args, dict) else None
                    replacements = args.get("replacements", []) if isinstance(args, dict) else []
                    
                    if not file_path:
                        tool_context = f"\n\n[System Tool Error: File path was not provided for replacement.]\n"
                    elif not os.path.exists(file_path):
                        tool_context = f"\n\n[System Tool Error: File '{file_path}' was not found in the workspace.]\n"
                    elif not replacements or not isinstance(replacements, list):
                        tool_context = f"\n\n[System Tool Error: Replacements list was empty or invalid format.]\n"
                    else:
                        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                            content = f.read()
                        
                        modified_content = content
                        applied_count = 0
                        not_found = []
                        
                        for idx, r in enumerate(replacements):
                            if not isinstance(r, dict):
                                continue
                            target_str = r.get("target")
                            replacement_str = r.get("replacement")
                            if target_str is None or replacement_str is None:
                                continue
                            
                            occurrences = modified_content.count(target_str)
                            if occurrences == 0:
                                not_found.append(f"Replacement {idx+1}: Target string not found (target: {repr(target_str[:50])})")
                            else:
                                modified_content = modified_content.replace(target_str, replacement_str)
                                applied_count += occurrences
                        
                        if not_found and applied_count == 0:
                            tool_context = f"\n\n[System Tool Error: None of the target strings were found in '{file_path}'. No changes were made. Details:\n" + "\n".join(not_found) + "]\n"
                            log_message(f"[Tool Warning] Multi replace failed on {file_path}. Target strings not found.")
                            agent_steps.append({
                                "step": tool_loop_count + 1,
                                "tool": f"multi_replace_string_in_file: {file_path}",
                                "status": "warning",
                                "details": "No targets found"
                            })
                        else:
                            with open(file_path, "w", encoding="utf-8") as f:
                                f.write(modified_content)
                            
                            status_msg = f"Successfully applied {applied_count} replacements to '{file_path}'."
                            if not_found:
                                status_msg += f" Note: some replacements were not found:\n" + "\n".join(not_found)
                            
                            tool_context = f"\n\n[System Tool Response: {status_msg}]\n"
                            log_message(f"[Tool Success] Multi replaced {applied_count} occurrences in: {file_path}")
                            agent_steps.append({
                                "step": tool_loop_count + 1,
                                "tool": f"multi_replace_string_in_file: {file_path}",
                                "status": "success",
                                "details": step_summary
                            })
                except Exception as e:
                    tool_context = f"\n\n[System Tool Error: Failed to perform multi-replace: {str(e)}]\n"
                    log_message(f"[Tool Error] Multi-replace failed: {e}")
                    agent_steps.append({
                        "step": tool_loop_count + 1,
                        "tool": "multi_replace_string_in_file",
                        "status": "error",
                        "details": str(e)
                    })

            elif target == "mcp:filesystem:grep_search":
                try:
                    args = safe_parse_tool_args(payload_data, "query")
                    query = args.get("query") if isinstance(args, dict) else ""
                    matches = []
                    ignore_dirs = {".git", "node_modules", "out", "dist", "build", ".vscode", "__pycache__"}
                    if query:
                        for root, dirs, files in os.walk("."):
                            # Filter dirs in-place to optimize traversal speed and prevent descending into big folders
                            dirs[:] = [d for d in dirs if d not in ignore_dirs and not d.startswith(".")]
                            for file in files:
                                if file.endswith((".py", ".ts", ".js", ".kt", ".txt", ".json", ".md", ".java", ".cpp", ".h")):
                                    path = os.path.join(root, file)
                                    try:
                                        with open(path, "r", encoding="utf-8", errors="ignore") as f:
                                            for line_num, line in enumerate(f, 1):
                                                if query.lower() in line.lower():
                                                    matches.append(f"{path}:{line_num}: {line.strip()}")
                                    except Exception:
                                        pass
                    if matches:
                        tool_context = f"\n\n[System Tool Response: Found {len(matches)} matches for grep query '{query}']\n" + "\n".join(matches[:20]) + "\n"
                        log_message(f"[Tool Success] Grep found {len(matches)} matches for '{query}'.")
                        agent_steps.append({
                            "step": tool_loop_count + 1,
                            "tool": f"grep_search: '{query}'",
                            "status": "success",
                            "details": step_summary
                        })
                    else:
                        tool_context = f"\n\n[System Tool Response: No occurrences of '{query}' were found in the workspace.]\n"
                        log_message(f"[Tool Warning] Grep found 0 matches for '{query}'.")
                        agent_steps.append({
                            "step": tool_loop_count + 1,
                            "tool": f"grep_search: '{query}'",
                            "status": "warning",
                            "details": step_summary
                        })
                except Exception as e:
                    tool_context = f"\n\n[System Tool Error: Failed to perform grep search: {str(e)}]\n"
                    log_message(f"[Tool Error] Grep search failed: {e}")
                    agent_steps.append({
                        "step": tool_loop_count + 1,
                        "tool": "grep_search",
                        "status": "error",
                        "details": str(e)
                    })

            if full_tool_context is None:
                full_tool_context = tool_context
            
            supervisor_context = f"{supervisor_context}{tool_context}"
            if len(supervisor_context) > 50000:
                supervisor_context = "...\n[Earlier tool responses truncated for context window limits]\n..." + supervisor_context[-20000:]
            
            worker_tool_responses.append(full_tool_context)
            total_worker_len = sum(len(r) for r in worker_tool_responses)
            if total_worker_len > 30000:
                rebuilt_responses = []
                for idx, r in enumerate(worker_tool_responses):
                    # Keep the last 2 tool responses in full, truncate older ones to just headers/first line
                    if idx >= len(worker_tool_responses) - 2:
                        rebuilt_responses.append(r)
                    else:
                        first_line = r.splitlines()[0] if r.strip() else "[Empty Tool Response]"
                        rebuilt_responses.append(f"\n\n{first_line}\n... [Early tool response contents truncated to preserve worker context window] ...\n")
                worker_context = "".join(rebuilt_responses)
            else:
                worker_context = "".join(worker_tool_responses)
            tool_loop_count += 1

        if tool_loop_count >= max_tool_loops:
            worker_id = "W1"
            instructions = "Fulfill this request using fully enriched context."
            log_message("[Supervisor Warning] Reached max loop depth. Routing to W1.")

        # --- Phase 2: Worker Synthesis ---
        if not is_programmatic:
            yield {"type": "progress", "text": f"Worker {worker_id} is generating response..."}
            await asyncio.sleep(0.01)

        worker_model = w1_res if worker_id == "W1" else (w2_res if worker_id == "W2" else (w3_res if worker_id == "W3" else w4_res))
        
        if (llama_server_process is not None and llama_server_process.poll() is None) and worker_model != supervisor_res and not (llama_cpp_available and resolve_gguf_path(worker_model)):
            log_message(f"[Orchestrator] VRAM Swap: Unloading Supervisor '{supervisor_res}' to free VRAM for '{worker_model}'...")
            try:
                async with httpx.AsyncClient(timeout=5) as client:
                    await client.post(LLAMA_SERVER_URL, json={"model": supervisor_res, "keep_alive": 0})
            except Exception:
                pass

        worker_mode_constraint = ""
        if mode == "explore":
            worker_mode_constraint = "\nCRITICAL: You are running in read-only EXPLORE mode. Do NOT generate file edits, writes, or modifications. Only analyze and explain structures."
        elif mode == "plan":
            worker_mode_constraint = "\nCRITICAL: You are running in PLAN mode. Do NOT generate file edits or modifications. Only output a structured, complete plan."

        worker_prompt = f"""<bos><|turn>system
<|think|>
You are SerenityDev {worker_id}, a specialized software engineering agent.{worker_mode_constraint}
Instructions: {instructions}
Provide a clean, direct, production-ready response in Markdown format. Avoid system markers.

PONYTAIL LAZINESS LADDER:
Before writing code, stop at the first rung that holds:
1. Does this need to exist? (YAGNI) -> skip it.
2. Already in codebase? -> reuse it, don't rewrite.
3. Stdlib does it? -> use it.
4. Native platform feature? -> use it.
5. Installed dependency? -> use it.
6. One line? -> one line.
7. Only then: minimum that works (without compromising safety or validation).
Never compromise on security, input validation, or error handling.
<turn|>
<|turn>user
<context>
{request.context}
{worker_context}
</context>

<user_request>
{request.prompt}
</user_request>
<turn|>
<|turn>model
<|channel>thought
"""

        log_message(f"[Orchestrator] Running Worker {worker_id} ({worker_model.split(':')[0]})...")
        if not is_programmatic:
            timeline_md = ""
            if agent_steps:
                timeline_md = "> 🛠️ **Agentic Tools Executed:**\n"
                for step in agent_steps:
                    icon = "🟢" if step["status"] == "success" else "🟡"
                    timeline_md += f"> {step['step']}. {icon} `{step['tool']}` ➡️ *{step['details']}*\n"
                timeline_md += ">\n"

            prelim_header = f"""> ### 🤖 SERENITY DEV ORCHESTRATION REPORT\n> 🗺️ **Routing:** `Supervisor ({supervisor_res.split(':')[0]})` ➡️ `Worker: {worker_id} ({worker_model.split(':')[0]})`\n> ⚙️ **Mode:** `{mode.upper()}`\n> 🎯 **Reason:** *"{reason}"*\n{timeline_md}> 🔍 **Review:** `⏳ In Progress (Awaiting Worker Draft & Supervisor Review)`\n\n---\n\n"""
            yield {"type": "content", "content": prelim_header}
            await asyncio.sleep(0.01)

        try:
            worker_draft_parts = []
            thought_filter = StreamingThoughtFilter()
            async for chunk in generate_completion_stream(worker_model, worker_prompt, 0.3, CONTEXT_WINDOW, min_p=0.05, repeat_penalty=1.05):
                worker_draft_parts.append(chunk)
                filtered = thought_filter.feed(chunk)
                if filtered:
                    yield {"type": "content", "content": filtered}
            
            remaining = thought_filter.flush_remaining()
            if remaining:
                yield {"type": "content", "content": remaining}
                
            worker_draft_raw = "".join(worker_draft_parts)
            final_answer = clean_thought_and_whitespace(worker_draft_raw)
        except Exception as e:
            yield {"type": "error", "detail": f"Worker failure during synthesis: {str(e)}"}
            return

        # --- Phase 3: Supervisor Review ---
        if not is_programmatic:
            yield {"type": "progress", "text": "Supervisor is reviewing draft quality..."}
            await asyncio.sleep(0.01)

        if (llama_server_process is not None and llama_server_process.poll() is None) and worker_model != supervisor_res and not (llama_cpp_available and resolve_gguf_path(worker_model)):
            log_message(f"[Orchestrator] VRAM Swap: Unloading Worker '{worker_model}' to free VRAM for Supervisor Review...")
            try:
                async with httpx.AsyncClient(timeout=5) as client:
                    await client.post(LLAMA_SERVER_URL, json={"model": worker_model, "keep_alive": 0})
            except Exception:
                pass

        log_message(f"[Orchestrator] Requesting Supervisor review of draft answer...")
        review_prompt = f"""<bos><|turn>system
<|think|>
You are the SerenityDev Hierarchical Supervisor. Your task is to review the draft answer generated by the worker and determine if it fully and accurately addresses the user's request.

You must respond with a JSON object matching this schema exactly:
{{
  "approved": true | false,
  "feedback": "Detail feedback or corrections if rejected, otherwise empty string."
}}
Respond ONLY with the raw JSON object. Do not wrap it in markdown code blocks or other text.
<turn|>
<|turn>user
<user_request>
{request.prompt}
</user_request>

<draft_answer>
{final_answer}
</draft_answer>
<turn|>
<|turn>model
<|channel>thought
"""

        approved = True
        feedback = ""
        try:
            res = await generate_completion(supervisor_res, review_prompt, 0.2, CONTEXT_WINDOW, min_p=0.05, repeat_penalty=1.05)
            review_raw = res.get('response', '')
            review_decision = extract_json(review_raw)
            if review_decision:
                approved = review_decision.get("approved", True)
                feedback = review_decision.get("feedback", "")
        except Exception as e:
            log_message(f"[Supervisor Error] Review phase failed: {e}")

        # --- Phase 4: Re-routing (On rejection) ---
        if not approved:
            if not is_programmatic:
                w1_name = w1_res.split(':')[0]
                yield {"type": "progress", "text": f"Supervisor rejected draft. Refinement model rewriting ({w1_name})..."}
                await asyncio.sleep(0.01)
                
                rejection_msg = f"\n\n---\n> 🔍 **Review:** `❌ Rejected -> Re-routed (Reason: {feedback[:100]}...)`\n> 🔄 **Refinement Model Rewriting ({w1_name})...**\n\n"
                yield {"type": "content", "content": rejection_msg}
                await asyncio.sleep(0.01)

            if (llama_server_process is not None and llama_server_process.poll() is None) and w1_res != supervisor_res and not (llama_cpp_available and resolve_gguf_path(w1_res)):
                log_message(f"[Orchestrator] VRAM Swap: Unloading Supervisor '{supervisor_res}' for W1 Refinement...")
                try:
                    async with httpx.AsyncClient(timeout=5) as client:
                        await client.post(LLAMA_SERVER_URL, json={"model": supervisor_res, "keep_alive": 0})
                except Exception:
                    pass

            log_message(f"[Supervisor Rejection] Draft failed quality check. Feedback: {feedback}")
            log_message(f"[Orchestrator] Re-routing task to high-capacity reasoning model {w1_res}...")
            
            refine_mode_constraint = ""
            if mode == "explore":
                refine_mode_constraint = "\nCRITICAL: You are running in read-only EXPLORE mode. Do NOT generate file edits, writes, or modifications. Only analyze and explain."
            elif mode == "plan":
                refine_mode_constraint = "\nCRITICAL: You are running in PLAN mode. Do NOT generate file edits or modifications. Only output a structured, complete plan."

            refine_prompt = f"""<bos><|turn>system
<|think|>
You are a SerenityDev Worker. You have been assigned to revise and correct a draft answer based on feedback from the Hierarchical Supervisor.{refine_mode_constraint}
Please rewrite the answer, ensuring all feedback is fully addressed, code is perfectly correct, and formatting is clean. Provide a direct, professional markdown solution.

PONYTAIL LAZINESS LADDER:
Before writing code, stop at the first rung that holds:
1. Does this need to exist? (YAGNI) -> skip it.
2. Already in codebase? -> reuse it, don't rewrite.
3. Stdlib does it? -> use it.
4. Native platform feature? -> use it.
5. Installed dependency? -> use it.
6. One line? -> one line.
7. Only then: minimum that works (without compromising safety or validation).
Never compromise on security, input validation, or error handling.
<turn|>
<|turn>user
User Request: {request.prompt}
Context: {request.context}
Supervisor Feedback: {feedback}
Previous Draft:
{final_answer}
<turn|>
<|turn>model
<|channel>thought
"""

            try:
                refined_parts = []
                thought_filter = StreamingThoughtFilter()
                async for chunk in generate_completion_stream(w1_res, refine_prompt, 0.3, CONTEXT_WINDOW, min_p=0.05, repeat_penalty=1.05):
                    refined_parts.append(chunk)
                    filtered = thought_filter.feed(chunk)
                    if filtered:
                        yield {"type": "content", "content": filtered}
                
                remaining = thought_filter.flush_remaining()
                if remaining:
                    yield {"type": "content", "content": remaining}
                    
                refined_raw = "".join(refined_parts)
                final_answer = clean_thought_and_whitespace(refined_raw)
                log_message(f"[Orchestrator] Refined solution successfully synthesized.")
            except Exception as e:
                log_message(f"[Orchestrator Error] Refinement loop failed, falling back to worker draft: {e}")
        else:
            if not is_programmatic:
                yield {"type": "content", "content": "\n\n---\n> 🔍 **Review:** `✅ Approved by Supervisor`"}

        # Assemble the report
        clean_answer = clean_thought_and_whitespace(final_answer)

        # Warm-load/preload supervisor back in background
        async def run_preload():
            try:
                if llama_cpp_available and resolve_gguf_path(supervisor_res):
                    loop = asyncio.get_event_loop()
                    await loop.run_in_executor(None, get_llama_model, supervisor_res, CONTEXT_WINDOW)
                else:
                    if llama_server_process is None or active_llama_server_model_name != supervisor_res:
                        await start_llama_server(supervisor_res, CONTEXT_WINDOW)
                    async with httpx.AsyncClient(timeout=20) as client:
                        await client.post(LLAMA_SERVER_URL, json={"model": supervisor_res, "prompt": "", "stream": False, "keep_alive": -1})
            except Exception:
                pass
        BackgroundTasks().add_task(run_preload)

        if session_id:
            sessions_history[session_id].append({
                "prompt": request.prompt,
                "answer": clean_answer
            })
            if len(sessions_history[session_id]) > MAX_HISTORY_TURNS:
                sessions_history[session_id].pop(0)

        review_badge = "✅ Approved by Supervisor" if approved else f"❌ Rejected -> Re-routed (Reason: {feedback[:60]}...)"

        yield {
            "type": "done",
            "routing": {
                "supervisor": supervisor_res,
                "worker": worker_id,
                "worker_model": worker_model,
                "reason": reason,
                "review_badge": review_badge if not is_programmatic else "OK",
                "steps": agent_steps
            }
        }

@app.post("/ask_stream")
async def ask_serenity_stream(request: QueryRequest, http_request: Request):
    """
    Hierarchical Supervisor Endpoint with Autonomous Multi-turn Tool-calling loop, returning a stream of chunks.
    """
    global server_paused
    if server_paused:
        raise HTTPException(status_code=503, detail="SerenityDev Server is currently paused. Please resume from the editor status bar or control panel.")

    async def event_generator():
        try:
            async for event in run_orchestration(request, http_request):
                if isinstance(event, dict) and event.get("type") == "error":
                    safe_event = dict(event)
                    safe_event["detail"] = "An internal error occurred."
                    yield f"data: {json.dumps(safe_event)}\n\n"
                else:
                    yield f"data: {json.dumps(event)}\n\n"
        except Exception as e:
            log_message(f"[Stream Error] ask_serenity_stream failed: {e}")
            yield f"data: {json.dumps({'type': 'error', 'detail': 'An internal error occurred.'})}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")

@app.post("/ask")
async def ask_serenity(request: QueryRequest, http_request: Request):
    """
    Hierarchical Supervisor Endpoint with Autonomous Multi-turn Tool-calling loop.
    """
    global server_paused
    if server_paused:
        raise HTTPException(status_code=503, detail="SerenityDev Server is currently paused. Please resume from the editor status bar or control panel.")

    answer_parts = []
    routing_info = {}
    error_detail = None

    try:
        async for event in run_orchestration(request, http_request):
            if event.get("type") == "content":
                answer_parts.append(event.get("content", ""))
            elif event.get("type") == "done":
                routing_info = event.get("routing", {})
            elif event.get("type") == "error":
                error_detail = event.get("detail")
    except Exception as e:
        error_detail = str(e)

    if error_detail:
        raise HTTPException(status_code=500, detail=error_detail)

    full_answer = "".join(answer_parts)

    # Max response length truncation logic for Copilot Chat compatibility
    if len(full_answer) > MAX_RESPONSE_LENGTH:
        # If too long, try stripping the report header and keeping just the answer
        # Locate the divider block
        divider = "\n\n---\n\n"
        divider_idx = full_answer.find(divider)
        clean_ans = full_answer[divider_idx + len(divider):] if divider_idx != -1 else full_answer
        
        if len(clean_ans) <= MAX_RESPONSE_LENGTH:
            full_answer = clean_ans
            log_message(f"[Orchestrator] Header stripped to fit Copilot Chat limit. Answer {len(clean_ans)} chars.")
        else:
            truncated_answer = clean_ans[:MAX_RESPONSE_LENGTH]
            last_newline = truncated_answer.rfind('\n')
            if last_newline > MAX_RESPONSE_LENGTH * 0.75:
                truncated_answer = truncated_answer[:last_newline]
            full_answer = truncated_answer + "\n\n*[Truncated for Copilot Chat. Full response in server logs.]*"
            log_message(f"[Orchestrator] Response truncated to {len(full_answer)} characters for Copilot Chat compatibility.")

    return {
        "answer": full_answer,
        "routing": routing_info
    }

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

        resolved_fim_model = await resolve_model(request.model) 
        fim_prompt = f"<pre>{request.prefix}<suf>{request.suffix}<mid>"

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

# --- Config Management ---

class ConfigUpdate(BaseModel):
    model_consolidation: Optional[bool] = None
    current_model: Optional[str] = None
    cache_type_k: Optional[str] = None
    cache_type_v: Optional[str] = None

@app.get("/api/config")
async def get_config():
    return {
        "model_consolidation": model_consolidation,
        "current_model": CURRENT_MODEL,
        "supervisor_model": SUPERVISOR_MODEL,
        "w1_model": W1_MODEL,
        "w2_model": W2_MODEL,
        "w3_model": W3_MODEL,
        "fim_model": FIM_MODEL,
        "cache_type_k": cache_type_k,
        "cache_type_v": cache_type_v
    }

@app.post("/api/config")
async def update_config(config: ConfigUpdate, background_tasks: BackgroundTasks):
    global model_consolidation, CURRENT_MODEL, cache_type_k, cache_type_v
    if config.model_consolidation is not None:
        model_consolidation = config.model_consolidation
        log_message(f"[Config] Model consolidation set to: {model_consolidation}")
    if config.cache_type_k is not None:
        cache_type_k = config.cache_type_k
        log_message(f"[Config] Key cache type (K) set to: {cache_type_k}")
    if config.cache_type_v is not None:
        cache_type_v = config.cache_type_v
        log_message(f"[Config] Value cache type (V) set to: {cache_type_v}")
        
    if config.current_model is not None or config.cache_type_k is not None or config.cache_type_v is not None:
        resolved = await resolve_model(config.current_model if config.current_model is not None else CURRENT_MODEL)
        if config.current_model is not None:
            CURRENT_MODEL = resolved
            log_message(f"[Config] Current consolidated model set to: {CURRENT_MODEL}")
        
        # Warm-load/preload the consolidated model in background to apply cache settings
        async def run_preload():
            async with inference_lock:
                log_message(f"[Config] Warm-loading selected consolidated model '{resolved}' with cache K={cache_type_k}, V={cache_type_v}...")
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
                    log_message(f"[Config] Successfully warm-loaded '{resolved}'.")
                except Exception as e:
                    log_message(f"[Config] Dynamic preload failed for '{resolved}': {e}")
        background_tasks.add_task(run_preload)
        
    return {
        "status": "success",
        "model_consolidation": model_consolidation,
        "current_model": CURRENT_MODEL,
        "cache_type_k": cache_type_k,
        "cache_type_v": cache_type_v
    }

# --- Web UI & Dashboard Endpoints ---

@app.get("/api/status")
async def get_status():
    """Returns real-time status of the orchestrator, installed models, and GPU memory metrics."""
    installed = get_installed_models()
    loaded_vram = []
    if llama_server_process is not None and llama_server_process.poll() is None:
        try:
            # Assuming llama-server provides /v1/models
            async with httpx.AsyncClient(timeout=1.5) as client:
                res = await client.get(f"{LLAMA_SERVER_BASE}/v1/models")
                if res.status_code == 200:
                    loaded_vram = [{"name": m["id"]} for m in res.json().get("data", [])]
        except Exception:
            pass

    # Windows GPU memory parser
    gpu_memory = None
    try:
        res = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total", "--format=csv,nounits,noheader"],
            capture_output=True, text=True, timeout=5
        )
        if res.returncode == 0 and res.stdout:
            parts = res.stdout.strip().split(",")
            if len(parts) == 2:
                gpu_memory = {
                    "used": float(parts[0].strip()) / 1024.0, # convert MiB to GiB
                    "total": float(parts[1].strip()) / 1024.0,
                    "unit": "GiB"
                }
    except Exception:
        pass

    targets = [
        {"name": SUPERVISOR_MODEL, "gguf": "gemma-4-26B-A4B-it-UD-Q4_K_XL.gguf", "dir": None},
        {"name": W3_MODEL, "gguf": "Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf", "dir": None},
        {"name": W2_MODEL, "gguf": "codegemma-7b-it-f16.gguf", "dir": None},
        {"name": FIM_MODEL, "gguf": "codegemma-2b-f16.gguf", "dir": None}
    ]

    registry_status = []
    for t in targets:
        name = t["name"]
        registered = any(m.startswith(name) or name.startswith(m) for m in installed)
        
        source_present = False
        source_type = "Missing"
        if t["gguf"] and os.path.exists(t["gguf"]):
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
        "model_consolidation": model_consolidation,
        "current_model": CURRENT_MODEL,
        "cache_type_k": cache_type_k,
        "cache_type_v": cache_type_v
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
                        <p class="text-xs text-slate-400">Manage VRAM consolidation and pre-swapping controls dynamically</p>
                    </div>
                    
                    <div class="flex items-center gap-3">
                        <span class="text-xs text-slate-400 font-mono">Model Consolidation</span>
                        <label class="relative inline-flex items-center cursor-pointer">
                            <input type="checkbox" id="consolidationToggle" onchange="toggleConsolidation()" class="sr-only peer">
                            <div class="w-11 h-6 bg-slate-800 peer-focus:outline-none rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-slate-400 after:border-slate-300 after:border after:rounded-full after:h-5 after:w-5 after:transition-all peer-checked:bg-purple-600 peer-checked:after:bg-cyan-300"></div>
                        </label>
                    </div>
                </div>

                <div class="grid grid-cols-1 md:grid-cols-2 gap-4 pt-3 border-t border-white/5">
                    <div>
                        <label class="block text-xs font-mono text-slate-400 mb-1.5">Consolidated Active Model</label>
                        <select id="consolidatedModelSelect" onchange="updateConsolidatedModel()" class="w-full bg-slate-950/80 border border-white/10 rounded-lg px-3 py-2 text-xs font-mono text-slate-200 focus:outline-none focus:border-purple-500">
                            <option value="gemma-4-26B-A4B">Gemma-4 (26B-A4B) [Supervisor / W1]</option>
                            <option value="qwen3.6-35B-A3B">Qwen-3.6 (35B-A3B) [W3 Tier]</option>
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
                    if (data.loaded_vram && data.loaded_vram.length > 0) {
                        data.loaded_vram.forEach(m => {
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

                // 3. Consolidated UI Switch Sync (prevent infinite triggers)
                if (!isConsolidationUpdating) {
                    document.getElementById("consolidationToggle").checked = data.model_consolidation;
                    document.getElementById("consolidatedModelSelect").value = data.current_model;
                    document.getElementById("consolidatedModelSelect").disabled = !data.model_consolidation;
                    
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
            
            const isUser = (sender === "User");
            const avatar = isUser ? "👤" : "🤖";
            const senderName = isUser ? "You" : "SerenityDev";
            const nameColor = isUser ? "text-cyan-400" : "text-purple-300";

            let routingBox = "";
            if (routing) {
                const stepCount = routing.steps ? routing.steps.length : 0;
                let stepDetails = "";
                if (routing.steps && routing.steps.length > 0) {
                    stepDetails = `<div class="mt-2 pl-4 border-l border-white/5 space-y-1 text-[10px] font-mono text-slate-400">`;
                    routing.steps.forEach(s => {
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
                        <div>Target Worker: <span class="text-cyan-400 font-semibold">${routing.worker}</span> (${routing.worker_model.split(':')[0]})</div>
                        <div>Reasoning: <span class="italic">"${routing.reason}"</span></div>
                        ${stepDetails}
                    </div>
                `;
            }

            // HTML content with basic markdown rendering for premium markdown answers
            const formattedText = marked.parse ? marked.parse(text) : text.replace(/\n/g, "<br/>");

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

        // Toggle Consolidation Setting API
        async function toggleConsolidation() {
            isConsolidationUpdating = true;
            const toggleVal = document.getElementById("consolidationToggle").checked;
            const activeSel = document.getElementById("consolidatedModelSelect");
            activeSel.disabled = !toggleVal;
            
            try {
                await fetch("/api/config", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ model_consolidation: toggleVal })
                });
            } catch (err) {}
            isConsolidationUpdating = false;
        }

        // Update Consolidated model API
        async function updateConsolidatedModel() {
            isConsolidationUpdating = true;
            const selectVal = document.getElementById("consolidatedModelSelect").value;
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
    """Checks if the target port is occupied on Windows or Linux/macOS and terminates the occupying process to prevent WinError 10048."""
    print(f"[Port Initializer] Scanning port {port}...")
    if os.name == 'nt':  # Windows
        try:
            cmd = f"netstat -ano | findstr LISTENING | findstr :{port}"
            res = subprocess.run(cmd, shell=True, capture_output=True, text=True)
            if res.returncode == 0 and res.stdout:
                lines = res.stdout.strip().split('\n')
                for line in lines:
                    parts = line.strip().split()
                    if len(parts) >= 5:
                        pid = int(parts[-1])
                        if pid != os.getpid() and pid > 0:
                            print(f"[Port Initializer] Port {port} is occupied by PID {pid}. Terminating process...")
                            subprocess.run(f"taskkill /F /PID {pid}", shell=True, capture_output=True)
        except Exception as e:
            print(f"[Port Initializer] Failed to free port on Windows: {e}")
    else:  # Linux / macOS
        try:
            cmd = f"lsof -t -i:{port}"
            res = subprocess.run(cmd, shell=True, capture_output=True, text=True)
            if res.returncode == 0 and res.stdout:
                pids = res.stdout.strip().split('\n')
                for pid_str in pids:
                    pid = int(pid_str.strip())
                    if pid != os.getpid() and pid > 0:
                        print(f"[Port Initializer] Port {port} is occupied by PID {pid}. Terminating process...")
                        os.kill(pid, signal.SIGKILL)
        except Exception as e:
            print(f"[Port Initializer] Failed to free port on Unix: {e}")

class RestartRequest(BaseModel):
    model: Optional[str] = None
    model_consolidation: Optional[bool] = None

@app.post("/api/restart")
async def restart_server(background_tasks: BackgroundTasks, request: Optional[RestartRequest] = None):
    """Soft reset: clears logs and resets internal counters. Optionally changes consolidated model."""
    global orchestrator_logs, independenttask_count, CURRENT_MODEL, model_consolidation, sessions_history
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
    unload_llama_model()
    
    if request:
        if request.model is not None:
            resolved = await resolve_model(request.model)
            CURRENT_MODEL = resolved
            log_message(f"[Server] Active consolidated model changed to: {resolved}")
            if background_tasks:
                async def run_preload():
                    async with inference_lock:
                        log_message(f"[Server Preload] Warm-loading model '{resolved}'...")
                        try:
                            if llama_cpp_available and resolve_gguf_path(resolved):
                                loop = asyncio.get_event_loop()
                                await loop.run_in_executor(None, get_llama_model, resolved, CONTEXT_WINDOW)
                            else:
                                if llama_server_process is None or active_llama_server_model_name != resolved:
                                    await start_llama_server(resolved, CONTEXT_WINDOW)
                                payload = {"model": resolved, "prompt": "", "stream": False, "keep_alive": -1}
                                async with httpx.AsyncClient(timeout=25) as client: await client.post(LLAMA_SERVER_URL, json=payload)
                            log_message(f"[Server Preload] Successfully loaded '{resolved}' into memory.")
                        except Exception as e:
                            log_message(f"[Server Preload] Preload failed for '{resolved}': {e}")
                background_tasks.add_task(run_preload)
        if request.model_consolidation is not None:
            model_consolidation = request.model_consolidation
            log_message(f"[Server] Model consolidation set to: {model_consolidation}")

    log_message("[Server] State reset complete. Ready for new requests.")
    return {
        "status": "soft reset complete",
        "current_model": CURRENT_MODEL,
        "model_consolidation": model_consolidation
    }

@app.post("/api/clear_logs")
async def clear_logs():
    """Clears the orchestrator activity logs."""
    global orchestrator_logs
    log_message("[Server] Clearing activity logs...")
    log_count = len(orchestrator_logs)
    orchestrator_logs = []
    return {"status": f"logs cleared", "cleared_count": log_count}

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
    free_port(8002)
    print("[Server] Starting SerenityDev Orchestrator...")
    uvicorn.run("serenitydevserver:app", host="0.0.0.0", port=8002, reload=False)