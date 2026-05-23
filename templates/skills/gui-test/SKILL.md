---
name: gui-test
description: "Automated visual testing with Playwright MCP - test web apps, presentations, websites, and documents with scalable reviewer perspectives"
keywords: [visual test, Playwright MCP, screenshot diff, web app test, UI test]
model: sonnet
---

# GUI Test Skill

Runs visual testing using the gui_testing framework + Playwright MCP. Supports web apps, presentations, websites, and documents with configurable complexity.

## When to invoke
- After implementing or modifying any frontend component
- When a bug is reported in the UI
- Before committing UI changes
- To review a generated presentation, website, or document visually

## Complexity levels
- `quick` — aesthetics/layout only (1 agent, fastest)
- `standard` — aesthetics + usability + bug detection (3 agents)
- `full` — all 5 perspectives (default for web apps)

## Target types
- `web_app` — default, UI testing (Tauri, Gradio, etc.)
- `presentation` — slide design, readability, visual hierarchy, brand consistency
- `website` — layout, navigation, responsiveness, content clarity
- `document` — formatting, typography, readability, structure

## Usage
```
/gui-test                                              # full suite, web_app (localhost:1420)
/gui-test http://localhost:7861                        # full suite, specific URL
/gui-test http://localhost:1420 CommsTab               # full suite, focus on component
/gui-test presentation slides.html quick               # quick presentation check
/gui-test website https://vibecodedtools.it standard   # standard website review
/gui-test document http://localhost:8080/report quick   # quick document check
```

## Requirements
- Playwright MCP must be connected
- Target server must be running (file:// URLs need no server)

## Invocation
Parse arguments:
1. If first token is a target_type keyword (`presentation`, `website`, `document`), use it; next token is URL
2. If a complexity keyword (`quick`, `standard`, `full`) appears, use it
3. Otherwise treat first token as URL (web_app, full)

Run via the MAO gui_testing framework:
```bash
python -m orchestrator.gui_testing.runner \
  --url {URL} \
  --complexity {quick|standard|full} \
  --target-type {web_app|presentation|website|document} \
  --output-dir /tmp/gui-report
```

After the agent completes, read the report and show the user:
1. Summary (pass/fail counts, target type, complexity used)
2. Critical bugs or issues (P0/P1)
3. Key screenshots (as image reads if supported)
4. Recommended next fixes
