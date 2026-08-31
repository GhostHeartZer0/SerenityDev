# Change Log
All notable changes to the "SerenityDev" extension will be documented in this file.

- **Direct Model GPU Offload & GGML Split Inputs Fix**: Resolved `GGML_ASSERT(n_inputs < GGML_SCHED_MAX_SPLIT_INPUTS)` crash during hybrid CPU/GPU layer offload in `get_llama_model()` by conditionally setting `flash_attn` to match `offload_kqv`. Enabled active in-memory model loading and inference execution directly utilizing VRAM (4.62 / 6.0 GiB active).
- **Online/Offline Server Toggle & Process Lifecycle Control**: Added `serenity.stopServer` and `serenity.toggleServer` commands with clean child process termination. Upgraded the SerenityDev webview status badge so clicking while online stops the server and clicking while offline starts it, with hover state and click feedback.
- **Active Model Dispatch & Config Corruption Fix**: Fixed `load_server_config()` and `update_config()` in `serenitydevserver.py` where `SUPERVISOR_MODEL` was inadvertently overwritten by `roles["orchestrator_turbo"]` (Nemotron). Updated `run_orchestration()` to respect `request.model` and fall back to `CURRENT_MODEL` before `SUPERVISOR_MODEL`. Updated webview `sendQuery` to pass the user's actively selected model directly in the query payload.
- **Chat Error Retry Button**: Added `[🔄 Retry]` action button in webview output whenever a model loading or inference error occurs, automatically re-submitting the last prompt with the active configuration.

- **Webview Script Syntax Error & UI Button Crash Fix**: Fixed unescaped inline single quotes in `parseProofBadges` and `renderMemories` (`src/extension.ts`) by replacing them with HTML entity `&quot;`. Template literal string evaluation in Node previously stripped backslashes from `\'`, emitting broken JS string concatenations (`'' + safeBId + ''`) and invalid regex quantifiers into the webview `<script>` block. This syntax error stopped webview script execution entirely and caused `Uncaught ReferenceError: sendCmd is not defined` on button clicks. Recompiled and packaged `SerenityDev-1.6.0.vsix`.
- **Debugging Attach to Process & Process-Blindness Fix**: Restored Python and Node "Attach using Process ID" (`debugpy`, `python`, `node`) as well as direct "Python Debugger: Launch SerenityDev Server" configurations in `.vscode/launch.json`. Added `PYTHONUNBUFFERED=1` environment variable when spawning `serenitydevserver.py` in `src/extension.ts` so all server startup, diagnostic, and error logs stream to the VS Code Output Channel in real time without block buffering delay.
- **MCP Logic Verification & Unified StreamableHTTP Engine**: Verified and unified the Model Context Protocol (MCP) JSON-RPC 2.0 StreamableHTTP `/mcp` endpoint in `serenitydevserver.py`. Standardized PQC Bearer token authentication (`ENFORCE_MCP_AUTH`), timing-safe verification, HTTPS scheme enforcement (`ENFORCE_MCP_HTTPS`), and tool execution dispatch across all 11 MCP tools (`read_file`, `write_file`, `list_directory`, `grep_search`, `insert_edit_into_file`, `replace_string_in_file`, `run_command`, `store_memory`, `query_memory`, `update_memory`, `delete_memory`). Cleaned up duplicate unmounted legacy handlers.
- **Dedicated MCP Test Suite**: Created automated unit test suite `src/test/test_mcp.py` validating MCP handshake (`GET /mcp`), protocol negotiation (`initialize`), notifications, ping, tools listing, tool calling, error handling (`-32601`, `-32700`), auth enforcement, and HTTPS enforcement. All 26 test cases pass cleanly.
- **Decoupled Reasoning Strength vs Limit Tiers**: Decoupled thought depth (`reasoning_strength`: `low`, `medium`, `high`, `xhigh`) from execution loop turn bounds (`limit_tier`: `default` [16 turns], `low` [8 turns], `medium` [25 turns], `high` [50 turns], `autonomy` [1000+ unconstrained turns with unlimited tool calling and auto-continue]).
- **Subagent Independent Loop Budgets & "Offload then Load" Flow**: Subagent worker tasks (`W1`..`W4`) execute with independent per-agent execution loop budgets (15 turns in standard mode, 100+ turns in autonomy mode), bypassing parent loop bounds. Subagent context is serialized, loaded into the subagent, and synthesized into a structured `HandoffReport` loaded back into the supervisor context without context bloat.
- **Agent-Maintained Persistent Long-Term Memory (LTM)**: Implemented `LongTermMemoryManager` with persistent JSON storage in `.serenity_cache/long_term_memory.json`. Integrated typed Python memory tools (`store_memory`, `query_memory`, `update_memory`, `delete_memory`), REST API endpoints (`GET /api/memory`, `POST /api/memory`, `DELETE /api/memory/{key}`, `DELETE /api/memory`, `DELETE /api/session/clear`), and automatic top-fact prompt context injection.
- **GPU Layer Text Input**: Replaced fixed hardcoded GPU layer offload dropdown options with direct text box / integer input (`Auto (-1)` or any custom layer count like `0` for CPU or `33`, `60`, `99`), applying changes directly without artificial layer limits.
- **Debugging Port Conflict & Server Startup Fix**: Removed `/api/status` bypass in `free_port()` so starting `serenitydevserver.py` directly (or in debugger) cleanly terminates stale background processes holding port 8002, eliminating `WinError 10048` socket collisions.
- **Pylance Type Narrowing for Subagent Dispatch**: Added explicit `isinstance(sub_tool.get("target"), str)` validation and typing for `dispatch_tool_call` target argument.

