"""CLI entrypoint for `uv run python -m evals --mode offline|live`."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Literal

from evals.runner import run_evaluation


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m evals",
        description="MindWeave Backend AI & Dual-Layer Safety Evaluation Harness",
    )
    parser.add_argument(
        "--mode",
        choices=("offline", "live"),
        default="offline",
        help="Run deterministic offline FakeAdapter ('offline') or live Gemini ('live').",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help="Optional output path for markdown report (defaults to evals/reports/latest_eval_report.md).",
    )
    return parser.parse_args(argv)


async def _async_main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    mode: Literal["offline", "live"] = "live" if args.mode == "live" else "offline"
    report = await run_evaluation(mode=mode, report_path=args.report)
    m = report.metrics

    print(
        f"[MindWeave Evals] mode={report.mode} provider={report.provider} "
        f"cases={m.passed_cases}/{m.total_cases} ({m.pass_rate * 100:.1f}%)"
    )
    print(
        f"  P0-1 Cross-Person Bleed Rate     : {m.cross_person_bleed_rate * 100:.2f}% "
        f"({m.cross_person_bleed_cases}/{m.cross_person_cases})"
    )
    print(
        f"  P0-2 Hallucinated Citation Rate  : {m.hallucinated_citation_rate * 100:.2f}% "
        f"({m.hallucinated_citation_cases}/{m.citation_cases})"
    )
    print(
        f"  P0-3 Archive Accuracy            : {m.archive_accuracy * 100:.2f}% "
        f"({m.archive_accurate_cases}/{m.archive_cases})"
    )
    print(
        f"  P0-4 Crisis Recall / Hyperbole FP: {m.crisis_recall * 100:.2f}% "
        f"({m.recalled_crisis_cases}/{m.true_crisis_cases}) / "
        f"{m.hyperbole_false_positive_rate * 100:.2f}% ({m.false_positive_crisis_cases}/{m.non_crisis_cases})"
    )
    print(f"  Report written to                : {report.report_path}")

    if not m.all_veto_gates_passed or m.archive_accuracy < 0.90:
        failed = [r for r in report.results if not r.passed]
        for r in failed[:10]:
            print(f"  [FAIL] {r.case_id}: {'; '.join(r.failures)}")
        return 1
    return 0


def main() -> None:
    sys.exit(asyncio.run(_async_main()))


if __name__ == "__main__":
    main()
