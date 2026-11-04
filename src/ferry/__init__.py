"""ferry: a project's git repos, kept in step across machines through an
encrypted folder on a shared drive. One module per layer:

  gitcli    the only module that runs git; methods named for what they mean
  crypto    the only module that uses age (pyrage)
  system    the rest of what ferry asks of the OS: the machine's name, whether a
            process lives, a detached rerun, renames that wait out an open handle
  manifest  a slot's manifest and a repo's epoch record: parsing and formatting
  plan      decisions with no I/O: what kind of push, whether to compact, the budget
  hub       the shared folder's layout: slots, manifests, packs, the compaction lock
  config    this machine's settings for a project, and where they live
  project   ferry.toml: which repos a project has, and where each one lives
  ui        what ferry says, to a terminal, Claude's session start, or the log
  engine    the features: pull, fetch, adopt, compact, push
  cli       the commands, and the flags they take

Lower layers never import higher ones. tests/test_ferry.py holds the import
rules for the three that touch the outside world.
"""