## Version 1.6.3
- **Auto Context Window Expansion Fix**: Fixed `cap_n_ctx_for_model()` and dynamic context expansion in `serenitydevserver.py`. Context window dynamically scales upward to fit prompts and tool outputs for models with large native train contexts without getting artificially clamped by the default `CONTEXT_WINDOW` config, while properly enforcing hard context caps on models like `codegemma` and `gemma-2`.
- **Accurate Model Execution Error Reporting**: Improved direct `llama_cpp` and server fallback error handling in `serenitydevserver.py` to report exact exception reasons rather than incorrectly masking context limit errors as unsupported architecture errors.
- **Split-Mode Bypass & Graph Partition Protection**: Passed `split_mode=0` (`LLAMA_SPLIT_MODE_NONE`) into `Llama` initialization and `-sm none` into `llama-server` in `serenitydevserver.py`. Prevents the GGML scheduler from treating partial CPU/GPU layer offloading as cross-device multi-GPU splits, completely preventing `GGML_SCHED_MAX_SPLIT_INPUTS` assertion crashes during hybrid execution.
- **Dynamic VRAM KV Guard for Large Architectures**: Retains full native context window and reasoning capacity for Qwen 3.8 and Nemotron 30B without arbitrary token truncation; memory safety is handled dynamically by pinning KV Cache to system RAM (`offload_kqv=False`) and scaling layer offload dynamically.
- **Virtual Environment & Python Interpreter Resolution**: Fixed Python interpreter discovery in `src/extension.ts` (`findPythonInterpreter`) to support direct `python.exe` binary paths, auto-derive `VIRTUAL_ENV` root directory and `PATH` prepending for virtualenvs, resolve `${workspaceFolder}` template variables, and fall back to VS Code's `python.defaultInterpreterPath`. Cleaned up `activate_virtualenv()` in `startup.py` to inspect active `VIRTUAL_ENV` and `sys.prefix` dynamically without hardcoded machine paths.
- **Dynamic Context Session History**: Replaced static 3-turn orchestration cap in `serenitydevserver.py` with dynamic session history retention scaled to the active context token budget.
- **Direct Model Output**: Eliminated hardcoded fallback placeholder string (`"I have completed analyzing the workspace."`) in favor of direct, unfiltered model output.
- **Font Options & UI Customization**:
  - Updated `UI_FONT_OPTIONS` and `MONO_FONT_OPTIONS` font configurations across UI themes.
  - Font packages referenced in `Misc/Fonts` with native Windows system font fallbacks.

## Version 1.6.2
- **Llama-Server Background Pipe Drain & Non-Blocking Polling**: Fixed `start_llama_server()` in serenitydevserver.py by attaching continuous daemon threads to drain `llama_server_process` `stdout` and `stderr` pipes. Eliminates OS pipe buffer saturation deadlocks and guarantees failure messages are captured cleanly during health checks.
- **Async Test Suite Integration**: Marked async test functions in `src/test/test_agent.py` with `@pytest.mark.asyncio`, allowing test suite execution across all test files.
- **Adaptive Thought Channel Closure & Zero-Response Fallback**: Fixed `StreamingThoughtFilter.flush_remaining()` and `run_orchestration` in `serenitydevserver.py` to synthesize and preserve valid final content when models complete generation without an explicit `<channel|>` closing tag. Added a safety text fallback so requests never return 0 tokens to VS Code.
- **Robust VS Code Chat Part Extraction**: Updated `provideLanguageModelChatResponse` in `src/extension.ts` to extract text from all part formats and duck-typed reference attachments.
- **Eliminated Subprocess Window Popups**: Added `creationflags=subprocess.CREATE_NO_WINDOW` across `start_llama_server` and `free_port` Windows commands (`netstat`, `taskkill`), guaranteeing 100% silent background subprocess execution without console popups.
- **Tool Workspace Path Resolution**: Implemented `resolve_workspace_path` and `get_primary_workspace_dir` in `serenitydevserver.py`. Filesystem tools (`read_file`, `list_directory`, `grep_search`, `write_file`, `insert_edit_into_file`, `replace_string_in_file`, `multi_replace_string_in_file`, `run_command`) now dynamically resolve relative paths against the active VS Code workspace folder (`request.workspace_dir` / `SERENITY_WORKSPACE_DIR`) instead of falling back to the packaged extension installation directory.
- **Nuked Model Consolidation**: Completely removed model consolidation flags, persistence, state variables, and UI controls across `serenitydevserver.py`, `serenity_config.json`, `src/extension.ts`, and Dashboard UI.
- **SerenityDev Rebranding**: Renamed planning and orchestration library views, commands, and headers to "SerenityDev" across `package.json`, `src/extension.ts`, and webviews.
- **Accurate Model Resolution & Low Memory Footprint**: Upgraded `resolve_model()` in `serenitydevserver.py` with token intersection scoring and substring matching. Short/abbreviated model names (such as `gemma-4-e2b-q2`) resolve directly to exact quantized GGUF weights (`gemma-4-E2B-it-qat-UD-Q2_K_XL`) rather than erroneously falling back to heavy supervisor models.
- **Direct Llama-CPP Streaming Fixes**: Fixed `llm.n_ctx()` call in `generate_completion_stream`, passed `type_k` and `type_v` quantized KV cache parameters (`q8_0`, `q5_1`) into `Llama` constructor, added dynamic context headroom clamping, and standardized SSE response headers with `data: [DONE]\n\n` termination.
- **Standardized Model Reporting**: Cleaned up `/api/models` and OpenAI-compatible `/v1/models` routes in `serenitydevserver.py` to dynamically report all 25+ installed GGUF models and role aliases directly to VS Code Copilot Chat and local OpenAI clients.
- **Verified Tool Calling & Standalone Boot**: Verified `serenitydevserver.py` standalone execution, port initialization with socket cleanup delay, streaming inference, and MCP tool execution (`list_directory`) via automated test suite `src/test/verify_fixes.py`.
- **Extension Package Build**: Rebuilt `SerenityDev-1.6.0.vsix` with 0 compilation errors.

