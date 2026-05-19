---
title: Motion Principles for UI
type: concept
tags:
- design
- motion
- animation
- UX
- accessibility
- mid-level-architecture
- micro-interactions
created: 2026-05-19T00:00:00Z
updated: 2026-05-19T00:00:00Z
valid_from: 2026-05-19T00:00:00Z
valid_until: null
status: active
---

# Motion Principles for UI

Motion in UI is not decoration. It's a communication channel that tells the user where they are, where they came from, and where they're going. Done well, motion makes a UI feel alive and responsive. Done badly, it slows users down, triggers vestibular disorders, and screams "designer self-indulgence."

This concept adapts the Disney 12 principles of animation (Thomas & Johnston, 1981) to UI, then adds the constraints specific to interactive software.

## The role of motion in UI

Motion answers four user questions:

1. **Where did this thing come from?** — animate elements into view from their semantic origin (a modal "comes from" the button that opened it; a notification "comes from" the edge of the screen).
2. **Where did this thing go?** — exit animations track the inverse, so the user can mentally locate the dismissed element if they want it back.
3. **What's happening right now?** — loading states, progress, in-flight transitions reduce uncertainty.
4. **What just changed?** — when state updates, draw the eye to the change (highlight pulses, slide-ins, gentle color shifts).

If a motion doesn't answer one of these, it's decoration. Decoration is sometimes okay (delight, brand personality), but it must defer to the four above when they conflict.

## Adapted Disney principles

The original 12 principles were written for character animation. They translate to UI with significant modification.

### 1. Squash and stretch → spring physics
Rigid linear motion looks robotic. Springs (overshoot slightly, settle) feel alive. Modern UI tooling (React Spring, Framer Motion, CSS @starting-style + transitions) supports spring config natively. A subtle spring on a card lift or button press costs nothing and reads as quality.

Caveat: never spring text-heavy elements that the user is trying to read. Spring containers, not content.

### 2. Anticipation → state-of-readiness
Buttons that show a hover-state aren't decoration; they're anticipation. The user understands "this is clickable" before clicking. Loading buttons that compress slightly under the cursor anticipate the press.

### 3. Staging → focal hierarchy
One thing moves at a time, or movements are choreographed to direct the eye. If 5 things animate in parallel, the user sees noise. Stagger entries by 30-50ms; orchestrate exits.

### 4. Straight-ahead vs pose-to-pose → keyframe vs interpolation
Modern motion tooling interpolates between keyframes. Designers think pose-to-pose: define the resting and active states; let the runtime tween. Don't hand-author straight-ahead motion in UI — it overfits.

### 5. Follow-through and overlapping action → physical realism
A dragged card's shadow lingers slightly after the card settles. A list re-order causes adjacent items to ripple slightly. These overlapping micro-motions make digital objects feel physical.

### 6. Slow in / slow out → easing curves
Linear easing looks mechanical. Real motion accelerates and decelerates. UI conventions:

- **Ease-out** (`cubic-bezier(0.0, 0.0, 0.2, 1)`) — element entering view; decelerates as it settles. Default for "in" motions.
- **Ease-in** (`cubic-bezier(0.4, 0.0, 1.0, 1.0)`) — element leaving; accelerates as it disappears.
- **Standard / ease-in-out** — both ends, for in-place transforms.
- **Sharp** — quick, attention-grabbing (small alerts).

Material Design's motion specification documents these as named tokens — recommend tokenizing easings in the design system.

### 7. Arcs → curved paths
Linear path = mechanical. Slight curved path = organic. Most UI motion is linear-translate; curved-translate is rare but powerful (e.g. an icon that flies from a source to a destination in an arc, not a straight line).

### 8. Secondary action → micro-feedback
Pressing a button: primary action is "navigate"; secondary actions might be "ripple from press point" and "slight color flash." The secondary actions are 100-200ms; the primary action runs in parallel.

### 9. Timing → durations
- **<100ms** — instantaneous, no animation needed
- **100-200ms** — micro-interactions (button press, hover state, toggle)
- **200-400ms** — small transitions (modal open, accordion expand)
- **400-600ms** — larger transitions (page transitions, sheet slide-in)
- **>600ms** — too long for UI unless intentional (onboarding moment, celebration)

Mobile is often slightly slower than desktop because finger interactions have more lag tolerance. Power-user keyboard interactions should be faster (100-200ms).

### 10. Exaggeration → restraint (inverted)
For characters, exaggeration is signature. For UI, exaggeration is fatiguing. UI motion is closer to "barely perceptible polish" than "expressive personality." Exaggerate only at brand-personality moments (onboarding hero, completion celebration).

