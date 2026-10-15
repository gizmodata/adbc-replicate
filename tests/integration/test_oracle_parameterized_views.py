"""Oracle "parameterized views" (a view reading SYS_CONTEXT, set by the application before querying) ->
the view, reading the context as NULL, plus a DuckDB table macro of the same name taking the context's
attributes as parameters (views.sys_context_macros). A macro call must return what Oracle returns for
the view's query with those values."""

import pytest
from conftest import (
    ORACLE_SYSTEM_PASSWORD,
    ORACLE_USERNAME,
    execute,
    make_config,
    oracle_connect,
    oracle_source,
    query,
    run,
)

from adbc_replicate.engine import EXIT_OK

pytestmark = pytest.mark.integration

SCHEMA = "PARAMSRC"
# the view's query, with {cutoff} / {item} standing for the context values
QUERY = ("SELECT id, item_id, entered FROM {schema}.entries WHERE entered <= NVL(TO_DATE({cutoff}), SYSDATE) "
         "AND (item_id IN ({item}) OR {item} IS NULL)")


@pytest.fixture(scope="module")
def param_source(oracle):
    system = oracle_connect(username="system", password=ORACLE_SYSTEM_PASSWORD)
    context = "SYS_CONTEXT('ADBC_REPLICATE_PARAMS', '{}')"
    view = QUERY.format(schema=SCHEMA, cutoff=context.format("CUTOFF_DATE"), item=context.format("ITEM_ID"))
    execute(conn=system, statements=[
        f"BEGIN EXECUTE IMMEDIATE 'DROP USER {SCHEMA} CASCADE'; EXCEPTION WHEN OTHERS THEN NULL; END;",
        f"CREATE USER {SCHEMA} IDENTIFIED BY paramsrc QUOTA UNLIMITED ON users",
        f"CREATE TABLE {SCHEMA}.entries (id NUMBER(10) PRIMARY KEY, item_id NUMBER(10), entered DATE)",
        f"INSERT INTO {SCHEMA}.entries SELECT LEVEL, MOD(LEVEL, 3), DATE '2026-01-01' + LEVEL FROM dual CONNECT BY LEVEL <= 20",
        "COMMIT",
        f"CREATE VIEW {SCHEMA}.entries_as_of AS {view}",
        f"GRANT SELECT ON {SCHEMA}.entries TO {ORACLE_USERNAME}",
        f"GRANT SELECT ON {SCHEMA}.entries_as_of TO {ORACLE_USERNAME}",
    ])
    yield system
    system.close()


def test_parameterized_view_gets_a_table_macro(param_source, gizmosql, prefix):
    config = make_config(prefix=prefix, source=oracle_source(), objects=[{"db_schemas": [SCHEMA],
                                                                         "target_db_schema": f"{prefix}_par"}],
                         views={"source_pseudo_columns": "null", "sys_context_macros": True, "source_time_zone": "UTC"})
    summary, out = run(config=config)
    assert summary.exit_code == EXIT_OK, out
    assert f"🧩 {prefix}_par.entries_as_of(...)" in out, out
    source = oracle_connect()
    try:
        cases = [  # (macro arguments, the same values as Oracle literals)
            ("", "NULL", "NULL"),
            ("cutoff_date := DATE '2026-01-08'", "'08-JAN-26'", "NULL"),
            ("item_id := 2", "NULL", "'2'"),
            ("cutoff_date := '2026-01-11', item_id := 1", "'11-JAN-26'", "'1'"),
        ]
        for arguments, cutoff, item in cases:
            expected = sorted(tuple(int(v) if not hasattr(v, "year") else v for v in row) for row in
                              query(conn=source, sql=QUERY.format(schema=SCHEMA, cutoff=cutoff, item=item)))
            actual = sorted(query(conn=gizmosql, sql=f"SELECT id, item_id, entered FROM {prefix}_par.entries_as_of({arguments})"))
            assert actual == expected, (arguments, actual, expected)
            assert len(expected) > 0
        # the view itself: no parameters set
        assert len(query(conn=gizmosql, sql=f"SELECT * FROM {prefix}_par.entries_as_of")) == 20
    finally:
        source.close()
