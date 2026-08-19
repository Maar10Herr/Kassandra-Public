#!/usr/bin/env python3
"""Reproducible synthetic scale benchmark for Kassandra scoring.

This measures execution cost only. It is not a model-quality benchmark.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import time
from typing import Any

from kassandra.db import migrate
from kassandra.scoring import compute_scores


def build_db(company_count: int) -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    migrate(db)
    db.executemany(
        """INSERT INTO registry
           (canonical_name, isin, jurisdiction, company_type, status, domain)
           VALUES (?, ?, 'DE', 'company', 'active', ?)""",
        [
            (f"Synthetic Company {index}", f"XS{index:010d}", f"company-{index}.test")
            for index in range(company_count)
        ],
    )
    db.commit()
    return db


def benchmark(company_count: int, repeats: int) -> dict[str, Any]:
    durations: list[float] = []
    scored = 0
    for _ in range(repeats):
        db = build_db(company_count)
        started = time.perf_counter()
        scored = len(compute_scores(db))
        durations.append(time.perf_counter() - started)
        db.close()
    return {
        "companies": company_count,
        "scored": scored,
        "repeats": repeats,
        "median_seconds": round(statistics.median(durations), 6),
        "max_seconds": round(max(durations), 6),
        "companies_per_second": round(company_count / statistics.median(durations), 1),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sizes", nargs="+", type=int, default=[50, 500, 3000])
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    print(json.dumps([benchmark(size, args.repeats) for size in args.sizes], indent=2))


if __name__ == "__main__":
    main()
