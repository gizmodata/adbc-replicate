"""Snowflake (adbc-driver-snowflake, key-pair auth) → GizmoSQL.

Reads SNOWFLAKE_SAMPLE_DATA.TPCH_SF1, the sample share every Snowflake account has, so the
test user needs no grants beyond PUBLIC and the tests write nothing to Snowflake. Needs
ADBC_REPLICATE_TEST_SNOWFLAKE_ACCOUNT and ADBC_REPLICATE_TEST_SNOWFLAKE_PRIVATE_KEY (plus
_PRIVATE_KEY_PASSPHRASE for an encrypted key). Marked `snowflake` rather than `integration`
because it needs credentials: CI runs it as its own job on main pushes and release tags,
where a missing secret is a failure, never a skip.

Views and time-travel snapshots need objects the test user could create, so they're covered
by the unit tests (view translation) and by the shared-database refusal below.
"""

import os

import pytest
from conftest import make_config, query, run

from adbc_replicate.engine import EXIT_OK
from adbc_replicate.planner import PlanError

pytestmark = pytest.mark.snowflake

ACCOUNT = os.getenv("ADBC_REPLICATE_TEST_SNOWFLAKE_ACCOUNT")
PRIVATE_KEY = os.getenv("ADBC_REPLICATE_TEST_SNOWFLAKE_PRIVATE_KEY")
PASSPHRASE = os.getenv("ADBC_REPLICATE_TEST_SNOWFLAKE_PRIVATE_KEY_PASSPHRASE")
USER = os.getenv("ADBC_REPLICATE_TEST_SNOWFLAKE_USER", "ADBC_REPLICATE_CI")
ROLE = os.getenv("ADBC_REPLICATE_TEST_SNOWFLAKE_ROLE")
WAREHOUSE = os.getenv("ADBC_REPLICATE_TEST_SNOWFLAKE_WAREHOUSE", "SNOWFLAKE_LEARNING_WH")
CATALOG = "SNOWFLAKE_SAMPLE_DATA"
SMALL_TABLES = ["NATION", "REGION", "SUPPLIER", "CUSTOMER"]


def snowflake_options() -> dict:
    if not ACCOUNT or not PRIVATE_KEY:
        pytest.fail("ADBC_REPLICATE_TEST_SNOWFLAKE_ACCOUNT / _PRIVATE_KEY are not set", pytrace=False)
    options = {"username": USER, "adbc.snowflake.sql.account": ACCOUNT,
               "adbc.snowflake.sql.warehouse": WAREHOUSE, "adbc.snowflake.sql.db": CATALOG,
               "adbc.snowflake.sql.auth_type": "auth_jwt",
               "adbc.snowflake.sql.client_option.jwt_private_key_pkcs8_value": PRIVATE_KEY}
    if PASSPHRASE:
        options["adbc.snowflake.sql.client_option.jwt_private_key_pkcs8_password"] = PASSPHRASE
    if ROLE:
        options["adbc.snowflake.sql.role"] = ROLE
    return options


@pytest.fixture(scope="module")
def snowflake():
    import adbc_driver_snowflake.dbapi as sf
    conn = sf.connect(db_kwargs=snowflake_options(), autocommit=True)
    yield conn
    conn.close()


def config(prefix, tables=SMALL_TABLES, consistency="none", **extra):
    # Column rules that match nothing stop the plan, so they're only given when CUSTOMER is copied.
    columns = [{"tables": ["TPCH_SF1.CUSTOMER"], "exclude": ["C_COMMENT"], "mask": {"C_PHONE": "hash"}}] \
        if "CUSTOMER" in tables else []
    return make_config(
        prefix=prefix, source={"type": "snowflake", "consistency": consistency, "options": snowflake_options()},
        objects=[{"catalog": CATALOG, "db_schemas": ["TPCH_SF1"], "tables": tables, "target_db_schema": f"{prefix}_s"}],
        columns=columns, **extra)


