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
    assert "FROM" not in tr(definition="SELECT SYSDATE AS now FROM DUAL").split("AS\n", 1)[1]
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
