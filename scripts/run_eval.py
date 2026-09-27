#!/usr/bin/env python3
"""Run eval dataset v1 with offline fake replay or an explicitly requested Pi run."""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from evals.runner import run_eval


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", choices=["v1"], required=True)
    parser.add_argument("--split", choices=["all", "dev", "holdout"], required=True)
    parser.add_argument("--agent", choices=["pi", "fake"], required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument("--derived-metric-render", choices=["v1", "v2"])
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    path = run_eval(split=args.split, agent=args.agent, run_id=args.run_id,
                    limit=args.limit, allow_dirty=args.allow_dirty,
                    derived_metric_render=args.derived_metric_render)
    print(path)


if __name__ == "__main__":
    main()
