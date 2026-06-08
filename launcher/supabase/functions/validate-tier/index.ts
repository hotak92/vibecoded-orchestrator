// validate-tier — Lemon Squeezy license validation + machine binding.
//
// Called by the orchestrator's Python validator on every startup
// (the license validator's `_remote_validate` entry point).
//
// Request:
//   POST /functions/v1/validate-tier
//   Body: { license_key: string (UUID), machine_id_hash: string (sha256 hex) }
//
// Response shapes — see end of file.

import {
  appendAdminAuthLog,
  assertNoPlaceholderKeysInProduction,
  bindVaultAdminMachine,
  fetchVaultAdminTokensJson,
  lookupVariant,
  lookupVaultAdminToken,
  type OrchestratorTier,
} from "../_shared/variant_map.ts";

// Pre-flight: hard-fail at module init if VARIANT_MAP still has
// placeholder keys in production. Same rationale as
// lemon-squeezy-webhook (audit blocker #3, 2026-05-07).
assertNoPlaceholderKeysInProduction();

const LS_BASE = "https://api.lemonsqueezy.com/v1";
const LS_TIMEOUT_MS = 8000;

const CORS_HEADERS = {
  // Orchestrator runs on user machines — origins vary. Validate-tier is a
  // public endpoint that authenticates via license_key, so wildcard is safe.
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type, Authorization",
  "Access-Control-Max-Age": "86400",
};

const JSON_HEADERS = { ...CORS_HEADERS, "Content-Type": "application/json" };

// ────────────────────────────────────────────────────────────────────────────
// Helpers
// ────────────────────────────────────────────────────────────────────────────

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: JSON_HEADERS });
}

/** UUID v4 / v5 string check — LS license keys are 36 chars with dashes. */
function isValidLicenseKey(s: unknown): s is string {
  return typeof s === "string"
    && s.length === 36
    && /^[0-9a-fA-F-]{36}$/.test(s);
}

/**
 * Vault-admin token shape check: `vct_admin_` prefix + URL-safe base64
 * body. Range chosen to accommodate the 64-char default
 * (token_urlsafe(48) → 64 chars) plus reasonable headroom for variants.
 */
function isValidVaultAdminToken(s: unknown): s is string {
  return typeof s === "string"
    && s.startsWith("vct_admin_")
    && s.length >= 30
    && s.length <= 256
    && /^vct_admin_[A-Za-z0-9_-]+$/.test(s);
}

/** sha256 hex output: 64 lowercase hex chars. */
function isValidMachineHash(s: unknown): s is string {
  return typeof s === "string" && /^[0-9a-f]{64}$/.test(s);
}

/** Safe license key fragment for logs. Never log the full key. */
function maskKey(key: string): string {
  return key.slice(0, 8) + "…";
}

/**
 * Fetch with timeout. Returns the Response or throws "TIMEOUT" / "NETWORK".
 */
async function fetchWithTimeout(
  url: string,
  init: RequestInit,
  timeoutMs: number,
): Promise<Response> {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    return await fetch(url, { ...init, signal: ctrl.signal });
  } catch (e) {
    if ((e as Error).name === "AbortError") throw new Error("TIMEOUT");
    throw new Error("NETWORK");
  } finally {
    clearTimeout(timer);
  }
}

// ────────────────────────────────────────────────────────────────────────────
// Lemon Squeezy calls
// ────────────────────────────────────────────────────────────────────────────

interface LSValidateResponse {
  valid: boolean;
  error?: string | null;
  license_key?: {
    id: number;
    status: string; // "active" | "inactive" | "expired" | "disabled"
    key: string;
    activation_limit: number;
    activation_usage: number;
    expires_at: string | null; // ISO 8601 or null for lifetime
  };
  meta?: {
    store_id: number;
    order_id: number;
    order_item_id: number;
    variant_id: number;
    variant_name: string;
    product_id: number;
    product_name: string;
    customer_email: string;
  };
}

interface LSActivateResponse {
  activated?: boolean;
  error?: string | null;
  instance?: { id: string; name: string; created_at: string };
  license_key?: LSValidateResponse["license_key"];
  meta?: LSValidateResponse["meta"];
}

/**
 * POST to LS /licenses/validate. Returns parsed body or throws "NETWORK"/"TIMEOUT".
 * 4xx responses are returned as data — caller decides what to do.
 */
