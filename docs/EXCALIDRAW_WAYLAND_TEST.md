# MANUAL_TEST_REQUIRED: Excalidraw embedded editor on Wayland + webkit2gtk

**Status (2026-05-25):** Documented, NOT yet executed. The headless test environment in which Phase 2 was implemented does not have a Wayland display, so the agent could not perform the test. The detection + fallback code path described in §3 below ships in Phase 2; the manual confirmation in §2 is needed before declaring the embedded editor production-ready on Wayland desktops.

## 1. Why this test exists

Plan §3 Phase 2 item 2 + §4 Risk 5: webkit2gtk on Wayland has documented canvas latency and pointer-event issues that affect HTML5 canvas-heavy apps. Excalidraw is canvas-heavy. We do not want to ship a degraded experience on Wayland desktops.

The launcher's `ExcalidrawEditor.svelte` mounts a real React `@excalidraw/excalidraw@0.18.1` component. On X11 (most current Linux setups) this works fine. On Wayland with webkit2gtk (Linux Tauri builds) it MAY exhibit:
- visible pointer-event lag (>100ms)
- clipboard copy/paste failures
- inconsistent re-render after viewport pan / zoom
- canvas rendering artifacts at the edges of large scenes

If any of those reproduce, the fallback (see §3) kicks in and routes the user to "Open in OS default editor" instead of the embedded canvas.

## 2. Manual test recipe

Run this on the actual hardware + display server combination you care about. The agent that authored Phase 2 cannot run it.

### Setup
1. Boot a desktop session under **Wayland** (confirm with `echo $XDG_SESSION_TYPE` → `wayland`).
2. Build + launch the VCO launcher (`cd launcher && pnpm tauri dev` or `cargo tauri dev`).
3. Open a project with the Diagrams module enabled.

### Repro
1. Navigate to the **Diagrams** tab.
2. Click **+ Add diagram**, choose type **Excalidraw**, name `wayland-smoke-test`, category path `tests/manual`.
3. Wait for the editor pane to mount (you should see the Excalidraw toolbar appear).

### What to verify

Each item must pass. Time each interaction with a stopwatch / browser devtools timeline; we're looking for sub-100ms feel, not absolute numbers.

| # | Action | Expected | Notes |
|---|---|---|---|
| 1 | Click the rectangle tool, draw a rectangle on the canvas. | Rectangle appears under the cursor with no perceptible lag. | If the rectangle stutters or appears after a delay → FAIL. |
| 2 | Draw 5 more shapes (text, arrow, line, ellipse, freedraw). | All draw smoothly; the canvas re-renders cleanly between shapes. | Visible re-render artifacts (ghost outlines, flicker) → FAIL. |
| 3 | Select all (Ctrl+A), drag the selection to a new position. | Selection moves continuously under the cursor (no jitter). | Jitter or jumpy motion → FAIL. |
| 4 | Pan the viewport (middle-click drag or two-finger trackpad gesture). | Viewport pans smoothly, content reflows in real time. | Stutter → FAIL. |
| 5 | Zoom in/out (Ctrl+scroll). | Smooth zoom, content rescales cleanly. | Cropping artifacts at zoom transitions → FAIL. |
| 6 | Type some text in a text element, then Ctrl+C / Ctrl+V it. | Text copies + pastes correctly. | Clipboard interaction fails or pastes wrong content → FAIL. |
| 7 | Wait 5 seconds without interacting. Then check the file on disk: `cat .claude/diagrams/tests/manual/wayland-smoke-test.excalidraw`. | The file contains the serialised scene with all 6 shapes + text. | File missing or empty → FAIL. |
| 8 | Close the launcher. Reopen it. Navigate back to the diagram. | Scene re-loads with all 6 shapes + text intact. | Lost data → FAIL. |
| 9 | Use the toolbar's **Export SVG** button. Save to disk. Open the SVG in a browser. | SVG renders all 6 shapes correctly. | Missing / malformed SVG → FAIL. |

### Pass criteria
- ALL 9 checks pass with NO visible rendering artifacts or interaction lag above 100ms.

### Fail criteria
- ANY check FAILs.
- The user reports they want to disable the embed (subjective UX feedback is valid here — Excalidraw is interactive software, "feels janky" is enough reason to fall back).

## 3. Fallback that ships in Phase 2

Even though the manual test hasn't been run, the launcher already ships the detection + fallback path:

`launcher/src/lib/project-state/DiagramsTab.svelte::detectExcalidrawFallback()` returns `true` when BOTH:
1. `navigator.userAgent` matches `/WebKit/i` (webkit2gtk surfaces this), AND
2. The Tauri backend confirms `XDG_SESSION_TYPE=wayland` via a `read_env_var` command.

When `true`, the Excalidraw branch of DiagramsTab renders a message + "Open in OS default editor" button instead of the embedded `ExcalidrawEditor` component.

Requiring BOTH signals (not either) reduces false positives — Safari and Tauri's macOS WebKit also match the UA, but only Linux+Wayland combines that with a `wayland` session type, so the embed stays usable for Mac users testing the launcher.

### Fallback escape hatches

If the fallback is incorrectly triggered on your machine, two workarounds:
1. Launch the desktop session under **X11/XWayland** instead of Wayland (`echo $XDG_SESSION_TYPE` should report `x11`).
2. Manually unset `XDG_SESSION_TYPE` before launching the launcher: `XDG_SESSION_TYPE= ./vct-launcher`. The detection sees an empty string and disables the fallback.

If the fallback fails to trigger when it SHOULD (the user is on Wayland+webkit2gtk and the embed is broken), report a bug; the detection logic can be tightened (e.g. add a third signal like a WebGL-fingerprint match).

## 4. After running the test

1. **All PASS** → close this issue, remove the `MANUAL_TEST_REQUIRED` marker from the header, write a short result note + your environment details (display server, distro, kernel, webkit2gtk version) at the bottom of this file.
2. **Any FAIL** → write the failure details at the bottom of this file. The Phase 2 fallback already handles user impact; you may also want to:
   - File an upstream issue at <https://github.com/tauri-apps/tauri/issues> or <https://github.com/excalidraw/excalidraw/issues> depending on root cause.
   - Tighten the detection logic in DiagramsTab if your machine reports different `navigator.userAgent` or `XDG_SESSION_TYPE` patterns than the fallback expects.

## 5. Test results log

Append your test runs below (date, environment, pass/fail per checklist item).

### YYYY-MM-DD — `<your username>` — `<your distro>` — `<kernel>` — `<webkit2gtk version>`

- Environment: …
- Result: PASS / FAIL
- Notes: …
