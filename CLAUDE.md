# CLAUDE.md — Inkwell review standards

Inkwell is a static blog landing page: `index.html`, `styles.css`, and whatever
JavaScript the page loads. There is no build step and no framework.

You are acting as a reviewer on a pull request. Read the files before judging
them. Cite `file:line` for every finding.

## REPORT — always raise these

- **security** — user-controlled content written into the DOM as markup
  (`innerHTML`, `document.write`), inline event handlers that evaluate strings,
  secrets or API keys in source, links to untrusted origins without
  `rel="noopener"`.
- **logic** — conditions that are inverted or off by one, handlers wired to the
  wrong element, values computed and never used, form actions that go nowhere.
- **ux** — states the user can reach that the page does not handle: images
  without `alt`, form controls without an associated `<label>`, interactive
  controls that are not reachable by keyboard, text whose contrast against its
  own background falls below WCAG AA (4.5:1 for body text).
- **reliability** — script that assumes an element exists, listeners bound
  before the node is in the DOM, anything that throws on a normal page load.

## SKIP — never raise these

- Indentation, line length, quote style, trailing commas, attribute order.
- Class-naming conventions and CSS property ordering.
- Preferences about semantic tag choice where the current tag is not wrong.
- Anything a formatter would fix on its own.

A review that lists attribute ordering next to an XSS sink has buried the
finding that mattered. If you have nothing in the REPORT categories, say so.

## Scope

Verify these specifically, rather than "check everything":

- Any value that reaches the page from `location`, `URLSearchParams`,
  `localStorage`, a form field, or a `fetch` response.
- Every `<img>`, `<input>`, `<select>`, `<textarea>`, and `<button>`.
- Every hardcoded colour pair used for text on a background.

Constants, static copy, and decorative markup do not need verification.

## Severity

- `critical` / `high` — exploitable, or breaks the page for a real user.
- `medium` — degrades the experience or will break under a plausible input.
- `low` — worth fixing, blocks nothing.

Only `security` and `logic` findings at `high` or `critical` block a merge.
