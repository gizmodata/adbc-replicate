import re

import pytest

from adbc_replicate.views import ViewTranslationError, translate_view


def tr(definition, dialect="oracle", case="lower", name_map=None, columns=None, schema="HR"):
    return translate_view(definition=definition, source_dialect=dialect, view_db_schema=schema,
                          target_db_schema=schema.lower(), target_view="v", name_map=name_map or {}, case=case,
                          source_columns=columns)


def test_oracle_functions_and_qualification():
    sql = tr(definition="SELECT id, NVL(amt, 0) AS amt, DECODE(id, 1, 'one', 'other') AS d FROM emp")
    assert 'FROM "hr"."emp"' in sql
    assert "COALESCE" in sql and "CASE WHEN" in sql
    assert sql.startswith('CREATE OR REPLACE VIEW "hr"."v" AS')


def test_oracle_outer_join_marks_become_left_joins():
    sql = tr(definition="SELECT e.id FROM emp e, dept d WHERE e.dept_id = d.id(+) AND d.kind(+) = 'X' AND e.x > 1")
    assert "LEFT JOIN" in sql
    assert '"d"."kind" = \'X\'' in sql
    assert "(+)" not in sql


def test_oracle_dual_and_rownum():
    assert "FROM" not in tr(definition="SELECT 1 AS one FROM DUAL").split("AS\n", 1)[1]
    sql = tr(definition="SELECT id FROM emp WHERE status = 'A' AND ROWNUM <= 10")
    assert "LIMIT 10" in sql and "ROWNUM" not in sql.upper().replace('"ROWNUM"', "")
    assert "LIMIT 4" in tr(definition="SELECT id FROM emp WHERE ROWNUM < 5")


def test_rownum_with_order_by_is_refused():
    with pytest.raises(ViewTranslationError, match="ROWNUM"):
        tr(definition="SELECT id FROM emp WHERE ROWNUM <= 5 ORDER BY id")


def test_oracle_column_list_from_dictionary():
    sql = tr(definition="SELECT id, name FROM emp", columns=["EMP_ID", "EMP_NAME"])
    assert '"hr"."v" ("emp_id", "emp_name") AS' in sql


def test_ctes_are_not_qualified():
    sql = tr(definition="WITH x AS (SELECT id FROM emp) SELECT id FROM x")
    assert 'FROM "x"' in sql and '"hr"."x"' not in sql


def test_name_map_repoints_references_and_drops_catalog():
    sql = tr(definition="CREATE VIEW V AS SELECT * FROM DB.STAGING.ORDERS", dialect="snowflake",
             name_map={("STAGING", "ORDERS"): ("core", "orders_copy")})
    assert 'FROM "core"."orders_copy"' in sql
    assert "DB" not in sql


def test_snowflake_create_view_column_list_is_kept_and_properties_dropped():
    sql = tr(definition="create or replace secure view V1 (A, B) comment='x' as select 1 as a, 2 as b from T",
             dialect="snowflake")
    assert '("a", "b") AS' in sql and "comment" not in sql.lower() and "secure" not in sql.lower()


def test_dbt_comments_are_dropped():
    sql = tr(definition='select 1 as a from t /* {"invocation_id": "123"} */', dialect="snowflake")
    assert "invocation_id" not in sql


def test_preserve_case_keeps_source_case():
    sql = tr(definition="SELECT id FROM emp", case="preserve")
    assert '"HR"."EMP"' in sql and '"ID"' in sql


def test_postgres_view():
    sql = tr(definition="SELECT e.id, e.amount::numeric(10,2) AS amt FROM emp e WHERE e.active", dialect="postgres",
             schema="public")
    assert 'FROM "public"."emp" AS "e"' in sql


def test_unparseable_definition():
    with pytest.raises(ViewTranslationError):
        tr(definition="SELECT FROM WHERE (((")


