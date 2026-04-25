# VCT Launcher - Setup Guide

## Prerequisites

### All Platforms
- Node.js >= 18
- Rust (via rustup: https://rustup.rs)
- npm

### Linux (Ubuntu/Debian)
```bash
sudo apt update
sudo apt install libwebkit2gtk-4.1-dev build-essential curl wget file \
  libssl-dev libayatana-appindicator3-dev librsvg2-dev \
  libgtk-3-dev libsoup-3.0-dev libjavascriptcoregtk-4.1-dev
```

### Windows
- WebView2 (pre-installed on Windows 10/11)
- Visual Studio Build Tools with C++ workload

### macOS
- Xcode Command Line Tools: `xcode-select --install`

## Getting Started

```bash
# Install dependencies
npm install
```

## Running the App

### Development (frontend + backend together)
```bash
npm run tauri dev
```
This single command starts:
- **Frontend**: Vite dev server on `http://localhost:1420` (hot reload)
- **Backend**: Rust/Tauri process that opens the native window with WebView

> First run compiles all Rust dependencies (~2-5 min). Subsequent runs are fast.

### Frontend only (no native window)
```bash
npm run dev
```
Opens the SvelteKit app in the browser at `http://localhost:1420`.
Useful for quick UI work — Tauri APIs won't be available.

### Build for production
```bash
npm run tauri build
```
Output in `src-tauri/target/release/bundle/`:
- **Windows**: `.msi` + `.exe` (NSIS installer)
- **Linux**: `.deb` + `.AppImage`
- **macOS**: `.dmg` + `.app`

### Other commands
```bash
npm run check          # TypeScript type-check
npm run check:watch    # TypeScript watch mode
npm run preview        # Preview production build (frontend only)
```

## Environment Variables

Copy `.env.example` or create `.env` with:

```env
# Supabase Auth
VITE_SUPABASE_URL=https://ltnlwhaxnpbiifordlbk.supabase.co
VITE_SUPABASE_ANON_KEY=<your-anon-key>

# Lemon Squeezy (license validation)
VITE_LEMONSQUEEZY_API_KEY=<your-ls-api-key>
```

## Database Setup

Run `supabase-setup.sql` on your Supabase project's SQL editor. This creates:
- `profiles` table with RLS policies
- Auto-create profile trigger on signup

## Supabase Edge Function (Webhook)

The webhook receives Lemon Squeezy purchase events and auto-activates apps.

### Deploy
```bash
# Set your access token
export SUPABASE_ACCESS_TOKEN=<your-token>

# Link project
npx supabase link --project-ref ltnlwhaxnpbiifordlbk

# Set webhook secret
npx supabase secrets set LEMON_SQUEEZY_WEBHOOK_SECRET=wh_vct_ls_2026_s3cur3k3y

# Deploy
npx supabase functions deploy lemon-squeezy-webhook --no-verify-jwt
```

### Lemon Squeezy Webhook Configuration
1. Go to **app.lemonsqueezy.com** → **Settings** → **Webhooks** → **Add Webhook**
2. **URL**: `https://ltnlwhaxnpbiifordlbk.supabase.co/functions/v1/lemon-squeezy-webhook`
3. **Signing Secret**: `wh_vct_ls_2026_s3cur3k3y`
4. **Events**: select only `order_created`

## Dev Mode

Without LS products configured, you can test activation with codes:
- `test-transcrypt` → activates Transcrypt
- `test-arzillibus` → activates Arzillibus
- `test-convertifacile` → activates ConvertiFacile
- etc.

Enter these in: Avatar menu → Activation Codes

## Project Structure

```
src/
  lib/
    components/       UI components (MenuBar, Sidebar, modals)
    stores/           Svelte stores (auth, licenses, settings)
    supabase.ts       Supabase client init
  routes/
    +page.svelte      Main app (Library + Store)
    +layout.svelte    Auth guard + loading screen
    auth/+page.svelte Login/Register page
  app.css             Global styles + design system
src-tauri/
  src/lib.rs          Tauri app entry (Rust)
  tauri.conf.json     Tauri config
  Cargo.toml          Rust dependencies
supabase/
  functions/
    lemon-squeezy-webhook/  LS webhook Edge Function
```