async function lsValidate(licenseKey: string): Promise<{ status: number; body: LSValidateResponse }> {
  const form = new URLSearchParams({ license_key: licenseKey });
  const resp = await fetchWithTimeout(
    `${LS_BASE}/licenses/validate`,
    {
      method: "POST",
      headers: {
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
      },
      body: form.toString(),
    },
    LS_TIMEOUT_MS,
  );
  // LS validate returns 200 with {valid:false} for bad keys, or 4xx for other errors.
  let body: LSValidateResponse;
  try {
    body = await resp.json();
  } catch {
    body = { valid: false, error: "Malformed LS response" };
  }
  return { status: resp.status, body };
}

/**
 * POST to LS /licenses/activate. Requires API key auth.
 * 422 response means the activation limit is exceeded.
 */
async function lsActivate(
  licenseKey: string,
  instanceName: string,
  apiKey: string,
): Promise<{ status: number; body: LSActivateResponse }> {
  const form = new URLSearchParams({
    license_key: licenseKey,
    instance_name: instanceName,
  });
  const resp = await fetchWithTimeout(
    `${LS_BASE}/licenses/activate`,
    {
      method: "POST",
      headers: {
        "Accept": "application/json",
        "Authorization": `Bearer ${apiKey}`,
        "Content-Type": "application/x-www-form-urlencoded",
      },
      body: form.toString(),
    },
    LS_TIMEOUT_MS,
  );
  let body: LSActivateResponse;
  try {
    body = await resp.json();
  } catch {
    body = { activated: false, error: "Malformed LS response" };
  }
  return { status: resp.status, body };
}

// ────────────────────────────────────────────────────────────────────────────
// Handler
// ────────────────────────────────────────────────────────────────────────────

