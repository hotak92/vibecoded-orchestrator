---
title: Merchant of Record vs Stripe for Indies
type: concept
tags: [saas, payments, business, founder, compliance, stripe, mid-level-architecture, active]
created: 2026-05-19T00:00:00Z
updated: 2026-05-19T00:00:00Z
valid_from: 2026-05-19T00:00:00Z
valid_until: null
status: active
---

# Merchant of Record vs Stripe for Indies

## Why This Decision Matters

The payment provider you pick determines who is legally on the hook for sales tax, VAT, GST, refunds, and chargebacks in every country your customers live in. For a solo founder selling globally, this is the difference between "I'm focused on product" and "I'm filing quarterly VAT returns in 7 jurisdictions."

There are two structural choices, not a list of brands:

1. **Direct payment processor** (Stripe, Paddle Billing API, Adyen) — you are the merchant. You charge cards, but you also owe sales tax / VAT / GST registration and remittance wherever you have a tax nexus.
2. **Merchant of Record (MoR)** (Lemon Squeezy, Polar, Paddle Classic, Gumroad, FastSpring) — the MoR is legally the seller. They collect tax and remit it. You receive a single net payout.

This is not a fee comparison — it's a "what do you want to be responsible for" decision.

## The 2026 Landscape

| Provider | Type | Fee (verified 2026-05-19) | Best for |
|----------|------|---------------------------|----------|
| **Stripe** | Direct processor | 2.9% + $0.30 US, ~1.5% + €0.25 EU (varies) | US-focused, scaling, want full control |
| **Lemon Squeezy** | MoR | 5% + 50¢ | Indies selling globally, want sales tax handled |
| **Polar.sh** | MoR (open source) | 4% + 40¢ | Open-source projects, GitHub-native flows |
| **Paddle** | MoR (Classic) / Processor (Billing) | 5% + 50¢ MoR; ~variable for Billing | B2B SaaS, EU/UK heavy |
| **Gumroad** | MoR | 10% (flat, no monthly) | Digital products, one-time sales |
| **FastSpring** | MoR | ~5.9% + $0.95 | Established cross-border SaaS |

**Pricing verified live**: Lemon Squeezy and Stripe pricing pages fetched 2026-05-19. Other rates from each provider's published pricing as of writing; rates do change — recheck before signing up.

## The Real Decision Tree

```
Are you selling to 1+ EU consumers?
├── Yes → MoR is almost certainly worth the 2% fee premium
│   (EU VAT MOSS registration + quarterly returns is a real time sink)
└── No (US-only B2B) → Stripe is fine
    └── Will you sell to EU within 12 months?
        ├── Yes → Start on MoR to avoid migration pain later
        └── No → Stripe; revisit when expanding

Do you want to issue invoices with your company name on them?
├── Yes → Direct processor (Stripe/Paddle Billing) — MoR invoices say "Lemon Squeezy" as seller
└── No → MoR fine

Are you under $5K MRR?
├── Yes → MoR. The 2% fee premium is cheaper than 3 hours of your time per quarter on tax filings.
└── No → Run the actual math:
    -  Stripe at 2.9% + tax/accounting cost (~$200-500/mo at scale) vs MoR at 5%
    - Crossover point usually ~$30-50K MRR depending on jurisdictions
```

## What the MoR Premium Actually Buys

The 2% extra (5% MoR vs 2.9% Stripe) is not a payment-processing fee. It's an outsourced compliance bundle:

- **Sales tax / VAT / GST** registration in every jurisdiction where they sell — collected and remitted on your behalf
- **EU VAT MOSS** quarterly returns
- **US state sales tax** (currently 45 states have economic-nexus laws triggered by digital sales)
- **GDPR DPA** — they're a co-controller; you sign their DPA once
- **Chargeback handling** — they fight chargebacks for you
- **Refund handling** — they process refunds against their merchant account
- **Fraud screening** — they bear the loss on accepted-then-disputed fraud

For a 1-person company with a global customer base, that's $5–15K/year of accountant/lawyer time. The MoR premium is usually cheaper.

## The Migration Reality

Switching payment providers is **painful**:

1. **Customer card data is not portable** between providers. You can't move active subscriptions from Stripe → Lemon Squeezy without re-asking every customer to re-enter their card.
2. **Subscription state** (billing dates, prorations, coupon history) needs custom migration code.
3. **Webhook contracts differ** — your billing-related code touches everything.

Practical migrations:
- Run both side-by-side: new signups on the new provider, existing subscriptions stay on the old. 18-month-tail until everyone's churned or manually moved.
- Cohort-migrate at annual renewal: ask each annual customer to re-subscribe on the new provider with a discount sweetener.

**Implication**: pick correctly day 1 if you can. The decision compounds.

## What This Means for "Default Stack" Advice

Common advice circa 2022–2023 was "use Stripe by default, it's the standard." That advice is **stale for solo founders selling globally** in 2026. The MoR ecosystem matured — Lemon Squeezy, Polar, and Paddle are all production-grade — and EU/US tax enforcement got more aggressive. The default for an indie shipping to a global audience is now MoR. Stripe remains the right call for US-first B2B or for products that need invoice-customisation, complex billing logic, or sub-1% fee tiers at scale.

## Related

- `[[implements::SaaS Pricing Psychology for Solo Founders]]`
- `[[relatedTo::GDPR and EU VAT Lite for Solo Founders]]`
- `[[relatedTo::Churn Taxonomy and Reduction Tactics]]`

## Sources

- Lemon Squeezy pricing page, https://www.lemonsqueezy.com/pricing — fetched 2026-05-19, confirmed "5% + 50¢"
- Stripe Atlas guide, *Merchant of record explained*
- Polar.sh public pricing — 4% + 40¢ as of 2026-05 (JS-rendered, not directly grep-verified)
- EU VAT MOSS / OSS docs — https://taxation-customs.ec.europa.eu/online-services/online-services-and-databases-taxation/oss-one-stop-shop_en
