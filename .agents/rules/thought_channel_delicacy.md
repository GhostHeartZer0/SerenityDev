---
trigger: model_decision
description: When working on or around a thought channel
---

# Thought Channel Isolation
- Take extra care not to break the split between internal reasoning and actual response.
- Thoughts and internal reasoning states MUST NOT leak into the actual response or user-facing output buffers.
- Ensure reasoning/thoughts are streamed directly to a dedicated section, not leak into response.
- Run tests to verify clean handling of thoughts vs response for each LLM in models dir.
