"""End-to-end engine tests with a DuckDB source and a DuckDB target: no servers needed, so they
run everywhere (every Python version in CI). The integration suite covers Oracle, PostgreSQL
and a real GizmoSQL server."""

import hashlib
import io
import json

import duckdb
import pytest
from click.testing import CliRunner

from adbc_replicate.cli import main
from adbc_replicate.config import parse_config
from adbc_replicate.engine import EXIT_FAILED, EXIT_OK, EXIT_VIEW_ERRORS, Replicator, RunOptions
from adbc_replicate.events import Reporter
from adbc_replicate.planner import PlanError


@pytest.fixture
def paths(tmp_path):
    src = tmp_path / "source.duckdb"
    con = duckdb.connect(database=str(src))
    con.execute(query="""
        CREATE SCHEMA hr;
        CREATE SCHEMA staging;
        CREATE TABLE hr.dept (id INTEGER, name VARCHAR);
        INSERT INTO hr.dept VALUES (1, 'Engineering'), (2, 'Sales');
        CREATE TABLE hr.employees (ID INTEGER, Name VARCHAR, SSN VARCHAR, salary DECIMAL(12,2), notes VARCHAR,
                                   dept_id INTEGER, loaded_at TIMESTAMP);
        INSERT INTO hr.employees SELECT i, 'emp' || i, '123-45-' || lpad(i::VARCHAR, 4, '0'), i * 1000.5, 'n',
                                        1 + i % 2, TIMESTAMP '2026-01-01' FROM range(1, 501) t(i);
        CREATE TABLE hr.orders_bak (id INTEGER);
        CREATE TABLE staging.raw_orders (id INTEGER, amount DOUBLE);
        INSERT INTO staging.raw_orders VALUES (1, 10.0), (2, 20.0);
        CREATE VIEW hr.v_emp AS SELECT e.id, e.name, d.name AS dept FROM hr.employees e JOIN hr.dept d ON d.id = e.dept_id;
        CREATE VIEW hr.v_emp_count AS SELECT dept, count(*) AS n FROM hr.v_emp GROUP BY dept;
        CREATE VIEW hr.v_orders AS SELECT * FROM staging.raw_orders;
    """)
    con.close()
    return {"source": str(src), "target": str(tmp_path / "target.duckdb")}


def make_config(paths, **overrides):
    data = {
        "version": 1,
        "name": "unit",
        "source": {"type": "duckdb", "path": paths["source"]},
        "target": {"type": "duckdb", "path": paths["target"], "checkpoint": False},
        "objects": [{"db_schemas": ["hr"], "exclude_tables": ["*_bak"]}],
        "columns": [{"tables": ["hr.employees"], "exclude": ["notes"],
                     "mask": {"ssn": {"strategy": "hash", "salt": "pepper"}},
                     "fingerprint_ignore": ["loaded_at"]}],
    }
    data.update(overrides)
    return parse_config(data=data)


def run(config, **options):
    out = io.StringIO()
    summary = Replicator(config=config, reporter=Reporter(fmt="text", stream=out),
                         options=RunOptions(**options)).run()
    return summary, out.getvalue()


def target_query(paths, sql):
    con = duckdb.connect(database=paths["target"], read_only=True)
    try:
        return con.execute(query=sql).fetchall()
    finally:
        con.close()


def source_exec(paths, sql):
    con = duckdb.connect(database=paths["source"])
    try:
        con.execute(query=sql)
    finally:
        con.close()


def test_full_run_copies_masks_and_creates_views(paths):
    summary, out = run(config=make_config(paths=paths))
    assert summary.exit_code == EXIT_VIEW_ERRORS, out          # v_orders reads a db_schema that isn't copied
    assert summary.stats["tables_copied"] == 2 and summary.stats["rows_copied"] == 502
    assert summary.stats["views_created"] == 2 and summary.stats["views_failed"] == 1   # v_orders: staging not copied
    cols = [r[0] for r in target_query(paths=paths, sql="DESCRIBE hr.employees")]
    assert cols == ["id", "name", "ssn", "salary", "dept_id", "loaded_at"]          # notes excluded, lower-cased
    ssn = target_query(paths=paths, sql="SELECT ssn FROM hr.employees WHERE id = 7")[0][0]
    assert ssn == hashlib.sha256(b"pepper123-45-0007").hexdigest()                  # masked in the source
    assert target_query(paths=paths, sql="SELECT count(*) FROM hr.v_emp")[0][0] == 500
    assert sorted(target_query(paths=paths, sql="SELECT * FROM hr.v_emp_count")) == [("Engineering", 250), ("Sales", 250)]
    assert target_query(paths=paths, sql="SELECT count(*) FROM duckdb_tables() WHERE table_name = 'orders_bak'")[0][0] == 0
    assert "📦" in out and "✅" in out


