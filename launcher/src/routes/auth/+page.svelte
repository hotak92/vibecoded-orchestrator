<script lang="ts">
  import { auth } from '$lib/stores/auth';
  import { goto } from '$app/navigation';

  let mode = $state<'login' | 'register'>('login');
  let email = $state('');
  let password = $state('');
  let name = $state('');
  let loading = $state(false);
  let error = $state<string | null>(null);
  let confirmEmail = $state(false);

  async function handleSubmit() {
    error = null;
    confirmEmail = false;
    loading = true;

    if (mode === 'login') {
      const success = await auth.login(email, password);
      loading = false;
      if (success) {
        goto('/');
      } else {
        auth.subscribe((s) => { error = s.error; })();
      }
    } else {
      const result = await auth.register(name, email, password);
      loading = false;
      if (result === 'confirm_email') {
        confirmEmail = true;
      } else if (result === true) {
        goto('/');
      } else {
        auth.subscribe((s) => { error = s.error; })();
      }
    }
  }

  function switchMode() {
    mode = mode === 'login' ? 'register' : 'login';
    error = null;
    confirmEmail = false;
  }
</script>

<!-- Aurora background -->
<div class="auth-wrapper">
  <div class="aurora-orb aurora-orb-1"></div>
  <div class="aurora-orb aurora-orb-2"></div>
  <div class="aurora-orb aurora-orb-3"></div>

  <div class="auth-container">
    <!-- Logo -->
    <div class="auth-logo">
      <div class="logo-icon">
        <span>V</span>
      </div>
      <h1 class="logo-text">VCT Launcher</h1>
      <p class="logo-subtitle">Your VibeCoded Tools hub</p>
    </div>

    <!-- Auth Card -->
    <div class="auth-card">
      <div class="auth-card-header">
        <h2>{mode === 'login' ? 'Welcome back' : 'Create account'}</h2>
        <p>{mode === 'login' ? 'Sign in to access your tools' : 'Get started with VibeCoded Tools'}</p>
      </div>

      <form onsubmit={(e) => { e.preventDefault(); handleSubmit(); }}>
        {#if mode === 'register'}
          <div class="form-group">
            <label for="name">Name</label>
            <input
              id="name"
              type="text"
              bind:value={name}
              placeholder="Your name"
              required
            />
          </div>
        {/if}

        <div class="form-group">
          <label for="email">Email</label>
          <input
            id="email"
            type="email"
            bind:value={email}
            placeholder="you@example.com"
            required
          />
        </div>

        <div class="form-group">
          <label for="password">Password</label>
          <input
            id="password"
            type="password"
            bind:value={password}
            placeholder="Enter password"
            required
            minlength="6"
          />
        </div>

        {#if error}
          <div class="auth-error">{error}</div>
        {/if}

        {#if confirmEmail}
          <div class="auth-success">
            Account created! Check your email to confirm, then sign in.
          </div>
        {/if}

        <button type="submit" class="btn-3d btn-3d-primary auth-submit" disabled={loading || confirmEmail}>
          {#if loading}
            <span class="spinner"></span>
          {:else}
            {mode === 'login' ? 'Sign In' : 'Create Account'}
          {/if}
        </button>
      </form>

      <div class="auth-switch">
        <span>{mode === 'login' ? "Don't have an account?" : 'Already have an account?'}</span>
        <button class="btn-3d btn-3d-ghost" onclick={switchMode}>
          {mode === 'login' ? 'Sign Up' : 'Sign In'}
        </button>
      </div>

    </div>
  </div>
</div>

<style>
  .auth-wrapper {
    position: relative;
    width: 100%;
    height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
    overflow: hidden;
    background: var(--color-bg);
  }

  /* Aurora orbs */
  .aurora-orb {
    position: absolute;
    border-radius: 50%;
    filter: blur(100px);
    opacity: 0.3;
    animation: float 8s ease-in-out infinite;
    pointer-events: none;
  }

  .aurora-orb-1 {
    width: 500px;
    height: 500px;
    background: var(--color-teal);
    top: -150px;
    left: -100px;
    animation-delay: 0s;
  }

  .aurora-orb-2 {
    width: 400px;
    height: 400px;
    background: var(--color-purple);
    bottom: -100px;
    right: -100px;
    animation-delay: -3s;
  }

  .aurora-orb-3 {
    width: 300px;
    height: 300px;
    background: var(--color-pink);
    top: 50%;
    left: 50%;
    transform: translate(-50%, -50%);
    opacity: 0.15;
    animation-delay: -5s;
  }

  @keyframes float {
    0%, 100% { transform: translate(0, 0) scale(1); }
    33% { transform: translate(30px, -20px) scale(1.05); }
    66% { transform: translate(-20px, 20px) scale(0.95); }
  }

  .aurora-orb-3 {
    animation-name: float-center;
  }

  @keyframes float-center {
    0%, 100% { transform: translate(-50%, -50%) scale(1); }
    33% { transform: translate(-45%, -55%) scale(1.1); }
    66% { transform: translate(-55%, -45%) scale(0.9); }
  }

  .auth-container {
    position: relative;
    z-index: 1;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 32px;
    width: 100%;
    max-width: 420px;
    padding: 24px;
  }

  /* Logo */
  .auth-logo {
    text-align: center;
  }

  .logo-icon {
    width: 56px;
    height: 56px;
    border-radius: 16px;
    display: flex;
    align-items: center;
    justify-content: center;
    background: linear-gradient(135deg, var(--color-teal), var(--color-purple));
    margin: 0 auto 16px;
    box-shadow:
      0 8px 32px rgba(0, 191, 166, 0.3),
      0 0 0 1px rgba(255, 255, 255, 0.1);
  }

  .logo-icon span {
    color: var(--color-bg);
    font-weight: 900;
    font-size: 22px;
  }

  .logo-text {
    font-size: 24px;
    font-weight: 800;
    color: var(--color-text);
    letter-spacing: -0.5px;
  }

  .logo-subtitle {
    font-size: 14px;
    color: var(--color-mid);
    margin-top: 4px;
  }

  /* Auth Card - Glassmorphism */
  .auth-card {
    width: 100%;
    background: rgba(255, 255, 255, 0.04);
    border: 1px solid rgba(255, 255, 255, 0.08);
    border-radius: 20px;
    padding: 32px;
    backdrop-filter: blur(24px);
    box-shadow:
      0 20px 60px rgba(0, 0, 0, 0.4),
      inset 0 1px 0 rgba(255, 255, 255, 0.06);
  }

  .auth-card-header {
    margin-bottom: 28px;
  }

  .auth-card-header h2 {
    font-size: 20px;
    font-weight: 700;
    color: var(--color-text);
    margin-bottom: 6px;
  }

  .auth-card-header p {
    font-size: 13px;
    color: var(--color-mid);
  }

  /* Form */
  .form-group {
    margin-bottom: 18px;
  }

  .form-group label {
    display: block;
    font-size: 12px;
    font-weight: 600;
    color: var(--color-mid);
    margin-bottom: 6px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }

  .form-group input {
    width: 100%;
    padding: 12px 16px;
    background: rgba(255, 255, 255, 0.06);
    border: 1px solid rgba(255, 255, 255, 0.1);
    border-radius: 12px;
    color: var(--color-text);
    font-size: 14px;
    font-family: inherit;
    outline: none;
    transition: all 0.25s ease;
  }

  .form-group input::placeholder {
    color: var(--color-muted);
  }

  .form-group input:focus {
    border-color: rgba(0, 191, 166, 0.5);
    box-shadow: 0 0 0 3px rgba(0, 191, 166, 0.1);
  }

  .auth-error {
    background: rgba(255, 79, 160, 0.1);
    border: 1px solid rgba(255, 79, 160, 0.25);
    border-radius: 10px;
    padding: 10px 14px;
    font-size: 13px;
    color: var(--color-pink);
    margin-bottom: 18px;
  }

  .auth-success {
    background: rgba(0, 191, 166, 0.1);
    border: 1px solid rgba(0, 191, 166, 0.25);
    border-radius: 10px;
    padding: 10px 14px;
    font-size: 13px;
    color: var(--color-teal);
    margin-bottom: 18px;
  }

  .auth-submit {
    width: 100%;
    margin-top: 6px;
  }

  .auth-switch {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 8px;
    margin-top: 20px;
    font-size: 13px;
    color: var(--color-mid);
  }

  /* Spinner */
  .spinner {
    display: inline-block;
    width: 18px;
    height: 18px;
    border: 2px solid rgba(5, 11, 31, 0.3);
    border-top-color: var(--color-bg);
    border-radius: 50%;
    animation: spin 0.6s linear infinite;
  }

  @keyframes spin {
    to { transform: rotate(360deg); }
  }
</style>
