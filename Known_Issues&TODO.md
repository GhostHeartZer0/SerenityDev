# Known Issues

1. **Extension Icon Display**: Extension icon not showing up in VSCode sidebar/marketplace.
2. **Unverified Features (Pending Test & Verification)**:
   - Real-time response streaming in `serenitydevserver.py` (bypassing post-facto chunking).
   - KV cache compression settings UI integration and backend toggles.
   - Independent K/V cache precision configuration (`fp16`, `q8_0`, `q5_1`, `q5_0`, `q4_0`, `turbo4_tcq`, `turbo3_tcq`, `turbo2_tcq`).
   - Terminal command whitelist/blacklist filtering in devserver for `run_command`.
   - High vs. Low execution mode logic split (Gemma-4-26B-A4B detailed thinking vs. minimal token overhead).
   - Dynamic temperature and sampling parameter propagation per task and worker persona.
   - Gork-build Rust architecture adaptations (Tool-Pair-Safe context trimming, Prompt Queue Manager, Sliding-window CircuitBreaker).
   - Smart App Control & localized cache environment variables (`TEMP`, `TMP`, CUDA compiler cache).

---

# TODO

## High Priority
- [ ] Fix VSCode extension icon path and asset resolution.
- [ ] Map MoE (Mixture of Experts) router sizes and parse active/total expert metadata.
- [ ] Test and optimize Gemma-4 generation parameters (temperature 0.3-0.5, repeat penalty 1.0, min_p 0.05-0.1) for code generation accuracy.
- [ ] Add Git command benchmarks to testing harness.
- [ ] Review FIM (Fill-In-Middle) handling and VRAM allocation; enable dynamic offloading for Orchestrator/Supervisor during saturation.

## Low Priority / TBD
- [ ] Implement auto-pushing verified updates.
- [ ] Auto-log git changes prior to pushing.
- [ ] Add workspace-specific queue buckets for prompt decongestion.
- [ ] Implement automatic queue cleanup for stale context/requests.
- [ ] Add auto-parameter adjustment mode in `serenitydevserver.py` based on task context (coding, multimodal, analysis).
- [ ] Complete README documentation update.
