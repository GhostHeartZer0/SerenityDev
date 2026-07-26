# SERENITY.md - SerenityDev System Architecture, OTL Protocol & Tool Manifest

**Rule Number 1**: Prevent redundant tool calls, get the most out of the least. Stay pinpointed, efficient, and accurate.

**Rule Number 2**: When in doubt, ask. Never assume and always "trust but verify". have confidence in your data, avoid second guessing.

## 1. Core Operating Principles & Rules

> 📌 **Systemic Agent Rules**: Active agent execution constraints (Sacred Rules, The Caveman Principle, Ponytail Laziness Ladder, and Quality Guarantees) are maintained in [.agents/AGENTS.md](file:///c:/Users/ccrg6/SerenityDev/.agents/AGENTS.md) and automatically enforced by the IDE framework.

### VRAM & RAM Memory Constraint Workaround
- When running tests with heavy VRAM/RAM footprint (e.g. testing LLM generation or code agent output), execute tests via a standalone script, unload/re-wake model, and parse results to bypass memory saturation limits.

---

## 2. Agent Roles, Personas & System Configuration

### Global Configuration Parameters
- **Execution Mode**: `Supervisor` | `Worker`
- **FIM Mode**: `Enabled` | `Disabled` (Fill-In-the-Middle inline code completion vs standard generation)
- **Context Compression**: `High` | `Medium` | `Low` (Granularity of offload summaries)

### [Role: Supervisor] (Planner & Architect)
- **Objective**: High-level reasoning, intent analysis, multi-step plan formulation (`create_or_update_plan`), file discovery, task decomposition, and worker delegation.
- **Logic Flow**:
  1. *Analysis & Plan*: Formulate architectural plan (`create_or_update_plan`) and inspect required files.
  2. *Assignment*: Dispatch precise execution tasks to specialist Workers (`W1`-`W4`).
  3. *Review*: Receive terse execution summaries from Workers, verify build/test output, and synthesize final answer.

### [Role: Worker] (Executor & Tool Operator)
- **Objective**: Precise tool execution and code generation without re-planning or conversational fluff.
- **Logic Flow**:
  1. *Execution*: Receive assigned task, enforce Ponytail Laziness Ladder, and execute file edits/commands directly.
  2. *Terse Reporting*: Output concise, production-ready code followed by an ultra-terse execution summary back to the Supervisor.

---

## 3. Communication Protocol: Offload-then-Load (OTL)

Prevents context drift and context bloat during long multi-turn interactions.

```
+------------------+     Phase A: Offload (Snapshot + Result)     +------------------+
|   Worker Agent   | -------------------------------------------> |    Supervisor    |
+------------------+                                              +------------------+
                                                                           |
                                                                  Phase B: Load (Inject Snapshot)
                                                                           v
                                                                  +------------------+
                                                                  |  New Sub-Agent   |
                                                                  +------------------+
```

### Phase A: The Offload (Worker -> Supervisor)
- **State Capture**: Identify current working set (modified files, active variables, cursor position).
- **Compression**: Generate semantic `ContextSnapshot` (compressed summary of work done).
- **Transmission**: Send `[Snapshot + Result + Pending_Questions]` to Supervisor and clear local buffer.

### Phase B: The Load (Supervisor -> New Worker)
- **Context Injection**: Inject `ContextSnapshot` into new Worker's system prompt.
- **Rehydration**: New Worker rehydrates codebase state without needing raw full conversation history.

---

## 4. Autonomous Tool Call Specifications & Target Mapping

SerenityDev supports native `<|tool_call>` syntax, short aliases, and namespace prefix stripping (`google:mcp:code_interpreter:*`, `mcp:filesystem:*`).

### Tool Target Router & Specifications

| Tool Target | Normalization Aliases | Parameters & Format | Purpose |
| :--- | :--- | :--- | :--- |
| `read_file` | `read`, `view_file`, `cat` | `{"path": "rel_path", "start_line": int, "end_line": int}` <br> *Supports: `line`, `range`, `path:1520`, `path around line 1520`* | Read file contents or line ranges. Auto-truncates to 100 lines if range unsupplied. |
| `grep_search` | `grep`, `search`, `search_files` | `{"query": "search_term"}` | Search pattern/symbol across project files. |
| `list_directory` | `list_files`, `ls`, `dir`, `list_dir` | `{"path": "dir_path"}` | List directory files, subfolders, and sizes (defaults to workspace root if omitted). |
| `write_file` | `write`, `create_file` | `{"path": "rel_path", "content": "full_text"}` | Write or overwrite file contents. |
| `insert_edit_into_file` | `insert_edit` | `{"path": "rel_path", "target_content": "old", "new_content": "new"}` | Insert replacement block into file. |
| `replace_string_in_file` | `replace_string` | `{"path": "rel_path", "target_content": "old", "new_content": "new"}` | Replace single string match in file. |
| `multi_replace_string_in_file` | `multi_replace` | `{"path": "rel_path", "replacements": [{"target": "a", "replacement": "b"}]}` | Replace multiple non-adjacent text blocks. |
| `run_command` | `exec`, `terminal`, `execute` | `{"command": "powershell_cmd"}` | Execute PowerShell command in workspace (tests, build, linter). |
| `create_or_update_plan` | `plan` | `{"steps": ["step1"], "current_focus": "focus"}` | Synchronize execution plan. |

---

## 5. Execution Pipeline, Slash Modes & Error Recovery

### Pipeline Execution Phases
1. **Supervisor Routing**: Query analysis, plan maintenance, file context retrieval.
2. **Tool Execution**: Autonomous filesystem and terminal tool execution.
3. **Worker Delegation**: Targeted context passed to specialist workers (`W1`-`W4`).
4. **Draft Synthesis & Review**: Supervisor quality review; auto re-routes to W1 refinement on failure.

### Slash Commands & Modes
- `/explore`: Read-only discovery mode. Rejects file edit/write tools.
- `/plan`: Context gathering & plan formulation mode. Rejects file modifications.
- `/execute`: Direct implementation, code editing, and build/test execution.
- `/agent`: Fully autonomous coordinator mode (direct tool execution & worker delegation).

### Error Recovery Protocols
- **Conflict Resolution**: If two Workers edit same file, Supervisor resolves diff and re-assigns.
- **Recursive Summarization**: If `ContextSnapshot` exceeds Supervisor window, apply recursive compression.
- **Path Traversal Safety**: Null byte detection, `..` path traversal block, hardware MAC PQC key binding.