def test_catalog_qualifies_only_ambiguous_names():
    sql = translate_view(definition="SELECT * FROM shop.orders o JOIN other.x ON 1 = 1", source_dialect="postgres",
                         view_db_schema="shop", target_db_schema="shop", target_view="v", name_map={}, case="lower",
                         target_catalog="shop")
    assert 'CREATE OR REPLACE VIEW "shop"."shop"."v"' in sql
    assert 'FROM "shop"."shop"."orders"' in sql and 'JOIN "other"."x"' in sql


@pytest.mark.parametrize("definition", [
    "SELECT TO_CHAR(dt, 'Mon YYYY') AS m FROM emp",                       # sqlglot: CAST(dt AS TEXT)
    "SELECT TO_NUMBER(s DEFAULT 0 ON CONVERSION ERROR) AS n FROM emp",   # sqlglot: CAST, raises instead
    "SELECT TO_NUMBER(s, '999D99') AS n FROM emp",
])
def test_constructs_sqlglot_would_silently_change_are_refused(definition):
    with pytest.raises(ViewTranslationError, match="no faithful DuckDB translation"):
        tr(definition=definition)


def test_to_number_becomes_an_exact_decimal_and_hints_are_dropped():
    sql = tr(definition="SELECT /*+ FULL(e) */ TO_NUMBER(code) AS n, TO_CHAR(id) AS s FROM emp e")
    assert 'CAST("code" AS DECIMAL(38, 10)) AS "n"' in sql
    assert 'TYPEOF("id")' in sql and 'ELSE CAST("id" AS TEXT)' in sql    # Oracle's number text, decided at run time
    assert "FULL" not in sql and "/*" not in sql


def test_oracle_pseudo_columns_fail_by_default_or_become_typed_nulls():
    definition = "SELECT i.id, i.ORA_ROWSCN AS item_scn, i.ROWID AS item_rowid FROM items i"
    with pytest.raises(ViewTranslationError, match="pseudo-column ORA_ROWSCN"):
        tr(definition=definition)
    sql = translate_view(definition=definition, source_dialect="oracle", view_db_schema="HR", target_db_schema="hr",
                         target_view="v", name_map={}, case="lower", pseudo_columns="null")
    assert 'CAST(NULL AS DECIMAL(38, 0)) AS "item_scn"' in sql and 'CAST(NULL AS TEXT) AS "item_rowid"' in sql
    assert "ora_rowscn" not in sql.lower()


def test_to_char_formats_with_an_exact_equivalent_are_translated():
    # Values are checked against Oracle itself in tests/integration/test_oracle_view_functions.py.
    sql = tr(definition="SELECT TO_CHAR(dt, 'YYYY-MM') AS m, TO_CHAR(n, 'fm0000000') AS z FROM emp")
    assert "STRFTIME(\"dt\", '%Y-%m')" in sql and "LPAD(" in sql


def ora(definition, **kwargs):
    return translate_view(definition=definition, source_dialect="oracle", view_db_schema="HR", target_db_schema="hr",
                          target_view="v", name_map={}, case="lower", **kwargs)


def test_sysdate_needs_the_source_time_zone():
    # sqlglot maps SYSDATE to UTC; an Oracle server on US Eastern would be 4-5 hours off.
    with pytest.raises(ViewTranslationError, match=r"views\.source_time_zone"):
        ora(definition="SELECT SYSDATE AS now FROM DUAL")
    sql = ora(definition="SELECT SYSDATE AS d, SYSTIMESTAMP AS ts, CURRENT_DATE AS cd FROM DUAL",
              time_zone="America/New_York")
    assert "CAST(CURRENT_TIMESTAMP AT TIME ZONE 'America/New_York' AS TIMESTAMP_S) AS \"d\"" in sql
    assert "CURRENT_TIMESTAMP AT TIME ZONE 'America/New_York' AS \"ts\"" in sql
    assert "UTC" not in sql


