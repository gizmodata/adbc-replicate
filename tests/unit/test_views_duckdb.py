"""View translations executed in DuckDB, so the rows can be checked, not just the SQL text.
(Kept apart from test_views.py because the Python 3.15 CI job has no DuckDB wheels.)"""

import duckdb

from adbc_replicate.views import translate_view


def tr(definition, schema="HR"):
    return translate_view(definition=definition, source_dialect="oracle", view_db_schema=schema,
                          target_db_schema=schema.lower(), target_view="v", name_map={}, case="lower")


def test_oracle_apply_becomes_lateral_join_with_same_rows():
    # sqlglot alone emits "LEFT JOIN LATERAL (...) AS d" with no ON clause, which DuckDB rejects.
    con = duckdb.connect()
    con.execute(query="CREATE SCHEMA hr; CREATE TABLE hr.emp (id INT); CREATE TABLE hr.badge (emp_id INT); "
                      "INSERT INTO hr.emp VALUES (1), (2); INSERT INTO hr.badge VALUES (1), (1)")
    outer = tr(definition="SELECT e.id, b.n FROM emp e OUTER APPLY "
                          "(SELECT COUNT(*) AS n FROM badge x WHERE x.emp_id = e.id) b")
    cross = tr(definition="SELECT e.id, b.emp_id FROM emp e CROSS APPLY "
                          "(SELECT x.emp_id FROM badge x WHERE x.emp_id = e.id) b")
    assert "LEFT JOIN LATERAL" in outer and "ON TRUE" in outer
    con.execute(query=outer.replace('"hr"."v"', '"hr"."v_outer"'))
    con.execute(query=cross.replace('"hr"."v"', '"hr"."v_cross"'))
    # Oracle: OUTER APPLY keeps employees with no badges (an aggregate returns one row); CROSS APPLY doesn't
    assert con.execute(query="SELECT * FROM hr.v_outer ORDER BY 1").fetchall() == [(1, 2), (2, 0)]
    assert con.execute(query="SELECT * FROM hr.v_cross ORDER BY 1").fetchall() == [(1, 1), (1, 1)]



def sf(definition, **extra):
    return translate_view(definition=definition, source_dialect="snowflake", view_db_schema="MART",
                          target_db_schema="mart", target_view="v", name_map={}, case="lower", **extra)


def test_snowflake_qualify_on_a_cte_window_column_becomes_where():
    # Snowflake merges the CTE, so QUALIFY rn = 1 sees the window function; DuckDB doesn't, and refuses
    # ("at least one window function must appear in the SELECT column or QUALIFY clause").
    con = duckdb.connect()
    con.execute(query="CREATE SCHEMA mart; CREATE TABLE mart.ss (k INT, v INT); "
                      "INSERT INTO mart.ss VALUES (1, 10), (1, 20), (2, 5), (3, NULL)")
    sql = sf(definition="WITH latest AS (SELECT k, v, ROW_NUMBER() OVER (PARTITION BY k ORDER BY v DESC) AS rn "
                        "FROM ss), d AS (SELECT * FROM latest QUALIFY rn = 1) SELECT k, v FROM d")
    assert "QUALIFY" not in sql
    con.execute(query=sql)
    # the rows Snowflake returns for the same query (checked live)
    assert con.execute(query="SELECT * FROM mart.v ORDER BY k").fetchall() == [(1, 20), (2, 5), (3, None)]


def test_snowflake_qualify_with_a_window_function_is_kept():
    sql = sf(definition="SELECT k FROM ss QUALIFY ROW_NUMBER() OVER (PARTITION BY k ORDER BY v) = 1")
    assert "QUALIFY" in sql


def test_star_exclude_drops_columns_the_copy_left_out():
    # The view excludes a column that column rules also left out of the copied table: DuckDB would fail
    # with 'Column "audit_blob" in EXCLUDE list not found'.
    con = duckdb.connect()
    con.execute(query="CREATE SCHEMA mart; CREATE TABLE mart.h (id INT, flag BOOLEAN, keep INT); "
                      "INSERT INTO mart.h VALUES (1, TRUE, 7), (2, FALSE, 8)")
    types = {("MART", "H"): {"ID": "NUMBER", "FLAG": "BOOLEAN", "KEEP": "NUMBER", "AUDIT_BLOB": "VARIANT"}}
    excluded = {("MART", "H"): {"AUDIT_BLOB"}}
    qualified = sf(definition="SELECT t.* EXCLUDE (id, audit_blob), t.id FROM h t WHERE t.flag",
                   column_types=types, excluded_columns=excluded)
    con.execute(query=qualified)
    assert [r[0] for r in con.execute(query="DESCRIBE mart.v").fetchall()] == ["flag", "keep", "id"]
    assert con.execute(query="SELECT * FROM mart.v").fetchall() == [(True, 7, 1)]
    only = sf(definition="SELECT * EXCLUDE (audit_blob) FROM h", column_types=types,
              excluded_columns=excluded)
    assert "EXCLUDE" not in only
    con.execute(query=only)
    # a column the copy kept stays excluded
    kept = sf(definition="SELECT * EXCLUDE (keep) FROM h", column_types=types, excluded_columns=excluded)
    assert 'EXCLUDE ("keep")' in kept


def test_column_list_restating_a_star_is_dropped_but_a_rename_is_kept():
    # Snowflake's GET_DDL writes the column list out; over SELECT * it also names columns the copy
    # left out, and DuckDB refuses "More VIEW aliases than columns in query result".
    con = duckdb.connect()
    con.execute(query="CREATE SCHEMA mart; CREATE TABLE mart.h (id INT, keep INT); INSERT INTO mart.h VALUES (1, 7)")
    types = {("MART", "H"): {"ID": "NUMBER", "KEEP": "NUMBER", "AUDIT_BLOB": "VARIANT"}}
    restated = sf(definition="create or replace view V(ID, KEEP, AUDIT_BLOB, N) as (SELECT t.*, 1 AS n "
                             "FROM h AS t)", column_types=types, excluded_columns={("MART", "H"): {"AUDIT_BLOB"}})
    con.execute(query=restated)
    assert [r[0] for r in con.execute(query="DESCRIBE mart.v").fetchall()] == ["id", "keep", "n"]
    renamed = sf(definition="create or replace view V(A, B) as SELECT * FROM h", column_types=types)
    con.execute(query=renamed)
    assert [r[0] for r in con.execute(query="DESCRIBE mart.v").fetchall()] == ["a", "b"]
