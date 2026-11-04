#!/usr/bin/env bash
#
# hub_test.sh — ferry end to end: fake machines, each with its own HOME and
# config folder, syncing through scratch hubs in a temp dir. Nothing outside it
# is touched; no real hub or config is ever read. Takes about two minutes.
#
#   tests/hub_test.sh
#   FERRY_TEST_SHARE=DIR tests/hub_test.sh
#
# The second puts the scratch hubs in a fresh folder under DIR, a folder on a
# real share, to try its locking, renames and listings; the folder goes after
# a clean run.
#
# Part 1 is the everyday: enroll, chains, hooks, merges, conflicts, budgets, an
# unreachable hub, rekey, retire, a move to a new hub. Part 2 is compaction.
# Each prints PASS/FAIL per check; the temp dir is kept when anything fails.
set -uo pipefail

TOOL=$(cd -- "$(dirname -- "$0")/.." && pwd -P)
ROOT=$(cd -- "$(mktemp -d)" && pwd -P)       # physical, as ferry resolves it
HUBS=$ROOT
if [ -n "${FERRY_TEST_SHARE:-}" ]; then
  HUBS=$(mktemp -d "$FERRY_TEST_SHARE/ferry-test.XXXXXX") || exit 1
  echo "hubs on the share: $HUBS"
fi
unset GIT_DIR GIT_WORK_TREE
# ferry as installed: this checkout, in a venv of the test's own, on the PATH
# where the post-commit hooks look for it.
UV_PROJECT_ENVIRONMENT=$ROOT/venv uv sync --quiet --project "$TOOL" || exit 1
BIN=$ROOT/venv/bin; [ -d "$BIN" ] || BIN=$ROOT/venv/Scripts
export PATH="$BIN:$PATH"
pass=0; fail=0

