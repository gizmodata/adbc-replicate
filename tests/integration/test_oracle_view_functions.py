"""Oracle functions that view translation rewrites, checked value by value against Oracle itself:
each expression is evaluated in Oracle, then translated and evaluated in GizmoSQL."""

import pytest
from conftest import query

from adbc_replicate.views import translate_view

pytestmark = pytest.mark.integration

NUMBERS = ["123.5", "12345678", "-0.4", "-123.5", "0", "9999999.4", "-9999999.6", "0.5", "-0.5", "NULL"]
DOUBLES = ["-42.4", "1234567.6", "0", "-0.4", "999999999.4", "999999999.5", "-1234.5", "-999999999.6", "NULL"]
TIMESTAMPS = ["TIMESTAMP '2026-10-14 09:05:07'", "TIMESTAMP '2026-10-18 23:59:00'", "TIMESTAMP '2026-01-01 00:00:00'",
              "TIMESTAMP '2026-12-31 12:30:59'", "NULL"]
CASES = [(v, "NUMBER", "DECIMAL(12,2)", f) for v in NUMBERS for f in ("fm0000000", "fm999,999,990", "fm00")] + \
        [(v, "BINARY_DOUBLE", "DOUBLE", f) for v in DOUBLES for f in ("fm999,999,990", "fm0000000")] + \
        [(v, "TIMESTAMP", "TIMESTAMP", f) for v in TIMESTAMPS
         for f in ("MI", "YYYYMMDD", "DAY", "fmDAY", "YYYY-MM-DD HH24:MI:SS", "DD/MM/YY HH12", "HH:MI", 'yyyy"Q"mm')]


def test_to_char_matches_oracle(oracle, gizmosql, prefix):
    schema = f"{prefix}_fn"
    query(conn=gizmosql, sql=f"CREATE SCHEMA {schema}")
    mismatches = []
    for i, (value, oracle_type, target_type, fmt) in enumerate(CASES):
        expected = query(conn=oracle, sql=f"SELECT TO_CHAR(CAST({value} AS {oracle_type}), '{fmt}') FROM dual")[0][0]
        query(conn=gizmosql, sql=f"CREATE OR REPLACE TABLE {schema}.t AS SELECT CAST({value} AS {target_type}) AS x")
        sql = translate_view(definition=f"SELECT TO_CHAR(x, '{fmt}') AS r FROM t", source_dialect="oracle",
                             view_db_schema=schema.upper(), target_db_schema=schema, target_view=f"v{i}",
                             name_map={}, case="lower")
        query(conn=gizmosql, sql=sql)
        actual = query(conn=gizmosql, sql=f"SELECT r FROM {schema}.v{i}")[0][0]
        if actual != expected:
            mismatches.append(f"TO_CHAR({value}, {fmt!r}): Oracle {expected!r}, translated {actual!r}")
    assert not mismatches, "\n".join(mismatches)


DATE_CASES = ["TRUNC(x)", "TRUNC(x, 'DD')", "TRUNC(x, 'hh')", "TRUNC(x, 'MM')", "TRUNC(x, 'YYYY')", "TRUNC(x, 'IW')",
              "TRUNC(x, 'MI')", "TRUNC(x, 'Q')", "CAST(x AS DATE)"]
DATE_VALUES = ["TIMESTAMP '2026-10-14 09:05:07.250'", "TIMESTAMP '2026-01-04 23:59:59'", "TIMESTAMP '2024-02-29 12:00:00'",
               "NULL"]