## Version 1.6.1
- **Streaming Output Disaggregation**: Reorganized streaming loop in `serenitydevserver.py` to dispatch `thought`, `tool_call`, and `result` events on separate async channels, ensuring JSON blocks are parsed and yielded immediately upon closing tag detection without being blocked by long-running synchronous operations like `llama_server_response.stdout.readline()` or `mcp.run_command()`.
- **Direct MCP Tool Execution**: Bypassed `llama-server` for MCP-registered Python tools by implementing a new streaming handler that detects tool calls targeting `mcp` scopes and routes them directly through the `subprocess.Popen` managed `mcp:terminal:run_command` interface.
- **Real-Time Stream Monitoring**: Added `is_streaming` flag and `asyncio.create_task` for non-blocking monitoring of the tool execution subprocess, enabling concurrent generation of completion tokens and command output.
- **Blocking Operation Mitigation**: Wrapped `pynvml` library usage in `get_vram_info()` and `calculate_dynamic_gpu_layers()` with `try...except` blocks to gracefully handle missing or incompatible NVML installations, preventing server crashes and falling back to memory-mapped file estimates.
- **Dependency Injection Pattern**: Refactored `serenitydevserver.py` to pass the `McpServerWrapper` instance into the `LLMStreamProcessor`, facilitating seamless tool targeting without relying on global state.
- **Robust Error Handling**: Enhanced error management across the streaming pipeline, adding `try...except` blocks around file I/O, subprocess execution, and model inference to provide specific feedback and prevent abrupt server termination.

## Version 1.6.0
- **Multi-Line PTC Parsing Fix**: Fixed `extract_json` in `serenitydevserver.py` to parse full Python code blocks with `ast.parse(mode='exec')` + `ast.walk()`, enabling multi-line triple-quoted arguments (e.g. `insert_edit_into_file` with `new_content="""..."""`) to parse and execute as tool calls instead of leaking as raw text. Falls back to line-by-line parsing for simple single-line calls. [Done]
- **Config Persistence (`serenity_config.json`)**: All server engine settings (KV cache quantization, context window, GPU layers, auto-continue, active model, model consolidation, role assignments) are now persisted to `serenity_config.json` on every `/api/config` POST and automatically loaded on server startup via `load_server_config()`. [Done]
- **96k Context Window Option**: Added `98304 (96k Extended)` to `serenity.setContextSize` QuickPick in `src/extension.ts`, filling the gap between 64k and 128k. Updated explainer text to reflect 2k-256k range. [Done]
- **Eliminated Windows Terminal Flashing**: Replaced synchronous `nvidia-smi` subprocess polling with in-process `pynvml` memory queries for real-time `/api/status` queries. Cached hardware entropy in `SerenityKeyVault` and added `creationflags=subprocess.CREATE_NO_WINDOW` across all Python subprocess executions (`wmic`, `powershell`, `nvidia-smi`, `mcp:terminal:run_command`, `seal_secrets.py`). Set `windowsHide: true` on Node.js `cp.spawn` in `src/extension.ts` to guarantee completely silent, invisible background server execution. [Done]
- **Server Auto-Start & Resilient Boot Engine**: Implemented `ensureServerStarted()` in `src/extension.ts` with multi-path virtualenv detection (workspace, extension dir, user home, global `SerenityDev`), automatic port 8002 health check probing, background boot on activation, and auto-generation of sealed hardware-bound keys in `startup.py`. Added command `serenity.startServer` and offline webview banner with one-click boot. [Done]
- **Active Model Selector & Dynamic Detection Scanner**: Added live Model Selector dropdown directly into the top header of the Planning & Orchestration sidebar webview, populated with all discovered GGUFs from `models/` and custom folders. Implemented commands `serenity.selectModel` (instant active model switching) and `serenity.scanModels` (trigger directory rescan and report detected model counts). [Done]
- **Role-Based Effort Levels & Dynamic Model Mapping**: Passed Supervisor/Orchestrator effort tiers as explicit roles (`supervisor_low`, `supervisor_high`, `orchestrator_turbo`, `w1_reasoning`, `w2_code`, `w3_fast`, `w4_specialized`, `fim`). Updated `serenitydevserver.py` to route and resolve specific models per role dynamically based on execution mode. Added interactive Role Assignment modal in the Planning & Orchestration Library webview and command `serenity.setRoleModel` for instant one-click model swapping across effort levels. [Done]
- **Auto-Continue (Unlimited Iteration) Toggle**: Implemented `auto_continue` toggle parameter in `QueryRequest`, `ConfigUpdate`, and `serenitydevserver.py` allowing the multi-turn agentic orchestrator to iterate up to 500 loops until task completion without premature cutoff. Added toolbar toggle in the Planning Library sidebar and command `serenity.toggleAutoContinue`. [Done]
- **Model Reporting & Capabilities Pipeline**: Updated `/api/models` endpoint and GGUF header parser in `serenitydevserver.py` to extract native model context lengths (e.g. 262,144 / 256k tokens) and report dynamic capabilities (`toolCalling`, `imageInput`, `tools`, `vision`). Updated `provideLanguageModelChatInformation` in `src/extension.ts` and added `262144 (Ultra 256k)` to `serenity.setContextSize` quickpick. [Done]
- **Server Control Commands Suite**: Implemented interactive VS Code commands with quickpick and input workflows in `src/extension.ts` and `package.json`:
  - `serenity.restartServer`: Soft restart endpoint `/api/restart` clearing server state and inference locks.
  - `serenity.unloadModel`: Dedicated `/api/control/unload` and `/api/unload` endpoints to release `llama-server` and direct `llama-cpp-python` allocations and free GPU VRAM instantly.
  - `serenity.setKVCache`: Interactive selection for Key and Value cache quantization (`f16`, `q8_0`, `q4_0`, `q5_1`).
  - `serenity.setContextSize`: Context window configuration (`2048` to `262144` tokens) with runtime reload.
  - `serenity.setGpuLayers`: Dynamic GPU layer offload control (`Auto (VRAM Guard)`, `0 (CPU)`, `4`, `16`, `32`, `40`, `60`, `99 (All Layers)`).
  - `serenity.showMenu`: Server control quickpick menu integrating all control and status actions. [Done]
