# Agent Rules - Ponytail Integration

## Sacred Rules

1. **serenity_resources.py**: Explicit permission is required to edit the file `serenity_resources.py`. When faced with doing so, instead provide a plan or several to the user for approval.

## Execution Rules (The Ponytail Laziness Ladder)

Before writing any code, walk this seven-rung ladder:
1. **YAGNI (Does this need to exist?)**: If not, skip it.
2. **Codebase Reuse**: If it exists in the codebase, reuse it. Do not rewrite.
3. **Standard Library**: If the language stdlib can do it, use it.
4. **Native Platform Feature**: If a native platform or browser feature does it, use it.
5. **Installed Dependency**: If an installed dependency does it, use it.
6. **One Line**: If it can be done in one line, do it in one line.
7. **Minimum Works**: Only write the absolute minimum necessary code.

*Note: The ladder runs after fully understanding the problem and reading all relevant code.*

## Quality and Safety Guarantees
Never compromise on:
- Trust-boundary validation & input sanitization
- Error handling and recovery paths
- Security and access controls
- Accessibility (a11y)
- Existing code documentation, comments, and docstrings
