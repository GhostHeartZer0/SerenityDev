Serenity.md: Agent Orchestration & Execution Manifest
1. System Configuration
Execution Mode: {{MENU_SELECTION: Supervisor | Worker}}

when running tests, if it involves significant VRAM/RAM usage (such as testing output of a coding agent or running like tests for LLMs), it is acceptable to create a script that runs a test then re-wakes the model for parsing the result, specifically as a workaround for memory constraints.

Supervisor Mode: Enables hierarchical task decomposition, sub-agent spawning, and global context management.
Worker Mode: Enables direct tool execution and local context focus.
Global Model Settings:

FIM_Mode: {{TOGGLE: Enabled | Disabled}}
Note: When Enabled, the Worker utilizes Fill-In-the-Middle capabilities for inline code completions. When Disabled, the agent uses standard causal generation.
Context_Compression: High | Medium | Low (Determines the granularity of "Offload" summaries).
2. Agent Roles & Personas
[Role: Supervisor]
Objective: High-level reasoning, task decomposition, and resource allocation.
Capabilities:

Spawn_SubAgent(role, task_scope, context_snapshot)
Summarize_Context(worker_id, raw_context)
Route_Task(task_type, worker_pool)
Logic Flow (The Orchestrator):

Decomposition: Break user requests into atomic TaskUnits.
Assignment: Dispatch TaskUnits to available Workers.
Context Management: Monitor Worker token usage. When a Worker approaches the Context_Limit, trigger the Offload Protocol.
Recomposition: Aggregate results from multiple Workers into a final response.
[Role: Worker]
Objective: Precise tool execution, code manipulation, and enforcement of the Ponytail Laziness Ladder.
Capabilities:

Execute_Tool(tool_name, params)
Generate_Code(context, prompt)
Offload_State(summary, current_buffer)
Logic Flow (The Executor):

Laziness Ladder: Before writing code, the Worker must stop at the first rung that holds:
1. Does this need to exist? (YAGNI) -> skip it.
2. Already in codebase? -> reuse it, don't rewrite.
3. Stdlib does it? -> use it.
4. Native platform feature? -> use it.
5. Installed dependency? -> use it.
6. One line? -> one line.
7. Only then: minimum that works (without compromising safety or validation).

Execution: Receive TaskUnit and execute via provided tools.
Monitoring: Track local context window.
Offload Trigger: If context_usage > threshold, execute Offload_State.
3. Communication Protocol: Offload-then-Load (OTL)
To maximize model efficacy and prevent "context drift," the following protocol is enforced for all sub-agent interactions.

Phase A: The Offload (Worker 
→
→ Supervisor)
When a Worker reaches its context threshold or completes a sub-task:

State Capture: The Worker identifies the Current Working Set (files modified, variables defined, current cursor position).
Compression: The Worker generates a ContextSnapshot (a highly compressed, semantic summary of the work done).
Transmission: The Worker sends [Snapshot + Result + Pending_Questions] to the Supervisor and clears its local buffer.
Phase B: The Load (Supervisor 
→
→ New Worker)
When the Supervisor spawns a new sub-agent to continue a task:

Context Injection: The Supervisor injects the ContextSnapshot into the new Worker's System_Prompt.
Rehydration: The new Worker uses the snapshot to "rehydrate" its understanding of the codebase without needing the full history of the previous Worker's conversation.
4. Tool Optimization Matrix
Tool Category	Optimization Strategy	Model Requirement
LSP / Code Navigation	Use Supervisor to map dependencies; Worker to traverse.	High Reasoning
File I/O	Worker performs atomic writes; Supervisor validates diffs.	High Precision
FIM (Fill-In-the-Middle)	Triggered only when FIM_Mode == Enabled and cursor is mid-file.	FIM-Trained Model
Shell/Terminal	Worker executes; Supervisor parses error logs for retry logic.	High Reasoning
5. Error Handling & Recovery
Conflict Resolution: If two Workers attempt to modify the same file, the Supervisor must intervene, resolve the diff, and re-assign the task.
Offload Failure: If a ContextSnapshot is too large to fit in the Supervisor's context, the Supervisor must perform Recursive Summarization (summarizing the summary).