- **Planning & Orchestration Library Explainer Dialog & UI**: Added an interactive explanation modal and standalone documentation panel (`serenity.explainPlanning`) detailing the hierarchical Supervisor-Worker architecture, multi-turn tool loops, and memory governance. Added quick-action toolbar controls directly into the sidebar webview. [Done]
- **PTC Prompt Directive Enforcement**: Updated `PYTHON_TOOL_STUBS` and worker prompt execution directives in `serenitydevserver.py` to mandate executable Python function call syntax and forbid unexecuted natural language tool intent statements. [Done]
- **Tool Call Isolation & Execution Fix**: Resolved premature tool call stripping before execution. Separated `strip_thought_blocks()` from `clean_thought_and_whitespace()` in `serenitydevserver.py`, updated `extract_json()` to parse all tool call syntaxes (Native Gemma `<|tool_call>`, XML `<tool_call>`, PTC Python functions, and markdown JSON) without pre-stripping, and hardened `StreamingThoughtFilter` lookahead buffering for closing tool tags (`<tool_call|>`, `</tool_call>`). [Done]
- **Shared VRAM Guard**: Implemented dynamic context-scaled VRAM headroom calculation in `get_vram_info()` and `calculate_dynamic_gpu_layers()` in `serenitydevserver.py`. Automatically calculates compute graph and CUDA driver buffer requirements, preventing allocations from spilling past dedicated physical VRAM into Windows Shared GPU Memory paging. [Done]
- **Anti-Split Graph Protection & KV RAM Pinning**: Pinned KV cache to system RAM (`offload_kqv=False`) during partial layer offloading, Sliding Window Attention (SWA), and high-context workloads (e.g., Gemma-4 / Gemma-2 at 49k+ context), eliminating cross-device attention graph splits and resolving `GGML_ASSERT(n_inputs < GGML_SCHED_MAX_SPLIT_INPUTS)` assertion crashes. [Done]
- **Resilient Recovery Fallbacks**: Wrapped `Llama` direct instantiation in `get_llama_model()` with multi-tier recovery fallbacks (auto-halving GPU layers, disabling flash attention, and fallback to CPU mode) to guarantee uninterrupted server uptime. [Done]
- **Updated CUDA 13.3 llama_cpp Binaries**: Synced latest CUDA-compiled `llama_cpp` binaries (including 258 MB `ggml-cuda.dll`) from `SerenityPC` into workspace `.venv`. [Done]
- **Extension Packaging & Version Sync**: Bumped extension version to 1.6.0 in `package.json`, excluded TLS keys (`*.key`, `*.pem`, `*.crt`) from VSIX packaging in `.vscodeignore`, and rebuilt `SerenityDev-1.6.0.vsix` via `build_extension.py`. [Done]

## Version 1.5.9
- **Grep Search Single-File & Regex Support**: Fixed `grep_search` in `serenitydevserver.py` to support targeting single files directly alongside directory trees (`os.path.isfile`), compiled regex queries with case-insensitivity, and added fallback to literal substring matching when regex compilation fails or produces no match. [Done]
- **NVML / NVIDIA-ML-PY Warning Cleanliness**: Filtered `pynvml` `FutureWarning` in `serenitydevserver.py` at module initialization and during VRAM metric discovery so `nvidia-ml-py` metrics query cleanly without console pollution. [Done]
- **Thought Channel Real-Time Demuxing & Lookahead Buffer**: Refactored `StreamingThoughtFilter` in `serenitydevserver.py` to strictly demux internal reasoning from user-facing output buffers without swallowing direct answers or error text, implementing lookahead buffering for partial tags (`<|channel`, `<think`, `<|turn`) and eliminating blank UI dropouts in the VSCode AI window. [Done]
- **Dynamic Model Folder Configuration & UI Picker**: Replaced hardcoded model paths with dynamic candidate discovery in `serenitydevserver.py` (`get_candidate_model_dirs`, `add_custom_model_dir`), added `serenitydev.modelsPath` VSCode workspace setting in `package.json`, and added `"Add Model Folder..."` quickpick action and command (`serenity.addModelFolder`) in `src/extension.ts`. [Done]
- **Thread Safety & Process Crash Protection**: Guarded direct `llama_cpp` generation in `generate_completion_stream` with an inference thread lock (`direct_llama_lock`) and thread-safe cancellation event (`cancel_event`), eliminating race conditions and native Access Violation `0xC0000005` process segfaults on cancelled or concurrent streams. [Done]
- **Fenced JSON Tool Parsing Repair**: Updated `extract_json` in `serenitydevserver.py` to extract fenced JSON tool calls before tag sanitization, resolving supervisor routing retry loops. [Done]
- **Incomplete Tool Call Regex Hardening**: Fixed word boundary matching in `_is_incomplete_tool_call` to prevent false positive buffer stalls on words ending in `"call"`. [Done]
- **VSCode Extension Python Path Prioritization**: Configured `src/extension.ts` to prioritize workspace `.venv\Scripts\python.exe` (Python 3.12) to ensure compiled C-extensions (`llama_cpp.pyd`) load seamlessly, added support for `System` messages in Language Model Chat Provider, and wired `thought` SSE channel updates to chat progress. [Done]

## Version 1.5.8
- Restored CUDA acceleration and VRAM offloading in local Python environment (`.venv`): deployed CUDA-compiled `llama_cpp_python` binaries with `ggml-cuda.dll`, enabled dynamic GPU layer allocation and Flash Attention on NVIDIA GeForce RTX 3050. [Done]
- Integrated `nvidia-ml-py` in [requirements.txt](requirements.txt) for direct NVML VRAM hardware metrics queries. [Done]

## Version 1.5.7
- Added Windows 11 CIM/PowerShell fallback to `SerenityKeyVault.get_machine_entropy()` in `serenitydevserver.py` and `seal_secrets.py` for environments where `wmic` is deprecated or uninstalled. [Done]
- Added multi-candidate entropy fallback list (`get_entropy_candidates()`) to `SerenityKeyVault.unlock()` ensuring graceful decoding across legacy MAC, MachineGuid, and BIOS UUID combinations. [Done]
- Decoupled `seal_secrets.py` from top-level `serenitydevserver.py` imports to break circular boot dependency deadlocks when `.env` is unsealed or corrupted. [Done]
- Integrated automated virtual environment (`.venv` / `venv`) activation across startup workflows in `startup.py`: `activate_virtualenv()` dynamically discovers local virtual environments, injects site-packages into `sys.path`, sets `VIRTUAL_ENV`, and prepends the virtual binary directory to `PATH`. [Done]
- Updated `start_native_mcp.py` to run environment and virtualenv validation before importing server and crypto libraries. [Done]
- Enhanced VSCode extension (`src/extension.ts`) server spawning to auto-detect workspace/extension `.venv` Python executables and propagate `VIRTUAL_ENV` and updated `PATH` to child processes. [Done]
- Created cross-platform launcher scripts for standard and MCP servers (`start_server.bat`, `start_server.ps1`, `start_server.sh`, `start_mcp.bat`, `start_mcp.ps1`, `start_mcp.sh`). [Done]