def test_oracle_trunc_and_cast_as_date_keep_date_semantics():
    types = {("HR", "EMP"): {"HIRED": "DATE", "RATE": "NUMBER"}}
    sql = ora(definition="SELECT TRUNC(e.hired) AS d, TRUNC(e.rate) AS r, TRUNC(e.hired, 'MM') AS m, "
                         "TRUNC(e.hired, 'hh') AS h, CAST(e.hired AS DATE) AS c FROM emp e", column_types=types)
    assert "DATE_TRUNC('day', \"e\".\"hired\") AS \"d\"" in sql
    assert "TRUNC(\"e\".\"rate\") AS \"r\"" in sql                       # a number stays a number
    assert "DATE_TRUNC('month'" in sql and "DATE_TRUNC('hour'" in sql
    assert "CAST(\"e\".\"hired\" AS TIMESTAMP_S) AS \"c\"" in sql       # not DATE: keeps the time of day
    with pytest.raises(ViewTranslationError, match="TRUNC"):
        ora(definition="SELECT TRUNC(e.hired, 'DY') AS w FROM emp e", column_types=types)


def test_instr_position_and_occurrence_are_translated():
    # Values are checked against Oracle itself in tests/integration/test_oracle_view_functions.py.
    sql = tr(definition="SELECT INSTR(name, '-', 1, 2) AS a, INSTR(name, '/', -1) AS b, INSTR(name, '-') AS c FROM emp")
    assert "LIST_FILTER" in sql and "LIST_REVERSE" in sql and 'STRPOS("name", \'-\') AS "c"' in sql
    assert "SUBSTRING(\"name\", -1)" not in sql          # sqlglot's translation of a negative position
    with pytest.raises(ViewTranslationError, match="ORA-01428"):
        tr(definition="SELECT INSTR(name, '-', 1, 0) AS a FROM emp")


def test_keep_dense_rank_becomes_min_by_max_by():
    sql = tr(definition="SELECT g, MIN(x) KEEP (DENSE_RANK FIRST ORDER BY k, k2 DESC) AS a, "
                        "MAX(x) KEEP (DENSE_RANK LAST ORDER BY k) OVER (PARTITION BY g) AS b FROM t GROUP BY g, x, k")
    sql = re.sub(r"\s+\)", ")", re.sub(r"\(\s+", "(", " ".join(sql.split())))     # pretty-printing aside
    assert ('MIN_BY("x", CREATE_SORT_KEY("k", \'ASC NULLS LAST\', "k2", \'DESC NULLS FIRST\', "x", '
            '\'ASC NULLS LAST\')) AS "a"') in sql
    assert ('MAX_BY("x", CREATE_SORT_KEY("k", \'ASC NULLS LAST\', "x", \'ASC NULLS FIRST\')) '
            'OVER (PARTITION BY "g") AS "b"') in sql


def test_keep_with_other_aggregates_is_refused():
    with pytest.raises(ViewTranslationError, match="MIN and MAX only"):
        tr(definition="SELECT SUM(x) KEEP (DENSE_RANK FIRST ORDER BY k) AS a FROM t")


def test_sys_context_fails_by_default_or_reads_as_null():
    definition = "SELECT id FROM t WHERE d <= NVL(TO_DATE(SYS_CONTEXT('CTX', 'CUTOFF')), DATE '2026-01-01')"
    with pytest.raises(ViewTranslationError, match="SYS_CONTEXT"):
        tr(definition=definition)
    sql = translate_view(definition=definition, source_dialect="oracle", view_db_schema="HR", target_db_schema="hr",
                         target_view="v", name_map={}, case="lower", pseudo_columns="null")
    assert "SYS_CONTEXT" not in sql.upper() and "STRPTIME" not in sql.upper()
    assert "CAST(NULL AS TIMESTAMP_S)" in sql


