"""Translate source view definitions into target (DuckDB-dialect) CREATE VIEW statements."""

from __future__ import annotations

import os
from pathlib import Path

import sqlglot
from sqlglot import exp, transforms
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers
from sqlglot.optimizer.qualify_tables import qualify_tables

from .matching import apply_case

# sqlglot renamed Select's "from" argument to "from_" in newer releases.
_FROM = "from_" if "from_" in exp.Select.arg_types else "from"


class ViewTranslationError(Exception):
    pass


def _quoted(name: str) -> exp.Identifier:
    return exp.to_identifier(name, quoted=True)


def _oracle_rewrites(body: exp.Expression) -> exp.Expression:
    """Oracle-only constructs with a well-defined DuckDB equivalent."""
    # (+) outer joins -> ANSI LEFT JOINs. sqlglot's generator would otherwise drop the (+)
    # silently, turning outer joins into inner joins.
    body = transforms.eliminate_join_marks(body)
    if any(c.args.get("join_mark") for c in body.find_all(exp.Column)):
        raise ViewTranslationError("Oracle (+) outer-join syntax that can't be rewritten as ANSI joins")

    for select in list(body.find_all(exp.Select)):
        # FROM DUAL -> no FROM clause
        from_ = select.args.get(_FROM)
        if from_ is not None and isinstance(from_.this, exp.Table) and not from_.this.db \
                and from_.this.name.upper() == "DUAL" and not select.args.get("joins"):
            select.set(_FROM, None)
        # WHERE ROWNUM <= n (alone or AND-ed at the top level) -> LIMIT n
        where = select.args.get("where")
        if where is None:
            continue
        conditions = list(where.this.flatten()) if isinstance(where.this, exp.And) else [where.this]
        limit = None
        kept = []
        for cond in conditions:
            if isinstance(cond, (exp.LTE, exp.LT)) and isinstance(cond.this, exp.Column) \
                    and cond.this.name.upper() == "ROWNUM" and not cond.this.table \
                    and isinstance(cond.expression, exp.Literal) and cond.expression.is_int:
                n = int(cond.expression.name) - (1 if isinstance(cond, exp.LT) else 0)
                limit = n if limit is None else min(limit, n)
            else:
                kept.append(cond)
        if limit is not None:
            if select.args.get("order") is not None or select.args.get("group") is not None:
                # ROWNUM filters before ORDER BY / GROUP BY; LIMIT would apply after.
                raise ViewTranslationError("ROWNUM combined with ORDER BY / GROUP BY")
            select.set("where", exp.Where(this=exp.and_(*kept)) if kept else None)
            select.limit(limit, copy=False)
    if any(isinstance(c, exp.Column) and c.name.upper() == "ROWNUM" and not c.table
           for c in body.find_all(exp.Column)):
        raise ViewTranslationError("ROWNUM used other than as a top-level 'ROWNUM <= n' filter")
    return body


