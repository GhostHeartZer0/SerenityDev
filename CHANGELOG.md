# Change Log
All notable changes to the "SerenityDev" extension will be documented in this file.

## Version 1.5.2
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