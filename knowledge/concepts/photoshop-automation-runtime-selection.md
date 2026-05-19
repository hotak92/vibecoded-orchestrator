---
title: Photoshop Automation Runtime Selection
type: concept
tags:
- design
- automation
- Photoshop
- GIMP
- UXP
- ExtendScript
- Python-Fu
- mid-level-architecture
- scripting
created: 2026-05-19T00:00:00Z
updated: 2026-05-19T00:00:00Z
valid_from: 2026-05-19T00:00:00Z
valid_until: null
status: active
---

# Photoshop Automation Runtime Selection

Adobe Photoshop and GIMP both support multiple scripting runtimes with overlapping but distinct capabilities. Picking the wrong runtime for a task means either a much harder script, a script that won't survive the next Adobe migration, or a script that can't be distributed to a team. This concept catalogs the decision.

## The runtimes

| Runtime | Language | Host | Use case |
|---|---|---|---|
| **UXP** (Unified Extensibility Platform) | Modern JavaScript (ES2020+, async/await) | Photoshop 2022+, Adobe XD, InDesign (rolling out) | New plugins, panels, modern scripts, anything that needs async I/O |
| **ExtendScript (JSX)** | ES5 JavaScript dialect | All Photoshop versions (legacy but still supported) | Standalone scripts, quick file-loop automation, one-off `.jsx` deliverables |
| **CEP (Common Extensibility Platform)** | HTML / JS / CSS panels backed by ExtendScript | Photoshop pre-2022 panels | Legacy — being phased out by Adobe; do not author new CEP panels |
| **Python-Fu** | Python 2 (GIMP 2.x) / Python 3 (GIMP 3.0+) | GIMP | Free / open-source automation; cross-platform without Adobe license |
| **Script-Fu** | Scheme dialect | GIMP | Legacy; prefer Python-Fu unless you maintain existing Scheme scripts |
| **Pillow / ImageMagick / libvips** | Python, shell | Cross-platform, no Photoshop / GIMP needed | Server-side asset pipelines, headless batch — see [[relatedTo::Batch Image Pipeline Recipes]] |

## Decision tree

1. **Does the task need to run without a designer present (server-side / CI)?**
   → Use Pillow / ImageMagick / libvips. Neither Photoshop nor GIMP is licensed for headless server use without a special license. See the batch image pipeline pattern.

2. **Is the deliverable a packaged plugin or custom panel for a team?**
   → **UXP plugin** (`.ccx` package). CEP is being phased out; ExtendScript-based panels won't be supported in future Photoshop versions.

3. **Is it a one-off script the designer runs via File → Scripts → Browse?**
   → **ExtendScript (`.jsx`)** is the simplest delivery. Pros: a single file, no plugin install. Cons: ES5-only (no `let`, `const`, arrow functions, template literals, `for...of`).

4. **Does the script need modern async patterns, fetch, or Node-style modules?**
   → **UXP**. ExtendScript is synchronous and has no `fetch`, no `Promise`-based APIs, no module system.

5. **No Adobe license available, but need GUI-tool automation?**
   → **GIMP Python-Fu**. Free, cross-platform, comparable feature set for most batch tasks. Layers, masks, paths, color profiles all scriptable.

6. **Heavy raw photo editing across hundreds of files?**
   → Lightroom Classic with batch presets, or Capture One scripting. Photoshop is the wrong tool for high-volume raw workflows.

## UXP vs ExtendScript — feature comparison

| Feature | UXP | ExtendScript |
|---|---|---|
| Language | Modern JS (ES2020+) | ES5 only |
| async / await | Yes | No |
| `fetch` / network | Yes | No (some old `Socket` APIs) |
| File system | Sandboxed; requires user file-picker token | Direct File / Folder objects |
| Modal wrapping required | Yes (`core.executeAsModal`) | No |
| Panels | Yes (HTML-based UI) | Via legacy CEP only |
| Adobe migration trajectory | Forward-looking | Legacy, supported but frozen |
| Easy `.jsx` delivery via File → Scripts → Browse | No | Yes |

**Critical UXP rule**: every document mutation must be wrapped in `core.executeAsModal`:

```javascript
const { app, core } = require("photoshop");

async function example() {
    await core.executeAsModal(async () => {
        const doc = app.activeDocument;
        // mutate doc here
    }, { commandName: "My operation" });
}
```

