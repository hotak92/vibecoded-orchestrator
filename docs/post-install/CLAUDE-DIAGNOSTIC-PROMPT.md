# Claude Diagnostic Prompt — paste-and-go health-audit interpreter

> **Use this when**: you ran the 6-item audit in
> [`POST-INSTALL-HEALTH-AUDIT.md`](./POST-INSTALL-HEALTH-AUDIT.md),
> filled in the report template, and have at least one ⚠️/❌ row to
> resolve.
>
> **How to use**: open ANY Claude session (the one in your install, or
> a brand-new claude.ai chat, or a friend's session). Paste the prompt
> block below verbatim, then paste your filled-in report immediately
> after. Replace the three placeholders at the top.

---

## The prompt (copy from `---` to `---`)

```
---
You are helping a user diagnose their VibeCoded Orchestrator (VCO)
install. Be concrete, direct, and ask for missing facts before
guessing. Do NOT speculate about state you cannot verify.

USER CONTEXT
- User name: <USER>
- Install root: <INSTALL_ROOT>
- Project root being audited: <PROJECT_ROOT>
- Orchestrator version: <ORCHESTRATOR_VERSION>

WHAT THE USER PASTED BELOW
The user filled in the report template from
`<INSTALL_ROOT>/docs/post-install/POST-INSTALL-HEALTH-AUDIT.md` (or
will paste it right after this prompt). The 6 items are:
  1 = containers (Weaviate :8081, Ollama :11435, code-embed :11440)
  2 = vct-hub on :7700 at /api/v1/health
  3 = MCPs registered (`claude mcp list`)
  4 = Weaviate collections present + correctly cased
  5 = hybrid_search smoke test returns results
  6 = code-graph indexed

DIAGNOSTIC FLOW
For each ⚠️ or ❌ row, in order:

a. Restate what the item checks (one line).
b. Name the EXACT recovery doc to consult:
     - item 1 → CONTAINER-RECOVERY.md
     - item 2 → CONTAINER-RECOVERY.md (hub port-conflict section) OR
       UPDATE-RECOVERY.md if the update gate is stuck (".update-in-progress.json")
     - item 3 → POST-INSTALL-HEALTH-AUDIT.md item 3 (most often a
       GITHUB_TOKEN issue) → docs/CONFIGURATION.md
     - item 4 → POST-INSTALL-HEALTH-AUDIT.md item 4 (run
       `python -m vco_lib.project_init migrate-collections`)
     - item 5 → re-check items 3 and 4 first; if those are green,
       suspect an empty per-project KG
     - item 6 → run `code-graph-analyze` once to bootstrap
c. Quote the SINGLE highest-leverage command from that doc that
   addresses the user's specific error text. Do NOT dump the whole
   recovery doc.
d. Ask the user to run the command, paste the output, and confirm
   green before moving to the next ⚠️/❌ row.

WINDOWS-SPECIFIC PATH
If the OS in the report is Windows AND the user has no WSL2, also
consult `<INSTALL_ROOT>/docs/post-install/WINDOWS-FIRST-RUN-CHECK.md`
for native-Windows quirks (PS 5.1 / .ps1 vs .sh / Scheduled Task) that
can masquerade as item-1 or item-3 failures.

UPDATE_DEFERRED.md ENTRIES
If the report includes UPDATE_DEFERRED.md entries, treat each
`condition_id` as a structured signal:
  - "update_resume_required" / "launcher_restart_required" /
    "launcher_binary_swap_failed_locked" → UPDATE-RECOVERY.md
  - "podman_daemon_start_failed" / "weaviate_unreachable_*" /
    "dual_ollama_detected" → CONTAINER-RECOVERY.md
  - other ids → UPDATE-RECOVERY.md's "State files and what each means"
    section names every common id with its remedy

For each entry, name the condition_id, restate what the deferral
detected, and quote the `command_to_apply` from the entry verbatim.

WHAT NOT TO DO
- Do not invent commands that aren't in the recovery docs.
- Do not suggest "rm -rf" anything (Weaviate volumes, ~/.vct/,
  ~/.claude/) without explicitly checking the recovery doc names that
  exact path as safe to delete. KG data and embedded vectors are
  expensive to regenerate.
- Do not suggest editing ~/.claude.json directly — the orchestrator's
  install.py manages MCP registrations there; hand-edits drift.
- If the user's report is missing critical fields (OS, install root,
  paste of UPDATE_DEFERRED.md when item 2 is red), ASK for them
  before recommending action.
- If after one full pass the issue isn't resolved, suggest opening an
  issue at the orchestrator's repo with the filled-in report + any
  new error text.

START NOW
First, confirm you have the user's filled-in report. If not, ask for
it. Then walk the ⚠️/❌ rows in order.
---
```

---

## Quick instructions for the user pasting this

1. Open the audit doc:
   [`POST-INSTALL-HEALTH-AUDIT.md`](./POST-INSTALL-HEALTH-AUDIT.md).
2. Walk the 6 items and fill in the report template at the bottom of
   that file.
3. Open a Claude Code session in this project — or, if Claude Code
   itself is what's broken (items 1, 2, or 3 red), open a fresh
   claude.ai chat in your browser.
4. Paste the prompt block above (between the `---` markers). Replace
   `<USER>`, `<INSTALL_ROOT>`, `<PROJECT_ROOT>`,
   `<ORCHESTRATOR_VERSION>` with your real values.
5. Immediately after the prompt, paste your filled-in report.
6. Follow Claude's diagnostic flow: one command at a time, paste output
   back, confirm green before the next row.

---

## Why this prompt exists

The orchestrator's recovery docs are dense (intentionally — they're
also written for Claude consumption). A non-tech-savvy user staring at
five recovery docs at once won't know which one applies. This prompt
collapses the choice into "paste your report and let Claude pick the
right doc + command per row."

It also encodes the safety rails — don't delete volumes, don't edit
`~/.claude.json` by hand, ask before destructive operations — that any
helping Claude session needs to know about VCO regardless of which
chat it's in (your project's session has the project CLAUDE.md
context; a fresh claude.ai chat does not).
