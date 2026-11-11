// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

// v0.2.106: is the launcher window something the user can see right now?
//
// Field report (a Windows user, 20 logical cores): the launcher's WebView2
// process sat at ~17% of total CPU while the launcher was idle. Two causes,
// both fixed with this module as the one home of the answer:
//
//  * WebView2 never learns that its window was MINIMISED. wry (the Tauri
//    webview layer) calls `ICoreWebView2Controller::SetIsVisible(false)` only
//    when the window is hidden, not on minimise, so `document.visibilityState`
//    stays "visible" and every CSS animation keeps rendering at full frame
//    rate. A hidden window (closed to the tray) does report "hidden".
//  * Polling timers (`setInterval` every 1-30 s) kept firing whatever the
//    window state.
//
// The launcher is ACTIVE when the document is visible AND the Tauri window is
// not minimised. While inactive:
//  * `<html data-window-inactive>` is set, and app.css pauses every CSS
//    animation under that attribute;
//  * timers started with `pollWhileActive` skip their ticks, and run ONCE as
//    soon as the window is active again (the user never sees stale data).

import { readable, type Readable } from 'svelte/store';
import { tauriAvailable } from './tauri';

/** The attribute on `<html>` that app.css keys the animation pause on. */
export const INACTIVE_ATTR = 'data-window-inactive';

type Listener = (active: boolean) => void;

/**
 * The pure state machine: two inputs (document hidden, window minimised), one
 * output (active). Exported for tests; the app uses the singleton below.
 */
export function createWindowActivity() {
  let hidden = false;
  let minimized = false;
  const listeners = new Set<Listener>();
  const active = () => !hidden && !minimized;

  function update(next: { hidden?: boolean; minimized?: boolean }): void {
    const before = active();
    if (next.hidden !== undefined) hidden = next.hidden;
    if (next.minimized !== undefined) minimized = next.minimized;
    const after = active();
    if (after !== before) for (const fn of [...listeners]) fn(after);
  }

  function subscribe(fn: Listener): () => void {
    listeners.add(fn);
    return () => listeners.delete(fn);
  }

  /**
   * `setInterval(fn, ms)` that does nothing while the window is inactive. A
   * tick skipped while inactive is made up by ONE call when the window
   * becomes active again. Returns the teardown.
   */
  function pollWhileActive(fn: () => unknown, ms: number): () => void {
    let missed = false;
    const run = () => {
      try {
        const r = fn();
        if (r && typeof (r as Promise<unknown>).catch === 'function') {
          (r as Promise<unknown>).catch(() => {});
        }
      } catch {
        // A poll failure must not stop the timer; callers log their own.
      }
    };
    const handle = setInterval(() => {
      if (active()) run();
      else missed = true;
    }, ms);
    const unsub = subscribe((isActive) => {
      if (isActive && missed) {
        missed = false;
        run();
      }
    });
    return () => {
      clearInterval(handle);
      unsub();
    };
  }

  return { isActive: active, update, subscribe, pollWhileActive };
}

const activity = createWindowActivity();

export const isWindowActive = activity.isActive;
export const pollWhileActive = activity.pollWhileActive;
export const onWindowActivityChange = activity.subscribe;
/** Feed one input change (the listeners below; tests drive it directly). */
export const setWindowState = activity.update;

/** Store form, for components that want `$windowActive`. */
export const windowActive: Readable<boolean> = readable(activity.isActive(), (set) => {
  set(activity.isActive());
  return activity.subscribe(set);
});

/** Reflect the state on `<html>` for the CSS animation pause. */
export function applyInactiveAttr(root: Element, active: boolean): void {
  if (active) root.removeAttribute(INACTIVE_ATTR);
  else root.setAttribute(INACTIVE_ATTR, '');
}

let started = false;

/**
 * Wire the real inputs: `visibilitychange` and, in Tauri, the window's
 * resize events (minimise / restore arrive as resizes) read through
 * `isMinimized()`. Idempotent; call once from the root layout.
 */
export async function startWindowActivity(): Promise<void> {
  if (started || typeof document === 'undefined') return;
  started = true;
  const root = document.documentElement;
  activity.subscribe((active) => applyInactiveAttr(root, active));
  const readHidden = () => setWindowState({ hidden: document.visibilityState === 'hidden' });
  document.addEventListener('visibilitychange', readHidden);
  readHidden();
  applyInactiveAttr(root, activity.isActive());
  if (!tauriAvailable()) return;
  try {
    const { getCurrentWindow } = await import('@tauri-apps/api/window');
    const win = getCurrentWindow();
    const readMinimized = async () => {
      try {
        setWindowState({ minimized: await win.isMinimized() });
      } catch {
        // Unknown → leave the last known state (never pause on a guess).
      }
    };
    await win.onResized(() => void readMinimized());
    await win.onFocusChanged(() => void readMinimized());
    await readMinimized();
  } catch (e) {
    console.warn('[window-activity] window events unavailable:', e);
  }
}
