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
