"""Deterministic, infrastructure-free dry-run over the case catalog.

Validates every case, prints the routing table, and optionally regenerates the
catalog and traceability reports.

Exit codes — the `Stop` hook depends on these:
    0  every case is valid
    1  at least one case is INVALID (including a case file that cannot be read
       or parsed)
    2  GATE ERROR — the check could not run at all (missing/invalid
       --cases-dir, bad flags, unwritable report path, or any unexpected
       internal failure)

The distinction between 1 and 2 matters: a per-case problem is a *case*
problem, so the caller should block (exit 1) — something in the catalog is
genuinely broken and needs a human to fix it. A whole-run problem means the
gate itself is broken, not the catalog, so the caller should fail **open**
(exit 2) rather than block on a tool that cannot even run. Argparse's own
`SystemExit(2)` for a bad flag already lands on the same code by convention
(see the comment near `parser.parse_args` below), so callers can treat any
nonzero-but-not-1 exit as "the gate itself is broken."
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import get_args

from tests.runner.discover import CASES_DIR, load_cases, route, unsupported_kinds
from tests.runner.reports import bucket_of, render_catalog, render_traceability
from tests.runner.schema import Tier

DEFAULT_TRACEABILITY = Path(__file__).resolve().parents[1] / "reports" / "traceability.md"

EXIT_OK = 0
EXIT_INVALID = 1
EXIT_GATE_ERROR = 2


def _write_atomic(path: Path, content: str) -> None:
    """Write `content` to `path` without ever leaving a truncated file behind.

    Writes to a sibling temp file first, then `os.replace()`s it onto the
    target. `os.replace` is atomic on both POSIX and Windows, so a crash or a
    permission failure mid-write cannot leave a half-written report committed.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _print_table(routing) -> None:
    cases = sorted(routing.all_cases, key=lambda case: case.id)
    if not cases:
        print("No cases found.")
        return
    width = max(len(case.id) for case in cases)
    tier_width = max(len(str(t)) for t in get_args(Tier))
    print(f"{'id'.ljust(width)}  {'tier'.ljust(tier_width)}  pri  execution")
    print(f"{'-' * width}  {'-' * tier_width}  ---  ---------------")
    for case in cases:
        bucket = bucket_of(case, routing)
        if bucket == "engine-unsupported":
            execution = f"unsupported:{','.join(sorted(unsupported_kinds(case)))}"
        else:
            execution = bucket
        print(
            f"{case.id.ljust(width)}  {case.tier.ljust(tier_width)}  "
            f"{case.priority}   {execution}"
        )


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
    # argparse calls sys.exit(2) itself on a bad flag, before this function's
    # try/except below is even entered. That already matches EXIT_GATE_ERROR,
    # so there is nothing to intercept here — just don't let it drift.
    args = parser.parse_args(argv)

    if not args.cases_dir.is_dir():
        print(f"gate error: --cases-dir {args.cases_dir} is not a directory")
        return EXIT_GATE_ERROR

    try:
        result = load_cases(args.cases_dir)

        if result.errors:
            print(f"INVALID — {len(result.errors)} case file(s) failed validation:\n")
            for error in result.errors:
                print(f"  {error}")
            print(f"\n{len(result.cases)} valid, {len(result.errors)} invalid.")
            return EXIT_INVALID

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
            _write_atomic(catalog_path, render_catalog(routing))
            print(f"wrote {catalog_path}")

        if args.write_traceability:
            _write_atomic(args.write_traceability, render_traceability(result.cases))
            print(f"wrote {args.write_traceability}")

        return EXIT_OK
    except Exception as exc:
        # The gate must never crash the caller: any unexpected failure here is a
        # broken gate, not a broken catalog, so it fails open at EXIT_GATE_ERROR.
        print(f"gate error: {exc}")
        return EXIT_GATE_ERROR


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
