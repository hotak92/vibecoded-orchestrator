<!-- Counter component for the golden-fixture repo (Svelte, regex-parsed). -->
<!-- Exercises: module-context script, default script functions,
     export function, arrow-export const, reactive declaration, markup. -->

<script context="module">
  import { readable } from 'svelte/store';

  export function createStore(initial) {
    return readable(initial);
  }
</script>

<script>
  import { onMount } from 'svelte';

  let count = 0;

  function increment() {
    count += 1;
  }

  export function reset() {
    count = 0;
  }

  export const double = (n) => n * 2;

  $: doubled = count * 2;

  onMount(() => {
    count = 1;
  });
</script>

<button on:click={increment}>
  count is {count} (doubled {doubled})
</button>
