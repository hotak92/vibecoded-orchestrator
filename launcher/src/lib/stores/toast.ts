// Tiny toast notification store. Auto-dismiss after 4 s.

import { writable } from 'svelte/store';

export type ToastKind = 'info' | 'success' | 'error';

export interface ToastItem {
  id: number;
  kind: ToastKind;
  message: string;
}

const items = writable<ToastItem[]>([]);
let nextId = 1;

function push(kind: ToastKind, message: unknown) {
  const id = nextId++;
  const text =
    typeof message === 'string'
      ? message
      : message instanceof Error
        ? message.message
        : String(message ?? 'unknown error');
  items.update((list) => [...list, { id, kind, message: text }]);
  setTimeout(() => {
    items.update((list) => list.filter((t) => t.id !== id));
  }, 4000);
}

export const toast = {
  subscribe: items.subscribe,
  info: (m: unknown) => push('info', m),
  success: (m: unknown) => push('success', m),
  error: (m: unknown) => push('error', m),
  dismiss(id: number) {
    items.update((list) => list.filter((t) => t.id !== id));
  },
};