def test_second_run_skips_unchanged_and_copies_changed(paths):
    run(config=make_config(paths=paths))
    summary, out = run(config=make_config(paths=paths))
    assert summary.stats["tables_copied"] == 0 and summary.stats["tables_skipped"] == 2, out
    assert summary.stats["views_created"] == 0 and summary.stats["views_skipped"] == 2

    source_exec(paths=paths, sql="UPDATE hr.employees SET loaded_at = now()")      # ignored column only
    summary, _ = run(config=make_config(paths=paths))
    assert summary.stats["tables_copied"] == 0

    source_exec(paths=paths, sql="UPDATE hr.dept SET name = 'Eng' WHERE id = 1")
    summary, out = run(config=make_config(paths=paths))
    assert summary.stats["tables_copied"] == 1 and "hr.dept" in out and "content changed" in out
    assert target_query(paths=paths, sql="SELECT name FROM hr.dept WHERE id = 1")[0][0] == "Eng"


def test_column_rule_change_forces_recopy(paths):
    run(config=make_config(paths=paths))
    config = make_config(paths=paths, columns=[{"tables": ["hr.employees"], "exclude": ["notes", "salary"],
                                                "mask": {"ssn": "null"}}])
    _, out = run(config=config)
    assert "column selection changed" in out
    assert target_query(paths=paths, sql="SELECT count(*) FROM hr.employees WHERE ssn IS NOT NULL")[0][0] == 0


def test_row_limit_sample_then_full_copy(paths):
    run(config=make_config(paths=paths), row_limit=10)
    assert target_query(paths=paths, sql="SELECT count(*) FROM hr.employees")[0][0] == 10
    _, out = run(config=make_config(paths=paths))
    assert target_query(paths=paths, sql="SELECT count(*) FROM hr.employees")[0][0] == 500, out
    assert "no sync state" in out


def test_unmatched_mask_rule_fails_the_plan(paths):
    config = make_config(paths=paths, columns=[{"tables": ["hr.employees"], "mask": {"snn": "hash"}}])
    with pytest.raises(PlanError, match="snn"):
        run(config=config)


def test_unmatched_rule_can_be_downgraded_to_a_warning(paths):
    config = make_config(paths=paths, columns=[{"tables": ["hr.employees"], "mask": {"snn": "hash"}}],
                         fail_on_unmatched_column_rules=False)
    summary, out = run(config=config)
    assert "snn" in out and summary.status != "failed"


def test_remapped_table_replaces_view_and_views_are_repointed(paths):
    config = make_config(paths=paths, objects=[
        {"db_schemas": ["staging"], "tables": ["raw_orders"], "views": [], "target_db_schema": "hr"},
        {"db_schemas": ["hr"], "exclude_tables": ["*_bak"]},
    ])
    summary, out = run(config=config)
    assert summary.exit_code == EXIT_OK, out
    assert target_query(paths=paths, sql="SELECT sum(amount) FROM hr.raw_orders")[0][0] == 30.0
    assert target_query(paths=paths, sql="SELECT sum(amount) FROM hr.v_orders")[0][0] == 30.0   # re-pointed


def test_view_failure_can_fall_back_to_materialization(paths):
    summary, out = run(config=make_config(paths=paths))
    assert summary.exit_code == EXIT_VIEW_ERRORS and summary.status == "completed_with_view_errors"
    assert "hr.v_orders" in summary.view_failures
    config = make_config(paths=paths, views={"on_failure": "materialize"})
    summary, out = run(config=config)
    assert summary.exit_code == EXIT_OK, out
    assert target_query(paths=paths, sql="SELECT count(*) FROM hr.v_orders")[0][0] == 2
    assert target_query(paths=paths, sql="SELECT count(*) FROM duckdb_tables() WHERE table_name = 'v_orders'")[0][0] == 1


def test_view_override_file(paths, tmp_path):
    overrides = tmp_path / "overrides"
    overrides.mkdir()
    (overrides / "HR.V_ORDERS.sql").write_text("SELECT 42 AS answer")
    summary, out = run(config=make_config(paths=paths, views={"overrides_dir": str(overrides)}))
    assert summary.exit_code == EXIT_OK, out
    assert target_query(paths=paths, sql="SELECT answer FROM hr.v_orders")[0][0] == 42


