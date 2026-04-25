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

    /**
     * Optimistically mark an app as active in the LOCAL store only.
     *
     * SECURITY: This does NOT write to the database. The source of truth for
     * `profiles.apps` is the lemon-squeezy-webhook (server-side, service_role).
     * RLS policies block the client from updating the `apps` column.
     *
     * Use this only after a successful license activation flow (which the
     * webhook will eventually mirror to the database). Call
     * `auth.refreshProfile()` afterwards to reconcile with the server.
     */
    markAppActiveLocal(appId: string) {
      update((s) => {
        if (!s.user || s.user.apps.includes(appId)) return s;
        const updatedUser = { ...s.user, apps: [...s.user.apps, appId] };
        return { ...s, user: updatedUser };
      });
    },

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

    async updateProfile(name: string) {
      update((s) => {
        if (!s.user) return s;
        const updatedUser = { ...s.user, name };
        // Update name only. RLS forbids the client from touching `apps`;
        // sending it (even unchanged) would be rejected by the WITH CHECK
        // clause in some configurations.
        supabase
          .from('profiles')
          .update({ name })
          .eq('id', s.user.id);
        return { ...s, user: updatedUser };
      });
    },
  };
}

export const auth = createAuthStore();
export const isAuthenticated = derived(auth, ($a) => $a.user !== null);
export const currentUser = derived(auth, ($a) => $a.user);
export const authLoading = derived(auth, ($a) => $a.loading);
