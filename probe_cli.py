"""D2 probe — do tool descriptions drive tool selection?

    python probe_cli.py                  # run both arms, compare
    python probe_cli.py --arm detailed   # one arm
    python probe_cli.py --show-options   # config only, no API call

Two arms, one refactoring task. The arms differ in exactly one thing: the prose
in the MCP tool descriptions. Same task, same schemas, same implementations,
same built-in tools — `Edit` is available in both, so the agent is free to
ignore the MCP server entirely.

    vague     "Extracts a function from code."
    detailed  what it does, when to use it, when NOT to, why to prefer it
              over Edit, the parameters, and a worked example

The measurement is the tool timeline: did `mcp__refactor__*` get called, or did
the agent reach for `Edit` and `Grep`?

This does NOT touch the PR review flow. `review_cli.py` stays read-only with no
MCP server — a reviewer that never refactors would never call these tools, so
wiring them into it would compare two arms of zero calls and prove nothing.

Each arm runs against its own throwaway copy of the repo, so the arms cannot
contaminate each other and your working tree is never modified.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent

try:
    from dotenv import load_dotenv

    load_dotenv(REPO / ".env.local")
except ImportError:  # pragma: no cover
    pass

import refactor_tools  # noqa: E402
from claude_agent_sdk import (  # noqa: E402
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    query,
)

if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DEFAULT_MODEL = os.environ.get("REVIEW_MODEL", "haiku")

# The task. It needs both tools, and both are things `Edit` could also do —
# that is what makes the choice informative rather than forced.
TASK = (
    "Two changes to `scripts.js`:\n"
    "\n"
    "1. The newsletter submit handler validates the email inline. Pull that "
    "validation out into its own top-level function called `isValidEmail`.\n"
    "2. Rename the `status` variable to `statusEl` everywhere it appears.\n"
    "\n"
    "Show me the resulting change."
)

# What the probe copies into each arm's sandbox. Not `.git`, not `.venv`.
ARM_FILES = ("index.html", "styles.css", "scripts.js", "CLAUDE.md")

MCP_TOOLS = ["mcp__refactor__extract_function", "mcp__refactor__rename_symbol"]
BUILTIN_TOOLS = ["Read", "Grep", "Glob", "Edit"]


@dataclass
class ArmResult:
    arm: str
    timeline: list[str] = field(default_factory=list)
    text: str = ""
    turns: int = 0
    cost_usd: float = 0.0
    wall_clock_s: float = 0.0
    error: str | None = None

    @property
    def mcp_calls(self) -> list[str]:
        return [t for t in self.timeline if t.startswith("mcp__refactor__")]

    @property
    def edit_calls(self) -> list[str]:
        return [t for t in self.timeline if t in ("Edit", "Write")]

    @property
    def used_mcp(self) -> bool:
        return bool(self.mcp_calls)


def build_options(arm: str, cwd: Path) -> ClaudeAgentOptions:
    """Identical in every respect except which description set is loaded."""
    server = refactor_tools.build_refactor_server(arm)
    return ClaudeAgentOptions(
        cwd=str(cwd),
        setting_sources=[],
        # `Edit` is deliberately present. The probe is only meaningful if the
        # agent has a real alternative to the MCP tools.
        tools=BUILTIN_TOOLS,
        allowed_tools=BUILTIN_TOOLS + MCP_TOOLS,
        mcp_servers={"refactor": server},
        # Without this, a stray project-level MCP config could add servers and
        # the two arms would stop being comparable.
        strict_mcp_config=True,
        permission_mode="dontAsk",
        model=DEFAULT_MODEL,
        max_budget_usd=0.75,
    )


def _make_arm_sandbox(arm: str, parent: Path) -> Path:
    """A throwaway copy of the source files, one per arm."""
    arm_dir = parent / arm
    arm_dir.mkdir(parents=True)
    for name in ARM_FILES:
        source = REPO / name
        if source.exists():
            shutil.copy2(source, arm_dir / name)
    return arm_dir


async def run_arm(arm: str, sandbox: Path) -> ArmResult:
    result = ArmResult(arm=arm)
    # The MCP tools read from disk, so point them at this arm's sandbox too.
    # Otherwise they would report on the real tree while `Edit` rewrote a copy.
    previous_root = refactor_tools.REPO
    refactor_tools.REPO = sandbox
    started = time.perf_counter()
    reason: str | None = None

    try:
        async for message in query(prompt=TASK, options=build_options(arm, sandbox)):
            if isinstance(message, AssistantMessage):
                if getattr(message, "error", None):
                    detail = " ".join(
                        b.text for b in message.content if isinstance(b, TextBlock)
                    ).strip()
                    reason = f"{message.error}: {detail}" if detail else str(message.error)
                for block in message.content:
                    if isinstance(block, ToolUseBlock):
                        result.timeline.append(block.name)
                    elif isinstance(block, TextBlock):
                        result.text += block.text
            elif isinstance(message, ResultMessage):
                result.turns = message.num_turns
                result.cost_usd = message.total_cost_usd or 0.0
                if message.is_error:
                    result.error = reason or str(message.subtype)
    except Exception as exc:  # noqa: BLE001
        result.error = reason or f"{type(exc).__name__}: {exc}"
    finally:
        refactor_tools.REPO = previous_root

    result.wall_clock_s = time.perf_counter() - started
    return result


def render(results: list[ArmResult]) -> str:
    out = ["", "=" * 72, "  D2 probe: do tool descriptions drive tool selection?", "=" * 72, ""]

    for r in results:
        out.append(f"  --- arm: {r.arm} " + "-" * (54 - len(r.arm)))
        if r.error:
            out += [f"      FAILED: {r.error}", ""]
            continue
        out.append(f"      tool timeline : {' -> '.join(r.timeline) or '(no tools called)'}")
        out.append(f"      MCP calls     : {len(r.mcp_calls)}")
        out.append(f"      Edit/Write    : {len(r.edit_calls)}")
        out.append(f"      {r.turns} turns | {r.wall_clock_s:.1f}s | ${r.cost_usd:.4f}")
        out.append("")

    ran = [r for r in results if not r.error]
    if len(ran) == 2:
        vague, detailed = ran[0], ran[1]
        out.append("-" * 72)
        if detailed.used_mcp and not vague.used_mcp:
            out.append("  RESULT: descriptions drove selection. The detailed arm called the")
            out.append("          MCP tools; the vague arm fell back to built-ins.")
        elif detailed.used_mcp and vague.used_mcp:
            out.append("  RESULT: both arms used the MCP tools. The task may be specific")
            out.append("          enough that the tool names alone were sufficient.")
        elif not detailed.used_mcp and not vague.used_mcp:
            out.append("  RESULT: neither arm used the MCP tools. Check that the tools are")
            out.append("          actually reachable before reading anything into this.")
        else:
            out.append("  RESULT: the vague arm used MCP and the detailed arm did not.")
            out.append("          Worth re-running; a single sample is not a finding.")
        out.append("")

    out += ["  One run is an anecdote. Repeat before claiming a result.", "=" * 72, ""]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="probe_cli.py",
        description="D2 probe: MCP tool descriptions vs tool selection.",
    )
    parser.add_argument("--arm", default=None, choices=("vague", "detailed"),
                        help="run one arm only (default: both)")
    parser.add_argument("--format", dest="fmt", default="text", choices=("text", "json"))
    parser.add_argument("--show-options", action="store_true",
                        help="print the configuration and task, then exit without calling the API")
    args = parser.parse_args(argv)

    if not (REPO / "scripts.js").exists():
        print(
            "scripts.js not found.\n"
            "This probe refactors the landing page's JavaScript, which only exists on\n"
            "the `test` branch. Run `git checkout test` first.",
            file=sys.stderr,
        )
        return 2

    arms = [args.arm] if args.arm else ["vague", "detailed"]

    if args.show_options:
        for arm in arms:
            print(f"--- arm: {arm} ---")
            opts = build_options(arm, REPO)
            print(f"  tools          = {opts.tools}")
            print(f"  allowed_tools  = {opts.allowed_tools}")
            print(f"  mcp_servers    = {{'refactor': <{arm} descriptions>}}")
            print(f"  strict_mcp_config = {opts.strict_mcp_config}")
            print(f"  setting_sources= {opts.setting_sources}")
            for name, text in refactor_tools.DESCRIPTION_SETS[arm].items():
                print(f"  {name}: {len(text)} chars")
            print()
        print("--- task ---\n")
        print(TASK)
        return 0

    results: list[ArmResult] = []
    with tempfile.TemporaryDirectory(prefix="d2-probe-") as tmp:
        for arm in arms:
            sandbox = _make_arm_sandbox(arm, Path(tmp))
            results.append(asyncio.run(run_arm(arm, sandbox)))

    if args.fmt == "json":
        print(json.dumps(
            [
                {
                    "arm": r.arm,
                    "timeline": r.timeline,
                    "mcp_calls": r.mcp_calls,
                    "edit_calls": r.edit_calls,
                    "used_mcp": r.used_mcp,
                    "turns": r.turns,
                    "cost_usd": r.cost_usd,
                    "wall_clock_s": round(r.wall_clock_s, 2),
                    "error": r.error,
                }
                for r in results
            ],
            indent=2,
        ))
    else:
        print(render(results))

    return 2 if any(r.error for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
