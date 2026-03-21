"""NEVAREP CLI entry point."""

import argparse
from datetime import datetime


def main():
    parser = argparse.ArgumentParser(
        description="NEVAREP — Indian Financial Intelligence Engine",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Commands:
  backfill     Run full historical data backfill
  daily        Run daily pipeline (all 4 jobs)
  predict      Generate today's prediction
  backtest     Run walk-forward backtest
  quality      Run data quality checks
        """,
    )
    parser.add_argument("command", choices=[
        "backfill", "daily", "predict", "backtest", "quality",
    ])
    parser.add_argument("--start", type=str, help="Start date YYYY-MM-DD")
    parser.add_argument("--end", type=str, help="End date YYYY-MM-DD")
    parser.add_argument("--index", type=str, default="NIFTY",
                        help="Index: NIFTY, BANKNIFTY, SENSEX")

    args = parser.parse_args()

    if args.command == "backfill":
        print("🚀 Starting NEVAREP historical backfill...")
        print("   This will take several hours.")
        # TODO: Import and run backfill pipeline
    elif args.command == "daily":
        print("📊 Running NEVAREP daily pipeline...")
        # TODO: Import and run daily orchestrator
    elif args.command == "predict":
        print("🔮 Generating today's prediction...")
        # TODO: Import and run prediction engine
    elif args.command == "backtest":
        print("📈 Running walk-forward backtest...")
        # TODO: Import and run backtester
    elif args.command == "quality":
        print("✅ Running data quality checks...")
        # TODO: Import and run quality monitor


if __name__ == "__main__":
    main()
