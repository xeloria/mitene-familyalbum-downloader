"""CLI parsing, config precedence, and output-mode tests."""

from __future__ import annotations

import io
import json
import sys
from datetime import date

import pytest

from mitene_download import cli
from mitene_download.album import Summary


def parse(config=None, argv=()):
    config = config or {}
    return cli.build_parser(config).parse_args(list(argv))


def test_config_defaults_are_used():
    args = parse({"dest": "archive", "concurrency": 7, "media_type": "videos"})
    assert args.dest == "archive"
    assert args.concurrency == 7
    assert args.media_type == "videos"


def test_cli_overrides_config():
    args = parse({"dest": "archive"}, ["--dest", "other"])
    assert args.dest == "other"


def test_watch_boolean_in_config_is_coerced():
    args = parse({"watch": True}, [])
    assert args.watch == 300
    args = parse({"watch": False}, [])
    assert args.watch is None
    assert parse({}, ["--watch"]).watch == 300
    assert parse({}, ["--watch", "60"]).watch == 60


def test_config_path_from_argv():
    assert cli._config_path_from_argv([]) == cli.Path("config.json")
    assert cli._config_path_from_argv(["--config", "x.json"]) == cli.Path("x.json")
    assert (
        cli._config_path_from_argv(["--config=y.json"]) == cli.Path("y.json")
    )


def test_parse_date():
    assert cli._parse_date("2026-07-31", "--start-date") == date(2026, 7, 31)
    assert cli._parse_date(None, "--start-date") is None
    with pytest.raises(SystemExit):
        cli._parse_date("31/07/2026", "--start-date")


def test_options_from_args_applies_floors():
    args = parse({}, ["--concurrency", "0", "--max-retries", "-2", "--timeout", "0"])
    options = cli.options_from_args(args, {})
    assert options.concurrency == 1
    assert options.max_retries == 1
    assert options.timeout == 1


def test_incremental_and_exiftool_flags():
    args = parse({}, ["--incremental", "--exiftool"])
    options = cli.options_from_args(args, {})
    assert options.incremental is True
    assert options.use_exiftool is True

    args = parse({"incremental": True, "use_exiftool": True})
    options = cli.options_from_args(args, {"incremental": True, "use_exiftool": True})
    assert options.incremental is True
    assert options.use_exiftool is True


def test_incremental_is_rejected_with_dry_run():
    args = parse({}, ["--incremental", "--dry-run"])
    with pytest.raises(SystemExit):
        cli.asyncio.run(cli._run(args, cli.options_from_args(args, {})))


def test_password_stdin_strips_crlf(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("hunter2\r\n"))
    args = parse({}, ["--password-stdin"])
    options = cli.options_from_args(args, {})
    assert options.password == "hunter2"


def test_repair_is_awaitable_and_receives_db_path(tmp_path, monkeypatch):
    """The CLI must await repair_directory and pass --db through."""
    calls = {}

    async def fake_repair(destination, dry_run=False, db_path=None):
        calls["destination"] = destination
        calls["db_path"] = db_path
        return Summary(renamed=2)

    monkeypatch.setattr(cli, "repair_directory", fake_repair)
    args = parse({}, ["--repair", "--dest", str(tmp_path), "--db", "x.db"])
    summary = cli.asyncio.run(cli._run(args, cli.options_from_args(args, {})))
    assert summary.renamed == 2
    assert calls["db_path"] == "x.db"


def test_json_log_emits_a_single_json_document(monkeypatch, capsys):
    async def fake_run(args, options):
        return Summary(downloaded=2, failed=0)

    monkeypatch.setattr(cli, "_run", fake_run)
    code = cli.main(["--json-log", "--dest", "unused"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["downloaded"] == 2
    assert payload["failed"] == 0
    assert code == 0


def test_failure_sets_nonzero_exit_code(monkeypatch):
    async def fake_run(args, options):
        return Summary(failed=1, failures=["u: nope"])

    monkeypatch.setattr(cli, "_run", fake_run)
    assert cli.main(["--dest", "unused"]) == 1


def test_json_formatter_includes_traceback():
    import logging

    try:
        raise ValueError("boom")
    except ValueError:
        record = logging.LogRecord(
            "mitene_download", logging.ERROR, __file__, 1, "failed", None,
            sys.exc_info(),
        )
    payload = json.loads(cli._JsonFormatter().format(record))
    assert "ValueError: boom" in payload["exception"]
