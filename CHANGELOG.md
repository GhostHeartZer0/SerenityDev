# Change Log

All notable changes to the "SerenityDev" extension will be documented in this file.

## [Unreleased]

- Initial commit

## Version 1.4.20 2026-07-20 
- Gemma-4 Chat templates updated to the july release, boasting improved benchmark scores, tool call handling, and thought handling.
- added compatibility with Windows Smart App Control and localized cache
- cleared up workflow and added TODO to CHANGELOG.

### To-Do List: (Items marked [Done] must be tested and verified, then they will be crossed out and added above)

1. resolve llm output is massdumping instead of streaming response.
   [Plan: In serenitydevserver.py, generate_completion_stream is called for worker drafts and refinement to yield chunks directly in real-time, bypassing post-facto chunking.] [Done] 

2. kv cache compression settings (add to UI and verify it works / changes) [Done] 

3. separate values for k and v: fp16, q8_0, q5_1, q5_0, q4_0, turbo4_tcq, turbo3_tcq, turbo2_tcq [Done] 

4. implement terminal command whitelist / blacklist filtering in devserver for run_command. [Done]

5. High and Low modes, split logic in two:
   - gemma-4-26B-A4B (High) explains everything it does, uses thinking to the max, as many tokens and tool uses as needed to solve the issue with utmost accuracy. Prioritizes self-optimizable pipeline: research - plan - worker selections - permission (if necessary) - execution - test.
   - gemma-4-26B-A4B (Low) minimal explanations, focus on efficiency and token savings while maintaining accuracy. Prioritizes if-necessary pipeline: research - plan - worker selections - permission - execution - test. [Done] 

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