def test_date_functions_match_oracle(oracle, gizmosql, prefix):
    schema = f"{prefix}_dt"
    query(conn=gizmosql, sql=f"CREATE SCHEMA {schema}")
    types = {(schema.upper(), "T"): {"X": "DATE"}}
    mismatches = []
    for i, (expression, value) in enumerate((e, v) for e in DATE_CASES for v in DATE_VALUES):
        expected = query(conn=oracle, sql=f"SELECT TO_CHAR({expression.replace('x', f'CAST({value} AS DATE)')}, "
                                          "'YYYY-MM-DD HH24:MI:SS') FROM dual")[0][0]
        query(conn=gizmosql, sql=f"CREATE OR REPLACE TABLE {schema}.t AS SELECT CAST({value} AS TIMESTAMP) AS x")
        sql = translate_view(definition=f"SELECT {expression} AS r FROM t", source_dialect="oracle",
                             view_db_schema=schema.upper(), target_db_schema=schema, target_view=f"v{i}",
                             name_map={}, case="lower", column_types=types)
        query(conn=gizmosql, sql=sql)
        actual = query(conn=gizmosql, sql=f"SELECT strftime(r, '%Y-%m-%d %H:%M:%S') FROM {schema}.v{i}")[0][0]
        if actual != expected:
            mismatches.append(f"{expression} on {value}: Oracle {expected!r}, translated {actual!r}")
    assert not mismatches, "\n".join(mismatches)


# Oracle date arithmetic: DATE +/- n days (rounded to the second; a TIMESTAMP is truncated first),
# DATE - DATE in days. Each expression runs in Oracle and, translated, in GizmoSQL.
ARITHMETIC = [
    "d + 1", "d - 1", "d + 1/7", "d - 1/7", "d + 1.5", "1 + d", "d + 1/86400*0.5", "d + 1/86400*0.4",
    "ts + 1/7", "ts - 1", "ts + 0",
    "d2 - d", "ROUND((d2 - d) * 24, 2)", "(d2 - d) * 24 * 60", "TRUNC(d2 - d)", "TRUNC(d) + 1", "ADD_MONTHS(d, 1) - d",
    "NVL(d2, d) - d", "d + n", "d - n",
    # implicit string -> number, and a real-world shift-rounding expression (SYSTIMESTAMP replaced by ts)
    "TO_CHAR(d, 'MI') / 6", "'5' * 2", "ROUND(TO_CHAR(d, 'MI') / 6) * 6 / 1440",
    "ROUND((TRUNC(ts, 'hh') + ROUND(TO_CHAR(ts, 'MI') / 6) * 6 / 1440 - CAST(d AS DATE)) * 24, 1)",
]


def test_date_arithmetic_matches_oracle(oracle, gizmosql, prefix):
    schema = f"{prefix}_ar"
    query(conn=gizmosql, sql=f"CREATE SCHEMA {schema}")
    query(conn=gizmosql, sql=f"CREATE TABLE {schema}.t AS SELECT TIMESTAMP '2026-01-01 08:15:30' AS d, "
                             "TIMESTAMP '2026-01-03 14:00:00' AS d2, TIMESTAMP '2026-01-01 00:00:00.700' AS ts, "
                             "CAST(0.3 AS DECIMAL(10,2)) AS n")
    oracle_row = ("SELECT TO_DATE('2026-01-01 08:15:30','YYYY-MM-DD HH24:MI:SS') AS d, "
                  "TO_DATE('2026-01-03 14:00:00','YYYY-MM-DD HH24:MI:SS') AS d2, "
                  "TO_TIMESTAMP('2026-01-01 00:00:00.700','YYYY-MM-DD HH24:MI:SS.FF3') AS ts, 0.3 AS n FROM dual")
    types = {(schema.upper(), "T"): {"D": "DATE", "D2": "DATE", "TS": "TIMESTAMP(6)", "N": "NUMBER"}}
    shapes = {
        "table": "SELECT {e} AS r FROM t",
        "subquery": "SELECT {e} AS r FROM (SELECT d, d2, ts, n FROM t) x",
        "cte": "WITH c AS (SELECT d, d2, ts, n FROM t) SELECT {e} AS r FROM c",
    }
    mismatches = []
    for i, expression in enumerate(ARITHMETIC):
        expected = query(conn=oracle, sql=f"SELECT {expression} FROM ({oracle_row})")[0][0]
        for shape, template in shapes.items():
            sql = translate_view(definition=template.format(e=expression), source_dialect="oracle",
                                 view_db_schema=schema.upper(), target_db_schema=schema, target_view=f"v{i}_{shape}",
                                 name_map={}, case="lower", column_types=types)
            query(conn=gizmosql, sql=sql)
            actual = query(conn=gizmosql, sql=f"SELECT r FROM {schema}.v{i}_{shape}")[0][0]
            same = (abs(float(actual) - float(expected)) < 1e-9) if not hasattr(expected, "year") else actual == expected
            if not same:
                mismatches.append(f"{expression} ({shape}): Oracle {expected!r}, translated {actual!r}")
    assert not mismatches, "\n".join(mismatches)


