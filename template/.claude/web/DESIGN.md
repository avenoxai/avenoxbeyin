---
name: Obsidian Citrus Cyber
colors:
  surface: '#10141a'
  surface-dim: '#10141a'
  surface-bright: '#21262d'
  surface-container-lowest: '#0a0e14'
  surface-container-low: '#181c22'
  surface-container: '#1c2026'
  surface-container-high: '#262a31'
  surface-container-highest: '#31353c'
  on-surface: '#dfe2eb'
  on-surface-variant: '#cfc6ab'
  inverse-surface: '#dfe2eb'
  inverse-on-surface: '#2d3137'
  outline: '#989178'
  outline-variant: '#4c4732'
  surface-tint: '#e5c500'
  primary: '#fff4d4'
  on-primary: '#393000'
  primary-container: '#f9d71c'
  on-primary-container: '#6e5d00'
  inverse-primary: '#6e5e00'
  secondary: '#ffd65b'
  on-secondary: '#3d2f00'
  secondary-container: '#e7b900'
  on-secondary-container: '#5f4a00'
  tertiary: '#d9ffd3'
  on-tertiary: '#00390e'
  tertiary-container: '#81f088'
  on-tertiary-container: '#006d23'
  error: '#ffb4ab'
  on-error: '#690005'
  error-container: '#93000a'
  on-error-container: '#ffdad6'
  primary-fixed: '#ffe25a'
  primary-fixed-dim: '#e5c500'
  on-primary-fixed: '#211b00'
  on-primary-fixed-variant: '#534600'
  secondary-fixed: '#ffe08b'
  secondary-fixed-dim: '#f0c110'
  on-secondary-fixed: '#241a00'
  on-secondary-fixed-variant: '#584400'
  tertiary-fixed: '#8bfb91'
  tertiary-fixed-dim: '#6fdd78'
  on-tertiary-fixed: '#002106'
  on-tertiary-fixed-variant: '#005319'
  background: '#10141a'
  on-background: '#dfe2eb'
  surface-variant: '#31353c'
  canvas-base: '#0a0e14'
  surface-panel: '#161b22'
  border-subtle: '#30363d'
  glow-lemon: rgba(249, 215, 28, 0.35)
  status-sync-glow: rgba(46, 160, 67, 0.35)
  status-sync-bright: '#67df70'
typography:
  headline-xl:
    fontFamily: Inter
    fontSize: 32px
    fontWeight: '700'
    lineHeight: 40px
    letterSpacing: -0.02em
  headline-xl-mobile:
    fontFamily: Inter
    fontSize: 24px
    fontWeight: '700'
    lineHeight: 32px
    letterSpacing: -0.02em
  headline-lg:
    fontFamily: Inter
    fontSize: 22px
    fontWeight: '600'
    lineHeight: 28px
    letterSpacing: -0.015em
  headline-md:
    fontFamily: Inter
    fontSize: 18px
    fontWeight: '600'
    lineHeight: 24px
    letterSpacing: -0.01em
  headline-sm:
    fontFamily: Inter
    fontSize: 15px
    fontWeight: '600'
    lineHeight: 20px
    letterSpacing: -0.005em
  body-lg:
    fontFamily: JetBrains Mono
    fontSize: 14px
    fontWeight: '400'
    lineHeight: 22px
  body-md:
    fontFamily: JetBrains Mono
    fontSize: 13px
    fontWeight: '400'
    lineHeight: 20px
  body-sm:
    fontFamily: JetBrains Mono
    fontSize: 12px
    fontWeight: '400'
    lineHeight: 18px
  label-md:
    fontFamily: JetBrains Mono
    fontSize: 12px
    fontWeight: '500'
    lineHeight: 16px
    letterSpacing: 0.02em
  label-sm:
    fontFamily: JetBrains Mono
    fontSize: 11px
    fontWeight: '600'
    lineHeight: 14px
    letterSpacing: 0.04em
  code-stat:
    fontFamily: JetBrains Mono
    fontSize: 13px
    fontWeight: '600'
    lineHeight: 16px
rounded:
  sm: 0.125rem
  DEFAULT: 0.25rem
  md: 0.375rem
  lg: 0.5rem
  xl: 0.75rem
  full: 9999px
spacing:
  gutter: 0.75rem
  gutter-compact: 0.5rem
  margin: 1rem
  margin-expanded: 1.5rem
  space-xs: 0.25rem
  space-sm: 0.5rem
  space-md: 0.75rem
  space-lg: 1rem
  space-xl: 1.5rem
---

## Brand & Style

This design system delivers a high-density, engineering-grade developer cockpit engineered for autonomous code reasoning, knowledge graph ingestion, and high-frequency data synthesis. Synthesizing technical minimalism with cybernetic luminescence, the environment feels like a low-latency instrument tuned for prolonged cognitive immersion.

The visual style departs from standard neutral dark themes by anchoring upon an obsidian core accented by luminous Lemon Yellow (`#f9d71c`) and Citrus Gold (`#f5c518`). Color serves an exacting operational role: it signals activation, real-time focus, dynamic backlink traversal, and neural vector activity. Status emerald is reserved strictly for network synchronization, continuous integration pipelines, and runtime health.

## Colors

The palette operates across strict pitch-black foundation tiers and calibrated luminescent highlights:

### Surface Tiers
- **Canvas Base (`#0a0e14`):** The primary ground layer for root viewports, graph orchestrations, and terminal split views.
- **Surface Panel (`#161b22`):** Structural sidebars, file trees, collapsible dockers, and fixed headers.
- **Elevated Surface (`#21262d`):** Knowledge cards, code inspector panes, tooltips, and floating quick-switchers.
- **Structural Outlines (`#30363d`):** 1px structural boundaries defining every viewport slice.

