---
name: hr-done
description: Prove a change is finished with evidence (checks, fix proof, diff vs plan). Run before opening a PR.
disable-model-invocation: true
allowed-tools: Bash(claude-hr-ctl verify run *) Bash(claude-hr-ctl verify status *) Bash(claude-hr-ctl fmt --check *)
---
Done means shown, not believed. For each item give the command, its exit code and the last lines of its output. If something could not be run, say so and why instead of reporting it as passing.

1. Checks: run `claude-hr-ctl verify run --tier full` when the repo has a verify recipe (`claude-hr-ctl verify status` tells). A recipe that is not trusted is not run for you: ask the user to run `claude-hr-ctl verify enable` in their own terminal (claude-hr also offers it when they start a session there; it shows them the commands first), and run the repo's own commands meanwhile. Otherwise run the commands from CLAUDE.md, package.json, pyproject.toml or the Makefile.
2. Formatting: `claude-hr-ctl fmt --check --changed` must be clean. Without the helper, run the repo's own formatter in check mode.
3. Bug fix: show a test that fails without the change and passes with it (stash only the fix with `git stash push -- <files>`, run the test, `git stash pop`). If that cannot be shown, say why.
4. New behaviour: name the test that covers it and show it passing.
5. Plan: list the changed files (`git status --short`, `git diff --stat`) next to the plan's steps. Point out files that are not in the plan and steps that are not done.
6. Docs: README, docs/ and CHANGELOG match the new behaviour.
7. Tests stay as they are: do not weaken, skip or delete one to get green. If a test looks wrong, say so and ask the user.

If `claude-hr-ctl` is missing, do the same steps by hand and say which helper was unavailable.

Finish with a summary of five lines: what changed, which checks ran and their result, the evidence for the fix, what was not checked, what is left or risky.
