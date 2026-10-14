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
    "SELECT INSTR(name, '-', 1, 2) AS p FROM emp",                        # sqlglot: first occurrence
    "SELECT TO_NUMBER(s DEFAULT 0 ON CONVERSION ERROR) AS n FROM emp",   # sqlglot: CAST, raises instead
    "SELECT TO_NUMBER(s, '999D99') AS n FROM emp",
])
def test_constructs_sqlglot_would_silently_change_are_refused(definition):
    with pytest.raises(ViewTranslationError, match="no faithful DuckDB translation"):
        tr(definition=definition)


def test_to_number_becomes_an_exact_decimal_and_hints_are_dropped():
    sql = tr(definition="SELECT /*+ FULL(e) */ TO_NUMBER(code) AS n, TO_CHAR(id) AS s FROM emp e")
    assert 'CAST("code" AS DECIMAL(38, 10)) AS "n"' in sql
    assert 'CAST("id" AS TEXT) AS "s"' in sql
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
