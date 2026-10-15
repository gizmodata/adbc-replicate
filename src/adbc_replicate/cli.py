"""The adbc-replicate command line."""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
from contextlib import suppress
from pathlib import Path

import click

from . import __version__
from .config import ConfigError, json_schema, load_config
from .engine import EXIT_CONFIG, EXIT_FAILED, EXIT_OK, Replicator, RunOptions
from .events import Reporter, fmt_seconds
from .planner import PlanError
from .views import ViewTranslationError, create_view_sql, load_override, translate_view

CONFIG_OPTION = click.option("-c", "--config", "config_path", required=True, type=click.Path(dir_okay=False),
                             envvar="ADBC_REPLICATE_CONFIG", help="Replication config (YAML or JSON).")
ENV_FILE_OPTION = click.option("--env-file", type=click.Path(dir_okay=False), envvar="DOTENV_PATH",
                               help="Load environment variables from this file first (existing variables win).")


def _load(config_path: str, env_file: str | None):
    if env_file:
        from dotenv import load_dotenv
        if not Path(env_file).is_file():
            raise ConfigError(f"env file {env_file} not found")
        load_dotenv(dotenv_path=env_file, override=False)
    return load_config(path=config_path)


def _fail(message: str, code: int = EXIT_CONFIG) -> None:
    click.echo(f"❌ {message}", err=True)
    sys.exit(code)


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(version=__version__, prog_name="adbc-replicate")
def main() -> None:
    """🔁 adbc-replicate — stream whole schemas from one database into another over ADBC.

    Describe what to replicate in a YAML config, check it with `plan`, then `run` it.
    """


@main.command()
@CONFIG_OPTION
@ENV_FILE_OPTION
@click.option("--full-refresh", is_flag=True, help="Copy every table and recreate every view, ignoring sync state.")
@click.option("--row-limit", type=click.IntRange(min=0), default=None,
              help="Copy at most N rows per table: a quick end-to-end trial. Sampled tables are re-copied in "
                   "full by the next normal run.")
@click.option("--tables", "only", multiple=True, metavar="PATTERN",
              help="Only these DB_SCHEMA.NAME patterns (repeatable), e.g. --tables 'HR.EMP*'.")
@click.option("--parallelism", type=click.IntRange(min=1, max=64), default=None,
              help="Override the config's parallelism.")
@click.option("--skip-views", is_flag=True, help="Copy tables only.")
@click.option("--log-format", type=click.Choice(["text", "json"]), default="text", show_default=True,
              help="json: one event object per line (for log pipelines and UIs).")
@click.option("-q", "--quiet", is_flag=True, help="Text mode: only warnings, errors and the summary.")
def run(config_path, env_file, full_refresh, row_limit, only, parallelism, skip_views, log_format, quiet) -> None:
    """Run the replication."""
    try:
        config = _load(config_path=config_path, env_file=env_file)
    except ConfigError as e:
        _fail(message=str(e))
    if parallelism and parallelism > 1 and config.source.consistency == "serializable":
        _fail(message="consistency 'serializable' needs parallelism 1")
    reporter = Reporter(fmt=log_format, quiet=quiet)
    started = time.monotonic()
    replicator = Replicator(config=config, reporter=reporter,
                            options=RunOptions(full_refresh=full_refresh, row_limit=row_limit, only=list(only),
                                               parallelism=parallelism, skip_views=skip_views))
    try:
        summary, signum = _run_interruptibly(replicator=replicator, reporter=reporter)
    except PlanError as e:
        reporter.emit(event="run_failed", text=f"❌ {e}", level="error", error=str(e))
        sys.exit(EXIT_CONFIG)
    except Exception as e:
        reporter.emit(event="run_failed", text=f"💥 {type(e).__name__}: {e}", level="error",
                      error=f"{type(e).__name__}: {e}")
        sys.exit(EXIT_FAILED)
    reporter.emit(event="elapsed", text=f"⏱️  {fmt_seconds(time.monotonic() - started)}",
                  seconds=round(time.monotonic() - started, 2))
    sys.exit(128 + signum if signum else summary.exit_code)