## Version 1.5.6
- Implemented Programmatic Tool Calling (PTC) & Clean Stubs paradigm (arXiv:2608.06370v1): transitioned tool declarations in prompts and chat templates from verbose Gemma pseudo-JSON tags to typed Python function stubs (`PYTHON_TOOL_STUBS`). [Done]
- Added AST-based Python function call parsing (`_parse_python_func_call` and `extract_json`) in `serenitydevserver.py` supporting both standard Python invocation syntax (`read_file(...)`, `run_command(...)`, code blocks) and backward-compatible JSON/native tags. [Done]
- Fixed VSCode tool calling visualization in `src/extension.ts`: enabled `supportHtml: true` and `isTrusted: true` in `createSafeMarkdown()` so collapsible `<details>` and formatted execution summary cards render natively in VSCode Markdown chat. [Done]
- Enhanced tool execution streaming across Standalone (Path A) and Supervisor (Path B) to yield formatted `<details>` collapsible cards with parameters, execution status, and output previews into the client SSE stream. [Done]
- Created unit tests in `src/test/test_ptc_parsing.py` validating Python function call, code block, and JSON fallback parsing. [Done]

## Version 1.5.5
- Comprehensive security hardening across extension & server: HTML entity escaping for webview inputs & proof badges, `createSafeMarkdown` helper (`isTrusted=false`, `supportHtml=false`), `shell=False` list-argument subprocess execution with security notes on affected modules, `validate_path_containment` with symlink & user-allowed dir resolution, robust `extract_json` parser, and async lock synchronization. [Done]
- Resolved tool call chat stream leakage in `serenitydevserver.py`: tool execution events now route as `type: "progress"` updates during orchestration streaming instead of cluttering main markdown content streams or truncating `/ask` JSON responses. [Done]
- Fixed syntax compilation error in VSCode extension (`src/extension.ts`): removed stray `pip uninstall bandit` command string from source. [Done]
- Completed Android Studio & JetBrains IDE plugin implementation in `IntelliJ.kt`: added interactive prompt input dialogs via `Messages.showInputDialog()`, thread-safe editor buffer replacements using `WriteCommandAction.runWriteCommandAction()`, and user error popups for offline devserver detection. [Done]
- Enhanced `clean_thought_and_whitespace()` in `serenitydevserver.py` to strip unhandled native `<|tool_call|>` and `call:...` tags from final synthesized outputs. [Done]
- Fixed TypeScript syntax & parsing errors in `src/extension.ts`: converted unescaped template literal backticks inside `parseProofBadges` webview HTML generator to single-quoted string concatenation. [Done]
- Fixed infinite hang during `llama-server` process startup in `serenitydevserver.py`: replaced `subprocess.DEVNULL` with `subprocess.PIPE` stdout/stderr capture, added `poll()` status checks inside health polling loop, migrated blocking `urllib.request` health checks to non-blocking `httpx.AsyncClient`, fixed invalid `-fa` flag syntax to `-fa on`, removed problematic `--model-draft` and `--mmproj` flags causing architecture load crashes (`dflash`), and added graceful `llama_cpp` fallback / error reporting when `llama-server` cannot load unsupported GGUF architectures (`muse-glimmer`). [Done]

## Version 1.5.4
- Created `dispatch_tool_call()` unified tool execution engine in `serenitydevserver.py`, consolidating tool execution logic across Standalone (Path A), Supervisor Pipeline (Path B), Worker Inner Tool Loop, and the MCP HTTP endpoint (`/mcp`). [Done]
- Hardened `StreamingThoughtFilter` and `_strip_tool_call_tags()` in `serenitydevserver.py` to buffer incomplete tag prefixes (`call:`, `<|tool_call`) and suppress raw JSON markdown blocks (`action: "call_tool"`) from leaking into user chat streams. [Done]
- Standardized line-range parsing (`parse_read_file_args`) and automatic output truncation across `read_file`, `list_directory`, and `grep_search` to prevent context window bloat during deep codebase exploration. [Done]
- Updated dynamic Llama-CPP context reload threshold calculation (`generate_completion_stream`) to expand in 8,192 token increments, eliminating frequent model reloads during conversation turns. [Done]

## Version 1.5.3
- Added edit proof calculation (`edited:filename-del+add`) and pre-edit snapshot backups (`backup_id`) across standalone (Path A) and supervisor (Path B) file edit tools (`write_file`, `replace_string_in_file`, `insert_edit_into_file`, `multi_replace_string_in_file`) in `serenitydevserver.py`. [Done]
- Implemented `/api/edit/revert` and `/api/edit/keep` endpoints in `serenitydevserver.py` to restore pre-edit file buffers or release backup snapshots on demand. [Done]
- Added interactive inline Keep (`✓ Kept`) and Reject (`❌ Reverted`) proof badge cards in VSCode chat webview (`src/extension.ts`) with postMessage IPC to server revert/keep endpoints. [Done]
- Standardized `/plan` slash command and plan generation mode with task list checkbox formatting (`- [ ] Task N`) and state tracking in `active_system_plan` (`serenitydevserver.py`). [Done]
- Fixed response assembly in `/ask` endpoint (`serenitydevserver.py`) and updated `IntelliJ.kt` so Android Studio receives formatted execution summaries with tool edit details instead of raw model signature headers ("Generated by gemma-4..."). [Done]

## Version 1.5.2

