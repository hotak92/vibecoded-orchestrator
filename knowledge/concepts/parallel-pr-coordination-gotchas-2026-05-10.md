---
title: Parallel-PR Coordination Gotchas (2026-05-10, extended through 2026-05-25 round 3)
type: concept
tags: [git, parallel-agents, claude-code, subagents, code-review, svelte, ci, gotchas, low-level-implementation, worktree, cherry-pick, multi-repo, ship-coordination, api-stub-reconciliation, migration-numbering, idempotency, sequential-merge, audit-after-merge, shared-working-tree, mcp-matcher-gaps, ship-blockers, integration-tests]
created: 2026-05-10T20:00:00Z
updated: 2026-06-04T18:00:00Z
valid_from: 2026-05-10T00:00:00Z
valid_until: null
status: active
---

# Parallel-PR Coordination Gotchas (2026-05-10)

Three load-bearing failure modes surfaced during a session that opened 5+ parallel PRs (VCO 0.2.x backlog), each with its own background Opus subagent. All three cost real time to diagnose; recording them so future runs trip the wire faster.

## 1. Subagent worktree isolation can silently fail

**Symptom**: An agent spawned with `isolation: "worktree"` writes into the *parent's* shared working tree instead of its own isolated one. Two such agents stomp on each other and on the parent's own in-progress work. `git status` in the parent's tree shows files the agent claimed to have committed — but to a branch that, on inspection, has 0 commits ahead of `main`.

**Detection**: `git worktree list` from the parent's tree. If you spawned N agents with `isolation: "worktree"`, you should see N + 1 entries (parent + N isolated). If the count is lower, some agents fell back to the shared tree.

**Mechanism (likely)**: the `isolation: "worktree"` parameter on the `Agent` tool is best-effort — when the parent worktree has uncommitted changes that the spawn-time `git worktree add` would conflict with, the harness can silently fall through to in-place execution rather than failing the spawn. Confirmed empirically by spotting `/tmp/agent-<id>-…` directories present only for SOME of the agents I spawned in one batch.

**Workaround**: before spawning multiple parallel agents, ensure the parent's worktree is clean (`git status --short` returns nothing) AND that any branch the agent might want to base off doesn't conflict with the current HEAD. If you can't avoid both, instruct each agent IN ITS PROMPT to **explicitly** run `git worktree add /tmp/<task-tag> -b <branch> origin/main` and `cd` into that path before any edits.

**Recovery once it's happened**: 
1. `git stash push -u -m "subagent <id> WIP"` to capture all the rogue files in one stash (use `-u` to grab untracked too).
2. Tell each affected agent via `SendMessage` what you stashed and that they should create their own worktree, then `git stash pop` there.
3. Verify before merge by running the verifier script (see `parallel-pr-verifier` pattern below).

**Cross-ref**: [[relatedTo::Claude Code Agent Teams]] — the official doc covers worktree isolation as a happy-path feature; this node covers the failure mode.

## 1b. Harness `isolation: "worktree"` branches off the WRONG repo when parent cwd is a fork

**Symptom (added 2026-05-23)**: spawning an agent with `isolation: "worktree"` while the parent Claude session's cwd is a private fork (e.g. `VCO_dev`) creates the agent's worktree under `<parent-cwd>/.claude/worktrees/agent-<id>` branched from VCO_dev's HEAD — even when the prompt explicitly says "work in the public repo at `/home/.../vibecoded-orchestrator/`". The agent dutifully does the work but commits against VCO_dev's git history. When the parent then tries `git cherry-pick <agent-sha>` from the public repo, git returns `fatal: bad revision` because the two repos are separate `.git/` stores.

**Detection**: in the agent's worktree, run `git log --oneline -2`. Does the HEAD's parent commit match what the public repo's HEAD is? If not (e.g. parent is `0936829 chore(VCO_dev): post-v0.2.21-install state` while public is `ecee838`), the agent branched off the wrong repo.

**Mechanism**: `isolation: "worktree"` calls `git worktree add` against the parent process's cwd. The parent's cwd determines which `.git/` directory the worktree clones from. If the user opens Claude Code in a private fork, every agent inherits that fork's history — there's no way for the agent's prompt to override the parent's cwd.

**Workaround (the pattern that worked all session)**: don't use harness `isolation: "worktree"` when the parent cwd ≠ target repo. Instead, the parent runs:
```bash
cd /home/martino/Desktop/PROGETTI/vibecoded-orchestrator   # the TARGET repo
git worktree add /tmp/vco-wt-<task-tag> -b <branch> HEAD
```
…then spawns the agent WITHOUT `isolation: "worktree"` + tells it in the prompt: "Use this exact worktree path: `/tmp/vco-wt-<task-tag>/`. Verify on entry: `cd <path> && git log --oneline -2` must show <expected base commit>. If not, STOP and report. DO NOT run any `git worktree add` yourself."

The agent runs in the parent's main process (no isolation flag) but is constrained to a manual worktree branched from the right base.

**Recovery once it's happened**:
1. Export the agent's commit as a patch from the wrong-repo worktree: `git format-patch -1 HEAD --stdout > /tmp/agent-patch.patch`.
2. From the target (correct) repo: `git apply --check /tmp/agent-patch.patch` to verify it applies cleanly.
3. If clean: `git apply /tmp/agent-patch.patch && git add <files> && git commit -m "<recovered msg>"`. The original sha is lost but the work is preserved.

**Or**: from the target repo, `git fetch /path/to/agent-worktree <branch-name>` followed by `git cherry-pick FETCH_HEAD`. Works when the two repos share enough history that the patch applies; otherwise fall back to `format-patch + apply`.

**Add to agent prompts as a guard**: "If you find yourself branched off the wrong repo (HEAD parent doesn't match the expected base commit), STOP and report. Don't proceed — the parent will recover via patch extraction." This guard cost us one round-trip cycle on 2026-05-23's v0.2.31 sprint (Agents D / E / F first attempt) before the manual `/tmp/vco-wt-*` pattern was adopted. Subsequent agents (D2 / E2 / F2 / G / H / I) all branched correctly.

**Why this matters more than 2026-05-10's variant**: §1 above is about silent in-place execution (agent stomps on parent's files); §1b is about successful-but-wrong-base execution (agent's work is internally consistent but can't merge to the target repo without manual surgery). §1's symptom is "shared dirty tree"; §1b's symptom is "diff is fine, sha doesn't exist in target".

## 1c. Stale-branch-state base re-use (recurrence, 2026-05-28)

**Symptom**: 6 v0.2.38 agents spawned with `isolation: "worktree"` from VCO_dev's parent session all branched off `8df070a` (v0.2.21 era, ~10 weeks old) while VCO_dev's `main` was at `adc6966` (v0.2.37). V38-A edited `rl_service.rs` (a v0.2.21-era filename — current main has `module_service.rs`); V38-MCP's diff was against `server.py` line 2648 while current main is at line 3284 (600+ line drift). All 6 commits unmergeable.

**Mechanism (new variant — distinct from §1b)**: when the parent session has accumulated `worktree-agent-*` branch refs from EARLIER fanouts in the same session, and those branches were created at an older HEAD, `git worktree add <path>` (without an explicit `<commit-ish>`) may re-use the last-known parent of the worktree-prefix namespace. The harness creates each new worktree branch as `worktree-agent-<id>`, and a leftover entry in `.git/refs/heads/worktree-agent-<old-id>` at SHA `8df070a` apparently seeded the resolution. Whether this is git behavior or harness behavior is unclear; what IS clear: **without an explicit base, the result is non-deterministic across sessions**.

**Detection (a third symptom on top of §1 + §1b)**: agent reports an edit to a file that doesn't exist on current `main` (renamed since the agent's base), OR edits at line numbers that don't exist on current `main` (drift), OR `git merge-base <agent-branch> main` returns an ancient commit that pre-dates the fanout's release scope.

**Workaround (consolidated)**: this recurrence confirms the §1b workaround is the right pattern — the parent should create worktrees manually BEFORE spawning agents and tell each agent the exact path WITHOUT `isolation: "worktree"`. Concretely:

```bash
# Parent session, before spawning:
cd <target-repo>
EXPECTED_BASE=$(git rev-parse HEAD)        # e.g. adc6966
EXPECTED_SUBJECT=$(git log -1 --pretty='%s')  # e.g. "chore(binary): refresh ..."
for AGENT in v38-a v38-b v38-mcp v38-kgsync v38-ci v38-train; do
    git worktree add /tmp/vco-wt-$AGENT -b $AGENT $EXPECTED_BASE
done
```

Then in each agent prompt:

