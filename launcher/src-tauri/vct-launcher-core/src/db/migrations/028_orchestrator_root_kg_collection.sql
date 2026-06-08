-- v0.2.49 access-matrix overhaul, Phase 1 (item #1).
--
-- Persists the canonical orchestrator-root KG collection name in the
-- launcher.db `app_state` key-value store.
--
-- Background
-- ----------
-- The orchestrator-root shared KG collection has had several names
-- across releases: `VibeCodedOrchestrator_KnowledgeGraph` (canonical),
-- `VibeCodedTools_KnowledgeGraph` (legacy), `VCODev_KnowledgeGraph`
-- (dev clone migrations), and whitelabel-distribution names. Code
-- across the launcher + hub + install.py + MCP servers has relied on
-- a hard-coded constant (`LAST_RESORT_SHARED_KG_COLLECTION` in some
-- call sites, the bare string `VibeCodedOrchestrator_KnowledgeGraph`
-- in others) to identify "the shared root collection." That
-- duplication is the root cause behind the v0.2.49 access-matrix
-- audit finding S-1 ("`is_shared` substring heuristic conflates
-- canonical and legacy names"): different code paths classify the
-- SAME collection differently depending on which constant they read.
--
-- Fix: a single persisted name lives in `app_state` under the key
-- `orchestrator_root_kg_collection`. Every consumer that needs to ask
-- "is this collection the shared root?" calls
-- `db::app_state::get_orchestrator_root_kg_collection()` and compares
-- by equality. White-label installers can override the value via
-- install.py (item #2 of this phase) before the rest of the launcher
-- ever sees it.
--
-- Semantics
-- ---------
-- The value is the EXACT collection name as it must appear in
-- Weaviate. No case-folding, no prefix stripping. Consumers use
-- byte-equal comparison.
--
-- Backfill
-- --------
-- INSERT OR IGNORE so existing installs that already have a row
-- (e.g. set by install.py before this migration shipped, or by an
-- earlier hand-applied SQL) are not clobbered. New installs get the
-- canonical default; install.py refines it (item #2) if a
-- whitelabel install root is detected.
--
-- The default value `VibeCodedOrchestrator_KnowledgeGraph` matches the
-- LAST_RESORT_SHARED_KG_COLLECTION constant that's been hard-coded
-- across the codebase since v0.2.28.

INSERT OR IGNORE INTO app_state (key, value, updated_at)
VALUES (
    'orchestrator_root_kg_collection',
    'VibeCodedOrchestrator_KnowledgeGraph',
    strftime('%s', 'now') * 1000
);