- Resolved git merge conflicts and syntax errors in `package-lock.json` across devDependencies, engine limits, and transitive dependency overrides. [Done]
- Added `diff` (`^8.0.3`) and `mocha` (`^12.0.0`) overrides to `package.json` to patch `jsdiff` DoS vulnerability (GHSA-73rr-hh4g-fpgx). [Done]
- Added `brace-expansion` override (`^5.0.8`) to `package.json` to enforce security patch for nested dependency resolution. [Done]
- Hardened `axios` against inherited prototype proxy/auth bypass (GHSA-gcfj-64vw-6mp9 / GHSA-xj6q-8x83-jv6g): normalized request configs in `Axios.prototype.request` via `mergeConfig` and `dispatchRequest` using null-prototype own-property checks, and restricted `setProxy` and `mergeConfig` map parsing to own properties (`proxy`, `auth`). [Done]
- Patched CWE-407 quadratic complexity / DoS vulnerability in `node_modules/shell-quote` (`parse.js`): replaced $O(n^2)$ `acc.concat` in `.reduce()` with linear `forEach`/`push` accumulator mutation and added defensive input length cap (`MAX_INPUT_LENGTH = 1,000,000`). [Done]
- Patched CWE-93 CRLF injection vulnerability (GHSA-hmw2-7cc7-3qxx) in `node_modules/form-data`: escaped `\r`, `\n`, and `"` as `%0D`, `%0A`, and `%22` in `_multiPartHeader` field names and `_getContentDisposition` filenames. [Done]
- Fixed raw native tool call leakage in `StreamingThoughtFilter` (`serenitydevserver.py`): replaced non-greedy regex stripping with `_strip_tool_call_tags()` using `_find_balanced_brace()` for brace-balanced argument parsing, added partial tool call stream buffering to prevent premature chunk flushing across boundaries, and added `[Tool Filter]` logging for stripped tool call text. [Done]
- Added standalone mini agentic tool loop (Path A) in `serenitydevserver.py`: after each generation, `extract_json()` detects native `<|tool_call>call:X{...}<tool_call|>` output; if a tool call is found, it is dispatched inline (all filesystem + terminal tools, full write access, security-gated), result appended to context, and re-generated. Loop capped at 8 iterations with duplicate call guard. Fixes Gemma-4 native tool calls leaking as plaintext and ending chat. [Done]
- Added `bash`, `sh`, `shell`, `run` aliases → `mcp:terminal:run_command` in target normalizer (`serenitydevserver.py`) to handle Gemma-4's `call:Bash{command:...}` tool call syntax. [Done]
- Added per-worker inner tool loop (Phase 2, Path B) in `serenitydevserver.py`: workers can now resolve native tool calls emitted mid-generation (up to 8 iterations, duplicate guard, full tool dispatch), appending results to worker context before re-generating. [Done]
- Added worker delegation caps in `serenitydevserver.py` supervisor loop: Supervisor Low = max 3 worker delegations per run; Supervisor High = max 5; Orchestrator/Turbo = unlimited. [Done]

## Version 1.5.1

- Fixed native tool call regex parsing in `extract_json()` (`serenitydevserver.py`) to support end tag variants like `<tool_call|>` / `<|tool_call|>` and sanitize template quote escape artifacts (`<|"|>`) prior to JSON deserialization. [Done]
- Added `shutil.which` resolution and `LLAMA_SERVER_BIN` environment variable lookup in `start_llama_server` (`serenitydevserver.py`) to resolve `FileNotFoundError: [WinError 2]` when `llama-server` binary is missing from PATH. [Done]
- Configured mode effort levels in `serenitydevserver.py`: Low Mode (8 max steps, low-resource efficiency), High Mode (25 max steps, full reasoning), and Autonomous Turbo Orchestrator Mode (100 max steps for nigh-indefinite sub-agent delegation, plan oversight, and tool execution). [Done]
- Adjusted duplicate tool call guard thresholds: allowed up to 5 identical retries for terminal polling/test commands (`run_command`) and 3 for identical file operations before breaking loop to Worker W1. [Done]
- Cleaned up model display names in `/api/models` (`serenitydevserver.py`) and `package.json`: removed redundant `Serenity:` prefix from model names, labeled Orchestrator as `Orchestrator - Turbo Mode`, and updated vendor display name to `SerenityDev`. [Done]
- Hardened `COMMAND_RULES` in `serenitydevserver.py`: explicitly blacklisted `python -c`, `python -i`, and bare `python`, while explicitly whitelisting `python --version`, `python -m pip (list|check)`, script execution (`python <script>.py`), and module entry points (`python -m <module>`). [Done]
- Implemented real-time `<details>` block streaming in `serenitydevserver.py` during orchestration, streaming tool args, status icons, and raw output blocks directly to VS Code Copilot Chat UI to eliminate blank "Analyzing" delays. [Done]