### 11. Solid drawing → solid composition
Doesn't translate directly. The UI equivalent: motion respects the layout. Elements don't fly across the grid; they translate along its axes. Off-grid motion feels chaotic.

### 12. Appeal → coherent personality
The motion language across the app is consistent. The same modal-open easing and duration everywhere. Inconsistent motion timing feels like multiple designers — because it usually was.

## Accessibility: `prefers-reduced-motion`

A non-negligible fraction of users (~3-5%, including those with vestibular disorders) experience nausea, dizziness, or migraine triggers from motion. Browsers expose the `prefers-reduced-motion` media query to surface this preference.

**Mandatory**: every motion in the design system must have a reduced-motion variant.

```css
@media (prefers-reduced-motion: reduce) {
  *, ::before, ::after {
    animation-duration: 0.01ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: 0.01ms !important;
    scroll-behavior: auto !important;
  }
}
```

The above is a heavy-handed reset. Better: per-component, design a reduced-motion variant that:
- Skips parallax / large-translate motions entirely
- Replaces movement with opacity fades (less vestibular impact)
- Maintains *functional* motion (loading spinners) but removes *decorative* motion (entry animations)

WCAG 2.2 ([w3.org/TR/WCAG22](https://www.w3.org/TR/WCAG22/)) success criterion 2.3.3 (Animation from Interactions, AAA) requires motion that's triggered by user interaction can be disabled — `prefers-reduced-motion` honoring satisfies this.

## Performance budget

Motion that drops below 60fps feels worse than no motion. Budget:

- **Transform and opacity only** for animated properties — the browser GPU-accelerates these. Animating `width`, `height`, `top`, `left`, `padding`, `margin` triggers layout and falls off 60fps.
- **`will-change`** sparingly — promotes layers but eats memory; remove after animation completes.
- **Composite-only** animations (transform, opacity, filter) at 60fps even on 5-year-old phones; layout-triggering animations stutter.

If a motion can't be done in transform+opacity, reconsider whether it's necessary.

## Signature motions (where motion earns its keep)

A brand-system motion has 3-5 signature transitions that distinguish it:

- **App-open / route-transition** — defines the spatial relationship between sections
- **Modal / sheet open** — defines the depth model (does the background dim? does the modal slide from an edge or fade in?)
- **List re-order** — when items move, do they spring? do siblings respond?
- **Confirmation / success** — the celebratory moment (subtle check + brief scale, not a fireworks explosion)
- **Error / shake** — communicates "this didn't work" without text

Document these in the design system with named easings and durations. See [[Design Tokens Architecture]] — motion can be tokenized too (DTCG `duration` and `cubicBezier` types).

## Tooling

- **CSS transitions + animations** — for simple cases. Tokenize the durations and easings.
- **Framer Motion** (React) — declarative API, supports spring physics, gesture-driven motion. Industry default 2024+.
- **React Spring** — physics-based, more control over springs.
- **GSAP** — heavyweight, scriptable, complex timelines (marketing sites, presentations).
- **Rive** / **Lottie** — pre-authored, designer-driven animations exported from Rive Editor or After Effects.
- **CSS `@starting-style`** — declarative entry animations, no JS needed (modern browsers 2024+).

## Anti-patterns

- **Motion as decoration** — animations that don't answer "where from / where to / what's happening / what changed."
- **Linear easing** — looks mechanical; use ease-out for entries, ease-in for exits.
- **No reduced-motion support** — actively harms vestibular users; WCAG violation.
- **Animating layout properties** (`width`, `top`) — stutters; use `transform` instead.
- **Long durations** (>500ms for everyday transitions) — power users will hate you.
- **5 things animating in parallel** — visual noise; stage and stagger instead.
- **Inconsistent timing across the app** — feels like motion was designed by 5 different people. Tokenize durations.
- **Motion for the demo video, not the user** — flashy reveals that look great in screenshots but slow daily use.

## Relations

[[implements::Disney 12 Principles]]
[[implements::WCAG 2.2 Animation Guidance]]
[[relatedTo::Design Tokens Architecture]]
[[relatedTo::Information Density Heuristics]]
[[uses::Framer Motion]]
[[uses::CSS Transitions]]

## References

- WCAG 2.2 (W3C): https://www.w3.org/TR/WCAG22/ — success criteria 2.3.3 (animation from interactions), 2.2.2 (pause, stop, hide)
- Thomas & Johnston, *The Illusion of Life: Disney Animation* (1981) — original 12 principles
- Material 3 motion guidelines — duration / easing tokens
- IBM Carbon motion guidelines — enterprise-focused motion specification
- Smashing Magazine articles on `prefers-reduced-motion` and accessibility
