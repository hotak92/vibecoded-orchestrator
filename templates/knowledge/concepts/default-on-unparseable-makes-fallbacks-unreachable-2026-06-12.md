---
title: Default-on-unparseable + early-return makes locale fallbacks unreachable
type: concept
tags: [low-level-implementation, windows, cross-os-parity, parsing, bug-pattern]
created: 2026-06-12T00:00:00Z
updated: 2026-06-12T00:00:00Z
valid_from: 2026-06-12T00:00:00Z
valid_until: null
status: active
---

# Default-on-unparseable + early-return makes locale fallbacks unreachable

## The pattern (v0.2.54 Track G, G-8)

`vct-hub/src/boot.rs` parsed `schtasks /Query /V /FO LIST` output for the
English markers `Enabled` / `Disabled`, **defaulting to `Enabled` when
neither appeared**. The caller early-returned on `Enabled` and only
consulted the locale-invariant PowerShell fallback (`Get-ScheduledTask
.State`, a CIM enum) for the `Disabled` branch.

Composition of the two choices: on non-English Windows — where
`schtasks` localises BOTH keys AND values (German: `Aktiviert` /
`Deaktiviert`), so a marker miss is the *expected* case — the parse
defaulted to `Enabled`, the early return fired, and the fallback that
was *specifically written for localised Windows* became unreachable. A
Disabled task on German Windows was reported Enabled. A unit test
(`parse_win_status_unparseable_defaults_to_enabled`) locked the wrong
behaviour in.

## The general lesson

When a parser feeds a dispatcher with a fallback chain:

1. **"Couldn't parse" must be a distinct value** (`Option`/`None`), never
   coerced into one of the legitimate outcomes. Coercion at the parse
   layer destroys the information the dispatch layer needs to route to
   the fallback.
2. **Apply the default at the END of the fallback chain**, not the start.
   The last resort may legitimately guess (`task exists and we never
   write Disabled → Enabled`), but only after every more-reliable source
   has been tried.
3. **Test the unparseable case against the fallback route**, not against
   the guessed default — asserting the default ("unparseable →
   Enabled") is how the wrong behaviour got pinned. Include a realistic
   localised fixture (`Status der geplanten Aufgabe: Deaktiviert`), not
   just lorem-ipsum garbage.
4. Comment claims like "values happen to be English even on localised
   Windows" deserve verification — that claim was false and justified
   the whole broken design.

## Where to look

- Fix: `launcher/src-tauri/vct-hub/src/boot.rs` (`parse_win_status_output`
  → `Option<BootStatus>`; `windows::status()` routes `None` to the
  PowerShell fallback).
- Same shape risk anywhere a "best-effort parse" feeds a status enum:
  container-runtime probes, GPU detection, version sniffing.

[[relatedTo::Cross-OS parity hardening]]