REGEX_CASES = [
    "REGEXP_REPLACE(s, '\\s+', ' ')", "REGEXP_REPLACE(s, '\\D')", "REGEXP_REPLACE(s, '(\\d{2})(\\d)', '\\2-\\1')",
    "CASE WHEN REGEXP_LIKE(s, 'quote \\d+', 'i') THEN 'y' ELSE 'n' END", "CASE WHEN REGEXP_LIKE(s, '^[[:digit:]]+$') THEN 'y' ELSE 'n' END",
    "REGEXP_SUBSTR(s, '^[A-Z0-9]+')", "REGEXP_SUBSTR(s, '[a-z_].*$')", "REGEXP_SUBSTR(s, 'QUOTE (\\d+)', 1, 1, 'i', 1)",
    "REGEXP_SUBSTR(s, 'QUOTE #: (\\d+)', 1, 1, 'i', 1)", "REGEXP_SUBSTR(s, 'zzz')", "REGEXP_SUBSTR(s, 'a|ab')",
]
REGEX_VALUES = ["'Quote 123 and QUOTE #: 4567'", "'AB12cd_ef'", "'  multiple   spaces  here '", "'12345'", "'ab'", "NULL"]


def test_regex_functions_match_oracle(oracle, gizmosql, prefix):
    schema = f"{prefix}_rx"
    query(conn=gizmosql, sql=f"CREATE SCHEMA {schema}")
    mismatches = []
    for i, (expression, value) in enumerate((e, v) for e in REGEX_CASES for v in REGEX_VALUES):
        expected = query(conn=oracle, sql=f"SELECT {expression} FROM (SELECT CAST({value} AS VARCHAR2(100)) AS s FROM dual)")[0][0]
        query(conn=gizmosql, sql=f"CREATE OR REPLACE TABLE {schema}.t AS SELECT CAST({value} AS VARCHAR) AS s")
        sql = translate_view(definition=f"SELECT {expression} AS r FROM t", source_dialect="oracle",
                             view_db_schema=schema.upper(), target_db_schema=schema, target_view=f"v{i}", name_map={},
                             case="lower", column_types={(schema.upper(), "T"): {"S": "VARCHAR2"}})
        query(conn=gizmosql, sql=sql)
        actual = query(conn=gizmosql, sql=f"SELECT r FROM {schema}.v{i}")[0][0]
        if actual == "" and expected is None:
            actual = None   # Oracle's empty string is NULL
        if actual != expected:
            mismatches.append(f"{expression} on {value}: Oracle {expected!r}, translated {actual!r}")
    assert not mismatches, "\n".join(mismatches)


# INSTR with a start position and/or occurrence: overlapping matches, backward search from a negative
# position, positions outside the string, NULLs, and positions/occurrences taken from columns.
INSTR_CASES = ["INSTR(s, sub, 1, 2)", "INSTR(s, sub, 1, 3)", "INSTR(s, sub, 3)", "INSTR(s, sub, -1)", "INSTR(s, sub, -1, 2)",
               "INSTR(s, sub, -4, 1)", "INSTR(s, sub, 0)", "INSTR(s, sub, p)", "INSTR(s, sub, p, n)", "INSTR(s, sub)", "INSTR(s, sub, p / 2)", "INSTR(s, sub, 1, 1)",
               "SUBSTR(s, INSTR(s, sub, -1) + 1)", "SUBSTR(s, 1, INSTR(s, sub, 1, 2) - 1)"]
