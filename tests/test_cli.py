"""Tests for the CLI argument parser (``harbor/_parse_args``).

The parser falls back to ``serve`` when no subcommand is given, but a known
subcommand wins when it appears as the first non-option token.  A leading
``--`` escapes paths that would otherwise collide with a subcommand name or
start with ``-``.
"""

import sys

import pytest

from harbor import __main__ as cli


def _parse(argv):
    """Run ``_parse_args`` against ``argv`` (mimicking ``sys.argv[1:]``)."""
    parser = cli._build_parser()
    old = sys.argv
    sys.argv = ["harbor", *argv]
    try:
        return cli._parse_args(parser)
    finally:
        sys.argv = old


def test_no_args_defaults_to_serve():
    ns = _parse([])
    assert ns.command == "serve"
    assert ns.roots == []


def test_paths_imply_serve():
    ns = _parse(["~/work", "~/personal"])
    assert ns.command == "serve"
    assert ns.roots == ["~/work", "~/personal"]


def test_flags_imply_serve():
    ns = _parse(["--port", "9000"])
    assert ns.command == "serve"
    assert ns.port == 9000


def test_mixed_flags_and_paths():
    ns = _parse(["--port", "9000", "~/work"])
    assert ns.command == "serve"
    assert ns.port == 9000
    assert ns.roots == ["~/work"]


def test_explicit_subcommand():
    ns = _parse(["status"])
    assert ns.command == "status"


def test_flags_before_positional_still_imply_serve():
    # Once a flag is present and the first non-option token isn't a subcommand,
    # the whole invocation is routed to ``serve``.
    ns = _parse(["--port", "9000", "status"])
    assert ns.command == "serve"
    assert ns.port == 9000
    assert ns.roots == ["status"]


def test_dir_named_like_subcommand_via_double_dash():
    # Without `--` this would be misread as the `status` subcommand, so the
    # escape hatch is what makes a directory literally named "status" usable.
    ns = _parse(["--", "status"])
    assert ns.command == "serve"
    assert ns.roots == ["status"]


def test_dash_leading_dir_via_double_dash():
    ns = _parse(["--", "-my-dir"])
    assert ns.command == "serve"
    assert ns.roots == ["-my-dir"]


def test_top_level_help_parsed_at_top():
    with pytest.raises(SystemExit) as exc:
        _parse(["--help"])
    assert exc.value.code == 0


def test_serve_subcommand_takes_paths():
    ns = _parse(["serve", "status"])
    assert ns.command == "serve"
    assert ns.roots == ["status"]
