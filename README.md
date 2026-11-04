# ferry

ferry keeps a project's git repos in step across your machines through a
folder on a shared drive, such as a home NAS. It encrypts end to end with
[age](https://age-encryption.org), so the folder only ever holds
ciphertext. There is no git server, no git remote and no daemon. Each machine
owns one slot on the hub and writes nothing outside it, so no two machines
ever write the same file.

It suits a project whose contents shouldn't leave your machines in the clear,
and which is made of more than one kind of repo:

- **kept** repos, whose history is the record: ferry never commits, merges or
  rewrites in them, and a post-commit hook publishes each commit;
- **auto** repos, which are folders of plain files (documents, PDFs, Claude
  Code's memory for the project). ferry commits whatever changed, merges every
  machine's changes, and throws old history away once it piles up.

## Install

Needs git 2.29 or later and [uv](https://docs.astral.sh/uv/). uv brings
Python and the age library (pyrage), so no age install is needed.

```sh
uv tool install git+https://github.com/ShinThirty/ferry
uv tool update-shell        # once, if ~/.local/bin isn't on the PATH yet
```

`uv tool upgrade ferry` updates it. Install it on each machine from a source
you trust: the hub never carries code.

## A project

The project folder is a git repo, `root`. A `ferry.toml` tracked in it names
the project and lists the other repos:

```toml
name = "notes"

[repos.lab]                  # a folder of the project with a .git of its own
path = "lab"

[repos.memory]               # Claude Code's memory folder for this project
claude-memory = true
auto = true
union = ["MEMORY.md"]        # a merge keeps both sides' lines

[repos.files]
path = "files"
auto = true
```

A repo is kept unless it says `auto = true`. Its `path` is a folder inside the project,
and root's `.gitignore` must ignore that folder and `.ferry/`. `init` checks
for both, and `status` warns if either changes later. ferry runs from the
project folder or any folder under it, or with `-C DIR`.

## Machines

On the first machine, with the share mounted:

```sh
cd notes && ferry init --hub /Volumes/share/notes-hub
```

This makes the hub and the project's key, `~/.config/ferry/notes/identity`. The key never
touches the hub, so copy it to the same place on every other machine by
AirDrop, USB or scp, never through the share. Then, on each of them:

```sh
ferry enroll notes --hub /Volumes/share/notes-hub --dest ~/notes
```

`--hub` is the hub's folder as the share is mounted on that machine, and
`--dest` is the folder the project goes in, which must be empty or not exist
yet. Where the share is
mounted differs from machine to machine, so each one keeps its own path to the
hub, in `.ferry/hub`. `ferry hub` shows it, and `ferry hub DIR` changes it if
the share moves. `git clone` won't set up
a machine: it drops everything root ignores, and every nested repo.
`ferry retire HOST` drops a machine that's gone for good. Do it, because
compaction waits for every machine. `ferry rekey` replaces the key after one
is lost.

**Moving to another hub.**

1. Run `ferry init --hub NEW` on one machine. It makes the new hub and moves
   that machine onto it.
2. Run `ferry hub NEW` on each of the others, as the share is mounted there.
3. Until a machine has moved, it keeps syncing through the old hub and
   doesn't see the moved machines' work, so don't compact until every machine
   has moved. The old hub can go once the last one has.

## Day to day

Each commit in a kept repo pushes in the background. ferry points those repos'
`core.hooksPath` at `.ferry/hooks/<repo>/`, so a repo's own `.git/hooks` no
longer run. For Claude Code, two hooks in the project's `.claude/settings.json`
pull at session start and push at session end:

```json
{
  "hooks": {
    "SessionStart": [{"matcher": "startup|resume", "hooks": [
      {"type": "command", "command": "ferry -C \"$CLAUDE_PROJECT_DIR\" pull --hook", "timeout": 150}]}],
    "SessionEnd": [{"hooks": [
      {"type": "command", "command": "ferry -C \"$CLAUDE_PROJECT_DIR\" push --background"}]}]
  }
}
```

`pull --hook` reports into the session and never fails it. Away from the hub,
commits stay local, and session start says so and lists what isn't on the
hub yet. Once back:

```sh
ferry            # pull, then push
ferry status     # every repo against every other machine
```

A fast-forward is the normal case. When a kept repo **diverges** (commits on
two machines between syncs), ferry reports it and leaves it alone. Finish
with `ferry git root merge refs/hub/<host>/<branch>`, then `ferry`. A
conflict in an auto repo is left unmerged the same way. Uncommitted edits in
a kept repo never travel.

## Commands

```
ferry [sync]                           pull, then push
ferry pull [--hook] [--background]
ferry push [--only REPO] [--background]
ferry fetch [--background]             download only; no working file changes
ferry status
ferry init --hub DIR [--host NAME]     make a hub: the first machine, or a move
ferry enroll PROJECT --hub DIR --dest DIR [--host NAME]
ferry hub [DIR]                        this machine's path to the hub, or a new one
ferry compact [REPO...]                compact auto repos now, whatever the size
ferry git REPO ARGS...                 git on one repo, e.g. to finish a merge
ferry retire HOST
ferry rekey
```

`--background` returns at once and logs to `.ferry/log`. A machine's name
defaults to its host name.

## How it works

```
<hub>/FORMAT                          the format, and the proof the share is mounted
<hub>/<host>/<repo>/manifest.age      a slot: one per machine and repo
<hub>/<host>/<repo>/<stamp>.pack.age  its chain of packs (git bundles)
<hub>/.compacting-<repo>/owner        the one hub-wide lock
```

A push appends one delta pack holding the commits its slot doesn't have yet,
then rewrites the manifest, which lists the chain and where each branch
points. A pull reads the other slots' manifests and downloads only the packs
bringing commits it can't already reach. So a new file crosses the share once
per machine, and a quiet pull costs a few stat calls. Packs are fetched by
path into `refs/hub/<host>/*`, so no repo gets a remote. Every file is written
beside its target and renamed into place.

- **Budget.** A session start downloads at most 32 MB (`FERRY_FOREGROUND_MB`).
  Anything bigger downloads in the background, without touching a working
  file, and lands at the next session start.
- **Restarts.** After 24 packs (`FERRY_MAXCHAIN`), a lost pack or a new key, a
  chain restarts from a base cut where the other machines already are, so a
  machine that kept up downloads only what's new.
- **Compaction.** An auto repo's history is ferry's own snapshots. Once old
  versions (files deleted or replaced) reach 64 MB (`FERRY_COMPACT_MB`) and a
  quarter of the current files, a push replaces that history with one commit
  of what is there now. Every other machine moves onto it at its next pull,
  keeping its own changes. One that was caught up builds the new commit
  itself instead of downloading it.
  - A compaction waits until every machine has moved onto the last one and
    its work is merged in, and only one machine compacts at a time.
  - Each machine keeps the replaced history for 14 days (`FERRY_KEEP_DAYS`).

On each machine, `~/.config/ferry/<project>/` holds the machine's name, the
project folder's path and the key. A copy of the folder therefore can't
publish itself as the original. `<project>/.ferry/` holds the hub's path, the
auto repos' git dirs (kept outside their folders, so no `.git` lands in
them), the hooks, the caches and the log.

## Platforms

ferry runs on macOS, Linux and Windows. Files on the hub and in every repo
are the same bytes on every machine: ferry turns off line-end conversion, so
on Windows set your editor to LF. Other notes:

- `enroll` refuses a folder inside OneDrive.
- On Linux, mount the share with the kernel's `cifs` rather than a file
  manager's gvfs.
- Before trusting a new share, run the test suite against it (below).

## Developing

```sh
uv run python -m unittest discover -s tests     # seconds
tests/hub_test.sh                               # machines and hubs, about 75 s
FERRY_TEST_SHARE=DIR tests/hub_test.sh          # the hubs in a folder on a real share
```

Only `gitcli` runs git, only `crypto` touches age, and the decisions with no
I/O in them are in `plan`. The unit tests hold those rules. Every machine
reads the hub, so a change to its format needs a new `FORMAT`.

## License

MIT, in `LICENSE`.