INSTR_VALUES = [("'apple-banana-cherry-date'", "'-'", "8", "2"), ("'aaaa'", "'aa'", "-1", "2"), ("'aaaa'", "'aa'", "2", "1"),
                ("'a/b/c.txt'", "'/'", "-2", "1"), ("'abc'", "'-'", "1", "1"), ("'abc'", "'c'", "5", "1"),
                ("'abc'", "'a'", "-5", "1"), ("NULL", "'-'", "1", "1"), ("'a-b'", "NULL", "1", "1"), ("'a-b'", "'-'", "NULL", "1"),
                ("'a-b-c'", "'-'", "1", "NULL")]


def test_instr_matches_oracle(oracle, gizmosql, prefix):
    schema = f"{prefix}_in"
    query(conn=gizmosql, sql=f"CREATE SCHEMA {schema}")
    types = {(schema.upper(), "T"): {"S": "VARCHAR2", "SUB": "VARCHAR2", "P": "NUMBER", "N": "NUMBER"}}
    mismatches = []
    for i, (expression, (s, sub, p, n)) in enumerate((e, v) for e in INSTR_CASES for v in INSTR_VALUES):
        expected = query(conn=oracle, sql=f"SELECT {expression} FROM (SELECT CAST({s} AS VARCHAR2(40)) AS s, "
                                          f"CAST({sub} AS VARCHAR2(5)) AS sub, CAST({p} AS NUMBER(5)) AS p, "
                                          f"CAST({n} AS NUMBER(5)) AS n FROM dual)")[0][0]
        query(conn=gizmosql, sql=f"CREATE OR REPLACE TABLE {schema}.t AS SELECT CAST({s} AS VARCHAR) AS s, "
                                 f"CAST({sub} AS VARCHAR) AS sub, CAST({p} AS DECIMAL(5,0)) AS p, CAST({n} AS DECIMAL(5,0)) AS n")
        sql = translate_view(definition=f"SELECT {expression} AS r FROM t", source_dialect="oracle",
                             view_db_schema=schema.upper(), target_db_schema=schema, target_view=f"v{i}", name_map={},
                             case="lower", column_types=types)
        query(conn=gizmosql, sql=sql)
        actual = query(conn=gizmosql, sql=f"SELECT r FROM {schema}.v{i}")[0][0]
        if actual == "" and expected is None:
            actual = None   # Oracle's empty string is NULL
        if expected is not None and not isinstance(expected, str):
            expected = int(expected)
        if actual != expected:
            mismatches.append(f"{expression} on {(s, sub, p, n)}: Oracle {expected!r}, translated {actual!r}")
    assert not mismatches, "\n".join(mismatches)


# MIN/MAX(x) KEEP (DENSE_RANK FIRST/LAST ORDER BY ...), as an aggregate and as an analytic function.
# Groups: ties on the first key with different x; NULL keys; a tied row with NULL x; all keys NULL;
# ties broken by a second key.
KEEP_ROWS = [(1, 1, 1, "'b'"), (1, 1, 1, "'a'"), (1, 2, 1, "'z'"), (2, None, 1, "'a'"), (2, 5, 1, "'m'"), (2, 5, 1, "'n'"),
             (3, 1, 1, "NULL"), (3, 1, 1, "'c'"), (3, 9, 1, "NULL"), (3, 9, 1, "'q'"), (4, None, 1, "'x'"), (4, None, 1, "'y'"),
             (5, 1, 2, "'p'"), (5, 1, 1, "'r'"), (5, 3, None, "'s'"), (5, 3, 4, "'t'")]
