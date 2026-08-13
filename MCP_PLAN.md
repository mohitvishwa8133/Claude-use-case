# MCP_PLAN.md — the D2 tool-description probe

Companion to `PLAN.md`. Covers the one piece that is not part of the PR flow:
an in-process MCP refactor server, and the experiment it exists to run.

---

## 1. The claim under test

**Tool descriptions drive tool selection.**

An agent picks a tool from its description, not its name. Two tools with
identical names, schemas and implementations will be chosen at different rates
if one description says what the tool is for and the other does not.

The probe runs the same refactoring task twice against the same code, with the
same tools available, changing exactly one thing: the prose in the `@tool`
description.

| Arm | `extract_function` description |
|---|---|
| `vague` | `"Extracts a function from code."` — 30 chars |
| `detailed` | what it does, when to use it, when **not** to, why to prefer it over `Edit`, the parameters, a worked example — 1267 chars |

Everything else is byte-identical: same JSON schemas, same Python
implementations, same built-in tool set, same model, same task.

---

## 2. Why this is a separate entry point

The first design put `--with-mcp vague|detailed` on `review_cli.py`. That does
not work, and the reason is worth keeping.

**The reviewer is read-only.** It runs `tools=["Read", "Grep", "Glob"]` in both
modes and reviews a static landing page. It has no reason to extract a function
or rename a symbol, so it would never call the refactor tools *in either arm*.
Both arms would record zero MCP calls, the probe would report "no difference",
and that null result would be an artifact of the harness rather than a fact
about descriptions. A probe that cannot fail is not a probe.

So the MCP server lives in `probe_cli.py`, which:

- gives the agent `Edit` as well, so ignoring the MCP tools is a real option;
- poses a task that genuinely needs refactoring;
- leaves `review_cli.py` and the PR workflow **completely untouched**.

The read-only reviewer is a D3 claim this repo makes elsewhere. Weakening it to
host a D2 demo would have cost more than the demo is worth.

---

## 3. What was built

### `refactor_tools.py`

An in-process MCP server (`create_sdk_mcp_server`), so there is no second
process to manage and the description set is a Python variable swapped per arm.

Two tools, both **advisory** — they compute the change and return it as a
diff, and never write to disk:

| Tool | What it does |
|---|---|
| `extract_function(file, start_line, end_line, new_name)` | Pulls a block of JavaScript into a new top-level function, infers the parameter list from the block's free variables, returns a unified diff. |
| `rename_symbol(old_name, new_name)` | Whole-identifier rename across every `.js`, `.css` and `.html` file; returns affected files with per-file match counts. |

Advisory on purpose. The agent still has `Edit`, so the arms compare *choice*,
not capability — and neither arm can corrupt the other's sandbox through the
MCP path.

### `probe_cli.py`

```bash
python probe_cli.py                  # both arms, compared
python probe_cli.py --arm detailed   # one arm
python probe_cli.py --format json    # machine-readable timelines
python probe_cli.py --show-options   # config + task, no API call
```

Each arm runs against its own throwaway copy of the source files in a temp
directory, so the arms cannot contaminate each other and your working tree is
never modified. `refactor_tools.REPO` is repointed at the sandbox for the
duration of the arm, so the MCP tools and `Edit` are looking at the same files.

`strict_mcp_config=True` is set: without it a stray project-level MCP config
could inject extra servers and the two arms would stop being comparable.

### The task

```
Two changes to `scripts.js`:

1. The newsletter submit handler validates the email inline. Pull that
   validation out into its own top-level function called `isValidEmail`.
2. Rename the `status` variable to `statusEl` everywhere it appears.

Show me the resulting change.
```

It needs both tools, and both are things `Edit` could also do. That is what
makes the choice informative rather than forced.

The task requires `scripts.js`, which exists only on the `test` branch. The
probe checks for it and says so rather than failing obscurely.

### The measurement

The tool timeline. Did `mcp__refactor__*` appear, or did the agent reach for
`Edit` and `Grep`?

```
  --- arm: vague ------------------------------------------
      tool timeline : Read -> Grep -> Edit -> Edit
      MCP calls     : 0
      Edit/Write    : 2

  --- arm: detailed ---------------------------------------
      tool timeline : Read -> mcp__refactor__extract_function -> mcp__refactor__rename_symbol
      MCP calls     : 2
      Edit/Write    : 0
```

(Illustrative. See §5 — this has not been run against a funded key.)

---

