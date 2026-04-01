"""
EMCD P2P Repricer — main entry point.

Usage:
    python emcd_repricer.py [--config config.yaml]

Each account defined in the config file is run concurrently via
``asyncio.gather``.  Logging goes to stdout with timestamps.
"""

import asyncio
import logging
import argparse

from config import load_config
from repricer_logic import run_account


def _setup_logging() -> None:
    """Configure root logger to write timestamped records to stdout."""
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )


async def main() -> None:
    """Parse CLI arguments, load config, and launch all account runners."""
    parser = argparse.ArgumentParser(description="EMCD P2P Repricer")
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to YAML configuration file (default: config.yaml)",
    )
    args = parser.parse_args()

    cfg = load_config(args.config)

    if cfg.dry_run:
        logging.getLogger().warning("DRY RUN mode — prices will NOT be changed.")

    await asyncio.gather(*[run_account(acc, cfg) for acc in cfg.accounts])


if __name__ == "__main__":
    _setup_logging()
    asyncio.run(main())