check() { if eval "$2"; then echo "PASS  $1"; pass=$((pass+1)); else echo "FAIL  $1"; fail=$((fail+1)); fi; }
q()     { "$@" >/dev/null 2>&1; }
as()    { local x=$1; shift; HOME=$T/$x/home XDG_CONFIG_HOME=$T/$x/conf "$@"; }   # a command as machine x
m()     { local x=$1; shift; as "$x" ferry -C "$(R "$x")" "$@"; }
mg()    { local x=$1 r=$2; shift 2; m "$x" git "$r" "$@"; }
gx()    { as "$@"; }                           # git as that machine: its commits fire its hooks
R()     { printf '%s/%s/proj' "$T" "$1"; }
key()   { printf '%s/%s/conf/ferry/proj' "$T" "$1"; }      # where a machine keeps the project's key
src()   { printf '%s/source' "$(R "$1")"; }
# A path as ferry sees it: in Git Bash, the Windows path, with long names.
native() { if command -v cygpath >/dev/null; then cygpath -w -l "$1"; else printf '%s\n' "$1"; fi; }
# Where Claude Code keeps a machine's memory: every character of the folder's
# path but an ASCII letter or digit becomes "-" (no path here passes 200).
memdir() { printf '%s/%s/home/.claude/projects/%s/memory' "$T" "$1" "$(native "$(R "$1")" | LC_ALL=C sed 's/[^A-Za-z0-9]/-/g')"; }
rnd()   { head -c "$2" /dev/urandom > "$1"; }
has()   { mg "$1" source cat-file -e "$2" 2>/dev/null; }
slot()  { du -sk "$H/$1/$2" | cut -f1; }      # KB in a machine's slot for a repo
settle() { local x; sleep 2; for x in "$@"; do while [ -d "$(R "$x")/.ferry/lock" ]; do sleep 0.5; done; done; }
# The first line of a slot's manifest: the format's name.
magic() { "$BIN/python" -c 'import sys; from pathlib import Path; from ferry.crypto import Keyring
print(Keyring.load(Path(sys.argv[1])).open_text(Path(sys.argv[2])).split("\n")[0])' "$(native "$(key A)")" "$(native "$H/$1/$2/manifest.age")"; }

setup() { # a fresh hub at $H, with machine A on it at $T
  mkdir -p "$T"
  local A; A=$(R A)
  mkdir -p "$T/A/home" "$T/A/conf" "$A"
  printf '%s\n' 'name = "proj"' '[repos.lab]' 'path = "lab"' \
    '[repos.memory]' 'claude-memory = true' 'auto = true' 'union = ["MEMORY.md"]' \
    '[repos.source]' 'path = "source"' 'auto = true' '[repos.personal]' 'path = "personal"' 'auto = true' > "$A/ferry.toml"
  printf 'source/\npersonal/\nlab/\n.ferry/\n' > "$A/.gitignore"; echo notes > "$A/notes.md"
  git -C "$A" init -q && git -C "$A" config user.email t@t && git -C "$A" add -A && git -C "$A" commit -qm init
  git init -q "$A/lab" && git -C "$A/lab" config user.email t@t
  echo wb > "$A/lab/x.py" && git -C "$A/lab" add -A && git -C "$A/lab" commit -qm wb
  mkdir -p "$A/source" "$A/personal" "$(memdir A)"
  echo alpha > "$A/source/a.txt"; rnd "$A/source/big1.bin" 3000000
  echo grant > "$A/personal/g.txt"; echo '- one' > "$(memdir A)/MEMORY.md"
  m A init --hub "$H" --host A
}
add() { # <name> — enroll another machine with A's key
  mkdir -p "$T/$1/home" "$(key "$1")"
  cp -f "$(key A)/identity" "$(key "$1")/identity"
  as "$1" ferry enroll proj --hub "$H" --dest "$(R "$1")" --host "$1"
  git -C "$(R "$1")" config user.email t@t; git -C "$(R "$1")/lab" config user.email t@t
}

# --- part 1: the everyday ------------------------------------------------------
T=$ROOT/daily H=$HUBS/daily-hub; out=$(setup 2>&1)
check "init publishes the first machine" '[[ $out == *"A is the first machine on the hub"* ]] && [ "$(cat "$H/FORMAT")" = "ferry 1" ]'
out=$(add B 2>&1); q add C
check "enroll copies every repo" '[[ $out == *"B is enrolled: proj is at"* ]] && diff -r "$(src A)" "$(src B)" >/dev/null && diff "$(memdir A)/MEMORY.md" "$(memdir B)/MEMORY.md" >/dev/null'

echo beta >> "$(src A)/a.txt"; out=$(m A sync 2>&1)
check "a push extends the chain" '[[ $out == *"source → hub: +"*"pack 2 of the chain"* ]]'
out=$(m B pull --hook 2>&1)
check "the session hook fast-forwards" '[[ $out == "ferry: source ← A: fast-forward, 1 commit(s)" ]]'
out=$(m B pull --hook 2>&1)
check "... and is quiet when there's nothing" '[[ $out == "ferry: up to date with the hub (other machines: A C)" ]]'
check "manifests name their format" '[ "$(magic A root)" = "ferry manifest 1" ]'

echo more >> "$(R A)/notes.md"; gx A git -C "$(R A)" commit -qam notes; settle A
check "a root commit pushes from the hook" 'grep -q "root → hub: +" "$(R A)/.ferry/log"'
out=$(m B sync 2>&1)
check "root fast-forwards elsewhere" '[[ $out == *"root ← A: fast-forward, 1 commit(s)"* ]]'
echo y > "$(R B)/lab/y.py"; gx B git -C "$(R B)/lab" add -A; gx B git -C "$(R B)/lab" commit -qm y; settle B
out=$(m A pull 2>&1)
check "lab does too" '[[ $out == *"lab ← B: fast-forward, 1 commit(s)"* ]]'

echo b >> "$(R B)/notes.md"; gx B git -C "$(R B)" commit -qam "b notes"; settle B
echo a > "$(R A)/other.md"; gx A git -C "$(R A)" add other.md; gx A git -C "$(R A)" commit -qm "a other"; settle A
out=$(m B sync 2>&1)
check "diverged root is never merged automatically" '[[ $out == *"root: diverged from A (1 commit(s) here, 1 there), not merged automatically"* ]]'
q mg B root merge -q --no-edit refs/hub/A/master; q m B sync; out=$(m A sync 2>&1)
check "... and resolves by hand" '[[ $out == *"root ← B: fast-forward, 2 commit(s)"* ]]'

echo fromA > "$(src A)/xa.txt"; echo fromB > "$(src B)/xb.txt"; q m A sync; out=$(m B sync 2>&1)
check "auto repos merge" '[[ $out == *"source ← A: merged 1 commit(s)"* ]]'
q m A sync
echo A-side > "$(src A)/a.txt"; echo B-side > "$(src B)/a.txt"; q m A sync; out=$(m B sync 2>&1)
check "a conflict is left unmerged" '[[ $out == *"source: conflicts with A, left unmerged"* ]] && [ "$(cat "$(src B)/a.txt")" = B-side ]'
q mg B source merge -q --no-edit refs/hub/A/master; echo resolved > "$(src B)/a.txt"
q mg B source add a.txt; q mg B source commit -qm resolved; q m B sync; q m A sync
check "... and resolves by hand" '[ "$(cat "$(src A)/a.txt")" = resolved ]'
echo '- from A' >> "$(memdir A)/MEMORY.md"; echo '- from B' >> "$(memdir B)/MEMORY.md"
q m A sync; q m B sync; q m A sync
check "MEMORY.md merges as a union" 'grep -q "from A" "$(memdir A)/MEMORY.md" && grep -q "from B" "$(memdir A)/MEMORY.md"'

q m C sync
for i in 1 2 3 4; do echo "r$i" >> "$(src A)/a.txt"; FERRY_MAXCHAIN=3 m A push --only source >/dev/null 2>&1; done
check "a full chain restarts" '[ "$(ls "$H/A/source/"*.pack.age | wc -l | tr -d " ")" -le 3 ]'
out=$(m C pull 2>&1)
check "a reader that kept up skips the new base" '[[ $out == *"source ← A: "*" KB"* && $out != *" MB"* ]] && [ "$(tail -1 "$(src C)/a.txt")" = r4 ]'

rnd "$(src A)/big2.bin" 3000000; q m A sync
out=$(FERRY_FOREGROUND_MB=1 m C pull --hook 2>&1)
check "a session start defers a big download" '[[ $out == *"is more than a session start waits for — downloading in the background"* ]] && [ ! -f "$(src C)/big2.bin" ]'
settle C; sleep 2; settle C; q m C pull --hook
check "... which lands at the next one" '[ -f "$(src C)/big2.bin" ]'

mv "$H" "$H.away"; echo offline >> "$(src A)/a.txt"
out=$(m A sync 2>&1); code=$?
check "an unreachable hub fails at the terminal" '[ $code -eq 1 ] && [[ $out == *"hub not reachable"*"Not on the hub yet: source."* ]]'
out=$(m A pull --hook 2>&1); code=$?
check "... and only warns the session" '[ $code -eq 0 ] && [[ $out == "ferry: WARNING — hub not reachable"* ]]'
mv "$H.away" "$H"; q m A sync

mkdir -p "$T/E/home" "$(key E)" "$T/E/conf/ferry/other"
cp -f "$(key A)/identity" "$(key E)/"; cp -f "$(key A)/identity" "$T/E/conf/ferry/other/"
e() { as E ferry "$@"; }
out=$(e -C "$(R A)" pull --hook 2>&1); code=$?
check "hooks stay silent on a machine that doesn't sync" '[ $code -eq 0 ] && [ -z "$out" ]'
out=$(cd "$T/E" && e pull --hook 2>&1); code=$?
check "... and in a folder that isn't a project" '[ $code -eq 0 ] && [ -z "$out" ]'
mkdir -p "$T/E/full"; echo x > "$T/E/full/f"
out=$(e enroll proj --hub "$H" --host E --dest "$T/E/full" 2>&1)
check "enroll refuses a folder in use" '[[ $out == *"exists and is not empty"* ]]'
out=$(e enroll proj --host E --dest "$T/E/new" 2>&1)
check "enroll needs the hub's folder" '[[ $out == *"enroll needs --hub DIR"* ]]'
out=$(e enroll proj --hub "$H" --host E 2>&1)
check "... and the folder the project goes in" '[[ $out == *"enroll needs --dest DIR"* ]] && [ ! -e "$T/E/proj" ]'
out=$(e enroll proj --hub "$H" --host B --dest "$T/E/new" 2>&1)
check "enroll refuses a name in use" '[[ $out == *"already has a machine named B"* ]]'
out=$(e enroll nokey --hub "$H" --host E --dest "$T/E/new" 2>&1)
check "enroll needs the project's key" '[[ $out == *"no key at"*"nokey"* ]]'
out=$(e enroll other --hub "$H" --host E --dest "$T/E/other" 2>&1)
check "enroll checks the hub holds that project" '[[ $out == *"holds the project proj, not other"* ]]'
F=$T/F/proj; mkdir -p "$F" "$T/F/home"; git init -q "$F"
printf '%s\n' 'name = "loose"' '[repos.data]' 'path = "data"' 'auto = true' > "$F/ferry.toml"
out=$(as F ferry -C "$F" init --hub "$T/F/hub" 2>&1)
check "init refuses a repo folder root doesn't ignore" '[[ $out == *"root doesn'"'"'t ignore .ferry/, data/"* ]] && [ ! -d "$T/F/conf/ferry" ]'
out=$(cd "$(R A)/lab" && as A ferry status 2>&1)
check "ferry finds the project from a folder under it" '[[ $out == *"A ⇄ "* && $out == *"lab "*"vs B: same"* ]]'
cp -R "$(R A)" "$T/A/copy"; out=$(as A ferry -C "$T/A/copy" sync 2>&1); code=$?; rm -rf "$T/A/copy"
check "a copy of the folder won't publish itself" '[ $code -eq 1 ] && [[ $out == *"syncs proj at"*"a copy must not publish itself as A"* ]]'
out=$(m A push --bogus 2>&1); check "unknown flags fail" '[[ $out == *"unknown flag: --bogus"* ]]'
out=$(m A compact root 2>&1); check "root never compacts" '[[ $out == *"root keeps its history"* ]]'
out=$(m A retire A 2>&1); check "a machine can't retire itself" '[[ $out == *"retire it from another one"* ]]'

out=$(m A rekey 2>&1)
check "rekey re-seals every repo" '[ "$(grep -c "chain restarted (new key)" <<< "$out")" -eq 5 ]'
echo after-rekey > "$(src A)/k.txt"; q m A sync
out=$(m B sync 2>&1)
check "the old key can't read the new copies" '[[ $out == *"root: can'"'"'t read A'"'"'s copy"* ]]'
cp -f "$(key A)/identity" "$(key B)/identity"
cp -f "$(key A)/identity" "$(key C)/identity"
out=$(m B sync 2>&1); q m C sync; q m A sync
check "the new key reads them, and re-seals its own" '[ -f "$(src B)/k.txt" ] && [[ $out == *"full copy (its manifest is unreadable — a new key?)"* ]]'

q m A retire C; q m B sync
check "retire drops the slot, and others forget it" '[ ! -d "$H/C" ] && [ -z "$(mg B source for-each-ref refs/hub/C/)" ]'
# A move to another hub: one machine makes it, and each other follows when it
# sets its path; until then it syncs through the old one.
H2=$HUBS/daily-hub2
out=$(m A hub 2>&1); check "hub says which hub this machine uses" '[[ $out == */daily-hub ]]'
mv "$(R B)/.ferry/hub" "$(R B)/.ferry/hub.x"; out=$(m B sync 2>&1); mv "$(R B)/.ferry/hub.x" "$(R B)/.ferry/hub"
check "... and a machine without one is told how to set it" '[[ $out == *"no hub set"*"ferry hub DIR"* ]]'
out=$(m A init --hub "$H" 2>&1); check "init makes no hub where there is one" '[[ $out == *"a hub at"*"already — ferry hub"* ]]'
echo moved > "$(src A)/m.txt"; out=$(m A init --hub "$H2" 2>&1)
check "init on a machine that syncs is a move" '[ "$(cat "$H2/FORMAT")" = "ferry 1" ] && [ "$(grep -c "full copy (first push)" <<< "$out")" -eq 5 ] && [[ "$(m A hub)" == */daily-hub2 ]]'
echo stayed > "$(src B)/s.txt"; out=$(m B sync 2>&1)
check "... and a machine not moved yet stays apart" '[ ! -f "$(src B)/m.txt" ] && [[ $out != *"not reachable"* ]]'
out=$(m B hub "$T/nowhere" 2>&1); check "hub needs a hub there" '[[ $out == *"no hub at"* ]] && [[ "$(m B hub)" == */daily-hub ]]'
out=$(m B hub "$H2" 2>&1)
check "it follows with ferry hub, bringing what it did meanwhile" '[ -f "$(src B)/m.txt" ] && [[ $out == *"source ← A: merged"* ]] && [ -z "$(ls "$(R B)/.ferry/seen" | grep -v "^A\.")" ]'
q m A sync
check "every machine ends with the same files" 'diff -r "$(src A)" "$(src B)" >/dev/null && diff -r "$(R A)/personal" "$(R B)/personal" >/dev/null && diff "$(memdir A)/MEMORY.md" "$(memdir B)/MEMORY.md" >/dev/null'

# --- part 2: compaction --------------------------------------------------------
T=$ROOT/compact H=$HUBS/compact-hub; q setup; q add B; q add C
BIG=$(mg A source rev-parse HEAD:big1.bin)

# forced compaction; caught-up B rebuilds the root, behind C carries a local file
rm -f "$(src A)/big1.bin"; q m A sync; q m B pull
echo c-local > "$(src C)/c.txt"
out=$(m A compact 2>&1)
check "A compacts source, skips memory/personal" '[[ $out == *"compacted — 2.9 MB"* && $out == *"memory: already compact"* ]]'
check "A slot is small" '[ "$(slot A source)" -lt 50 ]'
check "... and starts from a commit every machine can build" '[ "$(mg A source log -1 --format="%an %ae %cn %ce")" = "ferry ferry@epoch ferry ferry@epoch" ]'
check "lock released" '[ ! -d "$H/.compacting-source" ]'
out=$(m B sync 2>&1)
check "B moves across without downloading source" '[[ $out == *"moved onto the history A compacted (epoch 1)"* && $out != *"source ← A"* ]]'
check "no duplicate-parent noise" '[[ $out != *"duplicate parent"* ]]'
out=$(m C sync 2>&1)
check "C keeps its local file" '[[ $out == *"own changes kept on top"* && -f "$(src C)/c.txt" && ! -f "$(src C)/big1.bin" ]]'
q m A sync
check "A gets C's file" '[ -f "$(src A)/c.txt" ]'
q add D
check "D enrolls on epoch 1 without the deleted blob" '! has D "$BIG" && grep -q "^epoch 1 " "$(R D)/.ferry/source.git/sync-epoch"'

# conflicting edits across a compaction
for x in B C D; do q m $x sync; done; q m A sync
echo alpha-B > "$(src B)/a.txt"; echo alpha-A > "$(src A)/a.txt"
q m A sync; q m A compact source
echo more >> "$(src A)/c.txt"
out=$(m A compact source 2>&1)
check "compaction waits for machines on the old epoch" '[[ $out == *"hasn'"'"'t moved onto the last compaction yet"* ]]'
out=$(m B sync 2>&1)
check "B reports the conflict and stays put" '[[ $out == *"conflicts carrying"* ]] && grep -q "^epoch 1 " "$(R B)/.ferry/source.git/sync-epoch"'
out=$(m D sync 2>&1)
check "D skips B's old-epoch pack" '[[ $out == *"B is still on the history"* && $out != *"source ← B"* ]]'
q mg B source merge refs/sync/adopt; echo alpha-merged > "$(src B)/a.txt"; q mg B source add a.txt; q mg B source commit -qm resolved
out=$(m B sync 2>&1)
check "B moves across after resolving" '[[ $out == *"moved onto the history A compacted (epoch 2)"* ]]'
q m A sync
check "A has the resolution" '[ "$(cat "$(src A)/a.txt")" = alpha-merged ]'

# threshold-triggered compaction, and a session start over budget
for x in B C D; do q m $x sync; done; q m A sync
rnd "$(src A)/keep5.bin" 5000000; rnd "$(src A)/tmp3.bin" 3000000; q m A sync
TMP3=$(mg A source rev-parse HEAD:tmp3.bin)
rm -f "$(src A)/tmp3.bin"
out=$(m A sync 2>&1)
check "default threshold: no compaction for 2.9 MB" '[[ $out != *compacted* ]]'
echo t >> "$(src A)/a.txt"
out=$(FERRY_COMPACT_MB=1 m A sync 2>&1)
check "FERRY_COMPACT_MB=1: compacts on its own" '[[ $out == *"compacted — 2.9 MB"* ]]'
out=$(FERRY_FOREGROUND_MB=1 m B pull --hook 2>&1)
check "B's session start defers the 4.8 MB" '[[ $out == *"downloading in the background"* ]]'
settle B; sleep 2; settle B
out=$(FERRY_FOREGROUND_MB=1 m B pull --hook 2>&1)
check "next session start moves across" '[[ $out == *"moved onto the history A compacted (epoch 3)"* ]] && [ -f "$(src B)/keep5.bin" ]'
check "B never got the deleted 3 MB" '! has B "$TMP3"'

# locks, interrupted compaction, retire
for x in B C D; do q m $x sync; done; q m A sync
mkdir "$H/.compacting-source"; echo Z > "$H/.compacting-source/owner"
echo x >> "$(src A)/a.txt"; q m A sync
out=$(m A compact source 2>&1)
check "someone else's lock holds A off" '[[ $out == *"not compacting — Z is"* ]]'
rm -rf "$H/.compacting-source"
for x in B C D; do q m $x sync; done; q m A sync
mkdir "$H/A/source/.manifest.age.part"      # so its manifest can't be written
q m A compact source; rmdir "$H/A/source/.manifest.age.part"
check "a failed compaction keeps the lock" '[ "$(cat "$H/.compacting-source/owner")" = A ]'
echo z >> "$(src B)/c.txt"
out=$(m B compact source 2>&1)
check "... and holds B off" '[[ $out == *"not compacting — A is"* ]]'
q m A push
check "A's next push publishes and unlocks" '[ ! -d "$H/.compacting-source" ]'
out=$(m B sync 2>&1)
check "B moves across with its later edit" '[[ $out == *"own changes kept on top"* ]] && [ "$(tail -1 "$(src B)/c.txt")" = z ]'
mkdir "$H/.compacting-personal"; echo C > "$H/.compacting-personal/owner"
q m A retire C
check "retire drops the retired machine's lock" '[ ! -d "$H/.compacting-personal" ]'

# tidy, then a chain restart on the new epoch
q m A sync
FERRY_KEEP_DAYS=1 q m A push
check "tidy waits" 'has A "$TMP3"'
echo k >> "$(src A)/a.txt"; FERRY_KEEP_DAYS=0 q m A push
check "tidy drops the old history" '! has A "$TMP3" && mg A source fsck --no-dangling >/dev/null 2>&1'
for x in B D; do q m $x sync; done; q m A sync
for i in 1 2 3; do echo "r$i" >> "$(src B)/a.txt"; FERRY_MAXCHAIN=2 q m B sync; done
out=$(m D sync 2>&1)
check "restart on epoch 4 is read normally" '[ "$(tail -1 "$(src D)/a.txt")" = r3 ] && [[ $out != *warn* ]]'
out=$(m D status 2>&1)
check "status shows the epoch" '[[ $out == *"compacted "*"(epoch 4)"* ]]'

echo "== $pass passed, $fail failed"
if [ "$fail" -eq 0 ]; then rm -rf "$ROOT" "$HUBS"; else echo "kept for a look: $ROOT $HUBS"; fi
[ "$fail" -eq 0 ]