def test_parallel_run_matches_serial(paths):
    summary, out = run(config=make_config(paths=paths, parallelism=3))
    assert summary.stats["tables_copied"] == 2, out
    con = duckdb.connect(database=paths["source"], read_only=True)
    expected = con.execute(query="SELECT count(*), sum(salary) FROM hr.employees").fetchall()
    con.close()
    assert target_query(paths=paths, sql="SELECT count(*), sum(salary) FROM hr.employees") == expected


def test_table_failure_aborts_and_skips_views(paths):
    config = make_config(paths=paths, columns=[{"tables": ["hr.employees"],
                                                "mask": {"name": {"strategy": "expression", "sql": "CAST({column} AS INTEGER)"}}}])
    summary, out = run(config=config)
    assert summary.exit_code == EXIT_FAILED and summary.status == "failed"
    assert "hr.employees" in summary.table_failures
    assert "Views skipped" in out
    log = target_query(paths=paths, sql="SELECT status, tables_failed FROM _adbc_replicate.run_log")
    assert log == [("failed", 1)]


def test_only_filter(paths):
    summary, _ = run(config=make_config(paths=paths), only=["hr.dept"])
    assert summary.stats["tables_total"] == 1 and summary.stats["views_total"] == 0


def test_json_log_format_is_one_object_per_line(paths):
    out = io.StringIO()
    Replicator(config=make_config(paths=paths), reporter=Reporter(fmt="json", stream=out)).run()
    events = [json.loads(line) for line in out.getvalue().splitlines()]
    kinds = [e["event"] for e in events]
    assert kinds[0] == "run_started" and kinds[-1] == "run_finished"
    assert {"table_copied", "view_created", "view_failed"} <= set(kinds)


def test_cli_validate_plan_and_run(paths, tmp_path):
    import yaml
    cfg = tmp_path / "replicate.yaml"
    cfg.write_text(yaml.safe_dump(make_config(paths=paths).model_dump(mode="json", exclude_none=True)))
    runner = CliRunner()
    result = runner.invoke(main, ["validate", "--config", str(cfg)])
    assert result.exit_code == 0 and "valid" in result.output
    result = runner.invoke(main, ["plan", "--config", str(cfg), "--format", "json"])
    assert result.exit_code == 0, result.output
    plan = json.loads(result.output)
    assert {t["target"] for t in plan["tables"]} == {"hr.dept", "hr.employees"}
    assert {v["source"]: v["status"] for v in plan["views"]}["hr.v_emp"] == "translates"
    result = runner.invoke(main, ["run", "--config", str(cfg), "--skip-views"])
    assert result.exit_code == 0, result.output
    result = runner.invoke(main, ["run", "--config", str(cfg)])
    assert result.exit_code == EXIT_VIEW_ERRORS, result.output       # v_orders can't be created


def test_cli_init_writes_a_valid_example(tmp_path, monkeypatch):
    for var in ("ORACLE_USERNAME", "ORACLE_PASSWORD", "GIZMOSQL_USERNAME", "GIZMOSQL_PASSWORD"):
        monkeypatch.setenv(var, "x")
    path = tmp_path / "replicate.yaml"
    runner = CliRunner()
    assert runner.invoke(main, ["init", str(path)]).exit_code == 0
    result = runner.invoke(main, ["validate", "--config", str(path)])
    assert result.exit_code == 0, result.output
    assert runner.invoke(main, ["validate", "-c", str(path)]).exit_code == 0     # short alias still works


def test_catalog_named_like_a_db_schema(paths, tmp_path):
    # Target file hr.duckdb -> catalog "hr", which is also a target db_schema: "hr"."dept" would be
    # ambiguous to DuckDB, so DDL and translated views must qualify with the catalog.
    paths = {**paths, "target": str(tmp_path / "hr.duckdb")}
    summary, out = run(config=make_config(paths=paths), only=["hr.dept", "hr.employees", "hr.v_emp*"])
    assert summary.exit_code == EXIT_OK, out
    assert target_query(paths=paths, sql="SELECT count(*) FROM hr.hr.v_emp")[0][0] == 500
    summary, out = run(config=make_config(paths=paths), only=["hr.dept"], full_refresh=True)
    assert summary.exit_code == EXIT_OK, out


def test_reporter_survives_a_closed_pipe():
    class Closed(io.StringIO):
        def write(self, text):
            raise BrokenPipeError

    reporter = Reporter(fmt="text", stream=Closed())
    reporter.emit(event="x", text="hello")
    reporter.emit(event="y", text="still fine")


