# Themes

A theme is a JSON file of design tokens (CSS custom properties). The dashboard (`static/style.css`)
and every widget style themselves **only** through these tokens, so a theme file fully controls
the look.

- **Make your own:** copy any file here to `themes/<your-id>.json`, change `name` and the values,
  restart. It appears in the theme menu. (The assistant can also save custom themes for you.)
- **Tokens:** `midnight.json` is the reference theme and lists every token. Other themes may omit
  tokens; missing ones fall back to midnight's value.
- **Adding a token:** add it to `midnight.json` (with a sensible value), use it as `var(--name)` in
  `static/style.css` or widgets, and optionally set it in the other themes.

| token | used for |
|---|---|
| `--scheme` | `dark` or `light`: native form controls, scrollbars |
| `--bg` | page background |
| `--card` | widget cards, panels, dialogs |
| `--surface` | inputs, chips, chat bubbles, rows inside cards |
| `--border` | input and divider lines |
| `--text`, `--muted` | primary and secondary text |
| `--accent`, `--accent-text` | buttons, highlights, and the text on them |
| `--danger`, `--warning`, `--success` | errors, attention, OK states |
| `--radius` | corner radius of cards (smaller elements derive from it) |
| `--font` | font stack |
| `--shadow` | card shadow |
| `--overlay` | backdrop behind dialogs |
| `--accent-2` | secondary accent (gradients, highlights) |
| `--font-display` | headings and widget titles |
| `--label-font`, `--label-case`, `--label-tracking` | small UI labels: widget headers, buttons, form labels |
| `--card-clip`, `--control-clip` | `clip-path` for cards and buttons/inputs (`none`, or a polygon for cut corners) |
| `--frame` | card outline as a background image (use a solid `linear-gradient(c, c)` for a plain line) |
| `--backdrop` | page background layers drawn over `--bg` (glows, grids), or `none` |
| `--glow` | box-shadow for hover/focus glow, or `none` |

Fonts: the families in `static/fonts/fonts.css` (Space Grotesk, Chakra Petch, JetBrains Mono) are bundled and work offline, including inside widgets. Other names fall back to whatever the viewer's system has.

Token values may reference other tokens, e.g. `"--font-display": "var(--font)"`.

Values are plain CSS: colors, lengths, font stacks, shadows. `url(...)` and anything that could
break out of a declaration (`;`, `{`, `}`, `<`) is rejected.