KEEP_CASES = ["MIN(x) KEEP (DENSE_RANK FIRST ORDER BY k)", "MAX(x) KEEP (DENSE_RANK LAST ORDER BY k)",
              "MIN(x) KEEP (DENSE_RANK FIRST ORDER BY k, k2)", "MAX(x) KEEP (DENSE_RANK LAST ORDER BY k, k2)",
              "MIN(x) KEEP (DENSE_RANK LAST ORDER BY k DESC)", "MAX(x) KEEP (DENSE_RANK FIRST ORDER BY k DESC, k2 DESC)",
              "MIN(x) KEEP (DENSE_RANK FIRST ORDER BY k ASC NULLS LAST)",
              # the pairings and orders a single ROW() key can't express
              "MAX(x) KEEP (DENSE_RANK FIRST ORDER BY k)", "MIN(x) KEEP (DENSE_RANK LAST ORDER BY k)",
              "MAX(x) KEEP (DENSE_RANK FIRST ORDER BY k NULLS LAST, k2)", "MIN(x) KEEP (DENSE_RANK FIRST ORDER BY k NULLS FIRST)",
              "MAX(x) KEEP (DENSE_RANK LAST ORDER BY k ASC, k2 DESC)", "MIN(x) KEEP (DENSE_RANK FIRST ORDER BY k DESC NULLS LAST, k2)"]


def test_keep_dense_rank_matches_oracle(oracle, gizmosql, prefix):
    schema = f"{prefix}_kp"
    rows = " UNION ALL ".join(f"SELECT {g} AS g, {'NULL' if k is None else k} AS k, {'NULL' if k2 is None else k2} AS k2, "
                              f"CAST({x} AS VARCHAR2(5)) AS x FROM dual" for g, k, k2, x in KEEP_ROWS)
    query(conn=gizmosql, sql=f"CREATE SCHEMA {schema}")
    query(conn=gizmosql, sql=f"CREATE TABLE {schema}.t AS SELECT CAST(g AS INTEGER) AS g, CAST(k AS DECIMAL(5,0)) AS k, "
                             f"CAST(k2 AS DECIMAL(5,0)) AS k2, CAST(x AS VARCHAR) AS x FROM ({rows.replace(' FROM dual', '').replace('VARCHAR2(5)', 'VARCHAR')})")
    mismatches = []
    for i, expression in enumerate(KEEP_CASES):
        for shape, template in {"aggregate": "SELECT g, {e} AS r FROM t GROUP BY g ORDER BY g",
                                "analytic": "SELECT DISTINCT g, {e} OVER (PARTITION BY g) AS r FROM t ORDER BY g"}.items():
            definition = template.format(e=expression)
            expected = query(conn=oracle, sql=definition.replace("FROM t", f"FROM ({rows})"))
            sql = translate_view(definition=definition, source_dialect="oracle", view_db_schema=schema.upper(),
                                 target_db_schema=schema, target_view=f"v{i}_{shape}", name_map={}, case="lower")
            query(conn=gizmosql, sql=sql)
            actual = query(conn=gizmosql, sql=f"SELECT g, r FROM {schema}.v{i}_{shape} ORDER BY g")
            expected = [(int(g), r) for g, r in expected]
            if actual != expected:
                mismatches.append(f"{expression} ({shape}): Oracle {expected!r}, translated {actual!r}")
    assert not mismatches, "\n".join(mismatches)


def test_sys_context_reads_as_unset(oracle, gizmosql, prefix):
    """SYS_CONTEXT of a context nobody set is NULL in Oracle; translated (source_pseudo_columns: null)
    it is NULL too, and TO_DATE of it falls through to the NVL/COALESCE default, as in Oracle."""
    schema = f"{prefix}_sc"
    query(conn=gizmosql, sql=f"CREATE SCHEMA {schema}")
    query(conn=gizmosql, sql=f"CREATE TABLE {schema}.t AS SELECT 1 AS id")
    expression = ("CASE WHEN SYS_CONTEXT('ADBC_REPLICATE_TEST_CTX', 'CUTOFF') IS NULL THEN 'unset' END || '/' || "
                  "TO_CHAR(NVL(TO_DATE(SYS_CONTEXT('ADBC_REPLICATE_TEST_CTX', 'CUTOFF')), DATE '2026-01-02'), 'YYYY-MM-DD')")
    expected = query(conn=oracle, sql=f"SELECT {expression} FROM dual")[0][0]
    sql = translate_view(definition=f"SELECT {expression} AS r FROM t", source_dialect="oracle",
                         view_db_schema=schema.upper(), target_db_schema=schema, target_view="v", name_map={},
                         case="lower", pseudo_columns="null")
    query(conn=gizmosql, sql=sql)
    assert expected == "unset/2026-01-02"
    assert query(conn=gizmosql, sql=f"SELECT r FROM {schema}.v")[0][0] == expected


