# Frontend UI & accessibility review topic

Lens for reviewing user-facing interfaces: visual correctness, interaction
states, and WCAG 2.1 AA accessibility. Findings carry severity, `file:line`,
and evidence (screenshot or code path).

## Interaction states

- Every async view has loading, empty, error, and success states — a view
  that only handles success is a finding.
- Forms: inline validation, disabled-while-submitting, server errors
  surfaced at the field or form level, no silent failure.
- Destructive actions have a confirmation step; irreversible ones say so.
- Optimistic updates have a rollback path when the request fails.

## Accessibility (WCAG 2.1 AA)

- Semantic HTML first: buttons are `<button>`, links are `<a>`, headings nest
  without skips; ARIA only to fill gaps semantics can't.
- Keyboard: every interactive element reachable and operable via keyboard;
  visible focus indicator; no keyboard traps.
- Focus management: modals trap focus while open and return it on close;
  route changes move focus sensibly.
- Contrast: text ≥ 4.5:1 (3:1 for large text); non-text UI elements ≥ 3:1.
- Images/icons: meaningful ones have alt text; decorative ones are marked
  decorative.
- Motion: no auto-playing animation over ~5s without a pause; respect
  reduced-motion preferences.
- Labels: every input has a programmatic label; error messages are announced
  (aria-describedby / role=alert class).

## Responsive & cross-device

- Verified at mobile / tablet / desktop widths; no horizontal scroll at
  common widths; touch targets ≥ 44px.
- Text zoom to 200% without loss of content or function.

## Consistency

- Spacing/typography/color come from the project's design tokens, not
  one-off values; sibling screens use the same patterns for the same
  interactions.

## Verification

Where the UI cannot be exercised (no browser available to the reviewer),
mark findings UNVERIFIED-BY-RUN and cite the code path instead. Name the
screens/components not checked.
