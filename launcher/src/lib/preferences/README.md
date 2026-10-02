# Preferences page loaders

`routes/preferences/+page.svelte` reads its data through ONE registry,
`loaders.ts` → `PREF_LOADERS`. The page keeps the state it renders and every
write (`set_*`, `app_state_set*`, action commands); the registry owns every
read.

- **Eager** entries (`eager: true`) run at mount. Their combined IPC count is
  budgeted at `PREF_EAGER_IPC_BUDGET` (8) and *measured* by `loaders.test.ts`
  against a counting mock — today: window prefs (1), embedding catalog +
  defaults (2), the two OpenAI-key event subscriptions (2) = 5.
- **Lazy** entries (`eager: false`) run the first time their section scrolls
  into view (`use:lazySection`, 200 px early), then only on an explicit reload
  (a button, a save, `loaders.refresh(key)`).
- **Slow** entries (`slow: true`: the services probe, the Ollama tags fetch)
  must never be eager — the test pins it.

## How to add a section: one registry entry

1. Add ONE entry to `PREF_LOADERS` in `loaders.ts`:

   ```ts
   mySection: {
     eager: false,                       // true only if it is at the top of the page
     section: 'My section',              // the heading it feeds
     load: () => invoke<MyData>('get_my_thing'),
   },
   ```

   Reads only; return the data. If an `app_state` key is also written by the
   page, export the key constant from `loaders.ts` and import it in the page —
   never declare it twice.

2. In the page, write the apply function (`async function loadMySection()`
   that awaits `PREF_LOADERS.mySection.load()` and sets `$state`), and add
   `mySection: loadMySection` to the `createPreferenceLoaders({...})` map. The
   map is typed `Record<PrefLoaderKey, …>`, so forgetting this is a type error
   (`npm run check`).

3. Put `use:lazySection={{ loaders, keys: ['mySection'] }}` on the section's
   `<section>` element. A child panel that does its own reads on mount is
   gated the same way: a registry entry whose handler flips
   `shownPanels.<key>`, and `{#if shownPanels.<key>}<Panel />{/if}`.

4. Run `npx --no-install vitest run src/lib/preferences` — the tests check the
   budget, that every lazy key is attached to a section, that `onMount` makes
   no direct IPC, that no read the registry owns is invoked from the page, and
   that every write the page invokes has its read in the registry (the
   `set_*`/`get_*` census: add your pair to `PAIRS` there).

Do not call `invoke(` from `onMount`; `onMount` only calls
`loaders.mountEager()`.
