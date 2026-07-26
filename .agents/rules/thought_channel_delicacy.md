---
trigger: model_decision
description: When working on or around a thought channel
---

# Thought Channel Isolation
- Take extra care not to break the split between internal reasoning and output streams.
- Thoughts and internal reasoning states MUST NOT leak into the final response or user-facing output buffers.
- Keep thought processing strictly separated from public/rendered data paths.