```
# REQUIRED first action — verify-or-abort
cd /tmp/vco-wt-<agent-name>
EXPECTED_BASE="adc6966"
EXPECTED_SUBJECT="chore(binary): refresh vct-launcher + vct-hub dist binaries for v0.2.37"
actual_sha=$(git rev-parse HEAD)
actual_subj=$(git log -1 --pretty='%s')
if [ "$actual_sha" != "$EXPECTED_BASE" ] || [ "$actual_subj" != "$EXPECTED_SUBJECT" ]; then
    echo "STOP — wrong base. actual=$actual_sha:$actual_subj expected=$EXPECTED_BASE:$EXPECTED_SUBJECT"
    exit 1
fi
# proceed only after this passes
```

Three layers of safety:
1. Parent creates worktrees at known SHA (deterministic by construction)
2. Agent verifies SHA + subject string before editing (deterministic at run-time)
3. Agent prompt's file-line citations are valid (deterministic for downstream merge)

This pattern shipped 2026-05-23's Agents D2/E2/F2/G/H/I successfully (per §1b's note); the 2026-05-28 recurrence happened because I used the harness's auto-isolation instead of the manual pattern. Lesson re-learned.

**Cost of the recurrence (2026-05-28)**: 6 wasted agent runs (~40 min API + ~20 min agent confusion + ~30 min recovery diagnosis) = ~90 min. Total avoidance cost going forward: ~5 lines in each agent prompt.

## 2. Stale base branches show merged-elsewhere work as deletions

**Symptom**: a PR's "Files changed" tab shows MASSIVE negative diffs (thousands of `-` lines) covering files the parent agent never touched — including DB migrations, Rust files for unrelated features, etc. CI fails on drift / parity checks pointing at hooks the PR author claims they didn't modify.

**Mechanism**: parallel PRs each branch off the same starting point (`main` HEAD at the time the agent was spawned). If sibling PRs merge into main while one PR is still in flight, that in-flight PR's branch is now stale. When GitHub computes "files changed vs main", everything the sibling PRs merged shows up as if the current PR is DELETING it — because the current PR's tip doesn't have those changes.

**Detection**: `git merge-base origin/<pr-branch> origin/main` returns a commit that's days/hours old; `git log <merge-base>..origin/main` shows many commits the PR doesn't know about; `git diff <merge-base> origin/<pr-branch> --stat` (the "real" PR diff) is small; `git diff origin/main origin/<pr-branch> --stat` (what GitHub shows) is huge with mostly deletions.