### Accent Vectors
- **Lemon Yellow (`#f9d71c`):** Primary focal vector. Applied to active navigation items, progress rails, primary CTAs, active editor selections, and vector embeddings.
- **Citrus Gold (`#f5c518`):** Secondary container accent. Applied to active tabs, filter chips, secondary toggles, and metadata focus badges.
- **System Green (`#2ea043` / `#67df70`):** Exclusively reserved for deterministic runtime health, database sync validation, and operational uptime. Never used for decorative interactions.

## Typography

The typography couples the structural neutrality of `Inter` with the mechanical precision of `JetBrains Mono`:

- **Structural Headers (`Inter`):** Renders section hierarchies, modal headers, and main view titles with tight negative tracking (`-0.02em` to `-0.005em`), suppressing visual noise while keeping structural markers readable at high zoom.
- **Monospaced Data & Content (`JetBrains Mono`):** Renders code streams, editor blocks, terminal outputs, query params, micro-labels, and metadata badges.
- **Micro-Metrics & Badges:** Labels leverage positive tracking (`0.02em` to `0.04em`) to ensure instant legibility across dense tabular views and graph inspector feeds.

## Layout & Spacing

The layout is built for multi-pane fluid cockpits inspired by developer IDEs and tiling window managers:

- **Multi-Pane Workspace:** Tri-pane default layout: file tree and symbol navigator (`240px` to `280px`), central code/graph canvas (fluid stretch), and metadata inspector (`300px` to `340px`).
- **Spacing Rhythm:** Internal panel gaps rely strictly on `gutter-compact` (`0.5rem`) and `gutter` (`0.75rem`), prioritizing immediate screen efficiency and data density over expansive whitespace.
- **Responsive Adaptability:** On viewport widths below `1024px`, side panels retract into off-canvas sliding sheets accessible via shortcuts or command bar. Below `768px`, the central viewport converts to a single-column stack with persistent bottom-pinned status utilities.

## Elevation & Depth

Visual hierarchy rejects diffuse blurry drop-shadows in favor of razor-sharp surface contrasts, 1px boundary lines, and high-energy neon blooms:

- **Border Division:** Depth is rendered via `1px solid #30363d` framing layered against `#161b22` and `#21262d`.
- **Lemon Photonic Bloom:** Active selections, focused code lines, and highlighted graph nodes cast a tight, vibrant glow using `rgba(249, 215, 28, 0.35)` with an `8px` to `14px` blur radius.
- **Floating Modals:** Command palettes and quick-switchers float on `#21262d`, bordered by `1px solid #f9d71c` with an ambient ground occluding shadow of `0 16px 36px rgba(0, 0, 0, 0.85)`.

## Shapes

The interface implements a restrained soft-corner profile (`roundedness: 1`) to preserve terminal grid alignment while preventing visual fatigue:

- **Base Radius (`0.25rem` / `4px`):** Applied across all interactive buttons, inputs, dropdown menus, chips, and command items.
- **Container Radius (`0.5rem` / `8px`):** Applied strictly to floating dialogs, floating quick-switchers, and detached viewport cards.
- **Sharp Alignment:** Avoid pill shapes to preserve clean tabular alignment with monospaced data streams.

## Components

### Buttons
- **Primary CTA:** Background `#f9d71c`, text `#0a0e14` (`JetBrains Mono` semibold). Resting state has no shadow; hover activates `0 0 12px rgba(249, 215, 28, 0.35)` and subtle brightness lift.
- **Secondary Action:** Background `#21262d`, border `1px solid #30363d`, text `#f9d71c`. Hover shifts border to `#f9d71c` with `rgba(249, 215, 28, 0.1)` background tint.
- **Ghost Action:** Transparent background, text `#dfe2eb`. Hover triggers `#161b22` background with `#f5c518` text color.

### Chips & Badges
- **Active Navigation / Tag Chip:** Background `rgba(249, 215, 28, 0.1)`, border `1px solid rgba(249, 215, 28, 0.4)`, text `#f9d71c`, font `JetBrains Mono` (`11px`).
- **Health / Sync Chip:** Background `rgba(46, 160, 67, 0.12)`, border `1px solid rgba(46, 160, 67, 0.4)`, text `#67df70`. Features a 6px status pill with a synchronized pulse animation.

### Input Fields & Command Palette
- **Text Entry:** Background `#0a0e14`, border `1px solid #30363d`, text `#dfe2eb`, caret `#f9d71c`. Focus state locks border to `#f9d71c` accompanied by a `0 0 8px rgba(249, 215, 28, 0.35)` outer halo.
- **Command Palette:** Centered floating container (`#21262d`), framed with `1px solid #30363d`. Active selection item highlighted via `rgba(249, 215, 28, 0.15)` with a `2px` solid `#f9d71c` leading edge indicator.

### Checkboxes & Selection Controls
- Custom square `14px` toggles (`2px` border radius). Inactive state: `1px solid #30363d` over `#10141a`. Checked state: background `#f9d71c`, displaying an obsidian `#0a0e14` check glyph.

### Cards & Code Blocks
- Base surface `#161b22`, border `1px solid #30363d`, internal padding `space-md` (`0.75rem`).
- Code snippet blocks feature a subtle `#21262d` top header ribbon displaying language identifier and copy action in `JetBrains Mono` (`11px`). Hovering over cards highlights the border to `#f5c518` without spatial expansion.

### Progress Bars
- Track background `#21262d` (`4px` height). Filled bar transitions with a vibrant solid `#f9d71c` leading to a luminous glow at the head.