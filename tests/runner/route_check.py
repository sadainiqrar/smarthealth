"""Deterministic, infrastructure-free dry-run over the case catalog.

Validates every case, prints the routing table, and optionally regenerates the
catalog and traceability reports.

Exit codes — the `Stop` hook depends on these:
    0  every case is valid
    1  at least one case is invalid
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

from tests.runner.discover import CASES_DIR, load_cases, route, unsupported_kinds
from tests.runner.reports import render_catalog, render_traceability

DEFAULT_TRACEABILITY = Path(__file__).resolve().parents[1] / "reports" / "traceability.md"


def _print_table(routing) -> None:
    cases = sorted(routing.all_cases, key=lambda case: case.id)
    if not cases:
        print("No cases found.")
        return
    width = max(len(case.id) for case in cases)
    print(f"{'id'.ljust(width)}  tier         pri  execution")
    print(f"{'-' * width}  -----------  ---  ---------------")
    for case in cases:
        if case in routing.blocked:
            execution = "blocked"
        elif case in routing.unsupported:
            execution = f"unsupported:{','.join(sorted(unsupported_kinds(case)))}"
        elif case in routing.impl_backed:
            execution = "impl"
        else:
            execution = "declarative"
        print(f"{case.id.ljust(width)}  {case.tier:<11}  {case.priority}   {execution}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tests.runner.route_check",
        description="Validate and route the SmartHealth test case catalog.",
    )
    parser.add_argument("--cases-dir", type=Path, default=CASES_DIR)
    parser.add_argument(
        "--write-catalog", action="store_true", help="regenerate <cases-dir>/CATALOG.md"
    )
    parser.add_argument(
        "--write-traceability",
        type=Path,
        nargs="?",
        const=DEFAULT_TRACEABILITY,
        help="regenerate the requirement traceability report",
    )
    parser.add_argument("--quiet", action="store_true", help="print only errors and the summary")
    args = parser.parse_args(argv)

    result = load_cases(args.cases_dir)

    if result.errors:
        print(f"INVALID — {len(result.errors)} case file(s) failed validation:\n")
        for error in result.errors:
            print(f"  {error}")
        print(f"\n{len(result.cases)} valid, {len(result.errors)} invalid.")
        return 1

    routing = route(result.cases)
    if not args.quiet:
        _print_table(routing)
        print()

    by_tier = Counter(case.tier for case in result.cases)
    by_priority = Counter(case.priority for case in result.cases)
    print(
        f"{len(result.cases)} case(s) — "
        + ", ".join(f"{tier}:{count}" for tier, count in sorted(by_tier.items()))
        + " | "
        + ", ".join(f"{p}:{c}" for p, c in sorted(by_priority.items()))
    )
    print(
        f"declarative:{len(routing.declarative)} impl:{len(routing.impl_backed)} "
        f"unsupported:{len(routing.unsupported)} blocked:{len(routing.blocked)}"
    )

    for case in routing.blocked:
        print(f"  blocked  {case.id}: {case.blocked_on}")
    for case in routing.unsupported:
        print(
            f"  WARNING  {case.id} needs engine support for "
            f"{sorted(unsupported_kinds(case))} and will FAIL when run"
        )

    if args.write_catalog:
        catalog_path = args.cases_dir / "CATALOG.md"
        catalog_path.write_text(render_catalog(routing), encoding="utf-8")
        print(f"wrote {catalog_path}")

    if args.write_traceability:
        args.write_traceability.parent.mkdir(parents=True, exist_ok=True)
        args.write_traceability.write_text(render_traceability(result.cases), encoding="utf-8")
        print(f"wrote {args.write_traceability}")

    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
