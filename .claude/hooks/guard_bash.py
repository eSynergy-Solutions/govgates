"""PreToolUse hook for Bash.

- Blocks `databricks bundle destroy` and any bundle deploy/run/destroy against a target other than dev.
- Blocks `git push` while a tracked file mentions a name from .client-denylist.
- Asks before a shell command writes to a protected file (redirects, sed -i, tee, cp, mv, rm).
"""

from __future__ import annotations

import re
import sys

from _common import (
    SHELL_MARKERS,
    decide,
    denylist,
    project_dir,
    read_event,
    tracked_files_with_client,
)

BUNDLE = re.compile(r"\bdatabricks\s+bundle\s+(deploy|run|destroy)\b(.*)", re.IGNORECASE)
TARGET = re.compile(r"(?:-t|--target)(?:\s+|=)([\w.-]+)")
WRITE_HINT = re.compile(
    r">|\btee\b|\bsed\s+-i|\bcp\b|\bmv\b|\brm\b|\binstall\b|\btouch\b|\bgit\s+(checkout|restore|apply|mv|rm)\b"
    r"|\b(Set|Add|Clear)-Content\b|\bOut-File\b|\b(Copy|Move|New|Remove|Rename)-Item\b"
    r"|open\([^)]*['\"][wax]",
    re.IGNORECASE,
)


def main() -> None:
    event = read_event()
    cmd = (event.get("tool_input") or {}).get("command") or ""
    if not cmd:
        sys.exit(0)
    root = project_dir(event)

    for verb, rest in BUNDLE.findall(cmd):
        if verb.lower() == "destroy":
            decide("deny", "bundle destroy is never run by the agent. Ask the lead to run it themselves.")
        targets = TARGET.findall(rest)
        if any(t.lower() != "dev" for t in targets):
            decide("deny", f"Only the dev target is allowed (got {', '.join(targets)}). A person deploys the rest.")

    if re.search(r"\bgit\s+push\b", cmd):
        names = denylist(root)
        if not names:
            decide("deny", "No .client-denylist found. Create it (one name per line) before anything is pushed.")
        hits = tracked_files_with_client(root, names)
        if hits:
            decide("deny", "Client name found in tracked files: " + ", ".join(hits) + ". Remove it before pushing.")

    lowered = cmd.replace("\\", "/").lower()
    touched = [m for m in SHELL_MARKERS if m in lowered]
    if touched and WRITE_HINT.search(cmd):
        decide("ask", "This command may change a protected file (" + ", ".join(touched) + "). The lead decides.")

    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:  # fail open on hook bugs
        print(f"guard_bash hook error (ignored): {exc}", file=sys.stderr)
        sys.exit(0)
