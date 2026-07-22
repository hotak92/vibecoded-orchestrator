import { writable, derived } from 'svelte/store';
import { supabase, supabaseConfigured } from '$lib/supabase';
import type { User as SupabaseUser } from '@supabase/supabase-js';

export interface User {
  id: string;
  email: string;
  name: string;
  avatar?: string;
  apps: string[];
}

interface AuthState {
  user: User | null;
  loading: boolean;
  error: string | null;
}

function mapSupabaseUser(su: SupabaseUser, profile?: { name?: string; apps?: string[] }): User {
  return {
    id: su.id,
    email: su.email ?? '',
    name: profile?.name ?? su.user_metadata?.name ?? su.email?.split('@')[0] ?? 'User',
    apps: profile?.apps ?? [],
  };
}

function createAuthStore() {
  const { subscribe, set, update } = writable<AuthState>({
    user: null,
    loading: true,
    error: null,
  });

  async function loadProfile(userId: string): Promise<{ name?: string; apps?: string[] }> {

    try {
      const result = await Promise.race([
        supabase.from('profiles').select('name, apps').eq('id', userId).single(),
        new Promise<never>((_, reject) => setTimeout(() => reject(new Error('timeout')), 5000)),
      ]);

      return result.data ?? {};
    } catch (e) {

      return {};
    }
  }

  // NOTE: `profiles.apps` is server-managed (written only by the
  // lemon-squeezy-webhook with the service_role key). RLS forbids clients
  // from updating it. There is intentionally no `saveApps()` helper here.

  // Initialize: check existing session
  async function init() {
    // Offline mode: no Supabase configured — run as local user
    if (!supabaseConfigured) {
      set({
        user: {
          id: 'local',
          email: 'local@localhost',
          name: 'Local User',
          apps: ['orchestrator'], // Free tier always available
        },
        loading: false,
        error: null,
      });
      return;
    }

    try {
      const { data: { session } } = await supabase.auth.getSession();
      if (session?.user) {
        const profile = await loadProfile(session.user.id);
        set({
          user: mapSupabaseUser(session.user, profile),
          loading: false,
          error: null,
        });
      } else {
        set({ user: null, loading: false, error: null });
      }
    } catch (e) {
      // Auth failed (network, bad credentials, etc) — allow offline usage
      set({ user: null, loading: false, error: null });
    }
  }

  // Listen for auth state changes (handles all events including INITIAL_SESSION)
  if (supabaseConfigured) {
    supabase.auth.onAuthStateChange(async (event, session) => {
      if (session?.user) {
        const profile = await loadProfile(session.user.id);
        set({
          user: mapSupabaseUser(session.user, profile),
          loading: false,
          error: null,
        });
      } else {
        set({ user: null, loading: false, error: null });
      }
    });
  } else {
    // No Supabase — initialize immediately in offline mode
    init();
  }

  return {
    subscribe,

    async login(email: string, password: string): Promise<boolean> {
      update((s) => ({ ...s, loading: true, error: null }));

      const { data, error } = await supabase.auth.signInWithPassword({ email, password });


      if (error) {
        update((s) => ({ ...s, loading: false, error: error.message }));
        return false;
      }

      return true;
    },

    async register(name: string, email: string, password: string): Promise<boolean | 'confirm_email'> {
      update((s) => ({ ...s, loading: true, error: null }));

      const { data, error } = await supabase.auth.signUp({
        email,
        password,
        options: { data: { name } },
      });

      if (error) {
        update((s) => ({ ...s, loading: false, error: error.message }));
        return false;
      }

      // Profile row is auto-created server-side by the `handle_new_user`
      // trigger (see migrations). We just need to set the display name —
      // RLS allows the user to update their own `name`, but not `apps`.
      if (data.user) {
        await supabase
          .from('profiles')
          .update({ name })
          .eq('id', data.user.id);
      }

      // Supabase returns no session when email confirmation is required
      if (!data.session) {
        update((s) => ({ ...s, loading: false, error: null }));
        return 'confirm_email';
      }

      return true;
    },

    async logout() {
      await supabase.auth.signOut();
    },

    clearError() {
      update((s) => ({ ...s, error: null }));
    },

    // v0.2.54 Track H (P0-6): `markAppActiveLocal` was removed together
    // with its only caller, the legacy localStorage `licenses` store
    // that validated LemonSqueezy keys client-side. `profiles.apps`
    // remains server-managed (lemon-squeezy-webhook, service_role);
    // clients read it via `refreshProfile()`.

    async refreshProfile() {
      const { data: { session } } = await supabase.auth.getSession();
      if (session?.user) {
        const profile = await loadProfile(session.user.id);
        set({
          user: mapSupabaseUser(session.user, profile),
          loading: false,
          error: null,
        });
      }
    },

    async updateProfile(name: string): Promise<void> {
      // Snapshot the current user id synchronously (no forward reference to
      // the exported `auth` binding, which isn't assigned yet at closure
      // creation). The no-op `update` reads the live state and returns it
      // unchanged.
      let userId: string | null = null;
      update((s) => {
        userId = s.user?.id ?? null;
        return s;
      });
      if (!userId) return;

      // supabase-js v2 query builders are thenables that only fire an HTTP
      // request when awaited/`.then`'d. The builder MUST be awaited here or
      // no write ever reaches Supabase (the pre-fix version discarded an
      // un-awaited builder inside a synchronous store callback, so the row
      // never changed and the name silently reverted on the next fetch).
      //
      // Update name only. RLS forbids the client from touching `apps`;
      // sending it (even unchanged) would be rejected by the WITH CHECK
      // clause in some configurations.
      const { error } = await supabase
        .from('profiles')
        .update({ name })
        .eq('id', userId);

      if (error) {
        // Surface the failure so the caller can show an honest error
        // instead of an unconditional "Saved!".
        throw new Error(error.message);
      }

      // Mutate the local store only AFTER the write confirms.
      update((s) => (s.user ? { ...s, user: { ...s.user, name } } : s));
    },
  };
}

export const auth = createAuthStore();
export const isAuthenticated = derived(auth, ($a) => $a.user !== null);
export const currentUser = derived(auth, ($a) => $a.user);
export const authLoading = derived(auth, ($a) => $a.loading);
