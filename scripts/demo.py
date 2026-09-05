"""Run one agent task without starting the HTTP server.

    uv run python scripts/demo.py "Fix divide() so dividing by zero raises ValueError"
"""

import asyncio
import sys

from repopilot.agent import build_graph, initial_state
from repopilot.config import get_settings
from repopilot.evaluation import evaluate_run
from repopilot.llm import build_llm
from repopilot.observability import setup_logging
from repopilot.tools import build_registry
from repopilot.workspace import WorkspaceManager

DEFAULT_TASK = "Fix divide() in calculator.py so that dividing by zero raises ValueError."


async def main() -> int:
    setup_logging()
    settings = get_settings()
    task = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TASK

    manager = WorkspaceManager(settings.workspace_root)
    workspace = manager.create(settings.sample_repo)

    graph = build_graph(build_llm(), build_registry(), workspace)
    state = await graph.ainvoke(
        initial_state(workspace.run_id, task, str(settings.sample_repo), settings.max_retries)
    )

    print("\n" + "=" * 70)
    print("STEPS")
    for line in state["step_log"]:
        print(f"  - {line}")
    print("\nREPORT")
    print(state["final_report"])
    print("\nDIFF")
    print(state["diff"] or "(none)")
    print("\nEVALUATION")
    print(evaluate_run(state).model_dump_json(indent=2))
    print("=" * 70)
    print(f"\nworkspace kept at: {workspace.root}")
    return 0 if state.get("verdict") == "success" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
