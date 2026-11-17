#!/usr/bin/env bash
# Public-release scan. Run before anything goes to the public repo.
#
# Checks, against .client-denylist (whole words) and .release-denylist (substrings, e.g. internal
# repo names, tool names, our own workspace host), plus built-in patterns for hosts and secrets:
#   1. HEAD: every tracked file.               Any hit = FAIL (the release ships HEAD).
#   2. History: every commit on every branch,  Hits in the internal repo: never push it, release only through
#      scripts/make_release.py. Hits inside a release export: stop and escalate (published history can't change).
#      commit messages and branch names.
#   2b. Identities: commit author/committer and tag tagger names and emails (and, with
#      GG_RELEASE_EMAIL set, that every one uses that email).
#   3. Release basics: LICENSE, README.
#
# Names are never printed. A hit shows as "term #N" with its location, so the output is safe to paste.
# Exit 0 = ready, 1 = not ready, 2 = setup problem.
set -uo pipefail
cd "$(git rev-parse --show-toplevel)"
G="git -c safe.directory=* --no-optional-locks"

read_list() {  # normalise UTF-16/BOM/CRLF like check_client_names.sh
  local f=$1
  [ -f "$f" ] || return 0
  if [ "$(tr -d '\000' < "$f" | wc -c)" -ne "$(wc -c < "$f")" ]; then iconv -f UTF-16 -t UTF-8 "$f"; else cat "$f"; fi \
    | sed -e 's/^\xEF\xBB\xBF//' -e 's/\r$//' -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e '/^#/d' -e '/^$/d'
}

# GG_DENYLIST_DIR: where the gitignored denylists live (make_release.py points it at the source repo, so they
# are never copied into the export). Default: this repo.
DL=${GG_DENYLIST_DIR:-.}
mapfile -t CLIENT < <(read_list "$DL/.client-denylist")
mapfile -t RELEASE < <(read_list "$DL/.release-denylist")
if [ "${#CLIENT[@]}" -eq 0 ]; then echo "SETUP: .client-denylist missing or empty" >&2; exit 2; fi
[ "${#RELEASE[@]}" -eq 0 ] && echo "WARN: .release-denylist missing or empty (internal names, tools, hosts won't be checked)"

# Built-in regex patterns: workspace hosts and credentials. Generic, safe to commit.
PATTERNS=(
  'adb-[0-9]{6,}\.[0-9]+\.azuredatabricks\.net'
  '[a-z0-9-]+\.cloud\.databricks\.com'
  '[a-z0-9-]+\.gcp\.databricks\.com'
  'dapi[0-9a-f]{32}'
  'dose[0-9a-f]{32}'
  '-----BEGIN [A-Z ]*PRIVATE KEY-----'
  'client_secret[[:space:]]*[:=][[:space:]]*["'"'"']?[A-Za-z0-9._~-]{16,}'
)

