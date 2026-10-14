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