# Hierarchical queries (START WITH / CONNECT BY) -> WITH RECURSIVE. A small bill of materials: part 4
# (SPOKE) sits under two parents, siblings sort on codes including a NULL (last ascending, first
# descending), quantities have decimals (SYS_CONNECT_BY_PATH must print them as Oracle does: '.5').
BOM = [(None, 1, "1", "'BIKE'", 1), (1, 2, "2", "'WHEEL'", 1), (1, 3, "1", "'FRAME'", 2), (2, 4, "30", "'SPOKE'", 1),
       (3, 4, "0.5", "'SPOKE'", 1), (2, 5, "1.25", "'HUB'", 2), (None, 6, "1", "'TRIKE'", 1), (6, 2, "3", "'WHEEL'", 2),
       (6, 7, "1", "NULL", 1)]
ONE_TWO = "SELECT 1 AS id FROM dual UNION ALL SELECT 2 FROM dual"
HIERARCHIES = [
    # everything at once, depth-first numbering in ORDER SIBLINGS BY order
    "SELECT LEVEL AS lvl, child_id, SYS_CONNECT_BY_PATH(code, '/') AS path, CONNECT_BY_ROOT code AS root_code, "
    "CONNECT_BY_ISLEAF AS leaf, ROWNUM AS seq FROM t START WITH parent_id IS NULL "
    "CONNECT BY PRIOR child_id = parent_id ORDER SIBLINGS BY code",
    # mixed sibling order, DESC puts the NULL code first
    "SELECT ROWNUM AS seq, child_id, LEVEL AS lvl FROM t START WITH parent_id IS NULL "
    "CONNECT BY PRIOR child_id = parent_id ORDER SIBLINGS BY grp ASC, code DESC",
    # a BOM formula from the quantities (the root counts once), and numbers in a path
    "SELECT child_id, SYS_CONNECT_BY_PATH(CASE LEVEL WHEN 1 THEN 1 ELSE qty END, ' * ') AS formula, "
    "SYS_CONNECT_BY_PATH(qty, '/') AS qtys FROM t START WITH parent_id IS NULL CONNECT BY PRIOR child_id = parent_id",
    # no START WITH: every row is a root
    "SELECT CONNECT_BY_ROOT child_id AS anc, child_id AS des, LEVEL AS lvl FROM t CONNECT BY PRIOR child_id = parent_id",
    # upwards, from one part to the roots, with a qualified alias and a WHERE applied after the hierarchy
    "SELECT b.parent_id, b.child_id, LEVEL AS lvl, SYS_CONNECT_BY_PATH(b.code, ' > ') AS path FROM t b "
    "WHERE LEVEL > 1 START WITH b.child_id = 4 CONNECT BY PRIOR b.parent_id = b.child_id",
    # one anchor for a part that appears under two parents
    "SELECT LEVEL AS lvl, child_id, SYS_CONNECT_BY_PATH(child_id, '/') AS path FROM t "
    "START WITH child_id = 2 AND ROWNUM = 1 CONNECT BY PRIOR child_id = parent_id",
    # LEVEL as a row generator: one row per level from DUAL; from a two-row source every row is a child
    # of every row (2 + 4 + 8)
    "SELECT LEVEL AS n FROM dual CONNECT BY LEVEL <= 6",
    f"SELECT id, LEVEL AS n FROM ({ONE_TWO}) CONNECT BY LEVEL <= 3",
    # a hierarchy over a hierarchy (ancestor/descendant pairs keyed on the first one's numbering)
    "WITH tree AS (SELECT child_id, parent_id, ROWNUM AS sort_key, CONNECT_BY_ROOT child_id AS root_id FROM t "
    "START WITH parent_id IS NULL CONNECT BY PRIOR child_id = parent_id ORDER SIBLINGS BY code) "
    "SELECT CONNECT_BY_ROOT child_id AS anc, child_id AS des, LEVEL AS lvl, "
    "MAX(CONNECT_BY_ROOT sort_key) OVER (PARTITION BY CONNECT_BY_ROOT child_id) AS anc_key, sort_key AS des_key "
    "FROM tree CONNECT BY PRIOR child_id = parent_id AND PRIOR root_id = root_id",
]