head_fail=0; hist_hits=0; n=0
declare -a TERMS FLAGS
for t in "${CLIENT[@]}";  do TERMS+=("$t"); FLAGS+=("-F -i -w"); done
for t in "${RELEASE[@]}"; do TERMS+=("$t"); FLAGS+=("-F -i"); done
for t in "${PATTERNS[@]}"; do TERMS+=("$t"); FLAGS+=("-E -i"); done
nclient=${#CLIENT[@]}; nrel=${#RELEASE[@]}

label() { local i=$1
  if [ "$i" -le "$nclient" ]; then echo "client term #$i"
  elif [ "$i" -le $((nclient+nrel)) ]; then echo "release term #$((i-nclient))"
  else echo "pattern #$((i-nclient-nrel))"; fi; }

# One pass over all history: every added/removed line, prefixed with its path.
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
$G log --all -p --no-color --no-ext-diff --format='' \
  | awk '/^diff --git /{f=substr($4,3); next} /^(\+\+\+|---) /{next} /^[+-]/{print f "\t" substr($0,2)}' > "$TMP/hist"
$G log --all --format='%h %s%n%b' > "$TMP/msgs"
$G for-each-ref --format='%(refname:short)' > "$TMP/refs"

echo "== 1. HEAD (tracked files)"
for i in "${!TERMS[@]}"; do
  n=$((i+1)); t=${TERMS[$i]}; f=${FLAGS[$i]}
  hits=$($G grep -I -n $f -e "$t" -- . 2>/dev/null | cut -d: -f1,2 | sort -u)
  if [ -n "$hits" ]; then
    head_fail=1; c=$(printf '%s\n' "$hits" | wc -l)
    echo "  FAIL $(label $n): $c line(s)"; printf '%s\n' "$hits" | head -15 | sed 's/^/    /'
    [ "$c" -gt 15 ] && echo "    ... and $((c-15)) more"
  fi
done
[ "$head_fail" -eq 0 ] && echo "  clean"

echo "== 2. History (all branches, commit messages, branch names)"
for i in "${!TERMS[@]}"; do
  n=$((i+1)); t=${TERMS[$i]}; f=${FLAGS[$i]}
  files=$(grep $f -e "$t" "$TMP/hist" 2>/dev/null | cut -f1 | sort -u)   # path or content
  msgs=$(grep -c $f -e "$t" "$TMP/msgs" 2>/dev/null); msgs=${msgs:-0}
  refs=$(grep -c $f -e "$t" "$TMP/refs" 2>/dev/null); refs=${refs:-0}
  if [ -n "$files" ] || [ "$msgs" -gt 0 ] || [ "$refs" -gt 0 ]; then
    hist_hits=1
    nf=$( [ -n "$files" ] && printf '%s\n' "$files" | wc -l || echo 0)
    echo "  HIT  $(label $n): $nf file path(s) in history, $msgs commit-message line(s), $refs branch name(s)"
    [ -n "$files" ] && printf '%s\n' "$files" | head -8 | sed 's/^/    /'
  fi
done
[ "$hist_hits" -eq 0 ] && echo "  clean"

# Author, committer and tagger names/emails are public on the remote and survive force-pushes.
echo "== 2b. Identities (commit authors, committers, taggers)"
id_fail=0
{ $G log --all --format='%an <%ae>%n%cn <%ce>'; $G for-each-ref refs/tags --format='%(taggername) %(taggeremail)'; }   | sed '/^[[:space:]]*$/d' | sort -u > "$TMP/ids"
for i in "${!TERMS[@]}"; do
  n=$((i+1)); t=${TERMS[$i]}; f=${FLAGS[$i]}
  c=$(grep -c $f -e "$t" "$TMP/ids" 2>/dev/null); c=${c:-0}
  [ "$c" -gt 0 ] && { id_fail=1; echo "  HIT  $(label $n): $c identity line(s)"; }
done
# GG_RELEASE_EMAIL set (make_release.py sets it): every identity must use exactly that email.
if [ -n "${GG_RELEASE_EMAIL:-}" ]; then
  other=$(grep -v -F -e "<${GG_RELEASE_EMAIL}>" "$TMP/ids" | wc -l)
  [ "$other" -gt 0 ] && { id_fail=1; echo "  FAIL $other identity line(s) not using the agreed release email"; }
fi
echo "  $(wc -l < "$TMP/ids") distinct identity line(s)"
[ "$id_fail" -eq 0 ] && echo "  clean"

echo "== 3. Release basics"
basics=0
[ -f LICENSE ] || [ -f LICENSE.md ] || { echo "  MISSING LICENSE (needs a licence decision)"; basics=1; }
[ -f README.md ] || { echo "  MISSING README.md"; basics=1; }
[ "$basics" -eq 0 ] && echo "  ok"

echo "== Verdict"
# In the export (GG_RELEASE_EMAIL set) identities are what gets published: a hit is NOT READY. In the
# internal repo they are history, cleared by a fresh-history export with the agreed identity.
id_blocks=0
if [ "$id_fail" -ne 0 ]; then
  if [ -n "${GG_RELEASE_EMAIL:-}" ]; then id_blocks=1; else hist_hits=1; fi
fi
if [ "$head_fail" -ne 0 ] || [ "$basics" -ne 0 ] || [ "$id_blocks" -ne 0 ]; then
  echo "NOT READY: fix HEAD hits, identities and basics above."
  [ "$hist_hits" -ne 0 ] && echo "History also has hits: never push this history; release only through make_release.py."
  exit 1
fi
if [ "$hist_hits" -ne 0 ]; then
  echo "HEAD CLEAN, HISTORY DIRTY: internal repo -> release only through make_release.py; release export -> stop and escalate, never force-push."
  exit 1
fi
echo "READY"
