---
name: hr-claudemd-hygiene
description: Keep CLAUDE.md files short and correct. Use before adding to or editing any CLAUDE.md, CLAUDE.local.md or .claude/rules file.
---
Long or stale CLAUDE.md files make Claude less accurate and cost tokens in every session and every subagent.

Before editing: run `claude-hr-ctl claudemd lint`. After editing: run it again and fix anything it reports.

Add a line only if Claude cannot learn it from the code and it would otherwise make a mistake (commands, non-default conventions, environment quirks). Do not add architecture overviews, directory trees, README text, dates, or anything that will go stale. Prefer a path-scoped file in `.claude/rules/` (with `paths:` frontmatter) over a root-level line for language- or folder-specific guidance. Replace outdated lines rather than appending beside them.
