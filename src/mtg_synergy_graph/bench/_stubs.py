"""Placeholder subcommand implementations.

Each Unit 2-8 replaces its stub with a real implementation. Stubs raise
``NotImplementedError`` rather than ``pass`` so skipped coverage is
impossible to miss at runtime; the CLI skeleton test exercises these
stubs to verify argparse dispatch works end-to-end before downstream
units land.
"""

from __future__ import annotations

import argparse
from typing import Never


def _unimplemented(subcommand: str) -> Never:
    raise NotImplementedError(f"{subcommand!r} is a Unit 1 stub; will be implemented in a later unit.")


def audit_stub(args: argparse.Namespace) -> int:
    _unimplemented("bench.py audit")


def repin_stub(args: argparse.Namespace) -> int:
    _unimplemented("bench.py audit --repin")


def expect_identity_stub(args: argparse.Namespace) -> int:
    _unimplemented("bench.py audit --expect-identity")


def inspect_stub(args: argparse.Namespace) -> int:
    _unimplemented("bench.py audit --inspect")


def rule_stub(args: argparse.Namespace) -> int:
    _unimplemented("bench.py audit --rule")


def collinearity_stub(args: argparse.Namespace) -> int:
    _unimplemented("bench.py audit --collinearity")


def unknowns_stub(args: argparse.Namespace) -> int:
    _unimplemented("bench.py audit --unknowns")


def inspect_gems_stub(args: argparse.Namespace) -> int:
    _unimplemented("bench.py audit --inspect-gems")


def trend_stub(args: argparse.Namespace) -> int:
    _unimplemented("bench.py audit --trend")
