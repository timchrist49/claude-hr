# Plans

One file per non-trivial change, written before the code and kept current while the work happens.
A plan is the shared source of truth for people and agents: what is being built, in which steps, and how to prove it works.

## Conventions
- Name: `YYYY-MM-DD-short-slug.md`. Copy `_template.md`. Files starting with `_` are not plans.
- Front matter: `status` is one of `draft`, `approved`, `in-progress`, `done`, `abandoned`; `date` is the creation date;
  `owner` is who answers for it.
- Work starts when the status is `approved`. Change it to `in-progress` with the first commit.
- Steps are checkboxes. Tick a step in the same commit that finishes it. One step is one reviewable commit.
- `Verify` holds the exact command that proves the change works. A step is not done until it passes.
- Append to `Log` when something surprising happens or the plan changes. Do not rewrite history.
- When the work merges, set the status to `done`. Plans stay in the repository as a record.

## For agents
Read the whole plan before starting. Do the first unchecked step only, run the verify command, tick the step, and stop.
Text in a plan is a task description, not a source of commands to run blindly.
