# Bad Accessibility Examples (Anti-Patterns)

## Non-Semantic HTML

```html
<!-- Bad: div as button -->
<div onClick={handleClick}>Click me</div>

<!-- Bad: Generic containers for navigation -->
<div class="nav">
  <div><a href="/">Home</a></div>
  <div><a href="/about">About</a></div>
</div>
```

**Issues**:
- No keyboard accessibility (div doesn't receive focus)
- No semantic meaning for assistive technologies
- No default keyboard interaction (Enter/Space)

**Fix**: Use proper semantic elements (`<button>`, `<nav>`, `<main>`, etc.)

---

## Missing ARIA Labels

```html
<!-- Bad: Icon-only button without label -->
<button><IconX /></button>

<!-- Bad: Search input without label -->
<input type="search" placeholder="Search..." />
```

**Issues**:
- Screen readers announce "button" with no context
- Placeholder is not a substitute for label
- Users don't know what the button does

**Fix**: Add `aria-label` or visible labels

---

## Poor Keyboard Navigation

```jsx
// Bad: onClick on non-interactive element
<div onClick={handleClick}>
  Click me
</div>

// Bad: Missing keyboard handler
<span onClick={handleClick} role="button">
  Action
</span>
```

**Issues**:
- Not reachable via Tab key
- No keyboard event handling
- Doesn't work with assistive technologies

**Fix**: Use `<button>` or add `tabIndex={0}` + `onKeyDown`

---

## Missing or Poor Alt Text

```html
<!-- Bad: No alt text -->
<img src="chart.png" />

<!-- Bad: Redundant alt text -->
<img src="profile.jpg" alt="Image of profile picture" />

<!-- Bad: Filename as alt text -->
<img src="user-photo-2025.jpg" alt="user-photo-2025" />
```

**Issues**:
- Screen readers can't describe image
- "Image of" is redundant (screen reader says "image")
- Filename is not descriptive

**Fix**: Descriptive alt text or empty alt for decorative images

---

## Forms Without Labels

```html
<!-- Bad: Placeholder as label -->
<input type="text" placeholder="Email" />

<!-- Bad: Implicit label (not linked) -->
<label>
  Email
  <input type="text" />
</label>
<!-- While this works, explicit linking is better -->
```

**Issues**:
- Placeholder disappears when typing
- Not all screen readers announce placeholder
- Clicking label doesn't focus input (in first example)

**Fix**: Use explicit `<label for="id">` + `<input id="id">`

---

## Insufficient Color Contrast

```css
/* Bad: Low contrast (2.5:1 - fails WCAG AA) */
.text {
  color: #999999; /* Light gray */
  background: #FFFFFF; /* White */
}

/* Bad: Color as only indicator */
.error {
  color: red; /* Color-blind users can't distinguish */
}
```

**Issues**:
- Hard to read for users with low vision
- Fails WCAG 2.1 AA (requires 4.5:1)
- Color-only indication excludes color-blind users

**Fix**: Ensure 4.5:1 contrast (3:1 for large text), use icons + text

---

## No Focus Indicators

```css
/* Bad: Removing default focus outline */
button:focus {
  outline: none; /* Don't do this! */
}

/* Bad: Invisible focus */
a:focus {
  color: inherit;
  text-decoration: none;
}
```

**Issues**:
- Keyboard users can't see where they are
- Violates WCAG 2.1 AA (2.4.7 Focus Visible)

**Fix**: Use `:focus-visible` with clear indicator (3px outline)

---

## Inaccessible Modals

```jsx
// Bad: Modal without accessibility features
function BadModal({ isOpen, children }) {
  if (!isOpen) return null;

  return (
    <div className="modal">
      {children}
    </div>
  );
}
```

**Issues**:
- No `role="dialog"` or `aria-modal="true"`
- Focus not managed (stays on background)
- Can tab outside modal
- Screen reader doesn't announce modal

**Fix**: Add ARIA attributes, trap focus, announce to screen readers

---

## Auto-Playing Media

```html
<!-- Bad: Auto-play video with sound -->
<video src="promo.mp4" autoplay></video>

<!-- Bad: Auto-advancing carousel -->
<div class="carousel" data-autoplay="3000">
  <!-- Slides -->
</div>
```

**Issues**:
- Violates WCAG 2.1 AA (1.4.2 Audio Control)
- Distracts screen reader users
- No way to pause easily

**Fix**: No autoplay, or provide prominent pause button

---

## Complex Tables Without Structure

```html
<!-- Bad: Layout table used for data -->
<table>
  <tr>
    <td>Name</td>
    <td>John Doe</td>
  </tr>
  <tr>
    <td>Email</td>
    <td>john@example.com</td>
  </tr>
</table>
```

**Issues**:
- No `<th>` (header cells)
- No `scope` attribute
- Screen readers can't distinguish headers from data

**Fix**: Use `<th scope="row|col">` for headers, proper table structure

---

## Time-Limited Actions

```jsx
// Bad: Session timeout without warning
useEffect(() => {
  const timeout = setTimeout(() => {
    logoutUser(); // Sudden logout
  }, 600000); // 10 minutes
}, []);
```

**Issues**:
- Violates WCAG 2.1 AA (2.2.1 Timing Adjustable)
- Users with disabilities may need more time
- No warning before timeout

**Fix**: Warn before timeout (1-2 min), allow extension

---

## Missing Skip Links

```html
<!-- Bad: No way to skip navigation -->
<body>
  <nav>
    <!-- 30+ navigation links -->
  </nav>
  <main>
    <!-- Content -->
  </main>
</body>
```

**Issues**:
- Keyboard users must tab through all nav links
- Violates WCAG 2.1 AA (2.4.1 Bypass Blocks)

**Fix**: Add skip link to main content

```html
<a href="#main-content" class="skip-link">Skip to main content</a>
<nav>...</nav>
<main id="main-content">...</main>
```