Deno.serve(async (req: Request) => {
  if (req.method === "OPTIONS") {
    return new Response(null, { status: 204, headers: CORS_HEADERS });
  }
  if (req.method !== "POST") {
    return jsonResponse({ error: "Method not allowed" }, 405);
  }

  // LS API key check moved into Path B (LS-license validation) — Path A
  // (Vault-token admin) doesn't call LS, so missing LS key shouldn't block
  // admins. We still hard-fail at the LS-call site so misconfiguration is
  // visible if LS keys are submitted.

  // Parse + validate input
  let body: any;
  try {
    body = await req.json();
  } catch {
    return jsonResponse({ valid: false, tier: "free", message: "Invalid JSON body." }, 400);
  }

  const licenseKey = body?.license_key;
  const machineHash = body?.machine_id_hash;

  // machine_id_hash is required for both paths — validate it first
  if (!isValidMachineHash(machineHash)) {
    return jsonResponse(
      { valid: false, tier: "free", message: "Invalid machine_id_hash format." },
      400,
    );
  }

  // ── Path A: Vault-token admin (precedes LS-variant path) ───────────────
  // High-entropy random tokens stored in the `vct_admin_tokens` Vault
  // secret (JSON map). Used for solo + small-team admin without
  // creating an LS product. Per-token leak containment: optional
  // expiration + TOFU machine binding. See variant_map.ts for the
  // full design.
  if (isValidVaultAdminToken(licenseKey)) {
    const supabaseUrl = Deno.env.get("SUPABASE_URL");
    const serviceRoleKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");
    if (!supabaseUrl || !serviceRoleKey) {
      console.error("FATAL: SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY missing");
      return jsonResponse({ error: "Service misconfigured" }, 500);
    }

    const vaultJson = await fetchVaultAdminTokensJson(supabaseUrl, serviceRoleKey);
    const result = lookupVaultAdminToken(licenseKey, machineHash, vaultJson);

    if (result === null) {
      // Token shape was right, but didn't match any user. Fall through to
      // the LS path is wrong here (LS keys are UUIDs; vct_admin_ keys
      // are not). Reject explicitly.
      return jsonResponse(
        { valid: false, tier: "free", message: "Invalid or expired admin token." },
        401,
      );
    }

    // Audit every outcome (including failures) for forensic trace.
    appendAdminAuthLog(supabaseUrl, serviceRoleKey, {
      admin_user: result.user,
      machine_id_hash: machineHash,
      outcome: result.outcome,
    }).catch(() => {/* non-blocking */});

    if (result.outcome === "expired") {
      console.log(`[validate-tier] admin token expired user=${result.user}`);
      return jsonResponse(
        { valid: false, tier: "free", message: "Admin token expired." },
        401,
      );
    }
    if (result.outcome === "machine_mismatch") {
      console.warn(
        `[validate-tier] admin token machine mismatch user=${result.user}`,
      );
      return jsonResponse(
        {
          valid: false,
          tier: "free",
          error: "machine_mismatch",
          message:
            "Admin token is bound to a different machine. Contact the project owner to rebind.",
        },
        401,
      );
    }

    // Success. TOFU bind if needed.
    if (result.bind_machine_hash) {
      const bound = await bindVaultAdminMachine(
        supabaseUrl,
        serviceRoleKey,
        result.user,
        result.bind_machine_hash,
      );
      if (!bound) {
        console.warn(
          `[validate-tier] TOFU bind failed user=${result.user} ` +
            `(possible race; token still authenticates this request)`,
        );
      }
    }

    console.log(
      `[validate-tier] OK Vault-admin user=${result.user} ` +
        `(${result.bind_machine_hash ? "first-bind" : "matched"})`,
    );
    return jsonResponse({
      valid: true,
      tier: "admin",
      expires_at: null, // expiration is enforced server-side; client doesn't need it
      message: "Validated.",
      is_admin: true,
      unlock_all_modules: true,
      dev_features_enabled: true,
      admin_user: result.user,
    });
  }

  // ── Path B: LS license key (existing UUID path) ────────────────────────
  if (!isValidLicenseKey(licenseKey)) {
    return jsonResponse(
      { valid: false, tier: "free", message: "Invalid license_key format." },
      400,
    );
  }

  // LS API key required for the LS path. Hard-fail if missing — never
  // silently degrade to "everyone is on free tier" because that masks a
  // deployment bug.
  const apiKey = Deno.env.get("LEMON_SQUEEZY_API_KEY");
  if (!apiKey) {
    console.error("FATAL: LEMON_SQUEEZY_API_KEY not configured");
    return jsonResponse({ error: "Service misconfigured" }, 500);
  }

  const keyTag = maskKey(licenseKey);

  // ── Step 1: Validate the license key ────────────────────────────────────
  let validate: Awaited<ReturnType<typeof lsValidate>>;
  try {
    validate = await lsValidate(licenseKey);
  } catch (e) {
    const reason = (e as Error).message;
    console.warn(`[validate-tier] LS validate ${reason} for ${keyTag}`);
    return jsonResponse(
      { valid: false, tier: "free", message: "Validation service unavailable." },
      503,
    );
  }

  // 400 / 404 from LS → treat as invalid key
  if (validate.status === 400 || validate.status === 404) {
    return jsonResponse(
      { valid: false, tier: "free", message: "Invalid or expired license." },
      401,
    );
  }
  // 5xx from LS → bubble up as 503 (client falls back to cached tier)
  if (validate.status >= 500) {
    console.warn(`[validate-tier] LS validate ${validate.status} for ${keyTag}`);
    return jsonResponse(
      { valid: false, tier: "free", message: "Validation service unavailable." },
      503,
    );
  }

  if (!validate.body.valid || !validate.body.license_key) {
    return jsonResponse(
      { valid: false, tier: "free", message: "Invalid or expired license." },
      401,
    );
  }

  const lsKey = validate.body.license_key;
  const lsMeta = validate.body.meta;

  if (lsKey.status !== "active") {
    return jsonResponse(
      {
        valid: false,
        tier: "free",
        message: `License is ${lsKey.status}.`,
      },
      401,
    );
  }

  // ── Step 2: Activate this machine instance ──────────────────────────────
  // LS handles per-license deduplication: re-activating the same
  // instance_name on the same license is idempotent (returns 200 with the
  // existing instance). New machines beyond the limit return 422.
  let activate: Awaited<ReturnType<typeof lsActivate>>;
  try {
    activate = await lsActivate(licenseKey, machineHash, apiKey);
  } catch (e) {
    const reason = (e as Error).message;
    console.warn(`[validate-tier] LS activate ${reason} for ${keyTag}`);
    return jsonResponse(
      { valid: false, tier: "free", message: "Validation service unavailable." },
      503,
    );
  }

  if (activate.status === 422) {
    // Activation limit exceeded — known + handled client-side.
    return jsonResponse({
      valid: false,
      tier: "free",
      error: "instance_limit",
      message:
        "License activated on maximum allowed machines. " +
        "Deactivate old instance at vibecodedtools.it/account",
    }, 200);
  }
  if (activate.status >= 500) {
    console.warn(`[validate-tier] LS activate ${activate.status} for ${keyTag}`);
    return jsonResponse(
      { valid: false, tier: "free", message: "Validation service unavailable." },
      503,
    );
  }
  if (activate.status !== 200 && activate.status !== 201) {
    // Unknown 4xx — treat as invalid to be safe.
    console.warn(
      `[validate-tier] LS activate unexpected ${activate.status} for ${keyTag}: ${activate.body?.error}`,
    );
    return jsonResponse(
      { valid: false, tier: "free", message: "Activation failed." },
      401,
    );
  }

  // ── Step 3: Map variant_id → tier ───────────────────────────────────────
  const variantId = String(lsMeta?.variant_id ?? "");
  const mapping = lookupVariant(variantId);

  if (!mapping) {
    console.warn(`[validate-tier] Unknown variant_id ${variantId} for ${keyTag}`);
    return jsonResponse(
      { valid: false, tier: "free", message: "Unrecognized product." },
      401,
    );
  }

  const tier: OrchestratorTier =
    mapping.appId === "orchestrator" && mapping.tier ? mapping.tier : "free";

  if (tier === "free") {
    // The license is valid, but the product isn't an orchestrator tier —
    // tell the client there's nothing to unlock here.
    return jsonResponse({
      valid: true,
      tier: "free",
      message: "License valid but does not grant an Orchestrator tier.",
    });
  }

  console.log(
    `[validate-tier] OK key=${keyTag} variant=${variantId} tier=${tier} usage=${lsKey.activation_usage}/${lsKey.activation_limit}`,
  );

  // Bug 33: admin tier carries extra capability flags so the client
  // doesn't have to special-case the tier name. These are in addition
  // to the standard tier-feature gates (admin is treated as a superset
  // of enterprise by the client validator).
  const adminExtras = tier === "admin"
    ? {
      is_admin: true,
      unlock_all_modules: true,
      dev_features_enabled: true,
    }
    : {};

  return jsonResponse({
    valid: true,
    tier,
    expires_at: lsKey.expires_at, // null for lifetime
    message: "Validated.",
    ...adminExtras,
  });
});

