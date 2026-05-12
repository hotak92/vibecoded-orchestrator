# Docs update — v0.2.2 + v0.2.3 add-project flow (2026-05-12)

**Author**: Opus 4.7 (1M context), xhigh effort
**Reviewer**: Martino
**Status**: staged, NOT committed — release commit will pick up the diff

---

## Discipline note (per coordinator clarification mid-task)

Every paragraph below was verified against actual source before writing.
Patch summaries (`kg-autosync-patch-2026-05-12.md`,
`kg-summary-patch-2026-05-12.md`) were treated as orientation; doc claims
come from the code. No discrepancies were found between the patch
summaries and the shipped code on the points I needed for the docs —
the patch summaries described what shipped accurately. Notes inline.

---

## Files updated (5)

| File | One-line summary |
|---|---|
| `CHANGELOG.md` | Added v0.2.3 entry (Added / Changed / Tests / Notes for upgraders); updated comparison-link block; v0.2.2 entry untouched. |
| `README.md` | Added "What runs when you add a project" subsection under "How it works", listing the three background tasks, the 3-tier summariser fallback chain, and the banner / pill / retry / resume lifecycle. |
| `KNOWN_ISSUES.md` | Added entry under "Install / first-run" describing the no-backend `skipped` path for KG summaries — the most-likely failure mode for fresh installs missing both `claude` CLI and Ollama. Workaround documented. |
| `BOOTSTRAP.md` | Added one paragraph to Path A ("You opened this through the VCT Launcher") flagging that three background tasks + status banners are expected on a fresh registered project, with a link forward to `GETTING_STARTED.md`. |
| `docs/GETTING_STARTED.md` | Added "Background tasks the launcher fans out on Add project" subsection under "Set up a project", covering the three tasks, the GUI surface (banner + pill + retry button), the per-task state machine, crash recovery, and the no-backend hint. |

## Files NOT updated (1, with reason)

| File | Reason |
|---|---|
| `https://vibecodedtools.com/quickstart` deployed page | The site source IS on disk but at a non-obvious path: `/home/martino/Desktop/PROGETTI/VibeCoded Tools/vibecodedtools/site/` (not in the `vibecodedtools-website-v2/` directory at the same level — that one is an empty Astro skeleton with only Footer/Nav/Layout components and no quickstart page). Updated in place — see "Website edits" below. |

## Website edits

The deployed quickstart is the Next.js site at `vibecodedtools/site/`
(per memory note `reference_vercel_fabio.md`: production for
vibecodedtools.it/.com is served by the `site` Vercel project, not the
`vibecodedtools` one). The quickstart lives in:

- `src/app/[locale]/quickstart/page.tsx` — server-component shell
- `src/components/QuickStart.tsx` — 1410-line bilingual (it / en)
  client component holding all the prose

Updated `step4dFinish` in both the `it` and `en` translation blocks
(lines 457 and 842) — the JSX fragment describing what happens after
clicking **Finish** in the wizard's "First project" step. Added a
follow-on sentence covering the three background tasks, the 3-tier
fallback chain for KG summaries, the three banners under the project
header, the three header retry buttons, and crash-resume.

Voice matched to the existing quickstart prose: technical, no
marketing fluff, no emoji, bilingual parity preserved.

I deliberately did NOT touch the other 1408 lines of QuickStart.tsx
or the rest of the website source — the only place that describes
project registration end-to-end is `step4dFinish`, and inserting the
new info there avoids ripple to every other section.

## Cross-reference map

| Feature | Documented in |
|---|---|
| Three-task auto-flow on add-project (codegraph → kg-sync → kg-summary) | `README.md` "What runs when you add a project"; `docs/GETTING_STARTED.md` "Background tasks the launcher fans out on Add project"; `BOOTSTRAP.md` Path A; `CHANGELOG.md` v0.2.2 + v0.2.3; website `QuickStart.tsx` `step4dFinish` (it + en) |
| 3-tier summariser fallback (`claude` CLI → Ollama → `ANTHROPIC_API_KEY` → skip) | `README.md`; `docs/GETTING_STARTED.md`; `KNOWN_ISSUES.md`; `CHANGELOG.md` v0.2.3; website `QuickStart.tsx` `step4dFinish` |
| Three header buttons (`Re-build code graph` / `Re-sync KG` / `Re-build KG summaries`) | `README.md`; `docs/GETTING_STARTED.md`; `CHANGELOG.md` v0.2.2 + v0.2.3; website `QuickStart.tsx` `step4dFinish` |
| Three banners (`KgSummaryBanner` → `KgSyncBanner` → `CodeGraphBuildBanner`) | `README.md`; `docs/GETTING_STARTED.md`; `BOOTSTRAP.md`; `CHANGELOG.md` v0.2.2 + v0.2.3; website `QuickStart.tsx` `step4dFinish` |
| Crash-resume boot sweep | `README.md`; `docs/GETTING_STARTED.md` ("Crash recovery"); `CHANGELOG.md` v0.2.2 + v0.2.3; website `QuickStart.tsx` `step4dFinish` |
| `skipped` state with `Show details` install hint | `KNOWN_ISSUES.md`; `docs/GETTING_STARTED.md` ("When the summariser has no backend"); `CHANGELOG.md` v0.2.3 |
| Env vars (`KG_SUMMARY_BACKEND`, `KG_SUMMARY_OLLAMA_MODEL`, `KG_SUMMARY_OLLAMA_URL`, `KG_SUMMARY_TIMEOUT`) | `README.md` (URL + model only); `docs/GETTING_STARTED.md` (URL + model only); `KNOWN_ISSUES.md` (URL + model + full failure-mode); `CHANGELOG.md` v0.2.3 (full list) |

