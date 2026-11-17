"""Statement Execution API runner behind scripts/sql.sh. Dev warehouse only; prints a compact table.

Substitutes ${catalog}, ${raw_schema}, ${gov_schema}, ${ref_schema}, ${curated_schema} from env or the
bundle defaults, so spike and check SQL can use the same placeholders as the bundle.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

DEFAULTS = {"catalog": "gate_sandbox", "raw_schema": "raw", "gov_schema": "gov",
            "ref_schema": "ref", "curated_schema": "curated"}
CACHE = Path(".databricks/sql_warehouse_id")
POST_PATH = "/api/2.0/sql/statements/"          # trailing slash is required
GET_PATH = "/api/2.0/sql/statements/{id}"

# Things this fast path must never do. Use a reviewed job for anything like this.
BLOCKED = [
    (r"\bDROP\s+CATALOG\b", "DROP CATALOG"),
    (r"\bDROP\s+SCHEMA\b[^;]*\b(gov|raw|ref|curated)\b", "DROP SCHEMA on a product schema"),
    (r"\b(DELETE\s+FROM|TRUNCATE\s+TABLE|UPDATE)\b[^;]*\baudit_log\b", "changing gov.audit_log"),
    (r"\bDROP\s+GOVERNED\s+TAG\b", "DROP GOVERNED TAG (account-wide)"),
]


def cli() -> str:
    exe = shutil.which("databricks")
    if not exe:
        sys.exit("databricks CLI not on PATH")
    return exe


def api(method: str, path: str, body: dict | None = None) -> dict:
    args = [cli(), "api", method, path]
    tmp = None
    if body is not None:
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        json.dump(body, tmp)
        tmp.close()
        args += ["--json", f"@{tmp.name}"]
    env = dict(os.environ, MSYS_NO_PATHCONV="1")
    try:
        out = subprocess.run(args, capture_output=True, text=True, env=env)
    finally:
        if tmp:
            os.unlink(tmp.name)
    if out.returncode != 0:
        sys.exit(f"databricks api {method} {path} failed:\n{out.stderr.strip() or out.stdout.strip()}")
    return json.loads(out.stdout or "{}")


def warehouse_id() -> str:
    if os.environ.get("SQL_WAREHOUSE_ID"):
        return os.environ["SQL_WAREHOUSE_ID"]
    profile = os.environ.get("DATABRICKS_CONFIG_PROFILE", "default")
    name = os.environ.get("SQL_WAREHOUSE_NAME", "testing")
    cache = CACHE.with_name(f"sql_warehouse_id.{profile}.{name}")
    if cache.exists():
        return cache.read_text().strip()
    data = api("get", "/api/2.0/sql/warehouses")
    for wh in data.get("warehouses", []):
        if wh.get("name") == name:
            cache.parent.mkdir(exist_ok=True)
            cache.write_text(wh["id"])
            return wh["id"]
    sys.exit(f"No SQL warehouse named {name!r}. Set SQL_WAREHOUSE_ID or SQL_WAREHOUSE_NAME.")


def substitute(sql: str) -> str:
    def rep(m: re.Match) -> str:
        key = m.group(1)
        if key not in DEFAULTS:
            return m.group(0)
        return os.environ.get(key, DEFAULTS[key])
    return re.sub(r"\$\{(\w+)\}", rep, sql)


def split_statements(text: str) -> list[str]:
    """Split on ';' at end of line. Keep each statement's own lines together. Strip '--' comment lines."""
    stmts, buf = [], []
    for line in text.splitlines():
        if line.strip().startswith("--") and not buf:
            continue
        buf.append(line)
        if line.rstrip().endswith(";"):
            s = "\n".join(buf).strip().rstrip(";").strip()
            if s:
                stmts.append(s)
            buf = []
    tail = "\n".join(buf).strip().rstrip(";").strip()
    if tail and not all(line.strip().startswith("--") or not line.strip() for line in buf):
        stmts.append(tail)
    return stmts


def check_blocked(sql: str) -> None:
    for pat, what in BLOCKED:
        if re.search(pat, sql, re.I | re.S):
            sys.exit(f"Refused: {what}. Not allowed on the SQL fast path; use a reviewed job.")


def run_one(sql: str, wh: str, max_rows: int, timeout: int) -> bool:
    resp = api("post", POST_PATH, {
        "warehouse_id": wh, "statement": sql, "wait_timeout": "50s",
        # Pin the session catalog: the warehouse's own default catalog is not ours.
        "catalog": os.environ.get("catalog", DEFAULTS["catalog"]),
        "on_wait_timeout": "CONTINUE", "disposition": "INLINE", "format": "JSON_ARRAY",
    })
    sid = resp.get("statement_id")
    deadline = time.time() + timeout
    while resp.get("status", {}).get("state") in ("PENDING", "RUNNING"):
        if time.time() > deadline:
            api("post", GET_PATH.format(id=sid) + "/cancel", {})
            print(f"TIMEOUT after {timeout}s (statement {sid} cancelled)")
            return False
        time.sleep(2)
        resp = api("get", GET_PATH.format(id=sid))
    state = resp.get("status", {}).get("state")
    first = sql.strip().splitlines()[0][:100]
    if state != "SUCCEEDED":
        err = resp.get("status", {}).get("error", {})
        print(f"FAILED  {first}\n  {err.get('error_code', '')}: {err.get('message', state)}")
        return False
    cols = [c["name"] for c in resp.get("manifest", {}).get("schema", {}).get("columns", [])]
    rows = (resp.get("result") or {}).get("data_array") or []
    total = resp.get("manifest", {}).get("total_row_count", len(rows))
    print(f"OK      {first}")
    if cols:
        print("\t".join(cols))
        for r in rows[:max_rows]:
            print("\t".join("NULL" if v is None else str(v) for v in r))
        more = f" (showing {max_rows})" if total > max_rows else ""
        print(f"-- {total} row(s){more}")
    return True


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        print("usage: scripts/sql.sh \"<sql>\" | -f <file.sql> | -")
        return 2
    if argv[0] == "-p":                      # profile flag, so permission rules can match the command
        os.environ["DATABRICKS_CONFIG_PROFILE"] = argv[1]
        argv = argv[2:]
    if argv[0] == "-f":
        text = sys.stdin.read() if argv[1] == "-" else Path(argv[1]).read_text(encoding="utf-8")
    elif argv[0] == "-":
        text = sys.stdin.read()
    else:
        text = " ".join(argv)
    stmts = split_statements(substitute(text))
    for s in stmts:
        check_blocked(s)
    wh = warehouse_id()
    max_rows = int(os.environ.get("SQL_MAX_ROWS", "50"))
    timeout = int(os.environ.get("SQL_TIMEOUT", "300"))
    for s in stmts:
        if not run_one(s, wh, max_rows, timeout):
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
