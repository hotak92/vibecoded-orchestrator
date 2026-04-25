import { writable, derived } from 'svelte/store';
import { auth } from './auth';

export interface License {
  key: string;
  appId: string;
  appName: string;
  activatedAt: string;
  status: 'active' | 'expired' | 'invalid';
}

interface LicenseState {
  licenses: License[];
  validating: boolean;
  error: string | null;
  success: string | null;
}

const STORAGE_KEY = 'vct_licenses';

function loadLicenses(): License[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? JSON.parse(raw) : [];
  } catch {
    return [];
  }
}

function saveLicenses(licenses: License[]) {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(licenses));
}

// App ID mapping from Lemon Squeezy product variants
// Update these when you create products in Lemon Squeezy
const PRODUCT_MAP: Record<string, { appId: string; appName: string }> = {
  // Format: 'lemon_squeezy_variant_id': { appId, appName }
  // These will be filled in when LS products are created
  transcrypt: { appId: 'transcrypt', appName: 'Transcrypt' },
  arzillibus: { appId: 'arzillibus', appName: 'Arzillibus' },
  convertifacile: { appId: 'convertifacile', appName: 'ConvertiFacile' },
  dataweave: { appId: 'dataweave', appName: 'DataWeave' },
  formcraft: { appId: 'formcraft', appName: 'FormCraft' },
  pixelsnap: { appId: 'pixelsnap', appName: 'PixelSnap' },
};

function createLicenseStore() {
  const { subscribe, set, update } = writable<LicenseState>({
    licenses: loadLicenses(),
    validating: false,
    error: null,
    success: null,
  });

  return {
    subscribe,

    async validateCode(code: string): Promise<boolean> {
      update((s) => ({ ...s, validating: true, error: null, success: null }));

      const apiKey = import.meta.env.VITE_LEMONSQUEEZY_API_KEY;

      // Dev-mode test codes: ONLY available on dev builds. `import.meta.env.DEV`
      // is statically replaced by Vite at build time → this branch is fully
      // eliminated from production bundles (tree-shaken, dead-code removed).
      // Do NOT gate on `apiKey` presence — a prod build with a missing key
      // would otherwise expose free activation for any app.
      if (import.meta.env.DEV) {
        const match = code.match(/^test-(\w+)$/);
        if (match && PRODUCT_MAP[match[1]]) {
          const product = PRODUCT_MAP[match[1]];
          const license: License = {
            key: code,
            appId: product.appId,
            appName: product.appName,
            activatedAt: new Date().toISOString(),
            status: 'active',
          };

          update((s) => {
            const existing = s.licenses.find((l) => l.key === code);
            if (existing) {
              return { ...s, validating: false, error: 'This code is already activated' };
            }
            const licenses = [...s.licenses, license];
            saveLicenses(licenses);
            return { ...s, licenses, validating: false, success: `${product.appName} activated!` };
          });

          // Dev only: locally mark app as active in the store. In production,
          // entitlements come from `profiles.apps` written by the webhook.
          auth.markAppActiveLocal(product.appId);
          return true;
        }

        // Dev mode reached but the code isn't a recognized test code → fall
        // through to the real LS validation below (allows testing with real
        // sandbox keys in dev too).
      }

      // Production path: a real LS API key is mandatory. Fail loudly rather
      // than silently calling the LS API with an empty Authorization header
      // (which would 401 with a confusing "Connection error" toast).
      if (!apiKey || apiKey === 'your_lemon_squeezy_api_key_here') {
        update((s) => ({
          ...s,
          validating: false,
          error: 'License validation is not configured. Please contact support.',
        }));
        console.error(
          'VITE_LEMONSQUEEZY_API_KEY is missing or placeholder in a non-dev build.'
        );
        return false;
      }

      // Real Lemon Squeezy validation
      try {
        const response = await fetch('https://api.lemonsqueezy.com/v1/licenses/validate', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ license_key: code }),
        });

        const result = await response.json();

        if (!result.valid) {
          update((s) => ({ ...s, validating: false, error: 'Invalid or expired license key' }));
          return false;
        }

        // Map the LS product to our app
        const variantId = String(result.meta?.variant_id ?? '');
        const product = PRODUCT_MAP[variantId];

        if (!product) {
          update((s) => ({
            ...s,
            validating: false,
            error: 'License valid but product not recognized. Contact support.',
          }));
          return false;
        }

        // Activate the license on LS side
        await fetch('https://api.lemonsqueezy.com/v1/licenses/activate', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ license_key: code, instance_name: 'VCT Launcher' }),
        });

        const license: License = {
          key: code,
          appId: product.appId,
          appName: product.appName,
          activatedAt: new Date().toISOString(),
          status: 'active',
        };

        update((s) => {
          const existing = s.licenses.find((l) => l.key === code);
          if (existing) {
            return { ...s, validating: false, error: 'This code is already activated' };
          }
          const licenses = [...s.licenses, license];
          saveLicenses(licenses);
          return { ...s, licenses, validating: false, success: `${product.appName} activated!` };
        });

        // Optimistic local UI update. The webhook (server-side, service_role)
        // is the source of truth for `profiles.apps`; the next call to
        // `auth.refreshProfile()` will pull the authoritative list.
        auth.markAppActiveLocal(product.appId);
        auth.refreshProfile();
        return true;
      } catch (err) {
        update((s) => ({
          ...s,
          validating: false,
          error: 'Connection error. Check your internet and try again.',
        }));
        return false;
      }
    },

    clearMessages() {
      update((s) => ({ ...s, error: null, success: null }));
    },
  };
}

export const licenses = createLicenseStore();
