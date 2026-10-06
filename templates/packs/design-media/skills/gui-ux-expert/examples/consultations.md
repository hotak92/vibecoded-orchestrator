# Example Consultations

Worked examples showing the consultation format applied end to end. Use them as templates for structuring a response; adapt the specifics to the user's actual context.

## Example 1: Form Layout
**Q**: "How should I layout a data entry form with 15 fields (name, email, phone, address, preferences, etc.)?"

**A**:
## Layout Recommendation: Data Entry Form

**Context**: 15 fields, mix of required/optional, user needs to complete efficiently.

**Recommendation: Progressive Disclosure with Accordion Sections**

**Structure**:
1. **Essential Info** (always visible): Name, Email, Phone (required)
2. **Address Details** (accordion): Street, City, State, Zip
3. **Preferences** (accordion): 6 preference fields (optional)

**Layout**:
- Single column on mobile (full width)
- Two columns on desktop (600px+) for short fields (City/State, First/Last name)
- Keep related fields together (all address fields in one accordion)

**Rationale**:
- Reduces cognitive load (users see 3 fields initially, not 15)
- Progressive disclosure reveals complexity gradually (Principle 7)
- Groups related information (Principle 2)

**Accessibility**:
- `<fieldset>` for each section with `<legend>`
- Label every field (use `<label for="id">`)
- Required fields: asterisk + aria-required="true"
- Inline validation on blur (show errors immediately)
- Accordion: keyboard navigable (Enter/Space to toggle)

**Visual Hierarchy**:
- Required sections expanded by default
- Required fields marked with asterisk (red *)
- Submit button: primary style, bottom-right
- Save draft: secondary style, next to submit

**Spacing**:
- 24px between accordion sections
- 16px between fields within section
- 12px between label and input

**Alternative: Wizard/Stepper (if fields highly sequential)**
- Use when: Information collected in natural order (Step 1: Personal, Step 2: Address, Step 3: Preferences)
- Tradeoffs: More clicks, but clearer progress
- Good for onboarding, less ideal for editing

---

## Example 2: Cluttered Dashboard
**Q**: "My dashboard feels cluttered with 10 widgets showing different metrics. How can I improve it?"

**A**:
## Design Review: Dashboard Optimization

**Context**: 10 widgets, likely information overload, unclear hierarchy.