**Workaround**: rebase the stale PR onto current main: `git fetch origin main && git rebase origin/main`. Conflicts are rare because the duplicated work is byte-identical (it's the same upstream commit, just reached via two paths) — git's three-way merge drops the duplicates automatically.

**When this bites hardest**: when both PRs include the SAME bugfix commit (e.g. one PR-A originally included the hook-audit fixes; we then split them into PR-B which merged first; PR-A's stale tip still has the hook-audit fix as its own commit, which clashes after PR-B merges). The fix is in main twice → the stale PR shows those changes as deletions.

**Recovery**:
1. Verify the rebase is non-destructive: `git log <branch> --oneline | head -10` before; identify which commits are "your" work vs which are upstream duplicates.
2. `git rebase origin/main` — git's three-way merge handles the duplicates cleanly when they're byte-identical.
3. Force-push with `--force-with-lease` (NOT plain `--force` — protects against pushing over someone else's amend).
4. Re-run CI; the drift/parity checks should clear because the duplicate file modifications are gone.

## 3. Svelte 5 `$state` rune name-collides with the `$<store>` store-prefix syntax

**Symptom**: `svelte-check` fails with:
```
Cannot use 'state' as a store. 'state' needs to be an object with a subscribe method on it.
Argument of type 'XState' is not assignable to parameter of type 'SvelteStore<any>'.
  Property 'subscribe' is missing in type 'XState' but required in type 'SvelteStore<any>'.
```

The error column points at a `$state(...)` rune call, but the error text refers to "state" as a store name.

**Mechanism**: Svelte 5's runic syntax introduces `$state(initialValue)` as a reactivity primitive. Svelte's legacy store-auto-subscribe syntax is `$<storeName>` for reading a store's value. These two syntaxes parse identically at the `$<identifier>` lookahead — when both are used in the same `<script>` block, the compiler conflates them. The trigger we hit was:

```svelte
<script lang="ts">
  import { projects } from '$lib/stores/projects';
  const state = $derived($projects);          // legacy store-prefix read
  let updateAllOpen = $state(false);          // Svelte 5 rune
</script>
```

The compiler sees `$state(false)` and looks up `state` as a store name → finds the `const state = ...` declaration above → checks it for a `subscribe` method → reports the missing-`subscribe` type error.

**Workaround**: never name a local binding `state` (or anything else that could be a store name) in a file that also uses Svelte 5 runes. Rename the local to `store`, `data`, `s`, etc. The rune call doesn't need renaming; the local does.

**Detection**: `npm run check` from `launcher/` after introducing a `$state(...)` rune in a file that already had `const state = $derived(...)` (or any `$<something>` legacy-store read).

**Related Svelte 5 gotcha**: object-literal methods inside `createStore`-style factories like:
```ts
function createProjectsStore() {
  const { subscribe, update } = writable<State>({...});
  return {
    subscribe,
    async updateAll() { await this.load(); },  // BAD
    async load() { ... },
  };
}
```
`this` inside an object-literal method has NO inferred type under strict TS. The method-call goes through but the **return-type inference of the whole `createProjectsStore()`** silently fails, and every site that reads `$projects` later gets a "missing subscribe" error. Replace `this.load()` with a call against the exported singleton (`projects.load()`) or extract the body into a hoisted helper that both methods call.

## 4. Multi-repo ship sequencing (added 2026-05-23)

**Symptom (forward-looking, not yet hit but documented as a prevention pattern)**: when a release spans two repos (e.g. public AGPL launcher + private paid-module repo) and both repos' work must land "together" for a feature to function, naive parallel push creates a window where one side is live but the other isn't. Users updating during that window see broken behavior.

**v0.2.31's example**: launcher's module-DB-migrations capability + RL module's first SQL migration files. The launcher ships the mechanism (manifest schema + apply pipeline + hub endpoints). The RL module ships the SQL files + container's write paths. Both must be live for the dashboard widget to show data instead of placeholders. If launcher v0.2.31 ships first AND a user updates immediately, they have a launcher capable of applying module migrations but no module that ships any. If RL v0.2.6 ships first, the RL module's `vct-module.json` declares a `db` block the older launcher doesn't understand (silently ignored — manifest field is optional + forward-compat).

**Mitigation pattern: launcher-first, asymmetric forward-compat**:

```
T+0      Launcher v0.2.31 push.    Mechanism is in place. No module uses it yet — dormant.
T+15min  Launcher CI binaries live.
T+30min  User updates launcher (new mechanism present but no behavior change for existing modules).
T+2h     Paid-module v0.2.6 push. Module's `db` block declares migrations; launcher's
         apply pipeline picks them up at module-update time.
T+2h15m  User updates the paid module via launcher GUI. Migrations apply. Tables populate.
T+2h30m  Dashboard widget shows live data (placeholder → real).
```

Key rule: **the substrate (launcher) ships first; the consumer (paid module) ships second.** This way, no live install ever has the consumer trying to use a capability the substrate doesn't expose yet. The reverse order would either silently fail (the consumer declares fields the launcher ignores → degraded mode) or hard-fail (consumer at runtime calls hub endpoints the launcher doesn't expose → 404).

**The asymmetric forward-compat requirement**: launcher's manifest parser MUST treat the new `db` block as optional + ignore-unknown — so the new launcher reads OLD manifests (no `db` block) without crashing, AND the old launcher (pre-mechanism) reads NEW manifests (with `db` block) and ignores the block as an unrecognized field. Serde's `#[serde(default)]` + `#[serde(skip_unknown)]` on the manifest struct gives you this for free; verify before shipping.

**The downgrade-resistance corollary**: SQLite migrations are append-only (no down-migrations in this codebase). Once launcher v0.2.31's migration 019 (`module_db_migrations` table) runs, downgrading to v0.2.30 still works for the user (v0.2.30 doesn't touch the new table; SQLite ignores extra tables), but the migration row stays. If you ever need to rollback, fix-forward via v0.2.31.1 patch; don't try to undo a shipped migration.

**Coordination protocol checklist** (use when planning a multi-repo ship):
1. **Confirm asymmetric forward-compat** on both sides BEFORE either repo tags. New fields must be optional + ignored-by-old. New endpoints must return 404 (not crash) when called against the old server.
2. **Confirm ship-window expectations** (both chats commit to a window — e.g. "RL chat pushes v0.2.6 within 24h of launcher v0.2.31 binaries live"). Document the T+X timing explicitly so future incident response can recover.
3. **Confirm rollback constraint** — usually the substrate is downgrade-resistant; the consumer is forward-compat. Both chats accept "fix-forward, no rollback" before T+0.
4. **Pre-stage the consumer's work** — the paid-module chat drafts SQL + writers in their worktree against the substrate's spec BEFORE the substrate ships. Reduces the T+2h window to "test against live + push" rather than "implement + test + push".
5. **Smoke-test before declaring "shipped"** — orchestrator does an end-to-end manual flow (`/state_summary` → hub write → backfill → dashboard read) before the release is considered green.

## Verifier pattern for batch PR review

After this session I wrote a small `verify-subagent-pr.sh` that, given a PR number + optional pattern, prints:
- PR metadata (title, branch, state, mergeStateStatus)
- The actual merge-base + commits ahead vs `origin/main`
- Files changed with totals
- CI failures (filter on `fail|FAIL`)
- Touch points that might need cross-PR coordination (`.claude/hooks/`, `templates/hooks/`, `settings.json`, `install.{py,sh,ps1}`, `update_project_v2`)

The merge-base check is the load-bearing diagnostic — it instantly tells you whether the PR is current or stale.

## 6. Integration-time lessons from a 6-branch parallel build (2026-05-25)

Phase 1 + 1.5 of the diagrams-integration plan in `vibecoded-orchestrator` spawned 6 Opus agents in parallel (DB, wrapper MCP, UI, indexer, conditional template, retrieval CLI). All 6 landed cleanly on `main` with 2407 tests passing. Five concrete failure modes surfaced ONLY at integration / sequential-merge time — not visible from any single branch's tests.

### 6a. Migration-number collisions are a near-certainty in active parallel work

**Symptom**: my Phase 1.1 agent wrote `migrations/021_diagrams.sql`. Meanwhile a SEPARATE chat working on v0.2.33 in different `/tmp/vco-wt-v0233-*` worktrees landed `migrations/021_module_installs_broken_status.sql` on `main`. Direct collision — same slot, different content.

**Recipe** (validated end-to-end):
1. Rename the SQL file: `git mv migrations/021_X.sql migrations/022_X.sql`.
2. Bump the entry in `db/migrations.rs`: `version: 21` → `22`, `include_str!("migrations/022_X.sql")`. The collision shows up as a textbook 3-way conflict; resolve by keeping BOTH `Migration { version: 21, ... }` AND `Migration { version: 22, ... }` entries in order.
3. Rename the test file: `git mv tests/test_migration_021_X.py tests/test_migration_022_X.py`.
4. `sed` the contents (`021` → `022`, `Migration 021` → `Migration 022`, `TestMigration021X` → `TestMigration022X`).
5. Commit as a separate `chore: renumber` so reviewers see the renumber distinctly from the original feature.

**Prevention impossible without cross-chat coordination**: each chat picks "the next number" against its own view of `main`, which by definition lags real-`main` if multiple chats are landing concurrently. Treat the renumber as part of the integration step, not as a regression.

### 6b. Parallel agents stubbing each other's APIs produces 4 distinct seam-mismatch failure modes

When 6 agents work in parallel and each stubs the others' as-yet-unmerged APIs (against a plan spec), four classes of mismatch surface at integration:

1. **Two canonicals for the same file** (e.g. both 1.2 and 1.5.A wrote `vco_lib/diagram_paths.py`). Both believed they owned it. Resolution: pick the richer/more-tested one, append any unique-to-the-other functionality (in this case, 1.5.A's `_cli` for the PreToolUse hook), drop the duplicate.

2. **API contract drift between stub and real impl** (e.g. 1.2's `validate_scoped_path(path, kind) -> str | None` vs 1.5.A's stub `validate_scoped_path(file_path) -> tuple[str, str, str]`). The plan-spec wording was ambiguous enough that two reasonable readings produced different APIs. Resolution: pick one (1.2's, the richer error-message API), write an adapter wrapper (`_validate_scoped_path` re-derives the triple from the now-validated path).

3. **Missing helper exports** (e.g. 1.5.C's CLI imported `_read_sidecar`, `_sha256_bytes`, `drop_diagram_by_hash` from the indexer; 1.5.A's real indexer never exposed those names). The stub had them inline; the real one didn't. Resolution: add public helpers to the real impl — they're integration-glue, not feature scope.

4. **Missing instrumentation flags** (e.g. CLI counted `row.wrote_sidecar` / `row.wrote_weaviate` but the real `DiagramRow` dataclass had no such fields; the stub did). Resolution: real impl sets them as dynamic attributes on the returned row. Side-effect: forced `_weaviate_upsert` to return `bool` instead of `None` so the "skipped vs wrote" distinction reached the caller.

**Generalization**: when handing parallel agents a plan-spec API surface, ALSO enumerate every helper/attribute the consumers will read from it, not just the public entry points. The "internal" details ARE the seam.

### 6c. Idempotency contracts must explicitly cover every persistence layer

1.5.A's `index_diagram` was DB-idempotent (UPSERT) but unconditionally rewrote the sidecar JSON file on every call — even when the content hadn't changed. The rebuild-CLI's "second run = no-op (mtime preserved)" contract broke immediately. The DB layer was the only one with idempotency-by-construction; the sidecar layer needed an explicit content-hash compare before write.

**Pattern**: any "idempotent" claim spanning multiple persistence layers needs an explicit hash/version check per layer. "We use UPSERT" doesn't cover file writes; "we use atomic file writes" doesn't cover external-service calls. Each layer has its own dedup mechanism.

### 6d. Graceful degradation for "no launcher context" is required for CLI tools

1.5.A's `index_diagram` was designed assuming a launcher-managed environment (SQLite DB at `~/.vct/launcher.db`). When 1.5.C's `vco rebuild-diagram-index` test fixtures created tmp project folders WITHOUT a launcher.db, every index call raised. The CLI is supposed to work for ad-hoc usage outside the launcher.

**Fix pattern**: detect both "DB file absent" AND "schema not applied" (`sqlite3.OperationalError "no such table"`), fall back to sidecar-only mode with a log message. Sidecar carries enough metadata (content_hash, derived fields) for retrieval to work without the SQLite registry.

### 6e. Sequential-merge dependency ordering matters; pick it deliberately

Naive order (file-creation chronology, branch-name alphabetical) produces avoidable conflicts. Right order is **dependency topological**, derived from the spec:

For diagrams work: `1.1 (DB foundation) → 1.5.B (independent template) → 1.2 (wrapper, needs install + hub) → 1.5.A (indexer, needs DB + path validator) → 1.5.C (retrieval CLI, needs indexer) → 1.3 (UI, consumes all Tauri commands)`.

Why each merge boundary matters: 1.5.A's tests pass after merge ONLY because 1.2's `diagram_paths.py` is on main first. 1.5.C's tests pass after merge ONLY because 1.5.A's real indexer is on main first. Wrong order = each merge fights its predecessor + drags fixture work forward into the wrong commit.

### 6f. Post-merge cleanup commit is part of the work, not optional

Each agent leaves scaffolding behind: "STUB lives at X", "Phase Y.Z dependency wrapper", "until sibling lands", `_local_<fn>` fallbacks. After integration these are dead code AND active source of future confusion (a reader sees "STUB" and wonders if it's still a stub). Schedule a single cleanup commit immediately after the last merge:

- Drop fallback branches (`try/except ImportError → _local_X`).
- Collapse "Phase coordination notes" docstring sections to "Cross-module dependencies".
- Replace "STUB" / "until sibling lands" with present-tense descriptions of what the code does now.
- Re-target tests that exercised removed fallbacks at the canonical functions.

This session's cleanup removed 173 LOC of scaffolding while adding 60 LOC of substantive refactoring (net -113), no tests lost.

### 6g. Other-chat-landing-commits-mid-fanout (v0.2.33 lesson, 2026-05-25)

When multiple human sessions are active in the same repo (parallel feature work), commits from a sibling chat can land on `main` BETWEEN your `git pull --ff-only` (when you created the agent worktrees) and your cherry-pick of the agents' returned branches. Symptom: `git log main..agent-branch` shows the agent's commit + the sibling chat's commits interleaved as "unmerged", which looks alarming but is harmless — git's `cherry-pick` correctly applies just your agent's diff on top of the new main HEAD.

Defensive checks before declaring fanout broken:
- `git merge-base main agent-branch` should return the commit the worktree was created from (your old HEAD).
- `git log --oneline main..agent-branch` should show ONLY the agent's commit (other-chat commits don't reach the agent branch since it was branched before they landed).
- `git log --oneline agent-base..main` shows the sibling's intervening work — verify it doesn't touch the same files as your agent's diff before cherry-picking.

If files overlap, cherry-pick generates conflicts you resolve normally. If files are disjoint (v0.2.33 case: my work was `module_catalog_client.rs` + `module_manifest_extract.rs` + `installer_engine.rs` + `lib.rs`; sibling chat's Phase 0/1 work touched `vco_lib/`, `scripts/vco*`, `bundled_versions.rs`), cherry-pick is clean even though main has moved 10+ commits.

Discipline: still spot-check the cherry-pick result via `cargo test --lib` + `npm run check` after EACH cherry-pick, not just the final one.

### 6h. Planning-doc placeholder names vs deployed-function names (v0.2.33 lesson, 2026-05-25)

When the planning doc invents a placeholder endpoint name (e.g. "rl-pull-token" in the architecture review), that name propagates through every downstream artifact: agent prompts → agent-generated code → seed files → CI heredocs → published JSON schema → KG nodes. If the actual deployed function uses a different name (`rl-artifact-url`), every reference is wrong + the load-bearing seed JSON sets a 404 endpoint in production.

Catch: before agents touch downstream files, **grep the deployed-function names** (`ls launcher/supabase/functions/`) and reconcile against the planning doc's naming. If the planning doc invented a name, either rename the deployed function OR update the planning doc to match reality.

Recovery: a single `replace_all` across the load-bearing file (the one consumed by another chat's CI) + correction-note headers at the top of historical planning docs (don't rewrite history) + a small cleanup commit fixing all instances in source-controlled files. ~10 minutes if caught before push; if caught post-push, requires a hotfix release.

This is a specific case of "planning artifacts drift from runtime reality" — the broader pattern is: validate every endpoint URL, table name, env var, file path in a planning doc against `ls`/`grep` of the actual codebase BEFORE spawning implementation agents.

### 6i. Hidden DAG cycle in fanout sequence (v0.2.33 lesson, 2026-05-25)

The draft v0.2.33 plan sequenced agents as "D → A+F → C → B → E". The architecture review caught that this had a real hidden cycle: Agent B's TESTS needed Agent C's post-install path coherent (B reads `~/.vct/modules/<id>/` via C's atomic write), AND Agent B's COMPILE needed Agent A's client (B calls `cached_module_catalog(db)`). The fix: D first → F + A + C all parallel (each only depends on D's schema) → B after both A+C land → E after B lands.

How to spot a cycle:
- For each agent, list ALL dependencies: compile-time imports, test-time fixtures, behavior-time data sources. Don't just list "the obvious" dependency.
- Draw the DAG. If any agent has an arrow from TWO sequence stages, that's the integration point + its merge must happen AFTER both inputs.
- Tests are often the cycle-detector — an agent compiles fine in isolation but its tests need integrated state.

Architectural review caught this BEFORE any agent ran. Catching it after would mean Agent B's tests failing at cherry-pick time + a re-spawn.

## 7. Audit-after-merge: per-branch tests pass, integration silently broken (diagrams v0.2.34 lesson, 2026-05-25)

After Phases 0/1/1.5/2/3 of the diagrams feature merged on `main` with 2494 passing tests + zero regressions, a read-only "wiring audit" agent (spawned right before declaring shipped) found **7 ship-blockers + 1 ship-risk** that NO per-branch test had caught:

1. `index_diagram_async` ignored `diagrams_collection` and called `index_diagram` positionally — every save succeeded at writing the .mmd, never indexed to Weaviate.
2. `DIAGRAMS_COLLECTION` env var never written anywhere (not by `config_projection`, not by `install.py`, not by `project_init`).
3. `<Project>_Diagrams` Weaviate class never created at bootstrap.
4. `is_project_module_active` Tauri command declared in DB layer but never registered as `#[command]` → DiagramsTab silently never rendered.
5. `read_env_var` Tauri command missing → Wayland fallback silently dead.
6. Auto-snapshot on edit never wired (hook only called indexer, not snapshot CLI).
7. `set_project_module_enabled` Tauri command didn't trigger CLAUDE.md re-render.
8. Cross-project diagrams access reused `VCT_KG_ACCESS_LIST` (wrong granularity — granting KG leaked diagram visibility, granting only diagrams enabled no peer-collections).

Each broken-wire passed:
- Its own branch's unit tests (mocked the next link).
- The post-merge full regression sweep (no test exercised the end-to-end chain).
- The svelte-check / cargo test layer (compile-correct).

**Root cause**: parallel agents stubbed each other's APIs against the plan spec. Stub mocks return "OK" for whatever interface the consumer side asks for. The integration TEST that would catch the mismatch is "save a real .mmd, query Weaviate" — and that test doesn't exist on any branch (each agent was scoped to "test my surface, mock the seams").

**Fix discipline**:
1. After ALL parallel branches merge, run a **read-only audit agent** scoped to: walk every end-to-end chain in the plan spec, verify each link reads/writes what its sibling actually produces. NOT a code-review pass (that catches subtle bugs); this catches *broken wires* — clean code that doesn't connect.
2. The audit's question: "if the user did $WORKFLOW, would it actually work?" Not "does each component compile?"
3. Audit agent uses code-explorer (read-only), reports MISSING / PRESENT-AND-WIRED / PRESENT-BUT-BROKEN per chain, with severity SHIP-BLOCKER / SHIP-RISK / NICE-TO-HAVE.
4. The audit costs ~10 minutes of agent time vs ~hours of "why doesn't my diagram show up?" user reports later.

**Generalisation**: any multi-agent feature that crosses N persistence layers (file → DB → vector store → MCP-routed-tool-call) needs an integration test that exercises ALL N at once, ideally via a `verify-<feature>` CLI subcommand the user can run after install. We built `vco verify-diagrams` for exactly this in the same session.

## 8. Shared working tree between concurrent chats (diagrams v0.2.34 lesson, 2026-05-25)

A separate orchestrator chat working on v0.2.33 (release tagging, RL chat coordination, CHANGELOG entry) used the SAME `/home/.../vibecoded-orchestrator/` working directory as my Phases 0–3 chat — without us realising at first. Detection: my `git status` showed `M CHANGELOG.md` that I hadn't touched. Their working-tree edits and mine commingled in the same checkout.

**Risks**:
- Accidentally `git add .` and commit the OTHER chat's uncommitted work.
- Both chats edit the same file at the same time → second write clobbers first (no merge conflict; the editor doesn't know).
- Worktrees DO solve this for explicit branches, but the parent checkout is shared.

**Workaround that worked**:
- Always `git add <specific files>`, never `git add .` or `git add -A`.
- Before each commit, `git diff --cached --name-only` and verify the list matches only what YOU touched.
- For files genuinely needed by both chats (CHANGELOG.md — they're adding v0.2.33 section, I'm adding [Unreleased] entry): coordinate via shared planning doc, do the edits in non-overlapping regions of the file, commit independently.

**Better future fix**: each chat does ALL its work in an explicit worktree (`/tmp/vco-wt-<chat-tag>`), never in the main checkout. The shared `/home/.../vibecoded-orchestrator/` becomes integration-only, modified only when a chat explicitly merges.

### 8b. Silent branch-HEAD slip mid-session (v0.2.47 lesson, 2026-06-04)

Stronger version of §8: not just shared working tree, but shared `HEAD`. While one chat works a feature branch (`rl/citation-detection-mcp-side`), another chat ran `git checkout main` for its own work. Subsequent `git commit` calls from the first chat landed on `main` (whichever branch HEAD was on at commit-time), NOT on the intended feature branch — even though the feature branch was the last one the first chat explicitly checked out.

**Symptom**: `git log main --oneline -3` shows a commit you intended for the feature branch interleaved with the OTHER chat's commits. `git branch --show-current` returns `main` even though you remember checking out `rl/...`.

**Mechanism**: `HEAD` is a single pointer in `.git/HEAD`. There is no per-process / per-chat-instance `HEAD`. When chat A runs `git checkout main`, chat B's next `git commit` lands on main — chat B has no way to detect the swap except by re-reading `.git/HEAD` (i.e. `git branch --show-current`) immediately before every commit.

**Detection**: ALWAYS pair every `git commit` with a preceding `git branch --show-current` check. The branch name in the output is the only ground truth.

**Recovery (clean, no data loss)**:
1. Cherry-pick the misplaced commit onto the intended feature branch: `git checkout rl/<feature> && git cherry-pick <misplaced-sha>` — produces a new SHA (descendant of the feature branch tip).
2. Reset main back to where the other chat was: `git checkout main && git reset --hard <pre-misplaced-sha>` — undoes the misplaced commit from main's history.
3. Switch back to the feature branch and continue.

This is a destructive `reset --hard` on `main`, BUT (a) you authored the misplaced commit yourself only seconds earlier, (b) the cherry-pick already captured its content on the feature branch, and (c) the misplaced commit was never pushed. The CLAUDE.md "don't auto-destroy" rule doesn't apply when the destroyed work is preserved on another local branch with a different SHA.

**Prevention** (cheap, do it always): before every `git commit` in a long-running session, run `git -C <repo> branch --show-current` and assert it matches the branch you intended. Treat the check as part of the commit ritual, not optional. In a Bash session that may have been mid-`cd` or mid-`git checkout` by another chat, the check is the only ground truth.

## 9. MCP tool-call PreToolUse matchers (diagrams v0.2.34 lesson, 2026-05-25)

The Phase 1.5 PreToolUse path-validation hook only matched native `Write|Edit` tool calls. It did NOT catch MCP-routed saves like `mcp__mermaid__save_diagram` — those bypass the native-tool matcher entirely. The wrapper MCP validates internally, but a buggy/disabled wrapper would let violations through.

**Fix**: add a second matcher block per template that fires on `mcp__mermaid__.*|mcp__excalidraw__.*`, pointing at the same hook. The hook then probes `tool_input` for common path-key names (`file_path`, `path`, `output`, `target`, `scene_path`, `name`) since each MCP tool's argument schema is different.

**Generalisation**: any PreToolUse-style guard that's supposed to protect a path/resource MUST register matchers for BOTH native tools AND every MCP tool that touches the same resource. The matcher syntax is per-template-block, so adding a second block alongside the native one is cheap.

## Lesson — audit-before-fanout has higher leverage than I gave credit for (v0.2.37, 2026-05-27)

The v0.2.37 hotfix fan-out started with 4 agents (V37-A/B/C/D) on what I thought was the right scope: pull-deadlock + Svelte modal + install-bundle template gaps + docs. Mid-flight, the user pushed back ("don't assume features are missing, investigate first") and added two more agents (V37-E for resolver consolidation, V37-F as a read-only auditor + V37-G for CI deploy automation).

**V37-F's audit (read-only, no code, ~10min runtime) found the actual root cause that the other 4 agents would have missed**: `apply_project_env_via_python` at `projects_v2.rs:1626` doesn't pass `--orchestrator-root` to its Python subprocess. The Python writer correctly omits `VCT_ORCHESTRATOR_ROOT`/`VCT_INFRASTRUCTURE_DIR` when its arg is `None`. Every freshly-installed project since the 2026-05-25 Rust-to-Python writer migration has been broken silently.

Before V37-F's findings landed, I had pointed V37-E at a resolver-consolidation refactor (the dual `find_local_repo_root` + `resolve_install_root_sync` codebase quirk). That work is legitimate quality but is NOT a user-facing bugfix. V37-E was halfway through committing it when the audit landed; I sent a redirect message via SendMessage and V37-E pivoted to F1 + F6 (the actual fixes) layered on top of the consolidation work. Net result was clean (3 commits — consolidation + seed + F1+F6), but if V37-F had run BEFORE V37-E spawned, V37-E's prompt would have been "fix F1 + F6 directly" and the consolidation could have been a quality follow-up.

**Generalised rule**: for any hotfix fan-out that includes "fix the install/update flow", spawn a READ-ONLY audit agent FIRST and wait for its findings before assigning the other code-changing agents. The audit takes ~10-30min runtime and reframes the entire task set when the surface is poorly understood. Spending 20min of wall-clock on audit before parallel code work is cheaper than two agents producing complementary-but-misaligned work that then needs reconciliation.

**Anti-pattern caught**: in V37-C's case the agent ALSO independently found the F1 root cause from the Python side (threading `orchestrator_root` through `vco_lib.project_init.install_project_bundle`) — so both agents converged on the same fix from opposite ends. Lucky outcome here, but the overlap could just as easily have been a conflict. Audit-first would have routed the work cleanly: V37-C on template-script bugs only, V37-E on Rust caller F1 + backfill F6 only, no overlap.

**Process update (added to feedback memory `review_agent_diffs_before_merging`)**:
- For "fix the install flow" / "fix the update flow" / "audit production-bug surface" tasks, ALWAYS spawn a read-only audit agent as the FIRST agent in the fan-out
- Wait for audit completion before assigning code-changing agents
- Use audit findings to write the code-changing agent prompts with verified line numbers + verified root-cause framing
- Treats "agent runtime" as a cheap resource for reducing the cost of misaligned downstream work — a 1-hour audit can save 4 hours of wasted code work

The v0.2.37 ship still landed cleanly (per-branch diff review caught the few drift cases — V37-F's force-add of audit to gitignored `.claude/context/` and the path leak inside the audit body). But the integration tree would have been simpler if V37-F's findings had bounded the other agents' scope before they ran.

## Lesson — per-branch diff review during integration catches what agent reports miss (v0.2.37, 2026-05-27)

Per the `feedback_review_agent_diffs_before_merging` memory (saved 2026-05-27), every parallel-agent integration merge MUST run diff review before merging — not after. Each branch gets: git log vs report cross-check, --stat scope check, knowledge/ write check, path/token leak scan, TODO/FIXME scan, --no-ff merge, scoped gate run, only THEN proceed to next branch.

v0.2.37 integration caught two drift cases that wouldn't have appeared in any agent's final report:

1. **V37-F force-added audit doc to gitignored `.claude/context/`**: agent's final report described "audit doc committed to `.claude/context/install-update-audit-2026-05-27.md`" — accurate. But the path is gitignored, so the agent used `git add -f`. This is a working-state-vs-shipped-artifact discipline violation: `.claude/context/` is for in-progress notes the maintainer wants on disk, NOT for files shipped in the public release tarball. Caught during merge step 3 (file scope check), fixed by `git mv` to `docs/audits/install-update-audit-2026-05-27.md` (the intended-public location).

2. **Absolute path in audit body**: a personal absolute path appeared in the audit text. Path leaks have to be caught at the diff-grep step (memory's step 4), because agent final reports never include the body content verbatim. Redacted to a relative-path placeholder that preserves structural reference without leaking the host path.

Both were caught BEFORE the v0.2.37-integration merge to main, in standalone cleanup commits with clear messages. If integration had been "merge all branches then gate once at the end" (the lazy-but-fast approach), these would have either reached main as-is or required a confusing late-stage revert. Per-branch discipline pays for itself.

## 10. CHANGELOG conflict markers survive `git commit --no-edit` after a failed Edit (v0.2.40 lesson, 2026-05-30)

During v0.2.40's W40-C merge, a CHANGELOG conflict needed manual resolution. The Edit tool refused (`File has not been read yet`), but the subsequent `git add CHANGELOG.md && git commit --no-edit` accepted the file **with the `<<<<<<<` / `=======` / `>>>>>>>` markers still in it**. Git's content validation doesn't enforce marker-free files in markdown — it'll happily commit them as if they were text.

Caught at the next merge attempt (W40-B), when a marker-scan habit surfaced them still present in `## [0.2.40]`. Recovery: re-`Read` the file, `Edit` the resolved version, `git add && git commit --amend --no-edit`. Recovery was clean because no push had happened yet — but the local main was carrying a poisoned merge commit for ~5 minutes.

**Discipline that prevents recurrence**: after EVERY merge-conflict resolution (especially documentation files), run a marker scan BEFORE the merge commit lands:

```bash
git diff --check    # git's own combined whitespace + conflict-marker detector
# OR more explicit:
grep -rnE "^(<{7}|={7}|>{7})" --include='*.md' --include='*.rs' --include='*.py' . || echo "CLEAN"
```

If the merge commit was already made (because git accepted it), `git commit --amend` is the right recovery — don't add a "fixup" commit; just replace the bad merge in place. Force-push only if push already happened (and recall the policy: never force-push to main without explicit user OK).

**Sibling lesson**: the Edit tool's "file not read yet" guard SHOULD have prevented this — but the failure was silent (the Edit returned an error message that scrolled by, then the next bash invocation didn't notice). When an Edit fails during merge resolution, re-Read the file and retry BEFORE staging. Don't trust file state across tool boundaries.

## 11. Discovery sub-step pattern decouples blocked items in large fanouts (v0.2.40 lesson, 2026-05-30)

For the v0.2.40 16-item ship plan, three items had unknown architecture that would have blocked their fanout: (a) is the Supabase `rl-latest-weights` edge function deployed? (b) what did the orphan `RlRerankerDashboardWidget.svelte` originally do? (c) what is Fabio's `feat/orchestrator-update-progress-modal` branch going to touch?

Pattern applied: spawn 3 READ-ONLY **discovery agents** in PARALLEL, before any code-changing agents in the same dimension. Each writes a concise (~120-line) findings doc to `.claude/context/reviews/<release>-pre-push-<date>/discovery-<id>-<topic>.md`. Outcome:

- A1 (Supabase): found function never deployed; surfaced clean A-vs-B decision (redirect vs deploy).
- A2 (widget): found mount site (RL config tab), 3 missing getter Tauri commands, ~50 LoC rewire scope (refuting the "just delete" instinct).
- A3 (Fabio): mapped his expected file surface; identified ONLY L1 (license) has a real Svelte-store collision; produced exact namespacing guidance (`showLicenseManager`, NOT `showLicense` / `showModal`).

All 3 discoveries completed in ~5-10 min wall-clock each, in parallel — total <15 min. Their outputs were inlined directly into the subsequent code-agent prompts (e.g. L1's agent prompt explicitly cites A3's `showLicenseManager` constraint). Without the discoveries, L1 might have used the wrong store flag name and collided with Fabio's branch at integration time.

**Key property**: discovery agents are MUCH cheaper than code agents (no test writing, no commits, no build steps). When in doubt about an item's scope, spawn a discovery first; it's free insurance. Cost ratio is roughly 1:5 (discovery vs code) for typical scopes, so even 2-3 discoveries that "find nothing surprising" are cheaper than 1 code-agent rework.

**When to apply**:
- Item touches a subsystem not active-in-context (e.g. months-old orphan code)
- Item depends on external state (deployed services, in-flight branches on other people's machines)
- Item's scope is ambiguous in the source review (synthesizer says "audit first")
- Cross-agent collision risk is non-trivial

**When NOT to apply**:
- Item is small + scope is clearly defined in spec
- The discovery would just re-read what the synthesizer already cited
- A code agent's first action would naturally be the same investigation

Discoveries also surface USER-action items early (credentials needed, decisions needed) rather than blocking mid-fanout. v0.2.40 surfaced 2 user decisions (R4 redirect-vs-deploy, X1 macOS scope) at audit time, not at code-fanout time.

**Sibling pattern**: the v0.2.40 5+2+1 multi-Opus pre-tag review at `peer-review-via-parallel-opus-subagents.md` is the upstream complement. Discovery is "what's the scope before code"; peer review is "did the code we wrote actually do what we thought."

## 12. Verify agent-claimed deliverables before merge — "I wired the button" ≠ "the button works" (v0.2.42 lesson, 2026-05-31)

During the v0.2.42 8-worktree fanout, agent W6 reported "RT-4 Reset to global weights button — DONE" in its return summary. svelte-check passed, npm test passed, the file diff showed a button rendered. But a post-merge verification agent (D3, spawned later in the cycle to "verify reset-weights button + clean stale TODOs") found **4 functional defects**:

1. **Wrong command name**: `invoke('reset_weights_to_global', ...)` — actual registered Tauri command is `module_reset_weights_to_global` (W3 registered it under the prefixed name). IPC would 404 at click time.
2. **Missing `module_id` parameter**: command signature is `(module_id, project_id)`; only `{ projectId }` was passed. Rust handler would return `Err("module_id required")` on every click.
3. **No success toast**: spec required surfacing `ResetWeightsResult.version`. W6 only handled error path.
4. **No "nothing-to-reset" visibility gate**: button always rendered when module running, even when no prior download recorded. Spec said hide when `WEIGHTS_LAST_VERSION_KEY` absent.

**Why static gates didn't catch this**:
- `svelte-check` validates types + template syntax — doesn't know that "reset_weights_to_global" isn't a registered Tauri command name.
- `npm test` ran the component's render path but NOT a Tauri IPC round-trip (test mocks the invoke).
- Agent's own self-summary said DONE because all its declared sub-tasks (file modified, test passed) returned success. The DECLARED contract was incomplete vs the spec.

**Discipline that prevents recurrence**:

1. **Spec-vs-implementation diff before merge**: for any agent-claimed wiring of "GUI button calls Rust command X with params Y returns Z", do one of:
   - Read the agent's exact invoke call site + verify the command name matches the registered handler in `generate_handler!`
   - OR spawn a small verification agent on a fresh worktree that reads the spec + the impl + reports mismatches
   - OR run the actual UI in dev mode + click the button (heavy but truth-telling)
2. **Don't trust "DONE" without a concrete observable**: when the agent reports completion, the next question is "what's the observable that proves the user-facing behavior?" If the only observable is "code compiles + tests don't fail", that's necessary but NOT sufficient for IPC-bridged UI.
3. **Stale TODO comments are a signal**: D3 was tasked partly to "remove `TODO (W6)` markers." The fact that W6 left those TODOs while claiming "DONE" was a smoke signal that W6's mental model of "wired" didn't include "tested through the full IPC layer." Future agent prompts: when an agent declares a task done that has visible TODO comments related to that task, ALWAYS verify.
4. **Verification agents are cheap insurance**: D3's run cost ~250k subagent tokens + ~5 min wall-clock; W6's wrong implementation would have shipped to users without it. The cost-benefit favors a verify-agent for every multi-layer wiring claim.

**Sibling pattern**: this is the "consumer-side proof" complement to §11's discovery-agent pattern. Discovery = "what's the scope BEFORE coding"; this = "did the code actually deliver what I asked for AFTER it lands."

**When to apply automatically**:
- Any agent that crosses 3+ layers (Rust handler → Tauri bridge → Svelte component → user click → toast/state update)
- Any agent that touches paid-module surfaces (the cost of a broken paid feature is real money + support load)
- Any agent that touched a file the synthesizer marked "high-risk"

**When NOT to apply**:
- Single-layer fixes (e.g. a Rust function rename whose tests cover the call graph)
- Pure refactors with mechanical safety nets (cargo check + cargo test --lib)
- Adding tests (the test itself is the verification)

## 13. New runtime file lands on disk but Dockerfile COPY list isn't extended (v0.2.47 lesson, 2026-06-04)

Same family as §12 but earlier in the lifecycle: the file is correctly authored, correctly tested by Python unit tests on the host, correctly imported by the runtime code — and then the next image build silently doesn't ship it because the Dockerfile uses explicit per-file `COPY` rather than `COPY . /app`.

**Symptom timeline**:
1. Add `_training_targets.py` to `paid-modules/vct-rl-reranker/` (commit 2a, vendored helper for the unified target formula).
2. Add an import line in `retrieval_rl.py`: `from ._training_targets import compute_unified_targets`.
3. Host-side `pytest tests/` passes — the test runner sees the file on the filesystem.
4. CI builds image, push succeeds.
5. **Container starts → `ImportError: No module named '_training_targets'`.** The Dockerfile has an explicit per-file COPY list (`paid-modules/vct-rl-reranker/Dockerfile:169-176`) that didn't include the new file. Image ships without it.

**Why explicit per-file COPY exists**: it bounds the runtime image to a known minimal surface — `COPY .` would pull in `__pycache__/`, `tests/`, `state/`, every dev-only file, and arbitrary `_b64_chunk*.txt` cruft. The trade-off is correct: smaller, hermetic images at the cost of one extra line per new runtime file.

**Detection mechanism that should exist but doesn't**: a CI lint that diffs Python imports against Dockerfile COPY lines. Possible future work: a pre-commit hook that walks `from X import Y` / `import X` in every Python file under the COPY'd directories and asserts each transitively-imported file is on the COPY list. For now: human review.

**Fix when caught early**: just amend the COPY block with the new line. This was the v0.2.9 commit 2a hotfix in the RL chat (the file was committed in `6cd1aa9`, the Dockerfile COPY was missed; caught by the cross-commit audit and amended in `22c4f5b` before any tag).

**Generalisation**: ANY new runtime file added to a container-shipped paid module MUST be paired with a Dockerfile diff in the SAME commit. Treat the absence of a Dockerfile change as a smoke signal that the author forgot the runtime-shipping side of the work.

**Cross-link**: the discovery-agent pattern from §11 would catch this — a pre-commit verify step that lists every new file in `git diff --stat` and asserts each is referenced in either the Dockerfile (runtime files) OR the test config (test-only files) OR explicitly classified as "host-side only" in the commit message.

## 14. Live dev-machine services silently override test env-injection (v0.2.47 lesson, 2026-06-04)

**Symptom**: 26 tests across 6 files (KG access-list, hybrid_search, diagrams, shared-KG, get_node_info, search_knowledge) assert against env-vars they injected at setUp time. The assertion fails with the LIVE production-config value, NOT the injected one. Error shape:

```
AssertionError: ['VibeCodedOrchestrator_KnowledgeGraph', ...] != ['Alpha_KnowledgeGraph', ...]
```

— even though the test set `KG_COLLECTION=Alpha_KnowledgeGraph` in its env and reimported the module.

**Mechanism**: production code calls `_try_resolve_project_config()` → `vco_lib.project_config.resolve(...)` → HTTP call to the running `vct-hub` on the dev machine. The hub returns a populated `ProjectConfig` for THIS project (VCO_dev), which wins over `os.getenv(...)` in the code that constructs module constants (`KG_COLLECTION`, `SHARED_KG_COLLECTION`, etc.). The test's env-injection is invisible because the resolver returned a value first.

**Detection**: any test that ASSERTS on module-level "resolved" constants and FAILS with values matching live config — not the injected ones — is hitting this pattern. CI doesn't see this because CI doesn't run a vct-hub. Dev-machine-only failures.

**Fix pattern (v0.2.47 RL-6c)** — TWO-LAYER, neither alone is enough:

1. **Production-code env-guard**: at the top of every `_try_resolve_project_config()` (and every `_resolve_kg_collections()` / `_resolve_collections()` sibling in `templates/scripts/`), check `os.environ.get("VCT_DISABLE_HUB_RESOLVER")` and short-circuit to env-fallback when set:
   ```python
   if os.environ.get("VCT_DISABLE_HUB_RESOLVER"):
       return None  # caller's env-fallback path takes over
   ```

2. **`tests/conftest.py` autouse fixture** that sets `VCT_DISABLE_HUB_RESOLVER=1` for ALL tests EXCEPT an explicit opt-out list (tests that exercise the resolver itself — `test_caller_migration_step18.py`, `test_project_resolution.py`).

**Why the opt-out list matters**: making the guard always-on would silently break the resolver tests (they `mock.patch("vco_lib.project_config.resolve", ...)` and need the production code path to actually reach the patched function). The opt-out list is the canonical record of "tests that test the resolver itself" — keep it explicit so an accidentally-broken hub-resolver test surfaces loudly.

**Generalisation**: ANY production code that does best-effort I/O to a local service (hub, lock-files, daemons) for "default values" needs a test-mode short-circuit env-var + autouse conftest gate. The pattern repeats: lean-ctx had it (`LEAN_CTX_OFF`), the licensing tier check has it (`VCT_TIER_OVERRIDE`), the bash-shim has it (`VCT_DISABLE_HOOKS`). The discipline: every "best-effort local I/O for defaults" must have a `VCT_DISABLE_X` guard.

**Cross-link**: [[uses::Launcher / Hub Single-Writer Principle]] — the hub being a local service that the tests can hit is the same property that makes the single-writer principle work. The trade-off is that the hub's "single source of truth" semantics need test-mode escape hatches.

**v0.2.46 reinforcement (2026-06-04)**: hit again during the KG/Dev/access auto-heal work. **21 pre-existing test failures** on the dogfood box that ALL traced to the same root cause: env-var-fallback tests losing to live hub resolution. v0.2.47 RL-6c had landed the gate at `weaviate_mcp/server.py::_try_resolve_project_config` but NOT at `vco_lib.project_config.resolve()` itself — so CLI-side callers (`templates/scripts/get_node_info.py`, `templates/scripts/search_knowledge.py`, etc.) bypassed the guard entirely.

**v0.2.46 fix completes the pattern**: the gate now lives **inside `vco_lib.project_config.resolve()`** (at the top, before any HTTP probe). Every consumer of the resolver inherits the short-circuit — `_try_resolve_project_config` wrappers can be simplified or even removed in a future cleanup. The autouse `tests/conftest.py` fixture sets `VCT_DISABLE_HUB_RESOLVER=1` for the whole orchestrator suite via per-file opt-out (the v0.2.47 RL-6c shape — tests that explicitly want to exercise the resolver like `test_project_config.py`, `test_caller_migration_step18.py`, `test_project_resolution.py` are listed in `_RESOLVER_OPT_OUT_FILES`).

**Refined discipline (one canonical place for the gate, not many)**: the v0.2.47 RL-6c version put the guard at the MCP-side wrapper. v0.2.46 moved it into the shared library function. **The shared-library location is the correct one** — every wrapper inherits it, every script inherits it, every test inherits it. Per-wrapper guards drift over time (you discover a new caller, you forget to add the guard). One-place gate stays consistent by construction.

**Cross-link**: the same bug class fixed in v0.2.47 RL-6c (`weaviate_mcp/server.py`) and v0.2.46 (`vco_lib/project_config.py`) is now also pinned by [[uses::KG-Binding Clobber Bug + v0.2.28 Seed-Guard]] in its hypothesis-falsification log ("Hub restart would fix stale-shared-row warning → FALSE").

## 15. Hard cutover via writer-class internal rewire keeps callers untouched (v0.2.47 RL-6c lesson, 2026-06-04)

**Pattern**: when changing the TRANSPORT of a write path (JSONL → DB-via-HTTP, file → socket, sync → async-with-same-shape) but keeping the call shape, rewire INSIDE the writer module. Caller code stays identical. Tests at the caller level continue working unchanged.

**Concrete example**: pre-v0.2.47 `RLTelemetryWriter.log_retrieval(...)` wrote a JSONL file via `self._local.log_retrieval(...)` plus a consented-upload queue. v0.2.47 replaces the JSONL leg with a hub HTTP POST. `weaviate_mcp/server.py` has ~7 call sites of `writer.log_retrieval(...)` — NONE of them needed edits. The change was 13 files (the writer module + tests for it) instead of the ~30 files a caller-side rewrite would have touched.

**Rules of thumb**:

1. **Inject the transport via constructor kwarg with a default**. The writer's `__init__` accepts `hub_post_fn=None` (defaults to the production fn). Tests pass a capturing stub. Production code passes nothing — the default wins.
2. **Preserve the public method signatures byte-for-byte**. Even kwarg order matters for `**kwargs` forwarders. Adding new kwargs is fine; renaming/removing existing ones breaks transparency.
3. **Document the cutover in the module docstring** so the next reader understands why the implementation looks one way but the dependency tree from `weaviate_mcp/server.py` doesn't reflect that.
4. **Update tests that read implementation-side artifacts** (e.g. `log_path.read_text()`). They need to switch to reading the test stub's captured state. This is the ONE class of caller change that DOES leak — but it's confined to the writer's own test file.

**When NOT to do this**: when the new transport has fundamentally different async/sync nature, error semantics, or batch shape, the call shape SHOULD change to make those new semantics visible. Don't hide a sync→async change inside a sync-looking method.

## 16. Live class attribute as schema version: bump propagates instantly through every reader (v0.2.47 RL-3 lesson, 2026-06-04)

**Pattern**: `_REQUIRED_SCHEMA_VERSION = str(RLDataLogger.SCHEMA_VERSION)` is the loader's filter constant. Read at import time, holds the LIVE value of the class attribute. Bumping `SCHEMA_VERSION` 2 → 3 in one commit instantly invalidates every test fixture that ships `"schema_version": "2"` — they're now silently dropped by step 3 of the filter.

**Symptom**: 20 tests in `tests/test_training_loader.py` go from green to red with the shape `assert 0 == 1` / `assert results == []` — meaning every single fixture is dropped before any assertion can run. The dropped-row count is exactly equal to the fixture count.

**Detection** (when a SCHEMA_VERSION-style class attr is bumped):

1. `grep -rn 'schema_version.*"[0-9]"' tests/` — finds every literal version string in fixtures.
2. `grep -rn 'SCHEMA_VERSION' src/ tests/` — finds every reader that reads the constant.
3. For each reader, audit: do the test fixtures ship the OLD or the NEW version?

**Fix**: bump every fixture string to the new version in the same commit as the SCHEMA_VERSION bump. The v0.2.47 RL-3 commit missed this and produced 20 silently-dropped-fixture failures that surfaced 4 commits later when the broad sweep ran.

**Prevention**: add a contract test that imports the version constant and asserts ALL fixture builders use the SAME value:

```python
def test_fixtures_track_live_schema_version():
    from claude_mcp_servers.rl_client.rl_logger import RLDataLogger
    assert _make_retrieval()["schema_version"] == str(RLDataLogger.SCHEMA_VERSION)
```

Cheap insurance — add wherever fixture data and a runtime constant must agree.

**Sibling pattern**: this is the same shape as "manifest fixture drift" (the v0.2.46 fix where `vct_rl_reranker` test expected version `0.1.1` but the live manifest was at `0.2.8`). Both are "test fixture hardcoded a value that the production code reads from a live source." Same fix: bump the fixture, AND consider whether the fixture should derive from the live source dynamically (the manifest fix bumped statically; the RL-3 fixture could have been written to read `RLDataLogger.SCHEMA_VERSION` dynamically and never need bumping again).

## 17. Refactor → extend regression guards, don't let them pass vacuously (v0.2.46 KG-AUTO-HEAL-E lesson, 2026-06-04)

**Setup**: v0.2.42 V46-A introduced a 4-property safety triad in `install.py::_batch_query_weaviate_content_hashes` (no `where: Like "%"` / `limit: 10000` / errors-array inspection before data / saturation warning). V46-B added a source-inspection regression guard `V46BSourceRegressionGuardTest` that calls `inspect.getsource(install._batch_query_weaviate_content_hashes)` and asserts the broken pattern doesn't appear. The guard was the load-bearing defense against the bug reintroducing itself.

**v0.2.46 KG-AUTO-HEAL-E extraction**: the same hot path got refactored into `vco_lib.kg_sync.batch_query_content_hashes` (shared between `install.py --update` and the new KG-rebind re-sync). The install.py function became a **3-line wrapper** that delegates to the new helper. The V46-B guard still passed — but **vacuously**: the wrapper contains no GraphQL at all, so `_has_broken_filter()` returns False trivially.

**The failure mode this enables**: a future refactor of `vco_lib.kg_sync.batch_query_content_hashes` could drop the safety triad (remove `limit:10000`, re-introduce a `where:` clause, bypass `post_graphql_safe`, etc.) and the V46-B guard would STILL pass — because it's looking at the wrong function. The bug class V46-A was designed to prevent silently re-opens.

**The fix (load-bearing)**: when you refactor a hot path the V46-B guard inspects, you MUST extend the guard in the SAME COMMIT to inspect the new home of the safety properties. v0.2.46 KG-AUTO-HEAL-E added THREE new guards on the new helper:

- `test_vco_lib_kg_sync_does_not_contain_broken_filter` (Like-% pattern check)
- `test_vco_lib_kg_sync_uses_v46f_post_graphql_safe` (routing-via-V46F check + no raw `urlopen`)
- `test_vco_lib_kg_sync_keeps_limit_10000` (saturation cap check)

**Generalisation — discipline for hot-path refactors**:

1. Before the refactor: list every test that inspects the function's source (`grep -rn "inspect.getsource(<fn>)" tests/`).
2. In the SAME commit as the refactor: extend each guard to inspect the new home of the property being protected.
3. Don't trust "the old guard still passes" — if the refactor moved the code, the old guard is checking the wrong location.

**Why this matters more than other refactor disciplines**: regression guards are the residue of post-incident lessons. The 4-release-cycle v0.2.42→v0.2.45 re-embed bug burned weeks of dogfood time + customer trust. A vacuous guard isn't just useless — it's actively misleading (CI is green, but the original bug class is unguarded). The guard has to FOLLOW the code it guards, or it stops being a guard.

**Cross-link**: [[uses::Silent-Zero Fallback Antipattern]] — the bug class the V46-A safety triad prevents.

## 12. Cross-stream binding-shape mismatches (v0.2.49 lesson, 2026-06-07)

A 4-stream parallel fanout (A=Rust install scope, B=enable toggle, C=container API, D=GUI license gate) all completed independently with green per-stream gates. Integration merge was clean via `ort` 3-way auto-merge — NO manual conflict resolution needed across 3 overlap files (`modules.rs`, `lib.rs`, `manifest.rs`). Yet integration gates FAILED with 4 test failures on a previously-green tree. Failure mode: not git conflicts, not behavior bugs — **semantic binding-shape mismatches at API seams that 3-way merge cannot detect**.

Three distinct mismatch classes:

### 12a. Optional-vs-required schema mismatch at the shared-struct seam

Stream A added a new field `InstallBlock.scope: InstallScope` (non-optional with `#[serde(default)]`). Stream C's v0.2.10 manifest dropped a legacy field `RuntimeBlock.command: String` (required in the schema, no default). At integration, the launcher tried to parse Stream C's NEW manifest fixture and crashed with `missing field 'command'`. Both streams were internally consistent; neither knew the other was making the same struct-shape assumption from opposite directions.

**Detection**: Stream C's test fixture is `paid-modules/vct-rl-reranker/vct-module.json` — a real fixture file the schema deserialization tests load. The 4 test failures all pointed at `at line N column M, missing field <X>` errors. Files reading the new fixture against the old schema diverged.

**Fix discipline**: every parallel stream that touches a SHARED data struct (manifest, DB row, IPC message) MUST get an explicit "what's the binding-shape contract at integration?" line in its prompt. Two streams updating opposite halves of a struct's optional/required surface is a near-certainty in this fanout shape; pre-specifying the contract closes the seam.

**Recovery (~5 LoC)**: make the dropped field `#[serde(default)]` retroactively in the schema, AND update the consumer (`build_podman_run_args`) to handle the default value gracefully. Then the new fixture parses cleanly. This is also the right schema-level fix for the underlying bug class (Bug E: legacy `command: "podman"` shape was always wrong for declarative manifests).

### 12b. TODO-shim integration debt

Stream B authored a helper `install_scope_is_global()` that returned `false` unconditionally with a `TODO(stream-a)` annotation pointing at the suggested integration. The body said: "Stream A will add `install.scope`; flip this to read it." Worked perfectly for Stream B's tests (they didn't exercise the global-install path). At integration, the helper was still returning `false` → Stream A's new global-install code path never activated for ANY module → seeding hooks silently no-op'd.

**Detection** (only by reading the diff carefully): grep for `TODO(stream-` in every merged branch. Each represents a deferred integration step that NEEDS to be flipped during integration, not later.

**Fix discipline**: enumerate every `TODO(stream-X)` annotation immediately after merging that stream. Flip them ALL before running the next gate. Track them in the integration checklist explicitly, not as ambient code TODOs.

**Recovery (1-line edit)**: `false` → `self.install.scope.is_global()`.

### 12c. JSON-type mismatch at the schema-validated boundary

Stream C wrote a port mapping as `"ports": [{"host": 11450, "container": 11450}]` — integer values. The launcher's `PortMapping` struct declared `host: String` (intentional, so the field can be a `{PLACEHOLDER}` substituted later by the runtime layer). Serde failed deserialization: `invalid type: integer 11450, expected a string`. Pure value-shape mismatch; never caught in Stream C's tests because Stream C doesn't run the launcher-side schema validation.

**Detection**: same as 12a — fixture-loading tests fail with type-mismatch errors at the JSON parse layer. The error message points at the exact line and column.

**Fix discipline**: when a stream owns a fixture file AND a different stream owns the schema that fixture is validated against, the fixture stream needs visibility into the schema's exact type signature. The simplest fix is making fixtures into TYPED CONSTRUCTORS rather than hand-edited JSON — but that's a big rework. The cheaper fix: include the relevant schema struct definition VERBATIM in the prompt of the fixture-authoring stream, so the agent reads the exact type signature before writing the JSON.

**Recovery (1-character edit)**: `11450` → `"11450"` (quote the value).

### Generalisation — the integration agent's job

After parallel merges, the integration phase needs a deliberate pass with three explicit checks:

1. **TODO-shim sweep**: `grep -rn "TODO(stream-" -- '*.{rs,ts,svelte,py}'`. Flip every shim that's now wired by its corresponding stream. Each flip is 1-3 lines and load-bearing.
2. **Schema-fixture sweep**: re-run schema deserialization tests against every fixture file touched by any stream. The 3-way merge can't detect type-shape drift between fixture and schema.
3. **Run all gates**: not just per-stream gates, but the integrated tree's full gate suite. The first run-after-merge typically catches the binding-shape mismatches that pre-merge testing missed.

These are CHEAPER than running an adversarial-review Opus pass over the integrated diff (though that's still worth doing for behaviour bugs). They catch the structural mismatches that 3-way merge silently lets through.

**Cost of this lesson**: ~15 minutes of debugging across 4 test failures + 3 small fix commits at integration. Pre-emptive discipline would have been ~5 minutes of upfront contract-pinning in subagent prompts. ROI 3x.

**Cross-link**: this is §6b (parallel agents stubbing each other's APIs → 4 seam-mismatch failure modes) refined for the SPECIFIC case where structs are SHARED across streams. §6b's lesson was "stubs drift from real impls"; §12's lesson is "shared structs accumulate opposite-half assumptions that 3-way merge cannot reconcile."

## Related

- [[relatedTo::Claude Code Agent Teams]] — worktree isolation as a happy-path feature.
- [[relatedTo::Claude Code Hook Input/Output Contract (v2.1.x)]] — sibling lesson cluster on hook-side gotchas.
- [[relatedTo::Cross-OS Hook Portability]] — parity-check failures are another flavour of the same "CI knows things you forgot" lesson.
- [[relatedTo::launcher-packaging-paid-module-distribution]] — the substrate-vs-consumer architecture that §4 sequences. The packaging design says "launcher ships the substrate, modules consume"; this node says "and therefore launcher ships first."
- [[relatedTo::module-contributed-gui-tabs]] — the module-DB-migrations capability (v0.2.31 #20-Fix-3 + Layer 1) is a sibling pattern to module-contributed GUI tabs: both let modules extend a launcher capability without rebuilding the launcher. §4's sequencing applies to both.
- [[relatedTo::same-day-chained-release-pattern]] — when CI is in-flight + new commits land, chain v0.X.Y+1 rather than force-update tag. Different problem (intra-release timing) but adjacent discipline (write CHANGELOG AFTER tag is pushed).
- [[relatedTo::Pre-tag peer review via parallel Opus subagents]] — §10 + §11 are integration-time complements to the upstream multi-Opus pre-tag review pattern.