def _run_interruptibly(replicator: Replicator, reporter: Reporter):
    """Run on a worker thread so the main thread can act on SIGINT/SIGTERM at once: cancel every
    in-flight statement (any worker), let the run roll back, record and notify, then exit 130/143.
    (Killed outright, the target's server sessions would keep their transactions open; Kubernetes
    sends SIGTERM when it evicts a pod.) A second signal exits immediately."""
    outcome: dict = {}

    def work() -> None:
        try:
            outcome["summary"] = replicator.run()
        except BaseException as e:   # re-raised on the main thread
            outcome["error"] = e

    def on_signal(signum, frame) -> None:
        name = signal.Signals(signum).name
        if "signum" in outcome:
            reporter.emit(event="interrupted", text=f"🛑 {name} again: exiting now", level="error", signal=name)
            os._exit(128 + signum)
        outcome["signum"] = signum
        reporter.emit(event="interrupted", text=f"🛑 {name}: cancelling the statements in flight, then stopping…",
                      level="warning", signal=name)
        replicator.cancel(reason=name)

    previous = {sig: signal.signal(sig, on_signal) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        worker = threading.Thread(target=work, name="adbc-replicate-run", daemon=True)
        worker.start()
        while worker.is_alive():
            worker.join(timeout=0.25)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    if "error" in outcome:
        raise outcome["error"]
    return outcome["summary"], outcome.get("signum")


@main.command()
@CONFIG_OPTION
@ENV_FILE_OPTION
@click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text", show_default=True)
@click.option("--show-sql", is_flag=True, help="Print each table's SELECT and each view's translated DDL.")
@click.option("--tables", "only", multiple=True, metavar="PATTERN", help="Only these DB_SCHEMA.NAME patterns.")
def plan(config_path, env_file, fmt, show_sql, only) -> None:
    """Show what a run would copy, without writing anything.

    Connects to the source only: lists the matching tables and views, their target names,
    excluded and masked columns, and whether each view translates.
    """
    try:
        config = _load(config_path=config_path, env_file=env_file)
    except ConfigError as e:
        _fail(message=str(e))
    replicator = Replicator(config=config, reporter=Reporter(fmt="text", quiet=True),
                            options=RunOptions(only=list(only)))
    source = replicator.source
    conn = source.connect()
    try:
        try:
            the_plan = replicator.build_plan(source_conn=conn)
        except PlanError as e:
            _fail(message=str(e))
        definitions = {}
        for catalog in sorted({v.source.catalog for v in the_plan.views}, key=lambda c: c or ""):
            in_catalog = [v.source for v in the_plan.views if v.source.catalog == catalog]
            definitions.update(source.view_definitions(conn=conn, catalog=catalog, views=in_catalog))

        tables_out = []
        for t in the_plan.tables:
            tables_out.append({
                "source": t.source.qualified_name, "source_kind": t.source.kind,
                "target": f"{t.target_db_schema}.{t.target_table}",
                "columns": len(t.columns), "excluded_columns": t.excluded_columns,
                "masked_columns": {c.column.name: c.mask.strategy for c in t.columns if c.mask},
                "fingerprint": "yes" if source.fingerprint_sql(task=t) else "no (copied every run)",
                "note": t.note,
                **({"incremental_key": t.incremental_key, "incremental_note": t.incremental_note}
                   if config.refresh.mode == "incremental" else {}),
                **({"sql": source.select_sql(task=t)} if show_sql else {}),
            })
        views_out = []
        for v in the_plan.views:
            entry = {"source": v.source.qualified_name, "target": f"{v.target_db_schema}.{v.target_view}"}
            override = load_override(overrides_dir=config.views.overrides_dir, db_schema=v.source.db_schema,
                                     view=v.source.name)
            try:
                if override is not None:
                    sql = create_view_sql(target_db_schema=v.target_db_schema, target_view=v.target_view, body=override,
                                          target_catalog=config.target.catalog)
                    entry["status"] = "override"
                else:
                    definition = definitions.get((v.source.db_schema, v.source.name))
                    if not definition:
                        raise ViewTranslationError("the source returned no definition (no privilege on it?)")
                    sql = translate_view(definition=definition, source_dialect=source.dialect,
                                         view_db_schema=v.source.db_schema, target_db_schema=v.target_db_schema,
                                         target_view=v.target_view, name_map=the_plan.name_map,
                                         case=config.naming.case, source_columns=[c.name for c in v.source.columns],
                                         target_catalog=config.target.catalog,
                                         pseudo_columns=config.views.source_pseudo_columns,
                                         time_zone=config.views.source_time_zone,
                                         functions=config.views.functions,
                                         column_renames=the_plan.column_renames,
                                         column_types=the_plan.column_types)
                    entry["status"] = "translates"
                if show_sql:
                    entry["sql"] = sql
            except Exception as e:
                entry["status"] = "fails"
                entry["error"] = str(e)
            views_out.append(entry)
    finally:
        with suppress(Exception):
            conn.close()

    result = {"replication": config.name, "source": config.source.type, "target": config.target.type,
              "tables": tables_out, "views": views_out, "warnings": the_plan.warnings}
    if fmt == "json":
        click.echo(json.dumps(result, indent=2, default=str))
        return

    title = f" for {config.name}" if config.name else ""
    click.echo(f"🗺️  Plan{title}: {config.source.type} → {config.target.type}\n")
    click.echo(f"📦 Tables ({len(tables_out)})")
    for t in tables_out:
        extras = []
        if t["note"]:
            extras.append(t["note"])
        if t["excluded_columns"]:
            extras.append("excluded: " + ", ".join(t["excluded_columns"]))
        if t["masked_columns"]:
            extras.append("masked: " + ", ".join(f"{k} ({v})" for k, v in t["masked_columns"].items()))
        if t.get("incremental_key"):
            extras.append("incremental on " + ", ".join(t["incremental_key"]))
        elif t.get("incremental_note"):
            extras.append(f"whole-table copies: {t['incremental_note']}")
        if t["fingerprint"] != "yes":
            extras.append("no fingerprint: copied every run")
        click.echo(f"  • {t['source']} → {t['target']}  [{t['columns']} columns]"
                   + (f"  ({'; '.join(extras)})" if extras else ""))
        if show_sql:
            click.echo(f"      {t['sql']}")
    click.echo(f"\n🪟 Views ({len(views_out)})")
    icons = {"translates": "✅", "override": "📝", "fails": "❌"}
    for v in views_out:
        click.echo(f"  {icons[v['status']]} {v['source']} → {v['target']}"
                   + (f"  — {v['error'].splitlines()[0]}" if v.get("error") else ""))
        if show_sql and v.get("sql"):
            click.echo("      " + v["sql"].replace("\n", "\n      "))
    if the_plan.warnings:
        click.echo(f"\n⚠️  Warnings ({len(the_plan.warnings)})")
        for w in the_plan.warnings:
            click.echo(f"  • {w}")
    failing = sum(v["status"] == "fails" for v in views_out)
    click.echo(f"\n✨ {len(tables_out)} table(s) to copy, {len(views_out) - failing} view(s) ready"
               + (f", {failing} view(s) won't translate (add an override, or views.on_failure: materialize)"
                  if failing else ""))


@main.command("test-notifications")
@CONFIG_OPTION
@ENV_FILE_OPTION
def test_notifications(config_path, env_file) -> None:
    """Send a test message to every configured webhook (whatever its `when`)."""
    import datetime

    from .notify import TEST, build_payload, deliver

    try:
        config = _load(config_path=config_path, env_file=env_file)
    except ConfigError as e:
        _fail(message=str(e))
    if not config.notifications:
        _fail(message="the config has no notifications")
    failed = []
    reporter = Reporter(fmt="text")

    def emit(event, text=None, **fields):
        if event == "notification_failed":
            failed.append(fields.get("host"))
        reporter.emit(event=event, text=text, **fields)

    payload = build_payload(outcome=TEST, exit_code=EXIT_OK, run_id=None, replication=config.name,
                            source=config.source.type, target=config.target.type,
                            catalog=getattr(config.target, "catalog", None),
                            started_at=datetime.datetime.now(tz=datetime.UTC), seconds=0.0, stats={},
                            table_failures={}, view_failures={}, error=None)
    deliver(notifications=config.notifications, payload=payload, emit=emit)
    sys.exit(EXIT_FAILED if failed else EXIT_OK)


@main.command("test-connection")
@CONFIG_OPTION
@ENV_FILE_OPTION
def test_connection(config_path, env_file) -> None:
    """Connect to the source and the target and report what each sees."""
    try:
        config = _load(config_path=config_path, env_file=env_file)
    except ConfigError as e:
        _fail(message=str(e))
    replicator = Replicator(config=config)
    ok = True
    try:
        started = time.monotonic()
        conn = replicator.source.connect()
        try:
            catalog = replicator.source.current_catalog(conn=conn)
            db_schemas = replicator.source.list_db_schemas(conn=conn, catalog=None)
        finally:
            conn.close()
        click.echo(f"✅ Source ({config.source.type}): connected in {fmt_seconds(time.monotonic() - started)}; "
                   f"catalog {catalog}, {len(db_schemas)} db_schema(s) visible")
    except Exception as e:
        ok = False
        click.echo(f"❌ Source ({config.source.type}): {type(e).__name__}: {e}")
    try:
        started = time.monotonic()
        conn = replicator.target.connect()
        try:
            version = replicator.target.query(conn=conn, sql="SELECT version()")[0][0]
        finally:
            conn.close()
        click.echo(f"✅ Target ({config.target.type}): connected in {fmt_seconds(time.monotonic() - started)}; "
                   f"catalog {replicator.target.catalog}, DuckDB {version}")
    except Exception as e:
        ok = False
        click.echo(f"❌ Target ({config.target.type}): {type(e).__name__}: {e}")
    sys.exit(EXIT_OK if ok else EXIT_FAILED)


@main.command()
@CONFIG_OPTION
@ENV_FILE_OPTION
def validate(config_path, env_file) -> None:
    """Check a config file without connecting to anything."""
    try:
        config = _load(config_path=config_path, env_file=env_file)
    except ConfigError as e:
        _fail(message=str(e))
    click.echo(f"✅ {config_path} is valid: {config.source.type} → {config.target.type}, "
               f"{len(config.objects)} object rule(s), {len(config.columns)} column rule(s)")


@main.command()
def schema() -> None:
    """Print the config's JSON Schema (for editors, CI checks and UIs)."""
    click.echo(json.dumps(json_schema(), indent=2))


EXAMPLE_CONFIG = """\
# adbc-replicate config — see https://github.com/gizmodata/adbc-replicate
version: 1
name: oracle-to-gizmosql

source:
  type: oracle
  uri: oracle://oracle.example.com:1521/ORCLPDB1
  username: ${ORACLE_USERNAME}
  password: ${ORACLE_PASSWORD}
  consistency: serializable      # every table read from one point in time

target:
  type: gizmosql
  uri: gizmosql://gizmosql.example.com:31337
  username: ${GIZMOSQL_USERNAME}
  password: ${GIZMOSQL_PASSWORD}

objects:
  - db_schemas: [HR, SALES]
    exclude_tables: ["*_BAK", "/^TMP_/"]

columns:
  - tables: [HR.EMPLOYEES]
    exclude: [SSN]
    mask:
      PHONE_NUMBER: hash
"""


@main.command()
@click.argument("path", type=click.Path(dir_okay=False), default="replicate.yaml")
def init(path) -> None:
    """Write an example config to PATH (default replicate.yaml)."""
    p = Path(path)
    if p.exists():
        _fail(message=f"{path} already exists")
    p.write_text(EXAMPLE_CONFIG, encoding="utf-8")
    click.echo(f"📝 Wrote {path}. Edit it, then: adbc-replicate plan --config {path}")


if __name__ == "__main__":
    main()
