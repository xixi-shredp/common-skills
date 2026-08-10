#!/usr/bin/env python3
"""Suggest bounded build parallelism from current Linux host resources."""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path


GIB = 1024**3


def mem_available_bytes() -> int | None:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, value, unit = line.split()
            if key == "MemAvailable:":
                return int(value) * 1024 if unit == "kB" else None
    except OSError:
        pass
    return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Suggest total and per-slice make parallelism from host headroom."
    )
    parser.add_argument("--slices", type=int, required=True, help="simultaneous independent slices")
    parser.add_argument(
        "--gib-per-job",
        type=float,
        default=1.5,
        help="conservative available-memory estimate per compiler job (default: 1.5)",
    )
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args()
    if args.slices < 1 or args.gib_per_job <= 0:
        parser.error("--slices and --gib-per-job must be positive")

    cpus = os.cpu_count() or 1
    try:
        load_1m = os.getloadavg()[0]
    except OSError:
        load_1m = 0.0
    cpu_headroom = max(1, cpus - math.ceil(load_1m))
    available = mem_available_bytes()
    memory_jobs = max(1, int(available / (args.gib_per_job * GIB))) if available else cpu_headroom
    total_jobs = max(1, min(cpu_headroom, memory_jobs))
    per_slice_jobs = max(1, total_jobs // args.slices)
    result = {
        "cpu_count": cpus,
        "load_1m": round(load_1m, 2),
        "mem_available_bytes": available,
        "gib_per_job": args.gib_per_job,
        "slices": args.slices,
        "suggested_total_jobs": total_jobs,
        "suggested_jobs_per_slice": per_slice_jobs,
    }
    if args.format == "json":
        print(json.dumps(result, sort_keys=True))
    else:
        print(
            f"Suggested: {total_jobs} total compiler jobs; "
            f"make -j{per_slice_jobs} per slice for {args.slices} slices. "
            f"CPU={cpus}, load1={load_1m:.2f}, MemAvailable={available if available is not None else 'unknown'} bytes."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
