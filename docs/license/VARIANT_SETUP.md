# Lemon Squeezy Variant Setup — Orchestrator Pro

Step-by-step for the human with LS dashboard access (Martino / Fabio).
The orchestrator's license validator and Supabase edge functions are wired
end-to-end; the only remaining manual step is creating the variants in LS
and copying their numeric IDs into the variant map.

## Scope (OSS launch)

Only **Orchestrator Pro** is launch-blocking. MAO and the Telegram module
are post-launch.

| Variant                 | Price | Type                  | Activation cap |
| ----------------------- | ----- | --------------------- | -------------- |
| Pro Monthly             | €19   | Subscription, monthly | 3 machines     |
| Pro Annual              | €149  | Subscription, yearly  | 3 machines     |
| Pro Lifetime (limited)  | €199  | One-time, qty cap 100 | 3 machines     |

The 3-machine cap matches the validator's expectation: re-activation on the
same `machine_id_hash` is idempotent (LS dedupes on `instance_name`), and a
4th machine returns HTTP 422 → `error: instance_limit` from `validate-tier`.

## Steps in the LS dashboard

1. **Create the product** (one product, three variants):
   - Dashboard → Products → New product.
   - Name: `Vibecoded Orchestrator Pro`.
   - Description: short pitch — see `docs/POSITIONING.md`.
   - Add three variants (Variants tab → Add variant):
     - `Monthly` — Subscription, recurring monthly, €19, no trial.
     - `Annual` — Subscription, recurring yearly, €149, no trial.
     - `Lifetime` — One-time, €199, **enable inventory tracking** with
       quantity = 100. (LS will auto-disable purchase when sold out.)
   - On each variant: enable **License keys**, set **Activation limit = 3**,
     **Activation type = activations**, leave **License length** as default
     (lifetime = `0 days = lifetime`; subs inherit from billing cycle).

2. **Note the variant IDs**:
   - Open each variant's edit screen. The URL is
     `app.lemonsqueezy.com/products/<product_id>/variants/<variant_id>`.
     The trailing number is what you need.
   - Or call the API once a working PAT exists:
     ```bash
     KEY=$(cat ~/.vct-secrets/shared/squeezylemon_api_token | tr -d '\n')
     curl -s -H "Accept: application/vnd.api+json" \
          -H "Authorization: Bearer $KEY" \
          "https://api.lemonsqueezy.com/v1/variants" \
       | python3 -c "import sys,json; d=json.load(sys.stdin); [print(v['id'], v['attributes']['name']) for v in d['data']]"
     ```

3. **Update the variant map** at
   `launcher/supabase/functions/_shared/variant_map.ts`:
   ```ts
   export const VARIANT_MAP: Record<string, VariantMapping> = {
     "1234567": { appId: "orchestrator", tier: "pro" },  // Monthly
     "1234568": { appId: "orchestrator", tier: "pro" },  // Annual
     "1234569": { appId: "orchestrator", tier: "pro" },  // Lifetime
     // MAO_*_TODO and other placeholders stay until those products exist.
   };
   ```
   Keep the placeholder keys as comments so we know what's still TBD; the
   validator's lookup is exact-string keyed, so they're inert until purchased.

4. **Redeploy the edge functions** (Supabase CLI):
   ```bash
   cd launcher/supabase
   supabase functions deploy validate-tier
   supabase functions deploy lemon-squeezy-webhook
   ```

5. **Configure Supabase function secrets** (one-time per environment):
   ```bash
   supabase secrets set LEMON_SQUEEZY_API_KEY="<paste PAT>"
   supabase secrets set LEMON_SQUEEZY_WEBHOOK_SECRET="<random 32-byte hex>"
   ```
   Then in the LS dashboard → Settings → Webhooks, point the webhook at
   `https://<project>.supabase.co/functions/v1/lemon-squeezy-webhook` and
   paste the same secret.

6. **Smoke test** (uses a real LS test-mode license):
   - Switch the LS store to **Test mode**.
   - Generate a test license via the LS UI (Licensing → Generate test license,
     pick the Orchestrator-Pro Monthly variant).
   - Paste it into the launcher's Activation modal.
   - Expected: tier becomes `pro` within ~2 s; `~/.vibecoded/license_cache.json`
     shows `tier=pro`, `valid=true`, `last_validated_at=<recent>`.

## Failure modes you might hit

| Symptom                                              | Likely cause                              | Fix                                                                                    |
| ---------------------------------------------------- | ----------------------------------------- | -------------------------------------------------------------------------------------- |
| `validate-tier` returns 401, `Invalid or expired`    | Variant ID not in map                     | Add the ID to `variant_map.ts` and redeploy                                            |
| `validate-tier` returns 500 `Service misconfigured`  | LS API key not in Supabase secrets        | `supabase secrets set LEMON_SQUEEZY_API_KEY=...`                                       |
| Activation limit triggered immediately on first try  | `instance_name` collision across machines | Confirm `machine_id_hash` is sha256 of MAC bytes, hex lowercase (validator + Tauri)    |
| Webhook fires but profile.apps not updated           | RLS blocking service-role insert          | Confirm webhook function uses service-role key, not anon                               |
