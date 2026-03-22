"""
NEVAREP — CLI entry point.

Usage:
    python run.py setup       → Initial DB setup + trading calendar
    python run.py backfill    → Full historical data collection
    python run.py daily       → Run daily pipeline (all 4 jobs)
    python run.py predict     → Generate today's prediction
    python run.py backtest    → Walk-forward backtest
    python run.py quality     → Data quality checks
"""

import argparse
import sys


def main():
    parser = argparse.ArgumentParser(
        description="NEVAREP — Indian Financial Intelligence Engine",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("command", choices=[
        "setup", "backfill", "daily", "predict", "backtest", "quality",
    ], help="Command to run")
    parser.add_argument("--start", type=str, help="Start date YYYY-MM-DD")
    parser.add_argument("--end", type=str, help="End date YYYY-MM-DD")
    parser.add_argument("--sprint", type=int, help="Sprint number (1-6) for backfill")

    args = parser.parse_args()

    if args.command == "setup":
        print("Running initial setup...")
        import setup
        # setup.py runs on import

    elif args.command == "backfill":
        if args.sprint:
            print(f"Running Sprint {args.sprint} backfill...")
            if args.sprint == 1:
                from scrapers.global_fetcher import run_backfill
                run_backfill()
            elif args.sprint == 2:
                print("Sprint 2: NSE F&O bhav copy — coming soon")
            elif args.sprint == 3:
                print("Sprint 3: NSE participant OI — coming soon")
            elif args.sprint == 4:
                print("Sprint 4: FII/DII cash flows — coming soon")
            elif args.sprint == 5:
                print("Sprint 5: News articles — coming soon")
            else:
                print(f"Unknown sprint: {args.sprint}")
        else:
            print("Running full backfill (all sprints)...")
            print("Specify --sprint 1 through 6 to run individually")

    elif args.command == "daily":
        print("Running daily pipeline...")
        # TODO: pipeline/orchestrator.py

    elif args.command == "predict":
        print("Generating today's prediction...")
        # TODO: engine/predictor.py

    elif args.command == "backtest":
        print("Running walk-forward backtest...")
        # TODO: backtest/backtester.py

    elif args.command == "quality":
        print("Running data quality checks...")
        # TODO: pipeline/quality_monitor.py


if __name__ == "__main__":
    main()