def translate_view(definition: str, source_dialect: str, view_db_schema: str, target_db_schema: str,
                   target_view: str, name_map: dict[tuple[str, str], tuple[str, str]], case: str,
                   source_columns: list[str] | None = None, target_catalog: str | None = None) -> str:
    """Return a DuckDB-dialect CREATE OR REPLACE VIEW for a source view definition.

    - Accepts a bare SELECT or a full CREATE VIEW; only the query body is kept, so
      source-only view properties (COMMENT, SECURE, ...) don't leak through.
    - Unqualified table references are qualified with the view's own db_schema (that is how
      the source resolves them), CTE references excepted.
    - References to replicated objects are re-pointed at their target names (`name_map`,
      keyed by upper-cased (db_schema, name)); catalog qualifiers are dropped.
    - All identifiers follow naming.case, matching the column names the tables were loaded with.
    - A reference whose db_schema has the same name as `target_catalog` is qualified with the
      catalog too: DuckDB can't otherwise tell "shop"."orders" (catalog.table) from
      (schema.table) when a catalog and a schema are both called shop.
    """
    try:
        tree = sqlglot.parse_one(sql=definition, read=source_dialect)
    except sqlglot.errors.ParseError as e:
        raise ViewTranslationError(f"can't parse the view definition: {e}") from e

    column_list = None
    if isinstance(tree, exp.Create):
        if isinstance(tree.this, exp.Schema) and tree.this.expressions:
            column_list = [i.name for i in tree.this.expressions if isinstance(i, exp.Identifier)]
        body = tree.expression
    else:
        body = tree
        # Oracle's ALL_VIEWS.TEXT omits CREATE VIEW v (a, b) column lists; the dictionary
        # columns carry the view's real column names.
        if source_dialect == "oracle" and source_columns:
            column_list = source_columns
    if not isinstance(body, exp.Query):
        raise ViewTranslationError("the view definition isn't a query")

    body = normalize_identifiers(body, dialect=source_dialect)
    if source_dialect == "oracle":
        body = _oracle_rewrites(body=body)
    body = qualify_tables(body, db=_quoted(name=view_db_schema), dialect=source_dialect)

    table_nodes = set()
    for table in body.find_all(exp.Table):
        if not table.args.get("db"):
            continue   # a CTE reference or table function
        table_nodes.add(id(table.this))
        table_nodes.add(id(table.args["db"]))
        target = name_map.get((table.db.upper(), table.name.upper()))
        if target is not None:
            table.set("db", _quoted(name=target[0]))
            table.set("this", _quoted(name=target[1]))
        else:
            table.set("db", _quoted(name=apply_case(name=table.db, case=case)))
            table.set("this", _quoted(name=apply_case(name=table.name, case=case)))
        table.set("catalog", _catalog_qualifier(db_schema=table.db, catalog=target_catalog))

    for ident in body.find_all(exp.Identifier):
        if id(ident) not in table_nodes:
            ident.set("this", apply_case(name=ident.this, case=case))
            ident.set("quoted", True)

    # comments=False: dbt appends a per-run /* {"invocation_id": ...} */ comment, which would
    # otherwise change the definition hash on every dbt run.
    select_sql = body.sql(dialect="duckdb", pretty=True, comments=False, identify=True)
    return create_view_sql(target_db_schema=target_db_schema, target_view=target_view, body=select_sql,
                           columns=[apply_case(name=c, case=case) for c in column_list] if column_list else None,
                           target_catalog=target_catalog)


def _catalog_qualifier(db_schema: str, catalog: str | None) -> exp.Identifier | None:
    if catalog and db_schema.lower() == catalog.lower():
        return _quoted(name=catalog)
    return None


def create_view_sql(target_db_schema: str, target_view: str, body: str, columns: list[str] | None = None,
                    target_catalog: str | None = None) -> str:
    parts = [target_db_schema, target_view]
    if _catalog_qualifier(db_schema=target_db_schema, catalog=target_catalog) is not None:
        parts.insert(0, target_catalog)
    name = ".".join(exp.to_identifier(p, quoted=True).sql(dialect="duckdb") for p in parts)
    cols = ""
    if columns:
        cols = " (" + ", ".join(exp.to_identifier(c, quoted=True).sql(dialect="duckdb") for c in columns) + ")"
    return f"CREATE OR REPLACE VIEW {name}{cols} AS\n{body.strip().rstrip(';')}"


def load_override(overrides_dir: str | None, db_schema: str, view: str) -> str | None:
    """A hand-written view body from overrides_dir/<DB_SCHEMA>.<VIEW>.sql (case-insensitive)."""
    if not overrides_dir or not os.path.isdir(overrides_dir):
        return None
    wanted = f"{db_schema}.{view}.sql".lower()
    for entry in os.listdir(overrides_dir):
        if entry.lower() == wanted:
            return Path(overrides_dir, entry).read_text(encoding="utf-8")
    return None
