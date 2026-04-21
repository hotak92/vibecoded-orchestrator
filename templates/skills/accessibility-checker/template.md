# Accessibility Review Template

## Component/Page: [Name]

**Review Date**: [YYYY-MM-DD]
**WCAG Level Target**: [A / AA / AAA]
**Reviewer**: [Name]

---

## Issues Found

### Critical (Must Fix)

#### Issue 1: [Title]
- **WCAG Criterion**: [X.X.X - Criterion Name]
- **Severity**: Critical
- **Location**: [File/Component:Line or Selector]
- **Description**: [What's wrong]
- **Impact**: [How this affects users]
- **Current Code**:
  ```html
  [Problematic code]
  ```
- **Fix**:
  ```html
  [Corrected code]
  ```
- **Test**: [How to verify fix]

---

### Major (Should Fix)

#### Issue 2: [Title]
- **WCAG Criterion**: [X.X.X - Criterion Name]
- **Severity**: Major
- **Location**: [File/Component:Line]
- **Description**: [What's wrong]
- **Impact**: [How this affects users]
- **Fix**: [Specific recommendation]

---

### Minor (Nice to Have)

#### Issue 3: [Title]
- **WCAG Criterion**: [X.X.X - Criterion Name]
- **Severity**: Minor
- **Location**: [File/Component:Line]
- **Description**: [What's wrong]
- **Fix**: [Specific recommendation]

---

## Recommendations

### Quick Wins (Low effort, high impact)
1. [Recommendation 1]
2. [Recommendation 2]
3. [Recommendation 3]

### Systematic Improvements
1. [Pattern to apply across codebase]
2. [Component library update needed]
3. [Style guide addition]

---

## Testing Checklist

### Automated Testing
- [ ] Run axe DevTools browser extension
- [ ] Run Lighthouse accessibility audit
- [ ] Run WAVE browser extension
- [ ] Verify color contrast with checker tool

### Manual Testing
- [ ] Keyboard navigation (Tab, Shift+Tab, Enter, Space, Esc)
- [ ] Screen reader (NVDA/JAWS on Windows, VoiceOver on Mac)
- [ ] Zoom to 200% (no horizontal scroll, content reflows)
- [ ] Disable CSS (content still understandable)
- [ ] Check focus indicators visible
- [ ] Verify skip links work

### Real User Testing
- [ ] Test with screen reader user
- [ ] Test with keyboard-only user
- [ ] Test with user with low vision

---

## WCAG 2.1 Compliance Summary

| Level | Pass | Fail | N/A | Total |
|-------|------|------|-----|-------|
| A     | X    | Y    | Z   | Total |
| AA    | X    | Y    | Z   | Total |
| AAA   | X    | Y    | Z   | Total |

**Overall Status**: ❌ Fails WCAG 2.1 [A/AA/AAA] / ✅ Passes WCAG 2.1 [A/AA/AAA]

---

## Priority Action Items

### Week 1 (Critical fixes)
- [ ] [Action item 1]
- [ ] [Action item 2]

### Week 2-3 (Major improvements)
- [ ] [Action item 3]
- [ ] [Action item 4]

### Ongoing (Minor enhancements)
- [ ] [Action item 5]
- [ ] [Action item 6]

---

## Resources

**Tools Used**:
- [axe DevTools](https://www.deque.com/axe/devtools/)
- [WAVE](https://wave.webaim.org/)
- [WebAIM Contrast Checker](https://webaim.org/resources/contrastchecker/)

**Reference**:
- [WCAG 2.1 Guidelines](https://www.w3.org/WAI/WCAG21/quickref/)
- [ARIA Authoring Practices](https://www.w3.org/WAI/ARIA/apg/)

---

## Sign-Off

**Reviewed by**: [Name]
**Approved by**: [Name]
**Date**: [YYYY-MM-DD]
