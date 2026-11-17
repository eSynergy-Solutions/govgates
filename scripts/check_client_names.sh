#!/usr/bin/env bash
# Fails if any tracked file mentions a client name.
# Put one name per line in .client-denylist (gitignored, so the list itself never lands in the repo).
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

if [ ! -f .client-denylist ]; then
  echo "No .client-denylist found. Create one (one client name per line) before pushing." >&2
  exit 1
fi

hits=0
checked=0
# Normalise the list: Windows tools may write UTF-16 or a BOM, and CRLF line endings.
# A name with a stray \r or BOM matches nothing, which would make this check pass silently.
if [ "$(tr -d '\000' < .client-denylist | wc -c)" -ne "$(wc -c < .client-denylist)" ]; then  # has NUL bytes: UTF-16
  names=$(iconv -f UTF-16 -t UTF-8 .client-denylist)
else
  names=$(cat .client-denylist)
fi
names=$(printf '%s\n' "$names" | sed -e 's/^\xEF\xBB\xBF//' -e 's/\r$//' -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')

while IFS= read -r name; do
  [ -z "$name" ] && continue
  checked=$((checked + 1))
  if git ls-files -z | xargs -0 grep -I -n -i -w -- "$name" 2>/dev/null; then
    hits=1
  fi
done <<< "$names"

if [ "$checked" -eq 0 ]; then
  echo ".client-denylist has no usable names. Add at least one." >&2
  exit 1
fi
if [ "$hits" -ne 0 ]; then
  echo "Client name found in tracked files. Remove it before pushing." >&2
  exit 1
fi
echo "Client-name check passed."
