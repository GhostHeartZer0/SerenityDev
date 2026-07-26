---
trigger: model_decision
description: on_event(git_commit_prepare)
---

# Pre-Commit Checklist

- Run `npm run lint` and verify zero errors before completing the commit message.
- Ensure no raw secrets, `.env` variables, or console logs are included in the diff.
- scan the codebase to ensure all tests, scratch files, temp folders, and folders starting with a dot (.) are included in .gitignore.