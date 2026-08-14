# openmanet-node-builder — Claude Code Guide

Console (curses) tool to configure an OpenMANET node and bake a
ready-to-flash factory image. See `README.md` for what it does and why.
This file is about *how the TUI itself is built* — read it before adding
a screen or field so new work matches the existing chrome instead of
hand-rolling a one-off look.

## The chrome is a small design system — use it, don't bypass it

Every screen is built from the same handful of primitives in
`node_builder.py`. A new screen should never call `stdscr.erase()` /
`stdscr.addstr()` directly — go through these instead:

- **`draw_frame(stdscr, title, hint=None)`** — draws the shared panel
  (background fill, centered bordered box, title in the top border,
  hint in the bottom border) and returns the `(top, left, bottom,
  right)` interior rectangle. Every other primitive is built on this;
  call it directly only if you're adding a new primitive, not a new
  feature screen.
- **`select_from_list(stdscr, title, items, ...)`** — the menu/picker.
  `items` are `(label, value)` or `(label, value, style)`, `style` in
  `{None, "ok"/"action", "warn", "crit", "accent", "muted"}`. Scrolls
  automatically when the list outgrows the panel — never assume a list
  fits on screen (the country/channel pickers don't on a short
  terminal).
- **`prompt_text` / `prompt_int` / `prompt_bool`** — text/int/bool
  input, all built on `select_from_list` or the editable-field pattern
  below.
- **`message(stdscr, lines, color=None)`** — framed dialog for
  errors/notices/success. `color` also picks the dialog's title
  (`_MESSAGE_TITLES`) — pass `4` for a real error, not just a red font.

**Editable fields, not retype-from-scratch.** `prompt_text` pre-fills
the current value into a `curses.textpad.Textbox` (see
`_edit_textbox`) so the user edits in place (backspace, arrow keys)
instead of seeing "Current: X" and having to type a whole new value
blind. Enter submits, Esc discards and reverts to the original value.
Keep this pattern for any new text-entry field — it's the single
biggest usability win in this tool's history over the first cut.

**Proximity grouping via `SEP`.** `select_from_list` items can include
a divider row: `(None, SEP, None)`. It renders as a thin rule and is
never selectable — `_next_selectable` skips over it during navigation.
Use it to separate "things you edit" from "things you trigger" (see
`profile_menu_items`: config fields, divider, action buttons, divider,
back). Don't add a new action button to a screen without checking
whether it needs its own group.

## Color palette — why it looks like this, and how to extend it

The palette was deliberately redone after the first version (solid
saturated `COLOR_BLUE` background, bright `COLOR_CYAN` selection block)
came across as harsh. Current design, backed by actual research (see
chat history / commit message for sources — WCAG contrast guidance and
2026 terminal-theme roundups converged on the same points):

- **Never use a fully-saturated ANSI color as a background fill.**
  Saturated blocks read as "loud"/"offensive" against dark UIs. Use a
  desaturated, near-black background instead (current: xterm-256 index
  `236`, ≈ `#303030`) — and never pure black either (`#000000` reads as
  an extreme, eye-straining contrast against light text; a slightly
  lifted near-black is calmer).
- **Prefer muted/desaturated hues over the raw 8-color ANSI set** for
  anything drawn on that background — the raw `COLOR_RED`/`COLOR_YELLOW`
  etc. are far more saturated than they need to be for a status color.
  Current picks (Nord/Gruvbox-family, chosen from the xterm-256 fixed
  palette by index — no `init_color()`/dynamic reprogramming, which not
  every terminal honors):
  - accent (idx `116`, muted teal) — replaces neon cyan
  - ok (idx `108`, sage green), warn (idx `222`, muted gold),
    crit (idx `167`, brick red)
  - selection highlight: light text on a *lighter grey bar* (idx `240`),
    not a saturated color block — this is how modern TUIs (lazygit,
    k9s, bottom) indicate "selected," and it's much calmer than an
    inverted bright-cyan row.
- **256-color detection with an 8-color fallback is mandatory** — see
  `_c(idx256, basic)`. Never hardcode a 256-index color pair without a
  `curses.COLORS >= 256` guard; some terminals still only offer 8/16.
- **Every color pair shares the same background component.** This is
  what makes `stdscr.bkgd()` work as a real "theme" instead of leaving
  black holes where text was drawn with a pair whose bg is `-1`
  (terminal default) instead of the panel's actual fill color. If you
  add a new color pair, give it the same `bg` variable as the others in
  `init_colors`, not `-1`.

If you need a new status color, extend `_STYLE_PAIRS` and pick a muted
xterm-256 index in the same family (a 216-cube "medium, slightly
desaturated" color, not a pure primary), with an 8-color fallback.

## Layout

- **Panel is fixed-width and centered** (`PANEL_MAX_W = 78`,
  `PANEL_MAX_H = 28`), not stretched edge-to-edge — a menu that fills
  the whole terminal width looks unbalanced once lines are short. If a
  screen genuinely needs more width (a long table, say), raise the
  const rather than special-casing that one screen's frame.
- Content sits with 2-row/2-col padding off the border on every side
  (`top+2`/`left+2` from `draw_frame`'s return) — keep new content
  inside that rectangle; don't draw flush against the border.
- Aligned label columns: use a `field(label, value)`-style helper (see
  `profile_menu_items`) with a fixed label width (currently 16 chars)
  rather than ad hoc string concatenation — misaligned columns are the
  fastest way to make a list look unpolished.

## General UI principles applied here (apply them to new screens too)

- **Contrast**: status/accent colors need to read clearly against the
  panel background at a glance — if you're unsure a new color pair is
  legible, err toward the lighter/more saturated end relative to the
  near-black background, not the other way.
- **Proximity**: group related controls together and separate unrelated
  groups with whitespace or a `SEP` divider (see above).
- **Alignment**: label columns, not free-floating text at varying
  indents.
- **Repetition/consistency**: every screen uses the same chrome
  (`draw_frame` + one of the primitives above). A screen that looks
  different from the rest is a bug, not a style choice — fix the shared
  primitive instead of diverging.

## Testing changes to the TUI

`curses` needs a real TTY — you can't just run the script and eyeball
stdout. To verify changes headlessly (no hardware/display needed):

1. Open a pty with `pty.openpty()`, fork, `dup2` the slave onto
   fd 0/1/2 in the child, run the code under `curses.wrapper(...)`
   there.
2. Inject keystrokes with `curses.ungetch(ch)` **in reverse order**
   (it's a LIFO stack) before calling whatever `getch()`-driven function
   you're testing — works for `select_from_list`, `_edit_textbox`, etc.
   without needing to monkeypatch `getch` (you can't — it's a read-only
   attribute on the real curses window type).
3. To eyeball layout/geometry without a real display, dump the screen
   with `stdscr.instr(y, 0, w)` per row and print it — box-drawing
   characters come back as raw VT100 alternate-charset fallback bytes
   (`l`, `q`, `x`, `k`, `m`, `j` for corners/lines), which is expected
   and still confirms the border/geometry is where you think it is.
4. Set `TERM=xterm-256color` in the child before `initscr()` if you're
   testing anything that depends on `curses.COLORS >= 256`.

This pattern caught real bugs during development (a page-counter drawn
at the wrong absolute row after the panel became centered instead of
edge-to-edge) — don't skip it for a "just cosmetic" change.
