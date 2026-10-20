---
name: api-review
description: Runs a bounded headless Claude Code task (typically a final Fable code review) billed to the Claude Console API key instead of the subscription, and reports its cost. Use when the subscription is short or should not be spent.
short_desc: "api-review: headless review/task on Console API credits, with a cost summary"
keywords: ["api review", "review on api credits", "fable review via api", "console credits", "api key review", "claude -p review", "review without subscription", "subscription out of usage", "headless review", "cost of a review"]
argument-hint: "[what to review, e.g. 'branch feature-x against main']"
model: sonnet
---

# api-review — a bounded task on Console API credits

`claude-api-run` runs ONE headless `claude -p` task in a project directory,
billed to your Claude Console API key, then prints what it cost. The run is
a normal Claude Code session (your MCP servers, hooks and project settings
apply); only the billing, the bounds and the tool policy differ.

## When to use it

- A **final review** or another bounded task (minutes, one diff, one report)
  while your subscription is out of usage, or when the task should not spend
  it.
- **Not** for long interactive chats or very large contexts: every turn
  re-sends the context, so a multi-hour session costs real money (measured:
  about $0.26 per Opus turn at 300k–800k tokens of context). Keep those on
  the subscription.

## Prerequisites

- The Console API key saved in the launcher's **Secrets panel, Shared
  scope**, as `ANTHROPIC_API_KEY_GATEWAY` (the "Claude API key" button fills
  the name). Another Shared secret can be named with `--key-name`.
- Run it from a project registered in the launcher (the shipped wrapper
  names the project it is installed in as the key's requester).
- The run is refused, with nothing spawned or billed, when the environment
  routes Claude elsewhere (`CLAUDE_CODE_USE_BEDROCK` / `_VERTEX` /
  `_FOUNDRY`, an `ANTHROPIC_BASE_URL` that is not VCO's local gateway, your
  own `ANTHROPIC_AUTH_TOKEN`) or the key does not resolve. VCO's own gateway
  routing is removed for the run, which goes straight to `api.anthropic.com`.

## Steps

1. **Pin the target.** Find the exact commits: `git rev-parse <base> HEAD`,
   `git diff --stat <base>...HEAD`. Name them in the prompt so the reviewer
   reviews what you mean, not whatever is checked out later.
2. **Write the prompt to a file** in the project's scratch dir
   (`.claude/scripts/vco-scratch path`). A good review prompt states:
   - the scope: "Review `git diff <base>...<head>` in this repository";
   - what the change is meant to do (one paragraph, from the plan/PR);
   - what to look for: correctness bugs, missed call-sites, tests that do
     not prove what they claim, security, docs that no longer match;
   - the output format: findings ranked HIGH/MEDIUM/LOW, each with
     `file:line` evidence and a concrete fix, then a one-line verdict.
3. **Run it** (POSIX; Windows: `.claude\scripts\claude-api-run.ps1`):

   ```bash
   bash .claude/scripts/claude-api-run.sh \
     --prompt-file <scratch>/review-prompt.md \
     --report <scratch>/review.md
   ```

   Defaults: `--model claude-fable-5-1`, `--effort high`,
   `--max-budget-usd 25` (always passed; the CLI stops at it),
   `--policy review`. Add `--max-turns N` to cap turns, `--cwd DIR` to run
   elsewhere, `--json` for a machine-readable summary.
4. **Read the report**, verify each finding in the source before acting on
   it, and relay the cost line to the user.

## Tool policy

- `review` (default): only `Read`, `Grep`, `Glob` and `Bash`; Bash is
  pre-approved for read-only git (`diff`, `log`, `show`, `status`, `blame`,
  `ls-files`, `rev-parse`, `merge-base`), VCO's KG / code-graph search tools
  are pre-approved, and everything else is refused without a prompt
  (`--permission-mode dontAsk`). Git's mutating subcommands, `rm`, `Edit`
  and `Write` are denied outright. The model cannot write files: the
  report is written by the tool from the model's final answer.
- `edit`: the full tool set with `--permission-mode acceptEdits`, for a
  bounded task that must change files in `--cwd`. Review the diff after.

## Cost and ledger

After the run the tool prints the total cost, per-model tokens (input,
output, cache read, cache write) and cost, turns, duration, and the API
error if there was one (e.g. `API status 400; Credit balance is too low`).
Each run appends one JSON line — no prompt, no result text, no key — to
`claude_api_runs.jsonl` in VCO's metrics directory (`~/.vct/metrics/` by
default, `$VCT_STATE_DIR/metrics` when set). Exit codes: 0 ok, 1 the run
failed, 2 usage, 3 refused before anything was spawned.
