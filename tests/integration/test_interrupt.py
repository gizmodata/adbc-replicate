"""SIGTERM mid-load (what Kubernetes sends on eviction) must cancel every in-flight load and leave no
GizmoSQL session holding a transaction. Found on a real run: a killed process left sessions whose open
transactions blocked the next run's replace of the same tables ("Catalog write-write conflict")."""

import os
import signal
import subprocess
import sys
import time

import pytest
import yaml
from conftest import execute, make_config, postgres_source, query, run

from adbc_replicate.engine import EXIT_OK

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def bulk(postgres):
    # Two loads that stay in flight for ~6 s while moving only kilobytes: views (copied as tables)
    # that sleep per row (~2 ms each). (Big tables did the same, but OOM-killed the test server.)
    slow = "SELECT i AS id, md5(i::text) AS payload FROM generate_series(1, 3000) i, LATERAL (SELECT pg_sleep(0.0005 + 0 * i)) s"
    execute(conn=postgres, statements=[
        "DROP SCHEMA IF EXISTS bulk CASCADE",
        "CREATE SCHEMA bulk",
        f"CREATE VIEW bulk.big_a AS {slow}",
        f"CREATE VIEW bulk.big_b AS {slow}",
    ])
    yield "bulk"
    execute(conn=postgres, statements=["DROP SCHEMA IF EXISTS bulk CASCADE"])


def test_sigterm_cancels_in_flight_loads_and_leaves_no_locks(bulk, gizmosql, prefix, tmp_path):
    config = make_config(prefix=prefix, source=postgres_source(), parallelism=2,
                         objects=[{"db_schemas": [bulk], "materialize_views": ["*"], "target_db_schema": f"{prefix}_bulk"}])
    path = tmp_path / "replicate.yaml"
    path.write_text(yaml.safe_dump(config.model_dump(mode="json", exclude_none=True)), encoding="utf-8")
    process = subprocess.Popen([sys.executable, "-m", "adbc_replicate.cli", "run", "--config", str(path)],
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                               env={**os.environ, "PYTHONUNBUFFERED": "1"})
    output, copying = [], 0
    deadline = time.monotonic() + 120
    while copying < 2 and time.monotonic() < deadline:     # wait until both workers are loading
        line = process.stdout.readline()
        output.append(line)
        copying += "📦 Copying bulk.big_" in line
    assert copying == 2, "".join(output)
    time.sleep(1.0)
    process.send_signal(signal.SIGTERM)
    rest, _ = process.communicate(timeout=120)
    text = "".join(output) + rest
    assert process.returncode == 128 + signal.SIGTERM, text
    assert "🛑 SIGTERM: cancelling" in text and "Run interrupted" in text, text
    # both loads were cancelled mid-flight (not finished before the signal arrived)
    assert "❌ bulk.big_a" in text and "❌ bulk.big_b" in text, text
    assert query(conn=gizmosql, sql=f"SELECT status FROM {prefix}_state.run_log")[0][0] == "interrupted"

    # The proof that no session kept a transaction: a new run replaces the same tables at once.
    summary, out = run(config=config)
    assert summary.exit_code == EXIT_OK, out
    assert summary.stats["tables_copied"] == 2, out
    assert query(conn=gizmosql, sql=f"SELECT count(*) FROM {prefix}_bulk.big_a")[0][0] == 3_000