def test_to_date_of_default_date_text_reads_fields_by_position():
    # Oracle: TO_DATE(TO_CHAR(SYSDATE), 'YYYY-MM-DD') on 16-OCT-26 is 0016-10-26 (values checked against Oracle in
    # tests/integration/test_oracle_view_functions.py)
    sql = translate_view(definition="SELECT TO_DATE(TO_CHAR(SYSDATE), 'YYYY-MM-DD') AS a, "
                                    "TO_DATE(TO_CHAR(SYSDATE), 'DD-MON-RR') AS b FROM dual",
                         source_dialect="oracle", view_db_schema="HR", target_db_schema="hr", target_view="v",
                         name_map={}, case="lower", time_zone="America/New_York")
    assert "STRPTIME" not in sql.upper() and "STRFTIME" not in sql.upper()
    assert sql.count("MAKE_TIMESTAMP(") == 2


@pytest.mark.parametrize("fmt", ["MM-DD-RR", "DD-MON", "YYYYMMDD", "DD-DD-RR", "fxDD-MON-RR", 'DD"x"MON-RR'])
def test_to_date_of_default_date_text_refuses_formats_it_cant_read_exactly(fmt):
    with pytest.raises(ViewTranslationError, match="reads the date's DD-MON-RR text"):
        translate_view(definition=f"SELECT TO_DATE(TO_CHAR(SYSDATE), '{fmt}') AS a FROM dual", source_dialect="oracle",
                       view_db_schema="HR", target_db_schema="hr", target_view="v", name_map={}, case="lower",
                       time_zone="America/New_York")


def test_greatest_and_least_are_null_when_any_argument_is():
    sql = " ".join(tr(definition="SELECT GREATEST(a, b) AS g, LEAST(a, GREATEST(b, c), 1) AS l, GREATEST(a) AS one "
                                 "FROM t").split())
    assert 'CASE WHEN NOT "a" IS NULL AND NOT "b" IS NULL THEN GREATEST("a", "b") END AS "g"' in sql
    assert sql.count("CASE WHEN") == 4 and 'GREATEST("a") AS "one"' in sql


def test_connect_by_becomes_a_recursive_cte():
    # Rows are checked against Oracle itself in tests/integration/test_oracle_view_functions.py.
    sql = tr(definition="SELECT LEVEL AS lvl, SYS_CONNECT_BY_PATH(code, '/') AS path, CONNECT_BY_ROOT id AS root_id, "
                        "CONNECT_BY_ISLEAF AS leaf, ROWNUM AS seq FROM nodes START WITH parent_id IS NULL "
                        "AND ROWNUM = 1 CONNECT BY NOCYCLE PRIOR id = parent_id ORDER SIBLINGS BY code")
    assert sql.count("WITH RECURSIVE") == 1 and "UNION ALL" in sql and "LIMIT 1" in sql
    assert "CONNECT" not in sql.upper().replace("CONNECT_BY", "") and "PRIOR" not in sql.upper().replace("__CB1_PRIOR", "")
    assert 'ROW_NUMBER() OVER (ORDER BY "nodes"."__cb_order") AS "seq"' in sql


@pytest.mark.parametrize("definition, message", [
    ("SELECT LEVEL FROM a JOIN b ON a.id = b.id CONNECT BY PRIOR a.id = a.parent_id", "over a join"),
    ("SELECT * FROM a CONNECT BY PRIOR id = parent_id", r"SELECT \*"),
    ("SELECT id FROM a WHERE ROWNUM < 3 CONNECT BY PRIOR id = parent_id", "ROWNUM in the WHERE"),
    ("SELECT id FROM a CONNECT BY id = parent_id", "without PRIOR or LEVEL"),
])
def test_connect_by_forms_that_are_refused(definition, message):
    with pytest.raises(ViewTranslationError, match=message):
        tr(definition=definition)


