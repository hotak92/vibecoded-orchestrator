# VCT Launcher - Handoff per Martino

## Stato Attuale (2026-03-07)

Il launcher e' funzionante con GUI completa, autenticazione, e sistema di pagamento predisposto.

### Cosa funziona gia'
- **GUI completa**: menu bar, griglia app, sidebar destra contestuale, status bar
- **Design**: tema dark, bottoni 3D, effetti glassmorphism + aurora
- **Auth**: login/registrazione con Supabase (email/password), sessione persistente
- **Settings**: pannello impostazioni (profilo, downloads, about)
- **Attivazione codici**: modale per inserire codici licenza (manual + dev mode)
- **Webhook Lemon Squeezy**: Edge Function deployata su Supabase, auto-attiva le app dopo l'acquisto
- **Store**: bottone "Get" apre il checkout LS con email pre-compilata
- **Auto-refresh**: quando l'utente torna sul launcher dopo l'acquisto, il profilo si ricarica

### Cosa manca
1. **Creare i prodotti su Lemon Squeezy** (vedi sezione sotto)
2. **Configurare il webhook su LS** (vedi sezione sotto)
3. **Download/installazione reale** delle app via Tauri
4. **Lancio reale** delle app via Tauri
5. **Orchestrazione inter-app** (backend Rust)

---

## Setup per Sviluppo

### Prerequisiti
- Node.js >= 18
- Rust (https://rustup.rs)

### Linux (Ubuntu/Debian)
```bash
sudo apt update
sudo apt install libwebkit2gtk-4.1-dev build-essential curl wget file \
  libssl-dev libayatana-appindicator3-dev librsvg2-dev \
  libgtk-3-dev libsoup-3.0-dev libjavascriptcoregtk-4.1-dev
```

### Avvio
```bash
npm install
npm run tauri dev
```

### Build produzione
```bash
npm run tauri build
# Output: src-tauri/target/release/bundle/
# Windows: .msi + .exe
# Linux: .deb + .AppImage
# macOS: .dmg + .app
```

---

## Configurazione Lemon Squeezy

### 1. Creare i prodotti
Per ogni app (Transcrypt, Arzillibus, ConvertiFacile, DataWeave, FormCraft, PixelSnap):
1. Vai su **app.lemonsqueezy.com** → **Products** → **New Product**
2. Abilita **"Generate license keys"**
3. Dopo la creazione, prendi il **variant_id** (visibile nell'URL del prodotto o nella pagina)
4. Prendi anche il **checkout URL** del prodotto

### 2. Aggiornare il codice con i variant_id

**File**: `supabase/functions/lemon-squeezy-webhook/index.ts`
```typescript
const VARIANT_MAP: Record<string, string> = {
  "VARIANT_ID_QUI": "transcrypt",
  "VARIANT_ID_QUI": "arzillibus",
  // ... etc
};
```

**File**: `src/routes/+page.svelte` — aggiornare checkoutUrl per ogni app:
```typescript
{ id: 'transcrypt', ..., checkoutUrl: 'https://TUOSTORE.lemonsqueezy.com/checkout/buy/HASH' },
```

### 3. Ri-deployare la Edge Function
```bash
export SUPABASE_ACCESS_TOKEN=<token>
npx supabase functions deploy lemon-squeezy-webhook --no-verify-jwt
```

### 4. Configurare il webhook su LS
1. **app.lemonsqueezy.com** → **Settings** → **Webhooks** → **Add Webhook**
2. **URL**: `https://ltnlwhaxnpbiifordlbk.supabase.co/functions/v1/lemon-squeezy-webhook`
3. **Signing Secret**: `wh_vct_ls_2026_s3cur3k3y`
4. **Events**: seleziona solo **order_created**

---

## Test in Dev Mode

Senza prodotti LS, si puo' testare con codici dev:
- `test-transcrypt` → attiva Transcrypt
- `test-arzillibus` → attiva Arzillibus
- `test-convertifacile` → attiva ConvertiFacile
- `test-dataweave` → attiva DataWeave
- `test-formcraft` → attiva FormCraft
- `test-pixelsnap` → attiva PixelSnap

Inserire in: Menu avatar → Activation Codes

---

## Credenziali e Servizi

| Servizio | Dettaglio |
|----------|-----------|
| Supabase Project | ltnlwhaxnpbiifordlbk |
| Supabase URL | https://ltnlwhaxnpbiifordlbk.supabase.co |
| Edge Function | lemon-squeezy-webhook (deployed) |
| Webhook Secret | wh_vct_ls_2026_s3cur3k3y |
| LS API Key | in .env (VITE_LEMONSQUEEZY_API_KEY) |

---

## Compatibilita'

| Piattaforma | Stato | Note |
|-------------|-------|------|
| Windows 10/11 | OK | Testato, WebView2 pre-installato |
| Linux (Ubuntu 22+) | OK | Richiede deps (vedi sopra) |
| macOS | OK | Richiede Xcode CLI tools |

Il codice e' completamente cross-platform. Tauri 2 genera bundle nativi per ogni OS.

---

## Struttura Progetto

```
VCT-Launcher/
  src/                    Frontend SvelteKit
    lib/
      components/         MenuBar, Sidebar, modals, etc.
      stores/             auth, licenses, settings (Svelte stores)
      supabase.ts         Client Supabase
    routes/               Pagine (main + auth)
    app.css               Design system globale
  src-tauri/              Backend Rust (Tauri 2)
  supabase/
    functions/
      lemon-squeezy-webhook/   Edge Function per webhook LS
  docs/                   Documentazione
  .env                    Credenziali (NON committare)
  supabase-setup.sql      Schema DB
```