DRAINING = "IO: [GizmoSQL] [FlightSQL] the connection is draining (Unavailable; ExecuteIngest). Vendor code: 14"


def test_transient_ingest_error_is_retried(paths, monkeypatch):
    from adbc_replicate.targets.duckdb import DuckDBFamilyTarget
    real_ingest = DuckDBFamilyTarget.ingest
    failures = {"hr.employees": 1}

    def flaky_ingest(self, conn, db_schema, table, reader):
        if failures.get(f"{db_schema}.{table}", 0):
            failures[f"{db_schema}.{table}"] -= 1
            reader.read_all()                      # the upload happened, then the proxy dropped it
            raise RuntimeError(DRAINING)
        return real_ingest(self, conn=conn, db_schema=db_schema, table=table, reader=reader)

    monkeypatch.setattr(DuckDBFamilyTarget, "ingest", flaky_ingest)
    summary, out = run(config=make_config(paths=paths, ingest={"retry_wait_seconds": 0}), skip_views=True)
    assert summary.exit_code == EXIT_OK, out
    assert "🔁 hr.employees" in out and "attempt 2 of 3" in out
    assert target_query(paths=paths, sql="SELECT count(*) FROM hr.employees")[0][0] == 500


def test_non_transient_ingest_error_is_not_retried(paths, monkeypatch):
    from adbc_replicate.targets.duckdb import DuckDBFamilyTarget
    calls = []

    def broken_ingest(self, conn, db_schema, table, reader):
        calls.append(table)
        raise RuntimeError("413 Request Entity Too Large (Unknown; ExecuteIngest)")

    monkeypatch.setattr(DuckDBFamilyTarget, "ingest", broken_ingest)
    summary, out = run(config=make_config(paths=paths, ingest={"retry_wait_seconds": 0}), skip_views=True)
    assert summary.exit_code == EXIT_FAILED and "🔁" not in out
    assert len(calls) == 1                         # aborted after the first table, never retried


def test_statement_retried_only_when_it_never_ran(monkeypatch):
    from adbc_replicate.targets import duckdb as target_mod
    monkeypatch.setattr(target_mod.time, "sleep", lambda s: None)

    class FakeCursor:
        def __init__(self, errors):
            self.errors = errors
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def execute(self, operation, parameters=None):
            if self.errors:
                raise RuntimeError(self.errors.pop(0))
        def fetchall(self):
            return [(1,)]

    class FakeConn:
        def __init__(self, errors):
            self.errors = list(errors)
        def cursor(self):
            return FakeCursor(errors=self.errors)

    prepare = "the connection is draining (Unavailable; Prepare). Vendor code: 14"
    assert target_mod.DuckDBFamilyTarget.query(conn=FakeConn(errors=[prepare, prepare]), sql="SELECT 1") == [(1,)]
    executed = "the connection is draining (Unavailable; ExecuteUpdate). Vendor code: 14"
    with pytest.raises(RuntimeError, match="ExecuteUpdate"):     # may have run: never resent
        target_mod.DuckDBFamilyTarget.execute(conn=FakeConn(errors=[executed]), sql="INSERT ...")


def test_rename_columns(paths):
    config = make_config(paths=paths, columns=[
        {"tables": ["hr.employees"], "exclude": ["notes"], "mask": {"ssn": {"strategy": "hash", "salt": "pepper"}},
         "rename": {"NAME": "employee_name", "ssn": "SSN_Hash"}}])
    summary, out = run(config=config, skip_views=True)
    assert summary.exit_code == EXIT_OK, out
    cols = [r[0] for r in target_query(paths=paths, sql="DESCRIBE hr.employees")]
    assert "employee_name" in cols and "SSN_Hash" in cols and "name" not in cols and "ssn" not in cols
    # the mask still applies to the renamed column
    assert target_query(paths=paths, sql='SELECT count(*) FROM hr.employees WHERE "SSN_Hash" LIKE \'123-%\'')[0][0] == 0


def test_rename_of_a_missing_column_fails_the_plan(paths):
    config = make_config(paths=paths, columns=[{"tables": ["hr.employees"], "rename": {"NAEM": "x"}}])
    with pytest.raises(PlanError, match=r"columns\[0\]\.rename pattern 'NAEM'"):
        run(config=config)


