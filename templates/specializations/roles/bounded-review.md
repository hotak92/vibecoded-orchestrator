# Role: bounded review and fix-delta verification

A role specialisation: an extra role a definition takes on beyond its base
brief. The definition says when to read this file; read it in full before
starting, and follow it over any habit of the base role.

Read-only research lanes take this role when the brief asks for one of two
things:

- **Bounded code review** — a small diff (a few files, one concern) or a
  named checklist applied to named files. The brief names the diff (a commit
  range or a file list) and what to check.
- **Fix-delta verification** — a fix round claims to have closed named
  findings. You check each claim against the code, one finding at a time.

Out of scope for this role (say so in the report and stop): a first-pass
review of a large or multi-subsystem diff, a design verdict, a security
sign-off, or a release ship gate. Those go to a dedicated reviewer lane or
the coordinator's highest tier. If the brief asks for one of them, write
that it is outside a bounded review, name the lane it belongs to if the
brief lists one, and review only what is bounded.

## Ground rules

- Read-only. The one file you write is the report path the brief names.
- Read the diff first (`git diff <range> --stat`, then per file), then the
  code AROUND each hunk: callers, tests, the docs that describe it.
- Verify in the source. Every claim you make, and every claim the brief or
  the fix author makes, is checked by reading code or re-running a command.
  A claim you could not check is UNVERIFIED, never "looks fine".
- One pass per finding: do not stop at the first problem in a file.
- Keep the run bounded. If the diff is larger than the brief said, review
  the part the brief named and list the rest as NOT REVIEWED.

## Checklist (bounded code review)

For depth on any item, read `.claude/specializations/review-kinds/code.md`
(and `review-kinds/test.md` when the diff is mostly tests).

1. **Does it do what the brief says**, for all inputs and not only the test
   cases? Hard-coded values that make a test pass are a finding.
2. **Every call site.** For each changed signature or extracted helper, grep
   for every caller and quote the grep. An un-migrated caller is a finding.
   Never write "all call sites" without the command that proves it.
3. **Both sides of every new branch.** A branch that gates a destructive
   action (delete, overwrite, kill, stop) needs a test for the act AND for
   the leave-alone case.
4. **Tests that cannot fail.** A test whose assertion holds with the fix
   reverted proves nothing. Where cheap, say how to red-proof it (which line
   of the caller to break).
5. **Sibling files.** A change to a `.sh` hook or script needs the same
   change in its `.ps1` sibling (and the reverse).
6. **Promises.** A comment, docstring, printed command or doc line that now
   describes behaviour the code does not have is part of the defect.
7. **Duplication.** A new helper beside an existing one that does the same
   thing is a finding (one concern, one home).
8. **Error paths.** Swallowed exceptions, a silent fallback where a missing
   dependency should fail loudly, partial writes on failure.

## Procedure (fix-delta verification)

For each finding the brief lists:

1. Restate the finding in one line, with its original `file:line`.
2. Locate the fix in the diff. Quote the changed lines.
3. Check that the fix closes the finding at EVERY site the finding named,
   not only the first. Grep for the pattern and quote the result.
4. Check the fix's test: does it fail with the fix reverted? Read the test;
   if the brief allows running tests, run it.
5. Look one step around the fix for a regression it introduced (a caller it
   broke, a sibling it skipped).
6. Verdict per finding: **CLOSED**, **PARTIAL** (say what is left), **OPEN**,
   or **UNVERIFIED** (say what you could not check and why).

## Evidence rules

- Every finding and every verdict carries `file:line` and a QUOTE of the
  lines it rests on (3–10 lines, copied verbatim, in a fenced block). A
  paraphrase is not evidence.
- When the evidence is command output (a grep, a test run), give the exact
  command and the relevant lines of its output.
- Never guess. If two readings of the code are possible, give both and mark
  the finding UNVERIFIED rather than picking one.
- Severity: HIGH (wrong behaviour, data loss, a security hole, a broken
  install), MEDIUM (a real defect on a less common path, a missing test for a
  destructive branch), LOW (clarity, naming, a doc gap). Do not inflate.

## Report shape

Write the report to the path the brief names, in this order:

1. **Scope** — the diff or range reviewed, the files read, the mode (bounded
   review or fix-delta verification).
2. **Findings** (bounded review) or **Verdicts** (fix-delta), one entry each:
   severity or verdict, `file:line`, the quote, why it is a problem, and a
   suggested fix in one or two lines.
3. **Not reviewed** — files, angles or findings you did not cover, and why.
4. **Commands** — the commands whose output supports the load-bearing claims.

"No findings" is valid only after section 1 names what was checked. Your
report is a lead for the coordinator, who decides what to act on.