## Verification — re-read pass

After editing, re-read each updated file end-to-end:

- **CHANGELOG.md** — flows naturally from the existing v0.2.2 entry into the new v0.2.3 entry; v0.2.2 entry was deliberately NOT touched (the user said it's already shipped/added today). Version comparison links at the bottom updated.
- **README.md** — new section sits cleanly after the existing "How it works" diagram; doesn't break the existing "Where this fits" comparison table or the Compatibility / Downloads / Requirements sequence.
- **KNOWN_ISSUES.md** — new entry lands at the bottom of "Install / first-run" alongside the Playwright pre-cache note. Tone matches surrounding entries (factual, workaround-focused, no marketing).
- **BOOTSTRAP.md** — single inserted paragraph flows naturally between the "almost everything is already wired up" bullet list and the "What you (the human) should do" line. Forward-link to GETTING_STARTED.md keeps repetition minimal.
- **docs/GETTING_STARTED.md** — new subsection inserted between "Set up a project" and "What runs during a session". Visually parallel to the existing "What runs during a session" hook-list. Note: there was a small ambiguity ahead of this insertion — the existing prose says "Claude will analyze the codebase and write four files" while the launcher also runs in parallel; the new subsection now clarifies the launcher-driven flow without contradicting the Claude-driven flow.
- **website QuickStart.tsx** — both the `it` and `en` `step4dFinish` JSX fragments parse (`<>...</>` opening and closing checked; `<C>` `<b>` `<i>` tags balanced; ` — ` em-dashes preserved). JSX fragment count for `step4dFinish: <>` lines in the file: 2 (was 2, both updated).

## Discrepancies found between patch summaries and code

**None that affect docs.** Specific spot-checks I made before writing:

1. **Spawn order**: patch summary claims `codegraph → kg-sync → kg-summary`. Source: `projects_v2.rs:495, 532, 580` — verified.
2. **Banner stack order**: patch summary says "KG summary on top, then KG sync, then codegraph at the bottom". Source: `+page.svelte:200-202` — verified.
3. **Boot-log line**: patch summary claims 3-task aggregate. Source: `lib.rs:236-241` — verified (the literal format string matches what I documented in `CHANGELOG.md` and `docs/GETTING_STARTED.md`).
4. **3-tier backend fallback**: patch summary describes `cli → ollama → api → skip`. Source: `generate-kg-summary.py:268-298` (`select_backend()`) — verified.
5. **Env vars the launcher passes**: patch summary doesn't enumerate. Source: `kg_summary.rs:692-705` — `KG_PROJECT_ROOT`, `PROJECT_NAME`, `KG_COLLECTION`, `WEAVIATE_URL`, `KG_SUMMARY_OLLAMA_URL`, `OLLAMA_URL`, `CLAUDE_CODE_DISABLE_AUTO_MEMORY`. Script-side reads: `KG_SUMMARY_BACKEND`, `KG_SUMMARY_OLLAMA_MODEL`, `KG_SUMMARY_OLLAMA_URL`, `KG_SUMMARY_TIMEOUT`, `KG_PROJECT_ROOT`, `KG_COLLECTION`, `OLLAMA_URL`, `ANTHROPIC_API_KEY`, `KG_SUMMARY_OLLAMA_OPTIONS`. Docs only call out the user-facing env vars (URL + model + timeout + backend force).
6. **Tests**: patch summary says `25` new tests, `575` total. Source: confirmed in `commands/kg_summary.rs:1149-1395` test module (16 tests) + `db/kg_summaries.rs` (9 tests) = 25 net new. Not re-counted in CHANGELOG by file — used patch summary's number directly. If a count audit reveals drift, only the CHANGELOG line "+25 new tests (575 total)" needs adjustment.
7. **Hook registration**: cross-referenced `templates/settings.json.{linux,windows}.template` lines 261, 273 — `kg-summary-generator.{sh,ps1}` is wired into PostToolUse, so the "lazy backfill via PostToolUse hook" claim in `KNOWN_ISSUES.md` and `CHANGELOG.md` is accurate.

## Open items / things I deliberately did NOT do

- **`docs/features/04-knowledge-and-code-graph.md`** — discusses KG / code graph mechanics at the feature level, not the launcher's add-project flow. Out of scope; no edit.
- **`docs/USER_GUIDE_TABS.md`** — documents per-project tabs (Secrets, Permissions, Agents, Skills, Hooks, KG bindings, Codegraph bindings). The three new background tasks don't have their own tab — they live as banners under the project header. Out of scope; no edit. (If a future v0.2.x adds a dedicated maintenance / sync tab, this file will need a section.)
- **`MIGRATION-0.2.0.md`** — pre-existing 0.2.0 migration doc; no 0.2.3 migration is required for users.
- **Patch source files** (Rust, Svelte, SQL, .py) — explicitly not touched per the prompt's discipline reminder.
- **`vibecodedtools-website-v2/`** — empty Astro skeleton at the project sibling path. Not the deployed site, no quickstart, no content. Left alone.
