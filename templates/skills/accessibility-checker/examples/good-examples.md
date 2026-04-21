# Good Accessibility Examples

## Semantic HTML

```html
<!-- Good: Proper button element -->
<button onClick={handleClick}>Click me</button>

<!-- Good: Navigation with nav element -->
<nav aria-label="Main navigation">
  <ul>
    <li><a href="/">Home</a></li>
    <li><a href="/about">About</a></li>
  </ul>
</nav>
```

## ARIA Labels

```html
<!-- Good: Icon button with aria-label -->
<button aria-label="Close dialog">
  <IconX />
</button>

<!-- Good: Form with proper labels -->
<label htmlFor="email">Email Address</label>
<input type="email" id="email" name="email" required />
```

## Keyboard Navigation

```jsx
// Good: Keyboard-accessible interactive element
function InteractiveCard({ onClick }) {
  return (
    <div
      role="button"
      tabIndex={0}
      onClick={onClick}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          onClick();
        }
      }}
    >
      Interactive Content
    </div>
  );
}
```

## Alt Text

```html
<!-- Good: Descriptive alt text -->
<img
  src="chart.png"
  alt="Bar chart showing sales growth of 25% in Q4 2025"
/>

<!-- Good: Decorative image (empty alt) -->
<img src="decorative-divider.png" alt="" role="presentation" />
```

## Form Labels

```html
<!-- Good: Explicit label association -->
<label htmlFor="username">Username</label>
<input type="text" id="username" name="username" required />

<!-- Good: Error messages linked to input -->
<label htmlFor="password">Password</label>
<input
  type="password"
  id="password"
  name="password"
  aria-describedby="password-error"
  aria-invalid="true"
/>
<span id="password-error" role="alert">
  Password must be at least 8 characters
</span>
```

## Focus Styles

```css
/* Good: Clear focus indicator */
button:focus-visible {
  outline: 3px solid #0066cc;
  outline-offset: 2px;
}

/* Good: Skip link for keyboard users */
.skip-link {
  position: absolute;
  top: -40px;
  left: 0;
  background: #000;
  color: #fff;
  padding: 8px;
  text-decoration: none;
  z-index: 100;
}

.skip-link:focus {
  top: 0;
}
```

## Color Contrast (WCAG AA)

```css
/* Good: Sufficient contrast (4.5:1 for normal text) */
.text {
  color: #333333; /* Dark gray */
  background: #FFFFFF; /* White */
  /* Contrast ratio: 12.6:1 */
}

/* Good: Large text (3:1 minimum) */
.heading {
  font-size: 24px;
  font-weight: bold;
  color: #757575; /* Medium gray */
  background: #FFFFFF;
  /* Contrast ratio: 4.5:1 */
}
```

## ARIA Live Regions

```jsx
// Good: Announce dynamic changes to screen readers
function Notification({ message }) {
  return (
    <div role="status" aria-live="polite" aria-atomic="true">
      {message}
    </div>
  );
}

// Good: Urgent alerts
function ErrorAlert({ error }) {
  return (
    <div role="alert" aria-live="assertive">
      {error}
    </div>
  );
}
```

## Modal Dialogs

```jsx
// Good: Accessible modal with focus management
function Modal({ isOpen, onClose, children }) {
  const modalRef = useRef(null);

  useEffect(() => {
    if (isOpen && modalRef.current) {
      // Focus first focusable element
      const focusable = modalRef.current.querySelector('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])');
      focusable?.focus();
    }
  }, [isOpen]);

  if (!isOpen) return null;

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="modal-title"
      ref={modalRef}
    >
      <h2 id="modal-title">Modal Title</h2>
      <button onClick={onClose} aria-label="Close modal">
        <IconX />
      </button>
      {children}
    </div>
  );
}
```
