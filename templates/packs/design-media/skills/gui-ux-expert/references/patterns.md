# UI Pattern & Layout Reference

Catalog of reusable design patterns, layout strategies, and framework-specific guidance. Consult when recommending a component, a page layout, or framework-specific composition.

## Common Design Patterns

### Card Pattern
**Use when**: Grouping related information, scannable content
**Structure**: Header (title, icon), Body (content), Footer (actions)
**Spacing**: 16-24px padding, 16px gap between cards
**Accessibility**: Semantic headings, clickable card has accessible name

```
[Card Example]
┌─────────────────────┐
│ Title       [Icon]  │
│ Content summary     │
│ More details here   │
│ [Action]    [Info]  │
└─────────────────────┘
```

### Wizard/Stepper Pattern
**Use when**: Multi-step processes (3-7 steps), form workflows
**Structure**: Step indicator (top), Step content, Navigation (Back/Next/Submit)
**Best practices**:
- Show progress (Step 2 of 5)
- Allow backward navigation
- Validate per step before allowing Next
- Save progress if possible

### Tabs/Accordion Pattern
**Tabs**: 3-7 related sections, frequent switching expected
**Accordion**: Space-constrained, sequential reading likely

**Tabs best practices**:
- Active tab visually distinct (border, background, font weight)
- Keyboard: Arrow keys navigate, Tab enters content
- Don't hide critical info in non-default tabs

**Accordion best practices**:
- Icons indicate collapsed/expanded state
- Allow multiple open (unless mutually exclusive)
- Keyboard: Enter/Space toggles, Arrow keys navigate

### Modal Dialog Pattern
**Use when**: Critical decision, focus required, blocks other actions
**Structure**: Overlay (dims background), Dialog (max 600px wide), Focus trap
**Best practices**:
- Escape key closes modal
- Click outside closes (or show close button)
- Focus on first input or primary action on open
- Return focus to trigger element on close

**When NOT to use**: Non-critical information (use inline), complex workflows (use dedicated page)

### Empty State Pattern
**Use when**: No data to display (new user, filtered results, errors)
**Structure**: Icon/illustration, Heading, Explanation, Primary action
**Best practices**:
- Explain why empty ("No results match your filters")
- Suggest action ("Add your first item" button)
- Make helpful, not discouraging

### Loading State Pattern
**Options**:
- **Spinner**: Unknown duration, small area
- **Progress bar**: Known duration, percentage
- **Skeleton screen**: Preview layout, better perceived performance

**Best practices**:
- Show loading for operations >300ms
- Provide cancel option for long operations
- Show partial results if possible (progressive loading)

## Layout Strategies

### F-Pattern (Content-Heavy Pages)
Users scan left-to-right at top, then down left side.
- Most important content top-left
- Headings and key info along left edge
- Use for articles, documentation, text-heavy pages

### Z-Pattern (Landing Pages)
Eye movement: Top-left → Top-right → Diagonal → Bottom-left → Bottom-right
- Logo/brand top-left
- CTA top-right or bottom-right
- Use for marketing pages, simple conversions

### Grid Systems
**12-Column Grid** (most common):
- Desktop: 1-12 columns as needed
- Tablet: 6-8 columns
- Mobile: 4 columns or single column

**Gutter**: 16-24px between columns
**Margins**: 16-48px from edge (larger on desktop)

### Whitespace Usage
**Macro whitespace**: Between major sections (48-64px)
**Micro whitespace**: Between related elements (8-16px)

**Grouping**: Less space within group, more space between groups
**Breathing room**: Don't fill every pixel, let content breathe

### Visual Weight
Create hierarchy with:
- **Size**: Larger = more important
- **Color**: High contrast = attention-grabbing
- **Position**: Top-left = primary (Western reading)
- **Isolation**: Surrounded by whitespace = emphasis

## Technology-Specific Guidance

### Gradio Applications
**Layout components**:
- `gr.Blocks()`: Main container (use themes for consistency)
- `gr.Row()`: Horizontal layout
- `gr.Column()`: Vertical layout (use `scale` for proportions)
- `gr.Tab()`: Organize related sections (3-7 tabs max)
- `gr.Accordion()`: Collapsible sections (good for advanced options)

**Best practices**:
- Group related inputs in `gr.Group()` for visual unity
- Use `visible=False` for progressive disclosure
- `interactive=False` for read-only displays
- Themes: Define colors, fonts, spacing globally

**Accessibility**:
- Use `label` parameter for all inputs (screen readers)
- `info` parameter for help text
- `elem_id` for custom ARIA labels if needed

### React Applications
**Component composition**:
- Small, focused components (<200 lines)
- Container/Presentational pattern (logic vs UI)
- Compound components for complex UI (Tab.List, Tab.Panel)

**State management UX**:
- Optimistic updates (show immediately, revert on error)
- Loading states per component (not whole page)
- Error boundaries for graceful failures

**Accessibility**:
- Use semantic HTML (`<button>`, `<nav>`, `<main>`)
- ARIA labels for icon buttons
- Focus management in modals and dynamic content

### General Web Applications
**Responsive approach**:
- Mobile-first CSS (base styles for mobile, `@media` for larger)
- Fluid typography (`clamp()` for scaling text)
- Flexible images (`max-width: 100%`)

**Performance UX**:
- Lazy load below-fold images
- Skeleton screens during data fetch
- Perceived performance matters (show something immediately)