def test_configured_functions_replace_source_functions():
    functions = {"bom_factor": "coalesce(list_product(list_transform(string_split(substring({1}, 2), '/'), "
                                 "lambda v: CAST(NULLIF(v, '') AS DOUBLE))), 1)",
                 "pkg.fmt": "lpad(CAST({p_value} AS VARCHAR), {p_width}, '0')"}
    sql = translate_view(definition="SELECT BOM_FACTOR(path) AS f, pkg.fmt(p_width => 6, p_value => id) AS s FROM t",
                         source_dialect="oracle", view_db_schema="HR", target_db_schema="hr", target_view="v",
                         name_map={}, case="lower", functions=functions)
    assert 'LIST_PRODUCT(' in sql and 'SUBSTRING("path", 2)' in sql
    assert "LPAD(CAST(\"id\" AS TEXT), 6, '0')" in sql and "pkg" not in sql.lower()
    with pytest.raises(ViewTranslationError, match="doesn't pass"):
        translate_view(definition="SELECT pkg.fmt(p_value => id) AS s FROM t", source_dialect="oracle",
                       view_db_schema="HR", target_db_schema="hr", target_view="v", name_map={}, case="lower",
                       functions=functions)


def test_renamed_columns_are_repointed_in_views():
    renames = {("HR", "QUOTES"): {"ROW_ID": "source_row_id"}}
    types = {("HR", "QUOTES"): {"ID": "NUMBER", "ROW_ID": "NUMBER"}, ("HR", "LINES"): {"ID": "NUMBER", "QUOTE_ID": "NUMBER"}}
    sql = translate_view(definition="SELECT q.row_id, x.rid, row_id + 1 AS next_id FROM quotes q, "
                                    "(SELECT row_id AS rid FROM quotes) x WHERE q.row_id = x.rid",
                         source_dialect="oracle", view_db_schema="HR", target_db_schema="hr", target_view="v",
                         name_map={}, case="lower", column_types=types, column_renames=renames)
    assert '"q"."source_row_id" AS "row_id"' in sql                   # a bare column keeps its name
    assert '"source_row_id" AS "rid"' in sql and 'WHERE\n  "q"."source_row_id" = "x"."rid"' in sql
    assert '"row_id" + 1' in sql             # unqualified with a subquery in scope: left alone (ambiguous)
    sql = translate_view(definition="SELECT l.id, q.row_id FROM lines l JOIN quotes q ON q.id = l.quote_id",
                         source_dialect="oracle", view_db_schema="HR", target_db_schema="hr", target_view="v",
                         name_map={}, case="upper", column_types=types, column_renames=renames)
    assert '"Q"."source_row_id" AS "ROW_ID"' in sql                    # the target name isn't re-cased


def test_configured_function_signatures_and_return_types():
    functions = {"ADD_DAYS(start_date, n) RETURN DATE": "{start_date} + to_days(CAST({n} AS INTEGER))"}
    for call in ("ADD_DAYS(hired, 3)", "ADD_DAYS(n => 3, start_date => hired)"):
        sql = translate_view(definition=f"SELECT {call} - 7 AS d FROM emp", source_dialect="oracle",
                             view_db_schema="HR", target_db_schema="hr", target_view="v", name_map={}, case="lower",
                             functions=functions, column_types={("HR", "EMP"): {"HIRED": "DATE"}})
        assert '"hired" + TO_DAYS(CAST(3 AS INT))' in sql, sql
        assert "MAKE_TIMESTAMP(" in sql, sql            # RETURN DATE: "- 7" is Oracle date arithmetic (days)


def test_configured_package_function_with_signature():
    sql = translate_view(definition="SELECT erp_pkg.cost(p_item => i.id) AS c, erp_pkg.cost(i.id) AS d FROM items i",
                         source_dialect="oracle", view_db_schema="HR", target_db_schema="hr", target_view="v", name_map={},
                         case="lower", functions={"ERP_PKG.COST(p_item) RETURN NUMBER": "({p_item} * 2)"})
    assert sql.count('"i"."id" * 2') == 2 and "erp_pkg" not in sql.lower()
