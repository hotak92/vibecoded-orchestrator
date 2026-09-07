<!-- vco-deferral-reminder-begin -->
**Pending VCO action**: `.claude/context/UPDATE_DEFERRED.md` exists.
Read it at session start — it contains commands to resolve
unresolved VCO install actions.
Currently: **1 actionable**, 0 informational/records.

To remove THIS reminder block: once the deferral is resolved (e.g.
via `--update --force`), VCO's next install run will delete
UPDATE_DEFERRED.md AND strip this block. Manual cleanup if needed:
delete everything between the HTML-comment markers wrapping this
block.
<!-- vco-deferral-reminder-end -->

# VibeCoded Orchestrator

This file is auto-materialized by `install.py` from
`templates/ORCHESTRATOR-CLAUDE.md.template` at first-install + every
`--update` (--update preserves user edits between AUTO markers).

**First-time user**: run `bash first-install.sh` (Linux/macOS) or
`first-install.bat` (Windows). The installer will:

1. Render this file with values appropriate for your machine
2. Set up `.claude/` directory + hooks
3. Configure MCP servers
4. Start container services (Weaviate, Ollama)

Until you run first-install, this file just points you here.

For maintainers / contributors: edit the TEMPLATE
(`templates/ORCHESTRATOR-CLAUDE.md.template`), not this file.