**Issues Identified**:
1. **Lack of visual hierarchy**: All widgets equal weight (users can't prioritize)
2. **Cognitive overload**: Too much information at once
3. **No grouping**: Related metrics not visually connected

**Recommendation: Tiered Hierarchy with Card Pattern**

**Layout**:
```
┌─────────────────────────────────────────┐
│  Primary Metric (Large Card)           │
│  [Revenue: $XX,XXX]                     │
└─────────────────────────────────────────┘

┌──────────────┐ ┌──────────────┐ ┌──────────────┐
│ Metric 2     │ │ Metric 3     │ │ Metric 4     │
│ [Value]      │ │ [Value]      │ │ [Value]      │
└──────────────┘ └──────────────┘ └──────────────┘

┌─────────────────────────────────────────┐
│ Tab Navigation                          │
│ [Details] [Analytics] [Reports]         │
│                                         │
│ (6 secondary widgets in selected tab)  │
└─────────────────────────────────────────┘
```

**Strategy**:
1. **Identify primary metric** (most important KPI) → Large card, top position
2. **Group 3-4 secondary metrics** → Medium cards, below primary
3. **Move 6 less-frequent metrics** → Tabs (Details, Analytics, etc.)

**Visual Improvements**:
- **Card spacing**: 24px gaps between cards (breathing room)
- **Color coding**: Green for positive trends, red for negative (with icons, not color alone)
- **Typography**: Large numbers (32px+) for metrics, small labels (14px)
- **Whitespace**: 24px padding inside cards

**User Customization** (optional):
- Allow users to pin primary metric (personal preference)
- Reorder secondary metrics (drag-and-drop)
- Show/hide widgets based on role

**Accessibility**:
- Logical reading order (primary → secondary → tabs)
- Tabs: Arrow keys navigate, Enter activates
- Data updates announced to screen readers (aria-live="polite")

**Alternative: Dashboard Templates**
- Provide 2-3 preset layouts (Overview, Sales, Operations)
- Users switch templates based on task
- Tradeoff: Less customization, faster to set up

---

## Example 3: Accessibility Compliance
**Q**: "Need to make my login form WCAG 2.1 AA compliant. What do I need?"

**A**:
## Accessibility Audit: Login Form

**WCAG 2.1 AA Requirements**:

### 1.4.3 Contrast (Minimum)
- [ ] **Text contrast**: 4.5:1 for normal text, 3:1 for large (18pt+)
  - Check: Input labels, placeholder text, error messages
  - Fix: Use WebAIM Contrast Checker, darken text or lighten background
  - Example: #666 on #fff fails (3:1), use #595959 or darker

- [ ] **Button contrast**: Text + background both 4.5:1
  - Primary button: High contrast (white text on blue/green)
  - Disabled button: 3:1 minimum (convey state with text too)

### 2.1.1 Keyboard
- [ ] **Tab navigation**: Email → Password → Checkbox → Submit
  - All interactive elements in logical order
  - Skip links if header/footer present

- [ ] **Enter submits form**: Default browser behavior
  - Don't override with custom JavaScript unless equivalent

- [ ] **Checkbox toggle**: Space key toggles "Remember me"
  - Native `<input type="checkbox">` handles this

### 2.4.6 Headings and Labels
- [ ] **Form heading**: `<h1>` or `<h2>` "Sign In" or "Login"
- [ ] **Input labels**: `<label for="email">Email</label>`
  - Never rely on placeholder alone
  - Visible labels required

### 3.3.1 Error Identification
- [ ] **Error messages**: Specific, actionable
  - Bad: "Invalid input"
  - Good: "Email format incorrect. Example: user@example.com"

- [ ] **Error indication**: Text + icon (not color alone)
  - Red border + error text + ⚠️ icon

- [ ] **Error announcement**: `aria-live="assertive"` or `role="alert"`
  - Screen readers announce errors immediately

### 4.1.3 Status Messages
- [ ] **Success feedback**: "Login successful. Redirecting..."
  - `role="status"` or `aria-live="polite"`

**Complete Accessible Login Form Checklist**:

```html
<form aria-labelledby="login-heading">
  <h1 id="login-heading">Sign In</h1>

  <!-- Email -->
  <label for="email">Email <span aria-label="required">*</span></label>
  <input
    type="email"
    id="email"
    name="email"
    required
    aria-required="true"
    aria-describedby="email-error"
  />
  <span id="email-error" role="alert" aria-live="assertive">
    <!-- Error text if validation fails -->
  </span>

  <!-- Password -->
  <label for="password">Password <span aria-label="required">*</span></label>
  <input
    type="password"
    id="password"
    name="password"
    required
    aria-required="true"
    aria-describedby="password-error"
  />
  <span id="password-error" role="alert" aria-live="assertive"></span>

  <!-- Remember Me -->
  <label>
    <input type="checkbox" name="remember" />
    Remember me
  </label>

  <!-- Submit -->
  <button type="submit">Sign In</button>

  <!-- Status message container -->
  <div role="status" aria-live="polite" aria-atomic="true">
    <!-- Success/loading messages here -->
  </div>
</form>
```

**Visual Requirements**:
- Input height: 44px minimum (touch target)
- Focus indicator: 2px outline, high contrast (never remove with `outline: none`)
- Error color: Red with 4.5:1 contrast (#d32f2f or similar)
- Label font size: 16px minimum
- Spacing: 16px between inputs

**Testing**:
1. Keyboard only (unplug mouse): Can you complete login?
2. Screen reader (NVDA/JAWS): Are labels announced?
3. Contrast checker: All text passes 4.5:1?
4. Zoom to 200%: Layout still usable?

**Quick Wins**:
- Add visible labels (if using placeholder-only)
- Increase contrast on placeholder text (#999 → #666)
- Add focus indicators (default browser outline is fine)
- Connect labels with `for` attribute