Without this wrapper, Photoshop throws "Not allowed to execute…". This is the most common UXP gotcha.

## ExtendScript gotchas to internalize

- **ES5 only** — no `let`, no `const`, no arrow functions, no template literals, no `for...of`, no `Promise`. Write strictly to ES5.
- **`for...in` iterates object keys, not array values.** Use `for (var i = 0; i < arr.length; i++)`.
- **`alert()` blocks the script** — useful for debug, painful in batch. Use `$.writeln()` to the ExtendScript console for non-blocking logging.
- **Suppress dialogs for batches** — `app.displayDialogs = DialogModes.NO;` before the loop. Restore after.
- **`activeDocument` may be undefined** if no document is open — check before using.
- **Smart Object replacement** has no direct API; use the `placeSmartObject()` action descriptor pattern.

## GIMP Python-Fu notes

GIMP 3.0 (released 2025) moved to Python 3 with GObject-introspection bindings. Older GIMP 2.x uses Python 2.7 — write to the GIMP version actually installed. Script-Fu (Scheme) still works but is the legacy path; prefer Python-Fu for new work.

GIMP plugin folder differs by OS — check Edit → Preferences → Folders → Plug-Ins. Place `.py` files there and (on Linux/macOS) `chmod +x` them.

## Distribution paths

| Deliverable | Mechanism |
|---|---|
| One-off ExtendScript | Hand the user a `.jsx` file + "File → Scripts → Browse → select this." |
| Recurring ExtendScript for the team | Drop in `Photoshop/Presets/Scripts/` so it appears in the `File → Scripts` menu directly. |
| UXP plugin for the team | Package as `.ccx` (UXP Developer Tool's package command). Distribute via shared folder, Adobe Exchange, or sign for trust-level dialogs. |
| GIMP plugin | Drop `.py` in the user's plug-ins folder, make executable on macOS / Linux. |

## When to push back on scripting

The script is the wrong answer when:

- The task is fewer than ~20 files AND non-recurring — record an Action and move on.
- The task is server-side / unattended — Photoshop is not licensed for headless server use without a special license; use Pillow / ImageMagick instead.
- The task is heavy raw editing — Lightroom batch presets or Capture One are better fits.
- The task is purely resize / format-convert with no layer logic — use the asset-pipeline patterns; no Photoshop needed.

Push back rather than write the script.

## Cross-platform notes

- Photoshop scripts (both UXP and ExtendScript) run identically on Windows and macOS.
- File paths in ExtendScript: use `/` forward slashes everywhere. Avoid Windows backslashes — they're escape characters in JS strings.
- `Folder.selectDialog()` / `File.openDialog()` handle native OS dialogs transparently.
- UXP plugins ship as `.ccx` and install on both platforms via Creative Cloud.

## Output discipline for delivered scripts

1. **Single file**, runnable as-is, with install instructions at the top in a header comment.
2. **No placeholders** — put config in `CONSTANTS` at the top with sensible defaults.
3. **Try/catch per file** in batch loops so one bad file doesn't kill the run.
4. **Progress feedback** — log to console or update a status text layer.
5. **Idempotent** — re-running doesn't double-output or corrupt state.
6. **Dry-run path** — include a comment showing how to test on one file before running on the batch.

## Anti-patterns

- **Mixing UXP and ExtendScript APIs in one file** — they're different runtimes.
- **Writing new CEP panels in 2026** — CEP is being phased out; UXP is the path forward.
- **ES6+ syntax in ExtendScript** — silently fails to parse or runs incorrectly.
- **Forgetting `executeAsModal` in UXP** — every mutation throws "Not allowed to execute."
- **Scripting under 20 files without recurrence** — Actions recorder is faster.
- **Silently failing per-file** in a batch — log skips with reasons.

## Relations

[[relatedTo::Batch Image Pipeline Recipes]]
[[relatedTo::Color Management for Designers]]
[[uses::Adobe Photoshop]]
[[uses::GIMP]]
[[implements::Design Automation Practice]]

## References

- Adobe UXP for Photoshop: https://developer.adobe.com/photoshop/uxp/
- ExtendScript documentation (legacy reference): Adobe's Scripting Listener / docs in Photoshop install
- GIMP Python-Fu API: https://docs.gimp.org/en/gimp-filters-python-fu.html
- UXP Developer Tool (UDT) — Adobe's plugin packaging and trust-signing utility
