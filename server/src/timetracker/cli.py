"""Configuration initialization and read-only verification commands."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Sequence

from timetracker.config import ConfigurationError, config_path, init_config, load_config
from timetracker.service.errors import RecoveryError
from timetracker.storage import StorageError
from timetracker.verification import VerificationError, verify


def _settings_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", help="configuration file path")
    parser.add_argument("--data-dir")
    parser.add_argument("--bind-address")
    parser.add_argument("--port", type=int)
    parser.add_argument("--deep-check", choices=("startup", "interval", "manual"))
    parser.add_argument("--deep-check-days", type=int)
    parser.add_argument("--deep-check-events", type=int)
    parser.add_argument("--verify-journal-on-startup", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--verify-cross-check-on-startup", action=argparse.BooleanOptionalAction, default=None)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="timetracker")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init-config", help="create a default configuration")
    init.add_argument("--config")
    check = commands.add_parser("verify", help="verify without repairing")
    _settings_arguments(check)
    check.add_argument("--deep", action="store_true", help="compare every record field")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        if args.command == "init-config":
            path = config_path(os.environ, args.config)
            init_config(path)
            print(f"Created {path}")
            return 0
        overrides = {
            key: value for key, value in vars(args).items()
            if key not in {"command", "config", "deep"} and value is not None
        }
        settings = load_config(config=args.config, overrides=overrides)
        findings = verify(settings, deep=args.deep)
        if findings:
            for finding in findings:
                print(f"{finding.entry_id}: {finding.kind.value}: {finding.detail}", file=sys.stderr)
            return 1
        print("Verification passed" + (" (deep)" if args.deep else ""))
        return 0
    except (ConfigurationError, VerificationError, RecoveryError, StorageError, OSError) as exc:
        print(f"timetracker: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
