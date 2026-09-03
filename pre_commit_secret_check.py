#!/usr/bin/env python3
"""
Pre-commit hook to scan staged files for potential secrets and credentials.
Walks the staged diff and checks for patterns such as API keys, private keys,
and blocked files like .env or vault databases.
"""

import sys
import re
import subprocess

BLOCKED_FILES = [
    r"^\.env$",
    r"^.*\.env(\..+)?$",
    r"^.*\.pem$",
    r"^.*\.key$",
    r"^.*\.crt$",
    r"^.*\.pfx$",
    r"^.*\.p12$",
    r"^.*\.vsix$",
    r"^System/vault/.*",
    r"^System/config\.json$",
    r"^.*\.sqlite3?$",
]

SECRET_PATTERNS = [
    (r"(?i)(api[_-]?key|access[_-]?token|auth[_-]?token|secret[_-]?key)\s*[:=]\s*['\"][A-Za-z0-9_\-\.]{16,}['\"]", "High-entropy API/auth token"),
    (r"-----BEGIN (RSA|EC|DSA|OPENSSH|PRIVATE) KEY-----", "Unencrypted private key"),
    (r"(?i)bearer\s+[A-Za-z0-9_\-\.]{24,}", "Bearer token"),
    (r"(?i)ghp_[A-Za-z0-9]{36}", "GitHub personal access token"),
    (r"(?i)sk-[A-Za-z0-9]{32,}", "OpenAI API key"),
    (r"(?i)AIza[0-9A-Za-z-_]{35}", "Google API key"),
]

def check_staged_files():
    try:
        res = subprocess.check_output(["git", "diff", "--cached", "--name-only"], text=True)
    except Exception as e:
        print(f"[SECURITY CHECK] Failed to check staged files: {e}")
        return 0

    staged_files = [f.strip() for f in res.splitlines() if f.strip()]
    if not staged_files:
        return 0

    violations = []

    for filename in staged_files:
        for blocked_pat in BLOCKED_FILES:
            if re.match(blocked_pat, filename, re.IGNORECASE):
                violations.append(f"Blocked file staged: {filename} matches sensitive pattern '{blocked_pat}'")

    for filename in staged_files:
        try:
            diff_text = subprocess.check_output(
                ["git", "diff", "--cached", "-U0", "--", filename],
                text=True,
                errors="replace"
            )
        except Exception:
            continue

        for line in diff_text.splitlines():
            if not line.startswith("+") or line.startswith("+++"):
                continue
            added_content = line[1:]
            for pat, desc in SECRET_PATTERNS:
                if re.search(pat, added_content):
                    violations.append(f"Potential secret in {filename}: matches {desc}")
                    break

    if violations:
        print("\n" + "=" * 70)
        print("[SECURITY CHECK ERROR] Commit rejected. Potential secrets detected:")
        for v in violations:
            print(f" - {v}")
        print("=" * 70)
        print("If this is a false positive, verify the file or use 'git commit --no-verify'.\n")
        return 1

    return 0

if __name__ == "__main__":
    sys.exit(check_staged_files())