## Version 1.5.0
- Overhauled `extract_json()` and added `normalize_tool_action()` helper in `serenitydevserver.py` to parse JSON array tool call structures (`[{"tool_name": "...", "args": {...}}]`), extract non-standard schema keys (`tool_name`, `name`, `tool`, `function`, `args`, `arguments`, `parameters`), and eliminate raw tool call JSON string leakage in chat stream outputs. [Done]
- Hardened MCP tool execution parameter fallbacks in `execute_mcp_tool_call()` and supervisor tool handlers (`file_path` -> `path`, `replace_string` / `replacement` -> `new_content`, `search_string` / `old_content` -> `target_content`) in `serenitydevserver.py`. [Done]
- Hardened `SerenityKeyVault` with composite multi-factor hardware entropy (OS MAC, Windows `MachineGuid`, BIOS UUID) to prevent user-space MAC address spoofing attacks, squeezed 96-bit nonces directly from SHAKE-256 to prevent GCM nonce reuse catastrophes, enforced constant-time digest comparisons (`hmac.compare_digest`), implemented `SessionRotationManager` with encrypted state preservation/resumption, added `/api/session/rotate` and `/api/session/status` endpoints, and launched an autonomous downtime session rotation loop (default 10-minute idle threshold). [Done]
- Limited server process permissions & .env rogue agent isolation. Added is_path_allowed checks to read/write/edit/grep tools, blacklisted .env in command execution rules, filtered list_directory, and sanitized subprocess environment. [Done]
- Fixed LOCAL_API_KEY structure in dotenv loading & fallback to prevent ValueError startup crash and handle local key configuration. [Done]
- Implemented SerenityKeyVault with SHA3-512 & SHAKE-256 (Keccak XOF) hardware MAC entropy binding for pqc_v1: key blobs. [Done]
- Added PQCEnforcementMiddleware for request signature and 30s sliding window replay protection. [Done]
- Created startup.py & config_guard.py for strict environment validation; enforced zero fallback key policy in serenitydevserver.py (fails fast if LOCALAPIKEY is missing or unencrypted). [Done]
- Cleaned up and updated .gitignore rules (added build/test/venv/cache patterns, unignored .env.example template). [Done]
- Created seal_secrets.py CLI tool to generate hardware-bound PQC key blobs (`pqc_v1:...`) with MAC + SHA3-512 + SHAKE-256 entropy. [Done]
- Updated .vscodeignore to exclude .env, .venv, scratch, and cache files from VSIX packaging. [Done]
- Updated build_extension.py to calculate and output the absolute VSIX file path on successful build. [Done]
- Added Ollama & OpenAI API compatibility routes (`/api/tags`, `/api/version`, `/v1/models`, `/v1/models/{model_id}`, `/v1/chat/completions`, `/health`) and `CORSMiddleware` in `serenitydevserver.py` to prevent 404 errors and resolve client HTTP upgrade/preflight edge cases. [Done]
- Re-encrypted LOCAL_API_KEY in .env using encrypt_key.py bound to current machine hardware MAC entropy to resolve cryptography.exceptions.InvalidTag error. [Done]
- Added graceful `asyncio.CancelledError` handling across completion streaming, OpenAI compatibility routes, and supervisor loops in `serenitydevserver.py` to prevent task group tracebacks on client disconnections. [Done]
- Allowed absolute and drive-qualified workspace directory paths from Android Studio and VS Code plugins in `validate_workspace_path` and `run_orchestration` in `serenitydevserver.py`. [Done]
- Expanded native tool call parsing in `extract_json` in `serenitydevserver.py` to match tag variants like `<channel|><|tool_call>call:read_file{...}` and prevent orchestrator stalling. [Done]
- Implemented `get_vram_info()` in `serenitydevserver.py` using `pynvml` & `nvidia-smi` to track Total VRAM, Free VRAM, and Self Process VRAM (`free + self_used`) for accurate dynamic GPU offloading. [Done]
- Added `parse_read_file_args()` in `serenitydevserver.py` supporting all line range parameters (`start_line`, `startLine`, `start`, `line`, `range`, `path:1520`, `path around line 1520`). [Done]
- Added tool target normalization routing in `serenitydevserver.py` to map model tool aliases (`search_files` -> `grep_search`, `list_files` -> `list_directory`, etc.). [Done]
- Implemented tool target namespace prefix stripping in `serenitydevserver.py` (e.g. `google:mcp:code_interpreter:read_file` -> `read_file`) to ensure tool call execution across custom client schemas. [Done]
- Created and refactored `SERENITY.md` specification document with Caveman Principle, Ponytail Laziness Ladder, tool target schemas, and execution pipeline rules. [Done]
- Merged `src/Serenity.md` manifest into root `SERENITY.md` (adding Offload-then-Load OTL protocol, memory workaround specs, sub-agent logic flows, and tool optimization matrix). [Done]
- Updated `clean_thought_and_whitespace()` in `serenitydevserver.py` with robust regexes to strip pipe-wrapped think/thought tags (`<|think|>`, `<|thought|>`, `<|channel|>thought`) and prevent thought-trace JSON leakage from breaking tool calls. [Done]
- Removed duplicate leading `<bos>` tags from all prompt templates in `serenitydevserver.py` to eliminate `llama.py` duplicate BOS runtime warnings and preserve chat template token alignment. [Done]
- Updated `StreamingThoughtFilter` to initialize with `start_in_thought=True` by default and clean out unneeded native tool call tags during worker Markdown output streaming. [Done]
- Fixed f-string literal brace escaping in `review_prompt` in `serenitydevserver.py` (`{{` and `}}`) to resolve `ValueError: Invalid format specifier` runtime stream crash. [Done]
- Added parameter key fallbacks (`search_string`, `replace_string`, `old_content`, `new_content`, `target`, `replacement`) to `replace_string_in_file` handler in `serenitydevserver.py`. [Done]
- Reset `max_tool_loops` default to 10 (Low mode) / 30 (High mode) in `serenitydevserver.py`, and added optional `max_steps` parameter support in `QueryRequest` so clients can customize execution depth. [Done]
- Implemented `executed_tool_calls` signature tracking and duplicate tool call interception in `serenitydevserver.py` to block models from repeatedly re-reading identical files. [Done]
- Updated `list_directory` handler in `serenitydevserver.py` to parse and list subdirectories (`path`, `directory`, `folder`) instead of hardcoding `.`, allowing models to inspect subfolders like `gradle` or `app`. [Done]
- Hardened all MCP tool handlers in `serenitydevserver.py`: added auto-creation of parent directories (`os.makedirs`) in `write_file`, parameter key fallbacks across `insert_edit_into_file` and `multi_replace_string_in_file`, and directory scoping (`path`/`dir`) for `grep_search`. [Done]
- Added OpenAI `/v1/responses`, `/responses`, `/v1/completions`, `/completions`, and `/chat/completions` API compatibility endpoints to `serenitydevserver.py` to fix Android Studio, JetBrains, and VS Code IDE 404 connection errors. [Done]
- Updated tool matrix in `src/SERENITY.md` to document optional subdirectory `path` parameter support for `list_directory`. [Done]
- Implemented HTTPS-enforced StreamableHTTP Model Context Protocol (MCP) endpoint (`@app.api_route("/mcp")`) in `serenitydevserver.py` supporting `initialize`, `tools/list`, `tools/call`, `ping`, and `notifications/initialized` for Google AI Edge Gallery and external MCP clients. [Done]
- Created `start_native_mcp.py` to run custom native HTTPS MCP server directly on LAN IP (`0.0.0.0:8443`) with PQC hardware-seeded Local Root CA (`rootCA.pem`) and SHA-384 signed TLS certificates for Android trust without third-party tunnel services. [Done]
- Updated `.gitignore` to exclude TLS certificates, keys (`*.pem`, `*.key`, `rootCA.*`, `cert.*`), and credential files from git version control. [Done]
- Cleaned up rule redundancy between `.agents/AGENTS.md` and `src/SERENITY.md`: established `.agents/AGENTS.md` as the auto-injected systemic rulebook, and updated `src/SERENITY.md` to reference `.agents/AGENTS.md` while maintaining technical architecture specifications. [Done]
- Added `cap_n_ctx_for_model()` in `serenitydevserver.py` to auto-cap context allocations (`n_ctx`) to model-specific training limits (e.g. 8192 max for CodeGemma models), eliminating `n_ctx_seq > n_ctx_train` context overflow warnings. [Done]
- Added `/mcp` GET/POST Streamable HTTP JSON-RPC 2.0 endpoints with PQC Bearer token auth in `serenitydevserver.py`. Refactored `start_native_mcp.py` for HTTPS-only security with auto-serve `rootCA.pem` HTTP downloader on port 8080. Hardened `verify_mcp_auth` for flexible token headers (`Authorization: Bearer <token>`, `Authorization: <token>`, `Bearer: <token>`, `?token=`) and allowed optional GET discovery probes for Edge Gallery initialization. [Done]
- Enforced strict role separation in `serenitydevserver.py` and `src/SERENITY.md`: Supervisor functions as Planner & Architect (`create_or_update_plan`, task decomposition, review), while Workers function as pure Executors (tool operations, direct code synthesis, ultra-terse summary for Supervisor without chatter). [Done]
- Updated model registry (`/api/models`) and startup logs in `serenitydevserver.py` to assign the `Agent` role to registered/discovered models (e.g. `Serenity: <model> (Agent)`), reserving the `Worker` title strictly for explicit delegation phases by the Supervisor. [Done]
- Added `unload_direct_llama_model = unload_llama_model` function alias in `serenitydevserver.py` to resolve `NameError: name 'unload_direct_llama_model' is not defined` runtime stream failure during model reloading. [Done]
- Updated `/api/status` endpoint in `serenitydevserver.py` with non-blocking `query_nvidia_smi_sync()` executed in `asyncio.to_thread` with tight timeout (1.5s) and `cached_gpu_memory` fallback, resolving `subprocess.TimeoutExpired` crashes when `nvidia-smi` blocks on CUDA context locks. [Done]
- Updated optional `llama_cpp` import in `serenitydevserver.py` to check `importlib.util.find_spec("llama_cpp")` before loading, preventing IDE debuggers from breaking on unhandled `ModuleNotFoundError` exceptions. [Done]
- Fixed model unloading hangs in `unload_llama_model()` and `unload_llama_server()` by invoking explicit `.close()`, `del`, and 2s timeout fallbacks before garbage collection. [Done]
- Redesigned Orchestration Report output formatting in `serenitydevserver.py` using native GFM Markdown and `<details open>` collapsible blocks, ensuring clean layout rendering in VS Code and Android Studio IDE clients. [Done]
- Fixed host string literal in `start_native_mcp.py` (`host="0.0.0.0"`), resolving socket `[Errno 11001] getaddrinfo failed` startup crashes. [Done]
- Added automatic `free_port()` cleanup in `start_native_mcp.py` to auto-terminate zombie processes occupying ports 8443 or 8080 on Windows (`Errno 10048 WSAEADDRINUSE`) with `subprocess.run` exit code handling. [Done]

