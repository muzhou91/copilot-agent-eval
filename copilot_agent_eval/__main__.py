"""CLI entry point: python -m copilot_agent_eval"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .config import AppConfig
from .models import TestCase, load_cases
from .runner import TestRunner


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="copilot_agent_eval",
        description="Batch automated testing for a Copilot Studio agent embedded "
                    "via Direct Line (with Entra ID manual-auth sign-in).",
    )
    p.add_argument("-c", "--config", default="config.yaml",
                   help="Path to YAML config (default: config.yaml)")
    p.add_argument("-t", "--cases", default="cases.csv",
                   help="Path to CSV test cases (default: cases.csv)")
    p.add_argument("--priority", default=None,
                   help="Only run cases with this priority (e.g. P1)")
    p.add_argument("--ids", default=None,
                   help="Comma-separated case IDs to run (overrides --priority)")
    p.add_argument("--auth-only", action="store_true",
                   help="Only verify Direct Line + Entra ID sign-in, then exit")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="Enable debug logging")
    return p


def _filter_cases(cases: list[TestCase], args) -> list[TestCase]:
    if args.ids:
        wanted = {x.strip() for x in args.ids.split(",") if x.strip()}
        cases = [c for c in cases if c.case_id in wanted]
    elif args.priority:
        cases = [c for c in cases if c.priority.upper() == args.priority.upper()]
    return cases


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger("copilot_agent_eval")

    if not Path(args.config).is_file():
        log.error("Config not found: %s (copy config.example.yaml to config.yaml)", args.config)
        return 2
    if not Path(args.cases).is_file():
        log.error("Cases file not found: %s", args.cases)
        return 2

    config = AppConfig.load(args.config)
    cases = _filter_cases(load_cases(args.cases), args)
    if not cases:
        log.error("No test cases matched the filters.")
        return 2

    runner = TestRunner(config)

    if args.auth_only:
        from .runner import ConversationSession
        log.info("Auth-only check: starting one conversation and signing in...")
        session = runner._new_session()  # noqa: SLF001
        try:
            session.start()
            log.info("SUCCESS: conversation %s is authenticated.", session.conversation_id)
        finally:
            session.close()
        return 0

    results = runner.run(cases)
    failed = [r for r in results if r.status != "PASS"]
    log.info("Summary: %d total, %d passed, %d not passed.",
             len(results), len(results) - len(failed), len(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
