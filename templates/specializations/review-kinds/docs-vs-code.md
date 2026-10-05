# Docs-vs-code review kind

What to hunt when verifying that documentation matches the code it describes
(readme, CLAUDE.md-type instruction files, docstrings, comments, user-facing
texts, config docs). Every finding carries severity, `file:line` on BOTH
sides (doc and code), and the corrected text.

## The core rule

A statement about behavior is a claim that must be backed by executing code.
For each load-bearing claim in the doc under review:

1. Find the mechanism the claim names (function, flag, env var, file,
   command, hook).
2. Verify it exists and does what the doc says — in source, not from memory.
3. If it does not: the finding is "make it true, supersede it (name the
   replacement), or get an explicit owner deferral" — deleting the promise is
   a finding against the reviewer, not a disposition.

## High-yield check categories

- **Printed commands**: anything the software tells a user to run must exist
  and must help; destructive commands only for positively-confirmed
  conditions.
- **Declared config**: every documented key/flag/env var has a reader; a
  reader that changes nothing observable is the same defect one layer down.
- **Safety comments**: a comment claiming a guard is part of that guard —
  verify the guard fires (mutate the caller and prove the test goes red;
  a name in a comment proves nothing).
- **Counts and listings**: any doc stating "N agents/hooks/tools" or listing
  names matches the shipped directory at review time.
- **Cross-references**: file paths, anchors, and sibling-document names in
  the doc resolve; a renamed target is a finding.
- **Marker/sentinel conventions**: each marker has a live reader; a retired
  consumer's markers were retired in the same change.
- **Version/platform claims**: OS-specific instructions match current
  platform behavior; URLs/ports match the configured defaults.

## Stale-instruction drift

Instruction files loaded by agents (project CLAUDE.md, MEMORY-type notes,
reference docs) that cite a renamed subsystem, a removed guard, or a dead
tool are ACTIVE defects: every future agent re-derives the wrong conclusion.
Flag drift at the source-of-truth document, and check whether the doc's own
update rule was followed.

## What NOT to flag

- Historical records (changelogs, handoffs, dated incident notes) are
  snapshots — a past-tense statement that was true then is not a finding now.
- Aspirational text explicitly marked as a deferred/owned decision with a
  named who/when is standing, not false.

## Report shape

Table: doc `file:line` | claim | code `file:line` (or ABSENT) | verdict
(TRUE / FALSE / UNVERIFIED) | corrected text. Name which docs were not
checked.