## Version 1.4.20 2026-07-20 
- Gemma-4 Chat templates updated to the july release, boasting improved benchmark scores, tool call handling, and thought handling.
- added compatibility with Windows Smart App Control and localized cache
- cleared up workflow and added TODO to CHANGELOG.

## [Version 1.0.2]
- Initial commit

### To-Do List: (Items marked [Done] must be tested and verified, then they will be crossed out and added above)

2. kv cache compression settings (add to UI and verify it works / changes) [Done] 

3. separate values for k and v: fp16, q8_0, q5_1, q5_0, q4_0, turbo4_tcq, turbo3_tcq, turbo2_tcq [Done] 

4. implement terminal command whitelist / blacklist filtering in devserver for run_command. [Done]

6. ensure for each task and worker the temp and such is set properly thruout. [Done] 

7. Adapted gork-build Rust architecture to improve this codebase. Added: 1) Tool-Pair-Safe context trimming, 2) Workspace/Session prompt queue manager & decongestion, 3) Sliding-window CircuitBreaker for API/tool resilience. Annotated Rust-exclusive features (Alacritty PTY handles, Ratatui TUI rendering, D-Bus sleep listeners). [Done]

8. fix the extension icon not showing up in VSCode.

9. map MOE router sizes
test and learn gemma-4 params. figure out: what works best for coding? what has the lowest probability of skipping the response/ what parameters provide the most complete and accurate response?
add git commands to harness

10. review FIM handling and VRAM usage; let it be on unless VRAM is to be saturated especially by a large LLM being loaded. load FIM if there is space, auto-offload for Orchestrator / Supervisor and secondary routing phases.
11.  Smart App Control & Cache Localization:
   - Localize TEMP/TMP and CUDA compiler cache paths to the workspace to bypass Windows security policy blocks.
   - Ensure all subprocess backends (MSVC, CMake, Pip, PyTorch, Triton) respect the localized variables. [Done] 
"when running tests, if it involves significant VRAM/RAM usage (such as testing output of a coding agent or running like tests for LLMs), it is acceptable to create a script that runs a test then re-wakes the model for parsing the result, specifically as a workaround for memory constraints." [Done] 

TBD:

- make auto-pushing verified updates a thing. 
- auto-log git changes before pushing.

- test and learn gemma-4 params. figure out: what works best for coding? what has the lowest probability of skipping the response / what parameters provide the most complete and accurate response?
- introduce workspace-specific buckets for proper queue decongestion

- clearing irrelevant queue congestion

- for main.py (serenitydevserver.py): decide upon relevancy, consider an auto-param adjustment mode that does NOT override settings, but creates a temp state that bases off the persona settings, but adjusts (ie. lowers temp for coding, adjusts for content flow being multimodal, text, analysis and data handling, etc.)

- add git commands to harness (Note: harness/tests currently exist in SerenityPC workspace)

- update README and create LICENSE.md