def _normalized(rows):
    import decimal

    def value(v):
        if v is None:
            return "NULL"
        if isinstance(v, (int, float, decimal.Decimal)):
            return str(decimal.Decimal(str(v)).normalize())
        return str(v)
    return sorted(tuple(value(v) for v in row) for row in rows)


def test_connect_by_matches_oracle(oracle, gizmosql, prefix):
    schema = f"{prefix}_cb"
    data = " UNION ALL ".join(f"SELECT {'NULL' if p is None else p} AS parent_id, {c} AS child_id, {q} AS qty, "
                              f"CAST({code} AS VARCHAR2(10)) AS code, {g} AS grp FROM dual" for p, c, q, code, g in BOM)
    query(conn=gizmosql, sql=f"CREATE SCHEMA {schema}")
    query(conn=gizmosql, sql=f"CREATE TABLE {schema}.t AS SELECT CAST(parent_id AS DECIMAL(10,0)) AS parent_id, "
                             "CAST(child_id AS DECIMAL(10,0)) AS child_id, CAST(qty AS DECIMAL(38,10)) AS qty, "
                             f"CAST(code AS VARCHAR) AS code, CAST(grp AS DECIMAL(5,0)) AS grp "
                             f"FROM ({data.replace(' FROM dual', '').replace('VARCHAR2(10)', 'VARCHAR')})")
    mismatches = []
    for i, definition in enumerate(HIERARCHIES):
        with_data = f"WITH t AS ({data}) "
        oracle_sql = with_data + definition if not definition.startswith("WITH ") \
            else with_data.rstrip() + ", " + definition[len("WITH "):]
        expected = _normalized(query(conn=oracle, sql=oracle_sql))
        sql = translate_view(definition=definition, source_dialect="oracle", view_db_schema=schema.upper(),
                             target_db_schema=schema, target_view=f"v{i}", name_map={}, case="lower")
        query(conn=gizmosql, sql=sql)
        actual = _normalized(query(conn=gizmosql, sql=f"SELECT * FROM {schema}.v{i}"))
        if actual != expected:
            mismatches.append(f"{definition}\n  Oracle:     {expected}\n  translated: {actual}")
    assert not mismatches, "\n".join(mismatches)


def test_connect_by_nocycle_matches_oracle(oracle, gizmosql, prefix):
    """Parts 2 and 3 contain each other: NOCYCLE stops where a row would be its own ancestor."""
    schema = f"{prefix}_cy"
    data = " UNION ALL ".join(f"SELECT {'NULL' if p is None else p} AS parent_id, {c} AS child_id FROM dual"
                              for p, c in [(None, 1), (1, 2), (2, 3), (3, 2), (3, 4)])
    definition = ("SELECT LEVEL AS lvl, child_id, SYS_CONNECT_BY_PATH(child_id, '/') AS path, CONNECT_BY_ISLEAF AS leaf "
                  "FROM c START WITH parent_id IS NULL CONNECT BY NOCYCLE PRIOR child_id = parent_id")
    expected = _normalized(query(conn=oracle, sql=f"WITH c AS ({data}) {definition}"))
    query(conn=gizmosql, sql=f"CREATE SCHEMA {schema}")
    query(conn=gizmosql, sql=f"CREATE TABLE {schema}.c AS SELECT CAST(parent_id AS DECIMAL(10,0)) AS parent_id, "
                             f"CAST(child_id AS DECIMAL(10,0)) AS child_id FROM ({data.replace(' FROM dual', '')})")
    query(conn=gizmosql, sql=translate_view(definition=definition, source_dialect="oracle", view_db_schema=schema.upper(),
                                            target_db_schema=schema, target_view="v", name_map={}, case="lower"))
    assert _normalized(query(conn=gizmosql, sql=f"SELECT * FROM {schema}.v")) == expected
