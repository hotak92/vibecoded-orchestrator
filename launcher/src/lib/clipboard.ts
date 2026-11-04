// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 VibeCoded Tools

// The ONE clipboard write for the launcher.
//
// v0.2.105 (L-SMALL): the notification bell, the deferral-ledger rows and
// anything else that offers a Copy button go through this helper, so the
// success/failure wording and the "clipboard unavailable" handling cannot
// drift between surfaces. (Before this, `NotificationBell.svelte` carried its
// own inline copy routine.)
//
// Returns `true` when the text reached the clipboard, `false` when it did not
// — the caller does not need to know why, but the user always hears about it
// through the toast.

import { toast } from '$lib/stores/toast';

/** Copy `text` to the system clipboard, toasting the outcome. */
export async function copyToClipboard(text: string): Promise<boolean> {
  try {
    // The desktop/webview clipboard is a user-agent feature; a missing one
    // is a real failure to copy, not a crash.
    if (typeof navigator === 'undefined' || !navigator.clipboard) {
      throw new Error('clipboard unavailable');
    }
    await navigator.clipboard.writeText(text);
    toast.success('Copied to clipboard');
    return true;
  } catch {
    toast.error('Copy failed');
    return false;
  }
}