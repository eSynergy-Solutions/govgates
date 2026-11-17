"""PreToolUse hook for Edit / Write / MultiEdit / NotebookEdit.

- Denies content that mentions a name from .client-denylist (ground rule 1), without echoing the name.
- Asks before any change to a protected file: the prompt, the abbreviation dictionary, the persona
  matrix, the golden-set sources and the guards themselves. The lead approves or writes it.
"""

from __future__ import annotations

import sys

from _common import decide, denylist, is_protected, mentions_client, project_dir, read_event, rel_path


def new_text(tool_input: dict) -> str:
    parts = [tool_input.get("content"), tool_input.get("new_string"), tool_input.get("new_source")]
    parts += [e.get("new_string") for e in tool_input.get("edits") or [] if isinstance(e, dict)]
    return "\n".join(p for p in parts if isinstance(p, str))


def main() -> None:
    event = read_event()
    tool_input = event.get("tool_input") or {}
    root = project_dir(event)
    path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""

    names = denylist(root)
    if names and (mentions_client(new_text(tool_input), names) or mentions_client(path, names)):
        decide("deny", "This change contains a name from .client-denylist. Use generic naming (ground rule 1).")

    rel = rel_path(path, root)
    if rel and is_protected(rel):
        decide("ask", f"{rel} is protected: a human writes it. Approve only if you asked for this exact change.")

    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:  # fail open on hook bugs
        print(f"guard_edit hook error (ignored): {exc}", file=sys.stderr)
        sys.exit(0)