def test_snowflake_replication(snowflake, gizmosql, prefix):
    summary, out = run(config=config(prefix=prefix, parallelism=2))
    assert summary.exit_code == EXIT_OK, out
    assert summary.stats["tables_copied"] == 4, out
    s = f"{prefix}_s"
    for table in SMALL_TABLES:
        expected = query(conn=snowflake, sql=f"SELECT COUNT(*) FROM {CATALOG}.TPCH_SF1.{table}")[0][0]
        assert query(conn=gizmosql, sql=f"SELECT count(*) FROM {s}.{table.lower()}")[0][0] == int(expected), table

    # NUMBER(12,2) arrives as an exact decimal, not a float
    source_sum = query(conn=snowflake, sql=f"SELECT SUM(C_ACCTBAL) FROM {CATALOG}.TPCH_SF1.CUSTOMER")[0][0]
    assert query(conn=gizmosql, sql=f"SELECT sum(c_acctbal) FROM {s}.customer")[0][0] == source_sum
    assert query(conn=gizmosql, sql=f"SELECT data_type FROM information_schema.columns WHERE table_schema = '{s}' "
                                    "AND table_name = 'customer' AND column_name = 'c_acctbal'")[0][0] == \
        "DECIMAL(12,2)"

    # the excluded column never arrives; the masked one is a SHA-256 hex digest computed in Snowflake
    cols = [r[0] for r in query(conn=gizmosql, sql=f"DESCRIBE {s}.customer")]
    assert "c_comment" not in cols and "c_phone" in cols
    assert query(conn=gizmosql, sql=f"SELECT count(*) FROM {s}.customer "
                                    "WHERE NOT regexp_full_match(c_phone, '[0-9a-f]{64}')")[0][0] == 0

    # LAST_ALTERED skips untouched base tables; --full-refresh copies them anyway
    summary, out = run(config=config(prefix=prefix))
    assert summary.exit_code == EXIT_OK and summary.stats["tables_copied"] == 0, out
    assert out.count("unchanged (change marker)") == 4, out
    summary, out = run(config=config(prefix=prefix), full_refresh=True)
    assert summary.exit_code == EXIT_OK and summary.stats["tables_copied"] == 4, out


def test_snowflake_row_limit_on_large_table(gizmosql, prefix):
    summary, out = run(config=config(prefix=prefix, tables=["LINEITEM"]), row_limit=10)
    assert summary.exit_code == EXIT_OK, out
    assert query(conn=gizmosql, sql=f"SELECT count(*) FROM {prefix}_s.lineitem")[0][0] == 10
    types = dict(query(conn=gizmosql, sql="SELECT column_name, data_type FROM information_schema.columns "
                                          f"WHERE table_schema = '{prefix}_s' AND table_name = 'lineitem'"))
    assert types["l_extendedprice"] == "DECIMAL(12,2)" and types["l_shipdate"] == "DATE", types


def test_snowflake_snapshot_is_refused_on_a_shared_database(gizmosql, prefix):
    # A shared (imported) database has no time travel: every AT(TIMESTAMP => ...) read fails
    # with 000707, so the plan must stop before anything is copied.
    with pytest.raises(PlanError, match=r"SNOWFLAKE_SAMPLE_DATA is a shared \(imported\) database"):
        run(config=config(prefix=prefix, consistency="snapshot"))
    assert query(conn=gizmosql, sql=f"SELECT count(*) FROM duckdb_tables() WHERE schema_name = '{prefix}_s'")[0][0] == 0


def test_snowflake_watermark_merge(snowflake, gizmosql, prefix):
    # Snowflake has no row change tracking we can rely on (it's off unless the owner enables it), so
    # incremental loads use a watermark column and a configured key. The sample share never changes,
    # so the state's change marker is cleared to make the second run take the merge path: it reads
    # the rows within the lookback of the highest O_ORDERDATE, merges them, and the counts agree.
    refresh = {"mode": "incremental", "keys": {"TPCH_SF1.ORDERS": ["O_ORDERKEY"]},
               "watermark_column": "O_ORDERDATE", "watermark_lookback_minutes": 2880, "detect_deletes": False}
    cfg = config(prefix=prefix, tables=["ORDERS"], refresh=refresh)
    summary, out = run(config=cfg)
    assert summary.exit_code == EXIT_OK and summary.stats["tables_copied"] == 1, out
    watermark = query(conn=gizmosql, sql=f"SELECT change_token FROM {prefix}_state.table_state")[0][0]
    assert watermark.startswith("1998-08-02"), watermark       # MAX(O_ORDERDATE) in TPCH_SF1
    query(conn=gizmosql, sql=f"UPDATE {prefix}_state.table_state SET change_marker = NULL")
    summary, out = run(config=cfg)
    assert summary.exit_code == EXIT_OK and summary.stats["tables_merged"] == 1, out
    expected = query(conn=snowflake, sql=f"SELECT COUNT(*) FROM {CATALOG}.TPCH_SF1.ORDERS "
                                         "WHERE O_ORDERDATE > DATEADD(day, -2, '1998-08-02'::DATE)")[0][0]
    assert f"{int(expected):,} changed rows merged" in out, out
    assert query(conn=gizmosql, sql=f"SELECT count(*) FROM {prefix}_s.orders")[0][0] == 1_500_000