@pytest.fixture
def webhook():
    """A local HTTP server standing in for Slack: records each POSTed JSON body; status via .status."""
    import http.server
    import threading
    import typing

    class Hook:
        status = 200
        bodies: typing.ClassVar[list] = []
        paths: typing.ClassVar[list] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            Hook.bodies.append(json.loads(self.rfile.read(length)))
            Hook.paths.append(self.path)
            self.send_response(Hook.status)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            pass

    Hook.bodies, Hook.paths = [], []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    Hook.url = f"http://127.0.0.1:{server.server_address[1]}/services/T000/B000/SECRET-TOKEN"
    yield Hook
    server.shutdown()


def test_failure_notifies_slack_with_the_failed_tables(paths, webhook, monkeypatch):
    from adbc_replicate.targets.duckdb import DuckDBFamilyTarget

    def broken_ingest(self, conn, db_schema, table, reader):
        raise RuntimeError("disk full")

    monkeypatch.setattr(DuckDBFamilyTarget, "ingest", broken_ingest)
    config = make_config(paths=paths, name="nightly-hr", notifications=[{"url": webhook.url}])
    summary, out = run(config=config, skip_views=True)
    assert summary.exit_code == EXIT_FAILED
    assert len(webhook.bodies) == 1
    body = webhook.bodies[0]
    assert body["text"] == "❌ nightly-hr: replication FAILED"
    text = json.dumps(body["blocks"])
    assert "Table failures (1)" in text and "disk full" in text and "1 failed" in text
    assert "📣 Notified 127.0.0.1" in out and "SECRET-TOKEN" not in out


def test_when_selects_outcomes_and_json_format_carries_the_summary(paths, webhook):
    config = make_config(paths=paths, name="nightly-hr", notifications=[
        {"url": webhook.url},                                                    # default: failure, view_errors
        {"url": webhook.url + "/json", "when": ["success", "failure"], "format": "json"}])
    summary, _ = run(config=config, skip_views=True)
    assert summary.exit_code == EXIT_OK
    assert webhook.paths == ["/services/T000/B000/SECRET-TOKEN/json"]           # only the success subscriber
    body = webhook.bodies[0]
    assert body["outcome"] == "success" and body["exit_code"] == 0 and body["replication"] == "nightly-hr"
    assert body["stats"]["tables_copied"] == 2 and body["run_id"] == summary.run_id


def test_view_errors_and_plan_errors_notify(paths, webhook):
    summary, _ = run(config=make_config(paths=paths, notifications=[{"url": webhook.url, "format": "json"}]))
    assert summary.exit_code == EXIT_VIEW_ERRORS and webhook.bodies[-1]["outcome"] == "view_errors"
    assert "hr.v_orders" in webhook.bodies[-1]["view_failures"]
    bad = make_config(paths=paths, columns=[{"tables": ["hr.employees"], "mask": {"SNN": "hash"}}],
                      notifications=[{"url": webhook.url, "format": "json"}])
    with pytest.raises(PlanError):
        run(config=bad)
    assert webhook.bodies[-1]["outcome"] == "failure" and webhook.bodies[-1]["exit_code"] == 2
    assert "SNN" in webhook.bodies[-1]["error"]


def test_connection_failure_still_notifies(paths, webhook, tmp_path):
    config = make_config(paths={**paths, "target": str(tmp_path / "missing-dir" / "t.duckdb")},
                         notifications=[{"url": webhook.url, "format": "json"}])
    with pytest.raises(Exception):  # noqa: B017 - any connection error
        run(config=config)
    assert webhook.bodies[-1]["outcome"] == "failure" and webhook.bodies[-1]["error"]


def test_a_broken_webhook_never_fails_the_run(paths, webhook, monkeypatch):
    import adbc_replicate.notify as notify
    monkeypatch.setattr(notify.time, "sleep", lambda s: None)
    webhook.status = 500
    summary, out = run(config=make_config(paths=paths, notifications=[{"url": webhook.url, "when": ["success"]}]),
                       skip_views=True)
    assert summary.exit_code == EXIT_OK
    assert len(webhook.bodies) == 2                                              # one retry on a 5xx
    assert "Notification to 127.0.0.1" in out and "HTTP 500" in out and "SECRET-TOKEN" not in out


def test_cli_test_notifications(paths, webhook, tmp_path):
    import yaml
    cfg = tmp_path / "replicate.yaml"
    data = make_config(paths=paths, name="nightly-hr", notifications=[{"url": webhook.url}]).model_dump(
        mode="json", exclude_none=True)
    cfg.write_text(yaml.safe_dump(data), encoding="utf-8")
    result = CliRunner().invoke(main, ["test-notifications", "--config", str(cfg)])
    assert result.exit_code == 0, result.output
    assert webhook.bodies[-1]["text"] == "🧪 nightly-hr: test notification"
