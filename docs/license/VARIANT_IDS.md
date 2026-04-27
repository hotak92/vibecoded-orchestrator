# Orchestrator Pro — Variant IDs

> **Status as of 2026-04-26**: Variants not yet created in LS (LS API key
> setup pending). Fill these in once the variants are live.

| Tier         | Variant Name | LS variant_id | Price | Activation cap |
| ------------ | ------------ | ------------- | ----- | -------------- |
| Pro Monthly  | Monthly      | TBD           | €19   | 3              |
| Pro Annual   | Annual       | TBD           | €149  | 3              |
| Pro Lifetime | Lifetime     | TBD           | €199  | 3              |

## How to fetch IDs

Once a working LS API key is in `~/.vct-secrets/shared/squeezylemon_api_token`:

```bash
KEY=$(cat ~/.vct-secrets/shared/squeezylemon_api_token | tr -d '\n')
curl -s -H "Accept: application/vnd.api+json" \
     -H "Authorization: Bearer $KEY" \
     "https://api.lemonsqueezy.com/v1/variants?page[size]=100" \
  | python3 -c "import sys,json
d=json.load(sys.stdin)
for v in d.get('data', []):
    a = v['attributes']
    print(v['id'], '|', a.get('name'), '|', a.get('price'), '|', a.get('status'))"
```

## How to apply

After populating the table above, edit
`launcher/supabase/functions/_shared/variant_map.ts` and replace the
`*_TODO` placeholder keys with the real numeric IDs:

```ts
export const VARIANT_MAP: Record<string, VariantMapping> = {
  "1234567": { appId: "orchestrator", tier: "pro" },   // Monthly
  "1234568": { appId: "orchestrator", tier: "pro" },   // Annual
  "1234569": { appId: "orchestrator", tier: "pro" },   // Lifetime
};
```

That file lives in the launcher branch (`launcher/v1.0-gui-critical-path`
or wherever the launcher subtree is). Coordinate with the launcher GUI
agent to land the change — this branch must NOT modify `launcher/`.

## Verification

After variants exist and `variant_map.ts` is updated:

1. Deploy `validate-tier` and `lemon-squeezy-webhook` edge functions.
2. Buy a Pro license with a test card.
3. Run the validator with the test key:
   ```bash
   export VIBECODED_LICENSE_KEY="<test-key>"
   python -m VCThelpers.license.validator
   ```
4. Expected: `Tier: pro / Valid: True`.