## 4. Two bugs the build surfaced

Both were caught by exercising the implementations directly before wiring them
to a model, and both would have quietly degraded the probe.

### Free variables harvested from string literals

The first `extract_function` produced:

```
isValidEmail(Check, Please, Thanks, address)
```

`Check`, `Please`, `Thanks` and `address` are words from
`'Thanks! Check your inbox to confirm.'` and `'Please enter a valid email
address.'`. The identifier scan was reading prose inside string literals.

Fixed by scrubbing strings, template literals and comments before collecting
identifiers. Now:

```
handleEmail(email, status, subscribe)
```

Exactly the three free variables, which is the whole selling point of the tool.

### No brace-balance check

The tool would happily "extract" a half-open block — `} else {` through `});` —
and emit JavaScript that cannot parse. It now rejects a range whose braces or
parens do not close within it:

```
Lines 26-31 of scripts.js cannot be extracted: the block closes a brace it
never opened. Widen or narrow the range to a complete statement.
```

This matters beyond correctness. The detailed description claims *"PREFER THIS
OVER Edit for extraction"*. A tool that emits broken syntax makes that claim
false, and an agent that tried it once and got garbage would be right to fall
back to `Edit` — which would have shown up as a null result and been
misread as "descriptions don't matter".

---

## 5. Verification status

**Confirmed working:**

- Both tool implementations, exercised directly against the real `scripts.js`.
  `rename_symbol` correctly reports `scripts.js (4)`, `styles.css (1)`,
  `index.html (2)` for `status`. `extract_function` correctly infers
  `(email, status, subscribe)`.
- Guard paths: non-JS file, out-of-range lines, unbalanced block, no-match
  rename — all return a clear `is_error` result rather than nonsense.
- The MCP server registers in a live session:
  ```
  mcp_servers: [{'name': 'refactor', 'status': 'connected'}]
  tools: ['Edit', 'Glob', 'Grep', 'Read',
          'mcp__refactor__extract_function', 'mcp__refactor__rename_symbol']
  ```
- `--show-options` reports 30 chars vs 1267 chars for the two arms — the
  independent variable is real and measurable.

**Not yet proven:**

The probe has never completed a run. The available API key returns
`billing_error: Credit balance is too low`, same blocker as the review flow
(`PLAN.md` §5). Everything up to the model call is verified; the tool timeline
itself is not.

To finish:

```bash
git checkout test
python probe_cli.py
```

---

## 6. Reading the result honestly

One run is an anecdote. `probe_cli.py` prints that line every time, and it is
not decoration:

- The model may call the MCP tools in **both** arms — the tool *names* alone
  are fairly suggestive here. That is a real outcome, not a failed probe, and
  the honest reading is "the name carried it; the description was not the
  binding constraint for this task".
- The model may call them in **neither** arm. Check the tools are reachable
  before concluding anything — a disconnected server looks identical to a
  rejected one from the timeline.
- The vague arm may occasionally use them and the detailed arm not. Re-run.

The probe reports which of these happened rather than asserting the expected
result, which is the only way it can be evidence rather than a demo.

---

## 7. What this changes in domain coverage

D2 was the thinnest domain in this repo — built-in tools and gating only.
It now covers both halves:

| | Before | After |
|---|---|---|
| Built-in tools, read-only gating | ✅ | ✅ |
| Custom MCP server | ❌ | ✅ `refactor_tools.py` |
| Tool descriptions drive selection | ❌ | ✅ `probe_cli.py` |
| `strict_mcp_config` | ❌ | ✅ |

D1, D3, D4 and D5 are unaffected. The PR flow did not change — no commit in
this work touches `review_cli.py`, `CLAUDE.md`, `.claude/settings.json` or the
workflow.

---

## 8. Demo script

1. **Show the two descriptions.** `python probe_cli.py --show-options`.
   30 characters against 1267. Same schema, same implementation, same name.
2. **Run both arms.** `python probe_cli.py`.
3. **Read the timelines**, not the prose. The vague arm's fallback to
   `Grep` + `Edit` is the interesting part: it is what a manual rename looks
   like, and `Grep` matches substrings, so `nav` also hits `nav-toggle` and
   `site-nav`.
4. **Show why the tool is better**, not just preferred: ask for an unbalanced
   range and watch it refuse, then note that `Edit` would have accepted it.
5. **Say the limit out loud.** One sample per arm. This is a demonstration of
   the mechanism, not a measurement of the effect size.