// ────────────────────────────────────────────────────────────────────────────
// Response shape reference (for client devs)
// ────────────────────────────────────────────────────────────────────────────
//
// 200 — valid orchestrator license:
//   { valid: true, tier: "pro"|"mao"|"enterprise",
//     expires_at: "2027-04-18T00:00:00.000Z" | null,
//     message: "Validated." }
//
// 200 — Bug 33: valid admin license (variant in LS_ADMIN_VARIANT_IDS):
//   { valid: true, tier: "admin",
//     expires_at: "...",
//     message: "Validated.",
//     is_admin: true,
//     unlock_all_modules: true,
//     dev_features_enabled: true }
//
// 200 — Vault-token admin (vct_admin_<token> matched in vct_admin_tokens):
//   { valid: true, tier: "admin",
//     expires_at: null,
//     message: "Validated.",
//     is_admin: true,
//     unlock_all_modules: true,
//     dev_features_enabled: true,
//     admin_user: "<admin-identifier>" }
//
// 401 — Vault-token admin token expired / machine-bound to a different machine:
//   { valid: false, tier: "free", message: "Admin token expired." }
//   { valid: false, tier: "free", error: "machine_mismatch", message: "..." }
//
// 200 — valid license but not an orchestrator product:
//   { valid: true, tier: "free", message: "...does not grant..." }
//
// 200 — instance limit exceeded:
//   { valid: false, tier: "free", error: "instance_limit", message: "..." }
//
// 400 — malformed request:
//   { valid: false, tier: "free", message: "Invalid license_key format." }
//
// 401 — invalid / expired license:
//   { valid: false, tier: "free", message: "Invalid or expired license." }
//
// 500 — service misconfigured (LS API key missing):
//   { error: "Service misconfigured" }
//
// 503 — LS unreachable / 5xx / timeout:
//   { valid: false, tier: "free", message: "Validation service unavailable." }
//   (client falls back to its cached tier within the 3-day grace period)
