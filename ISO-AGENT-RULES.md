# ISOLATION RULES, read before anything else

You are working in a GIT WORKTREE, not the main checkout. This is not a
convention, it is the reason you are here.

A previous round of parallel agents all shared ONE working tree. One of them ran
`git checkout -- .` to clean up an experiment, and that silently reverted roughly
50 tracked files belonging to the other three agents. Around 21,000 lines of
completed work were destroyed and had to be recovered from a dangling commit.

So, absolutely:

- Work ONLY inside your worktree path. Every path in your brief is relative to
  your worktree, not to the main checkout.
- NEVER run `git checkout -- .`, `git restore .`, `git checkout <ref> -- .`,
  `git clean -fdx`, or `git reset --hard` anywhere. If you need to undo your own
  edit, edit the file back or use `git diff` to inspect. There is no exception to
  this rule and no situation that justifies it.
- NEVER touch the main checkout at
  /Users/apple/Downloads/v2-cleanup-remove-dead-temp-legacy-2026-07-31/v4
- Commit your work in your own worktree on your own branch. The controller
  merges; you do not merge.
- If you believe you must change a file outside your ownership, report
  NEEDS_CONTEXT. Do not edit it.
