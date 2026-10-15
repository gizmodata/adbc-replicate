"""Translate source view definitions into target (DuckDB-dialect) CREATE VIEW statements."""

from __future__ import annotations

import os
import re
from pathlib import Path

import sqlglot
from sqlglot import exp, transforms
from sqlglot.errors import ErrorLevel, UnsupportedError
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers
from sqlglot.optimizer.qualify_tables import qualify_tables

from .matching import apply_case

# sqlglot's Select keeps its FROM and WITH clauses under these argument names.
_FROM = "from_"
_WITH = "with_"


class ViewTranslationError(Exception):
    pass


def _quoted(name: str) -> exp.Identifier:
    return exp.to_identifier(name, quoted=True)


# Oracle TO_CHAR(x, 'format') -> an exact DuckDB equivalent, for the format elements whose
# output matches character for character. Anything else stays untranslatable.
_DATE_ELEMENTS = [("YYYY", "%Y"), ("HH24", "%H"), ("HH12", "%I"), ("HH", "%I"), ("MM", "%m"), ("MI", "%M"),
                  ("SS", "%S"), ("DD", "%d"), ("YY", "%y")]
_DATE_LITERALS = set("-/:., ")


def _strftime_format(fmt: str) -> str | None:
    out, i = [], 0
    while i < len(fmt):
        if fmt[i] == '"':                                   # quoted literal text
            end = fmt.find('"', i + 1)
            if end < 0:
                return None
            out.append(fmt[i + 1:end].replace("%", "%%"))
            i = end + 1
            continue
        if fmt[i] in _DATE_LITERALS:
            out.append(fmt[i])
            i += 1
            continue
        for element, code in _DATE_ELEMENTS:
            if fmt[i:i + len(element)].upper() == element:
                out.append(code)
                i += len(element)
                break
        else:
            return None
    return "".join(out)


def _fill(template: str, value: exp.Expression, second: exp.Expression | None = None,
          more: dict[str, exp.Expression] | None = None) -> exp.Expression:
    """Parse a DuckDB template and put the given nodes in place of __x__ (and __y__, and the keys of
    `more`). The first use of each takes the node itself, so later rewrites still reach the expressions
    inside it."""
    tree = sqlglot.parse_one(sql=template, read="duckdb")
    nodes, used = {"__x__": value, "__y__": second, **(more or {})}, set()
    if isinstance(tree, exp.Column) and nodes.get(tree.name) is not None:     # the template is just {1}
        return nodes[tree.name]
    for column in list(tree.find_all(exp.Column)):
        node = nodes.get(column.name)
        if node is None:
            continue
        column.replace(node.copy() if column.name in used else node)
        used.add(column.name)
    return tree


def _to_char(value: exp.Expression, fmt: str) -> exp.Expression | None:
    if fmt == "DAY":        # English day name, upper case, padded to 9 like Oracle
        return _fill(template="RPAD(UPPER(STRFTIME(__x__, '%A')), 9, ' ')", value=value)
    if fmt == "fmDAY":
        return _fill(template="UPPER(STRFTIME(__x__, '%A'))", value=value)
    body = fmt[2:] if fmt[:2].lower() == "fm" else None
    if body and re.fullmatch(r"0+", body):                  # fm0000000: zero-padded integer
        k = len(body)
        padded = f"LPAD(CAST(CAST(ABS(ROUND(__x__)) AS BIGINT) AS VARCHAR), {k}, '0')"
        return _fill(template=f"CASE WHEN ABS(ROUND(__x__)) >= {10 ** k} THEN REPEAT('#', {k + 1}) "
                              f"WHEN __x__ < 0 THEN '-' || {padded} ELSE {padded} END", value=value)  # -0.4: -0000000
    if body and re.fullmatch(r"9*0", body.replace(",", "")):   # fm999,999,990: grouped integer
        digits = body.replace(",", "")
        grouped = ",".join(reversed([digits[max(0, i - 3):i] for i in range(len(digits), 0, -3)]))
        if body != grouped:
            return None
        return _fill(template=f"CASE WHEN ABS(ROUND(__x__)) >= {10 ** len(digits)} THEN REPEAT('#', {len(body) + 1}) "
                              f"WHEN __x__ < 0 THEN '-' || FORMAT('{{:,}}', CAST(ABS(ROUND(__x__)) AS BIGINT)) "
                              f"ELSE FORMAT('{{:,}}', CAST(ROUND(__x__) AS BIGINT)) END", value=value)
    if body is None:
        code = _strftime_format(fmt=fmt)
        if code is not None:
            return exp.Anonymous(this="STRFTIME", expressions=[value, exp.Literal.string(code)])
    return None


# Oracle date semantics. An Oracle DATE has a time of day (to the second), unlike a DuckDB DATE,
# and SYSDATE / SYSTIMESTAMP / CURRENT_DATE are wall-clock time in the database server's (or
# session's) zone. sqlglot maps CAST(x AS DATE) to DuckDB DATE (dropping the time) and SYSDATE
# to UTC, both silently; these rewrites keep Oracle's meaning or refuse.
_TRUNC_UNITS = {"DD": "day", "DDD": "day", "J": "day", "HH": "hour", "HH12": "hour", "HH24": "hour",
                "MI": "minute", "MM": "month", "MON": "month", "MONTH": "month", "RM": "month", "Q": "quarter",
                "YYYY": "year", "YYY": "year", "YY": "year", "Y": "year", "YEAR": "year", "SYYYY": "year",
                "SYEAR": "year", "IW": "week"}
_SYSTIMESTAMP_NODES = (exp.Systimestamp, exp.Localtimestamp)
_NOW_NODES = (exp.CurrentTimestamp, exp.CurrentDate, *_SYSTIMESTAMP_NODES)
_DATE_RESULT_NODES = (exp.AddMonths, exp.LastDay, exp.NextDay, exp.StrToDate, exp.StrToTime, exp.DateTrunc)
_DATE_TYPES = (exp.DataType.Type.DATE, exp.DataType.Type.TIMESTAMP, exp.DataType.Type.TIMESTAMP_S,
               exp.DataType.Type.TIMESTAMPTZ, exp.DataType.Type.TIMESTAMPLTZ)


_STRING_RESULTS = (exp.ToChar, exp.Upper, exp.Lower, exp.Substring, exp.Concat, exp.DPipe, exp.Trim)
# what TO_CHAR rewrites produce, plus common Oracle string functions sqlglot leaves anonymous
_STRING_FUNCTIONS = {"STRFTIME", "RPAD", "LPAD", "FORMAT", "REPEAT", "SUBSTR", "INITCAP", "REPLACE"}
_NUMBER_TYPES = ("NUMBER", "FLOAT", "BINARY_FLOAT", "BINARY_DOUBLE", "INTEGER", "DECIMAL", "NUMERIC")
_NUMERIC_RESULTS = (exp.Round, exp.Floor, exp.Ceil, exp.Abs, exp.Count, exp.Sum, exp.Avg, exp.Mul, exp.Div,
                    exp.Mod, exp.Length, exp.Extract)


class _OracleTypes:
    """Infers the Oracle type class of an expression: DATE, TIMESTAMP, NUMBER, STRING, INTERVAL or None.
    Columns come from the source catalog, through table aliases, subqueries and CTEs."""

    def __init__(self, column_types: dict, default_schema: str) -> None:
        self.column_types = column_types
        self.default_schema = default_schema
        self.memo: dict[int, str | None] = {}

    def of(self, node: exp.Expression | None) -> str | None:
        if node is None:
            return None
        key = id(node)
        if key not in self.memo:
            self.memo[key] = None          # guards against cycles
            self.memo[key] = self._infer(node)
        return self.memo[key]

    @staticmethod
    def _class(data_type: str | None) -> str | None:
        data_type = (data_type or "").upper()
        if data_type == "DATE":
            return "DATE"
        if data_type.startswith("TIMESTAMP"):
            return "TIMESTAMP"
        if data_type.startswith(_NUMBER_TYPES):
            return "NUMBER"
        if data_type.startswith(("VARCHAR", "NVARCHAR", "CHAR", "NCHAR")):
            return "STRING"
        return None

    def _infer(self, node: exp.Expression) -> str | None:
        if isinstance(node, exp.Paren):
            return self.of(node.this)
        if isinstance(node, exp.Literal):
            return "STRING" if node.is_string else "NUMBER"
        if isinstance(node, _STRING_RESULTS) or (isinstance(node, exp.Anonymous)
                                                 and node.name.upper() in _STRING_FUNCTIONS):
            return "STRING"
        if isinstance(node, exp.Neg):
            return self.of(node.this)
        if isinstance(node, exp.CurrentDate) or (isinstance(node, exp.CurrentTimestamp)
                                                 and node.args.get("sysdate")):
            return "DATE"
        if isinstance(node, (exp.CurrentTimestamp, *_SYSTIMESTAMP_NODES)):
            return "TIMESTAMP"
        if isinstance(node, _DATE_RESULT_NODES):
            return "DATE"
        if isinstance(node, exp.Cast):
            to = node.to.this
            if to == exp.DataType.Type.DATE:
                return "DATE"
            if to in _DATE_TYPES:
                return "TIMESTAMP"
            return "NUMBER" if to in exp.DataType.NUMERIC_TYPES else None
        if isinstance(node, exp.Anonymous):
            name = node.name.upper()
            if name == "TRUNC" and node.expressions:
                kind = self.of(node.expressions[0])
                return "DATE" if kind in ("DATE", "TIMESTAMP") else kind
            if name in ("NVL", "NVL2", "GREATEST", "LEAST") and node.expressions:
                return self.of(node.expressions[-1] if name == "NVL2" else node.expressions[0])
            return None
        if isinstance(node, (exp.Coalesce, exp.Max, exp.Min, exp.Greatest, exp.Least)):
            return self.of(node.this)
        if isinstance(node, exp.Case):
            ifs = node.args.get("ifs") or []
            return self.of(ifs[0].args.get("true")) if ifs else self.of(node.args.get("default"))
        if isinstance(node, _NUMERIC_RESULTS):
            return "NUMBER"
        if isinstance(node, (exp.Add, exp.Sub)):
            left, right = self.of(node.this), self.of(node.expression)
            dates = ("DATE", "TIMESTAMP")
            plus = isinstance(node, exp.Add) and left == "NUMBER" and right in dates
            if (left in dates and right == "NUMBER") or plus:
                return "DATE"
            if isinstance(node, exp.Sub) and left in dates and right in dates:
                return "NUMBER" if left == right == "DATE" else "INTERVAL"
            if left == right == "NUMBER":
                return "NUMBER"
            return None
        if isinstance(node, exp.Column):
            return self._column(column=node)
        return None

    def _column(self, column: exp.Column) -> str | None:
        select = column.find_ancestor(exp.Select)
        if select is None:
            return None
        from_ = select.args.get(_FROM)
        sources = ([from_.this] if from_ is not None else []) + [j.this for j in select.args.get("joins") or []]
        if column.table:
            sources = [t for t in sources if t.alias_or_name.upper() == column.table.upper()]
        if len(sources) != 1:
            return None
        source = sources[0]
        if isinstance(source, exp.Subquery):
            return self._projection(query=source.this, name=column.name)
        if isinstance(source, exp.Table):
            cte = self._cte(node=select, name=source.name) if not source.db else None
            if cte is not None:
                return self._projection(query=cte.this, name=column.name)
            columns = self.column_types.get(((source.db or self.default_schema).upper(), source.name.upper()), {})
            return self._class(data_type=columns.get(column.name.upper()))
        return None

    @staticmethod
    def _cte(node: exp.Expression, name: str) -> exp.CTE | None:
        while node is not None:
            with_ = node.args.get(_WITH) if isinstance(node, exp.Query) else None
            for cte in (with_.expressions if with_ else []):
                if cte.alias.upper() == name.upper():
                    return cte
            node = node.parent
        return None

    def _projection(self, query: exp.Expression, name: str) -> str | None:
        while isinstance(query, exp.SetOperation):
            query = query.this       # UNION branches share the first branch's column types
        if not isinstance(query, exp.Select):
            return None
        for projection in query.expressions:
            if projection.alias_or_name.upper() == name.upper():
                return self.of(projection.this if isinstance(projection, exp.Alias) else projection)
        return None


def _date_plus(date: exp.Expression, days: exp.Expression, sign: str) -> exp.Expression:
    """Oracle DATE/TIMESTAMP +/- n days: a DATE, the timestamp first truncated to the second and the
    result rounded to the nearest second (checked against Oracle)."""
    return _fill(template=f"MAKE_TIMESTAMP(CAST(ROUND(EPOCH(DATE_TRUNC('second', __x__)) {sign} (__y__) * 86400) "
                          "AS BIGINT) * 1000000)", value=date, second=days)


def _oracle_dates(body: exp.Expression, time_zone: str | None, column_types: dict,
                  default_schema: str) -> exp.Expression:
    types = _OracleTypes(column_types=column_types, default_schema=default_schema)
    # Decide everything on the untouched Oracle tree, then rewrite.
    arithmetic = []
    for node in body.find_all(exp.Add, exp.Sub):
        left, right = types.of(node.this), types.of(node.expression)
        if left in ("DATE", "TIMESTAMP") and right == "NUMBER":
            arithmetic.append((node, "plus", node.this, node.expression))
        elif isinstance(node, exp.Add) and left == "NUMBER" and right in ("DATE", "TIMESTAMP"):
            arithmetic.append((node, "plus", node.expression, node.this))
        elif isinstance(node, exp.Sub) and left == right == "DATE":
            arithmetic.append((node, "diff", node.this, node.expression))
    # Oracle converts a string operand of arithmetic to a number (TO_CHAR(d, 'MI') / 6); DuckDB doesn't.
    to_number = [side for node in body.find_all(exp.Add, exp.Sub, exp.Mul, exp.Div)
                 for side in (node.this, node.expression) if types.of(side) == "STRING"]
    truncs = [node for node in body.find_all(exp.Anonymous)
              if node.name.upper() == "TRUNC" and len(node.expressions) == 1
              and types.of(node.expressions[0]) in ("DATE", "TIMESTAMP")]
    for side in to_number:
        side.replace(exp.Cast(this=side.copy(), to=exp.DataType.build("DECIMAL(38,10)")))
    for node, kind, a, b in arithmetic:
        if kind == "diff":           # DATE - DATE: days, as a number
            node.replace(_fill(template="(EPOCH(__x__) - EPOCH(__y__)) / 86400", value=a, second=b))
        else:
            node.replace(_date_plus(date=a, days=b, sign="-" if isinstance(node, exp.Sub) else "+"))
    # TRUNC(date) -> DATE_TRUNC (TRUNC(number) is DuckDB's too, so only known dates are rewritten).
    for node in truncs:
        node.replace(exp.DateTrunc(this=node.expressions[0], unit=exp.Literal.string("day")))
    # TRUNC(date, 'DD') arrives as DATE_TRUNC with Oracle's unit names, which DuckDB doesn't know.
    for node in list(body.find_all(exp.DateTrunc)):
        unit = node.args.get("unit")
        if isinstance(unit, exp.Literal) and unit.is_string:
            mapped = _TRUNC_UNITS.get(unit.this.upper())
            if mapped is None and unit.this.lower() not in set(_TRUNC_UNITS.values()):
                raise ViewTranslationError(f"TRUNC(date, '{unit.this}') has no exact DuckDB equivalent")
            node.set("unit", exp.Literal.string(mapped or unit.this.lower()))
    # CAST(x AS DATE): an Oracle DATE keeps the time of day, to the second.
    for node in list(body.find_all(exp.Cast)):
        if node.to.this == exp.DataType.Type.DATE:
            node.set("to", exp.DataType.build("TIMESTAMP_S", dialect="duckdb"))
    # SYSDATE & co: wall-clock time in the source's zone, like the DATE values stored there.
    for node in list(body.find_all(*_NOW_NODES)):
        if time_zone is None:
            raise ViewTranslationError(
                "uses SYSDATE / SYSTIMESTAMP / CURRENT_DATE, whose value depends on the Oracle server's time "
                "zone; set views.source_time_zone (e.g. America/New_York)")
        seconds = isinstance(node, exp.CurrentDate) or (isinstance(node, exp.CurrentTimestamp)
                                                        and node.args.get("sysdate"))
        now = f"CURRENT_TIMESTAMP AT TIME ZONE '{time_zone}'"
        node.replace(sqlglot.parse_one(sql=f"CAST({now} AS TIMESTAMP_S)" if seconds else now, read="duckdb"))
    return body


def _int_literal(node: exp.Expression | None, default: int) -> int | None:
    if node is None:
        return default
    if isinstance(node, exp.Neg):
        value = _int_literal(node=node.this, default=0) if node.this is not None else None
        return -value if value is not None and isinstance(node.this, exp.Literal) else None
    return int(node.this) if isinstance(node, exp.Literal) and node.is_int else None


def _regexp_substr(node: exp.RegexpSubstr) -> exp.Expression | None:
    args = node.args
    if _int_literal(node=args.get("position"), default=1) != 1 \
            or _int_literal(node=args.get("occurrence"), default=1) != 1:
        return None
    group = _int_literal(node=args.get("group"), default=0)
    flags = args.get("parameters")
    if group is None or (flags is not None and not (isinstance(flags, exp.Literal) and flags.is_string
                                                    and set(flags.this) <= {"i", "c"})):
        return None
    extract = [args["this"], args["expression"], exp.Literal.number(group)]
    if flags is not None and "i" in flags.this and not flags.this.endswith("c"):   # the last of i/c wins
        extract.append(exp.Literal.string("i"))
    return exp.func("NULLIF", exp.Anonymous(this="REGEXP_EXTRACT", expressions=extract), exp.Literal.string(""))


def _instr(node: exp.StrPosition) -> exp.Expression | None:
    """INSTR(s, sub, position, occurrence) -> the start of the occurrence-th match, searching forward
    from position (> 0) or backward from length(s) + position + 1 (< 0), matches allowed to overlap;
    0 if there is none (or position is 0), NULL if any argument is NULL. Checked against Oracle.
    INSTR(s, sub [, 1 [, 1]]) is plain STRPOS. (sqlglot turns a position into
    STRPOS(SUBSTRING(s, position), sub), wrong for a negative one, and refuses an occurrence.)"""
    position, occurrence = node.args.get("position"), node.args.get("occurrence")
    pos, occ = _int_literal(node=position, default=1), _int_literal(node=occurrence, default=1)
    if pos == 1 and occ == 1:       # sqlglot refuses even an explicit occurrence of 1
        return exp.StrPosition(this=node.this, substr=node.args["substr"]) if position or occurrence else None
    if occ is not None and occ < 1:
        raise ViewTranslationError(f"INSTR with occurrence {occ} (Oracle raises ORA-01428)")
    match = "substring(__s__, __instr_pos, length(__sub__)) = __sub__"
    forward = f"list_filter(range(__p__, length(__s__) - length(__sub__) + 2), lambda __instr_pos: {match})"
    last_start = "least(length(__s__) + __p__ + 1, length(__s__) - length(__sub__) + 1)"
    backward = f"list_reverse(list_filter(range(1, {last_start} + 1), lambda __instr_pos: {match}))"
    if pos is not None and pos > 0:
        found = forward
    elif pos is not None and pos < 0:
        found = backward
    elif pos == 0:
        return _fill(template="CASE WHEN __s__ IS NULL OR __sub__ IS NULL THEN NULL ELSE 0 END", value=node.this,
                     more={"__s__": node.this, "__sub__": node.args["substr"]})
    else:
        found = f"CASE WHEN __p__ > 0 THEN {forward} WHEN __p__ < 0 THEN {backward} END"
    found = f"list_extract({found}, __n__)"
    if occ is None:     # Oracle raises for occurrence < 1; DuckDB's list_extract(l, -1) is the last match
        found = f"CASE WHEN __n__ < 1 THEN error('INSTR occurrence must be at least 1') ELSE {found} END"
    def integer(arg: exp.Expression | None, literal: int | None) -> exp.Expression:
        if arg is None or literal is not None:
            return exp.Literal.number(1 if arg is None else literal)
        return _fill(template="CAST(TRUNC(__x__) AS BIGINT)", value=arg)   # Oracle truncates a fraction

    nodes = {"__s__": node.this, "__sub__": node.args["substr"],
             "__p__": integer(arg=position, literal=pos), "__n__": integer(arg=occurrence, literal=occ)}
    # Oracle returns NULL when any argument is NULL (coalesce would turn that into 0).
    maybe_null = [k for k, v in nodes.items() if not (isinstance(v, exp.Literal) or _int_literal(node=v, default=0))]
    template = f"coalesce({found}, 0)"
    if maybe_null:
        template = f"CASE WHEN {' OR '.join(f'{k} IS NULL' for k in maybe_null)} THEN NULL ELSE {template} END"
    return _fill(template=template, value=node.this, more=nodes)


def _keep(node: exp.Window) -> exp.Expression:
    """MIN/MAX(x) KEEP (DENSE_RANK FIRST|LAST ORDER BY k, ...) -> min_by/max_by(x, create_sort_key(k,
    <its order>, ..., x, <tie-break>)). Oracle takes the MIN/MAX of x over all the rows tied on the
    first/last keys, skipping NULL x, so x is the last sort key, ordered so that the wanted value wins.
    Each key keeps its Oracle order: ASC/DESC, NULLS LAST by default ascending and FIRST descending
    (sqlglot spells the defaults out). Checked against Oracle. Other aggregates are refused."""
    agg, first = node.this, bool(node.args.get("first"))
    keys = node.args["order"].expressions if node.args.get("order") else []
    if not isinstance(agg, (exp.Min, exp.Max)) or agg.expressions or isinstance(agg.this, exp.Distinct) or not keys:
        raise ViewTranslationError("KEEP (DENSE_RANK ...) is translated for MIN and MAX only")
    want_max = isinstance(agg, exp.Max)
    # min_by takes the smallest sort key (FIRST), max_by the largest (LAST); NULL x must never win.
    tie_break = {(True, False): "DESC NULLS LAST", (False, False): "ASC NULLS LAST",
                 (True, True): "ASC NULLS FIRST", (False, True): "DESC NULLS FIRST"}[(want_max, not first)]
    parts: list[exp.Expression] = []
    for key in keys:
        parts += [key.this, exp.Literal.string(f"{'DESC' if key.args.get('desc') else 'ASC'} NULLS "
                                               f"{'FIRST' if key.args.get('nulls_first') else 'LAST'}")]
    x = agg.this
    parts += [x.copy(), exp.Literal.string(tie_break)]
    return exp.Anonymous(this="MIN_BY" if first else "MAX_BY",
                         expressions=[x, exp.Anonymous(this="create_sort_key", expressions=parts)])


# Oracle hierarchical queries (START WITH / CONNECT BY) -> WITH RECURSIVE. The recursive CTE carries
# every column of the source plus: the LEVEL, the row's position in Oracle's depth-first order (a list
# of sibling ranks, ORDER SIBLINGS BY within each parent: ROWNUM follows it), the parent's position
# (for CONNECT_BY_ISLEAF), the ancestors' PRIOR values (a cycle stops instead of recursing forever;
# Oracle raises ORA-01436 without NOCYCLE), and one column per SYS_CONNECT_BY_PATH / CONNECT_BY_ROOT.
_CB_LEVEL, _CB_ORDER, _CB_PARENT, _CB_VISITED = "__cb_level", "__cb_order", "__cb_parent", "__cb_visited"


def _bare(column: exp.Expression, name: str) -> bool:
    return isinstance(column, exp.Column) and not column.table and not column.this.quoted \
        and column.name.upper() == name


def _rebind(node: exp.Expression, level: exp.Expression, prior: str | None, child: str | None) -> exp.Expression:
    """A copy of `node` for one side of the recursive CTE: LEVEL -> `level`; PRIOR x -> x read from the
    parent row (`prior`); other unqualified columns qualified with `child` (both sides have the same
    column names). Columns inside subqueries keep their own scope."""
    node = node.copy()
    wrapped = isinstance(node, exp.Prior)
    if wrapped:
        node = exp.Paren(this=node)            # so that a bare PRIOR x can be replaced in place
    done = set()
    for p in list(node.find_all(exp.Prior)):
        inner = p.this
        for column in inner.find_all(exp.Column):
            if not column.find_ancestor(exp.Select) and not _bare(column=column, name="LEVEL") and prior:
                column.set("table", _quoted(name=prior))
            done.add(id(column))
        p.replace(inner)
    for column in list(node.find_all(exp.Column)):
        if id(column) in done or column.find_ancestor(exp.Select):
            continue
        if _bare(column=column, name="LEVEL"):
            column.replace(level.copy())
        elif child and not column.table:
            column.set("table", _quoted(name=child))
    return node.this if wrapped else node


def _path_text(value: exp.Expression) -> exp.Expression:
    """How SYS_CONNECT_BY_PATH turns a value into text: numbers the way Oracle prints them (no
    trailing zeros, no leading 0 before the point: 1.50 -> '1.5', 0.5 -> '.5'), NULL as ''."""
    number = ("regexp_replace(regexp_replace(regexp_replace(CAST(__x__ AS VARCHAR), '(\\.\\d*?)0+$', '\\1'), "
              "'\\.$', ''), '^(-?)0\\.', '\\1.')")
    return _fill(template=f"coalesce(CASE WHEN typeof(__x__) LIKE 'DECIMAL%' OR typeof(__x__) IN ('DOUBLE', 'FLOAT') "
                          f"THEN {number} ELSE CAST(__x__ AS VARCHAR) END, '')", value=value)


def _connect_by(select: exp.Select, name: str) -> None:
    """Rewrite one hierarchical SELECT in place as SELECT ... FROM <recursive CTE `name`>."""
    connect: exp.Connect = select.args["connect"]
    from_ = select.args.get(_FROM)
    if from_ is None or select.args.get("joins") or not isinstance(from_.this, (exp.Table, exp.Subquery)):
        raise ViewTranslationError("CONNECT BY over a join (only a single table, view or subquery is translated)")
    source = from_.this
    if not source.alias_or_name:                # FROM (SELECT ...) without an alias
        source.set("alias", exp.TableAlias(this=_quoted(name=f"{name}_src")))
    alias = source.alias_or_name
    dual = isinstance(source, exp.Table) and not source.db and source.name.upper() == "DUAL"
    if any(isinstance(e, exp.Star) or (isinstance(e, exp.Column) and isinstance(e.this, exp.Star))
           for e in select.expressions):
        raise ViewTranslationError("SELECT * in a CONNECT BY query")
    start_columns = {id(c) for c in connect.args["start"].find_all(exp.Column)} if connect.args.get("start") else set()
    for column in select.find_all(exp.Column):
        if _bare(column=column, name="ROWNUM") and id(column) not in start_columns \
                and column.find_ancestor(exp.Where, exp.Connect, exp.Order, exp.Group, exp.Having) is not None:
            raise ViewTranslationError("ROWNUM in the WHERE / CONNECT BY clause of a CONNECT BY query")

    # START WITH ... AND ROWNUM <= n (or = 1): only the first n start rows (e.g. one anchor for an item
    # that appears under several parents).
    start, limit = connect.args.get("start"), None
    if start is not None:
        kept = []
        for cond in (start.flatten() if isinstance(start, exp.And) else [start]):
            if isinstance(cond, (exp.EQ, exp.LTE, exp.LT)) and _bare(column=cond.this, name="ROWNUM") \
                    and isinstance(cond.expression, exp.Literal) and cond.expression.is_int:
                n = int(cond.expression.name)
                n = 1 if isinstance(cond, exp.EQ) and n == 1 else n - 1 if isinstance(cond, exp.LT) else \
                    n if isinstance(cond, exp.LTE) else None
                if n is None:
                    raise ViewTranslationError("START WITH ... ROWNUM = n with n other than 1")
                limit = n if limit is None else min(limit, n)
            elif any(_bare(column=c, name="ROWNUM") for c in cond.find_all(exp.Column)):
                raise ViewTranslationError("ROWNUM in START WITH other than 'ROWNUM <= n'")
            else:
                kept.append(cond)
        start = exp.and_(*kept) if kept else None

    condition = connect.args["connect"]
    priors = [p.this for p in condition.find_all(exp.Prior)]
    if not priors and not any(_bare(column=c, name="LEVEL") for c in condition.find_all(exp.Column)):
        raise ViewTranslationError("CONNECT BY without PRIOR or LEVEL")
    siblings = []
    order = select.args.get("order")
    if order is not None and order.args.get("siblings"):
        siblings = order.expressions
        select.set("order", None)

    prior_t, child_t = f"{name}_prior", alias
    one, next_level = exp.Literal.number(1), exp.column(_CB_LEVEL, table=prior_t, quoted=True) + exp.Literal.number(1)
    anchor = [exp.Literal.number(1).as_(_CB_LEVEL, quoted=True)]
    step = [next_level.as_(_CB_LEVEL, quoted=True)]
    if not dual:
        anchor.insert(0, exp.Column(this=exp.Star(), table=_quoted(name=alias)))
        step.insert(0, exp.Column(this=exp.Star(), table=_quoted(name=alias)))

    def window(partition: exp.Expression | None, side: str | None) -> exp.Expression:
        keys = [_rebind(node=o, level=one if side is None else next_level, prior=prior_t, child=side) for o in siblings]
        spec = exp.Window(this=exp.RowNumber(), partition_by=[partition] if partition is not None else None,
                          order=exp.Order(expressions=keys) if keys else None)
        return exp.Array(expressions=[spec])

    prior_order = exp.column(_CB_ORDER, table=prior_t, quoted=True)
    anchor.append(window(partition=None, side=None).as_(_CB_ORDER, quoted=True))
    step.append(exp.DPipe(this=prior_order, expression=window(partition=prior_order.copy(), side=child_t))
                .as_(_CB_ORDER, quoted=True))
    no_parent = exp.Cast(this=exp.null(), to=exp.DataType.build("BIGINT[]", dialect="duckdb"))
    anchor.append(no_parent.as_(_CB_PARENT, quoted=True))
    step.append(prior_order.copy().as_(_CB_PARENT, quoted=True))
    # Oracle's cycle (NOCYCLE stops there, ORA-01436 without it): a row whose PRIOR values repeat an
    # ancestor's, checked against Oracle. A NULL value never repeats one. A LEVEL-bounded generator
    # without PRIOR can't cycle.
    visited_key = child_key = None
    if priors:
        keys = [_rebind(node=p, level=one, prior=None, child=None) for p in priors]
        visited_key = keys[0] if len(keys) == 1 else exp.Anonymous(this="ROW", expressions=keys)
        anchor.append(exp.Array(expressions=[visited_key]).as_(_CB_VISITED, quoted=True))
        child_key = _rebind(node=visited_key, level=next_level, prior=prior_t, child=child_t)
        step.append(exp.DPipe(this=exp.column(_CB_VISITED, table=prior_t, quoted=True),
                              expression=exp.Array(expressions=[child_key.copy()])).as_(_CB_VISITED, quoted=True))

    # SYS_CONNECT_BY_PATH / CONNECT_BY_ROOT -> CTE columns; LEVEL / ROWNUM / CONNECT_BY_ISLEAF -> derived
    me = exp.to_identifier(alias, quoted=True)
    for i, node in enumerate(list(select.find_all(exp.Anonymous, exp.ConnectByRoot))):
        if (isinstance(node, exp.Anonymous) and str(node.this).upper() != "SYS_CONNECT_BY_PATH") \
                or node.find_ancestor(exp.Select) is not select:
            continue
        column = f"__cb_{i}"
        if isinstance(node, exp.ConnectByRoot):
            value = node.this
            anchor.append(_rebind(node=value, level=one, prior=None, child=None).as_(column, quoted=True))
            step.append(exp.column(column, table=prior_t, quoted=True).as_(column, quoted=True))
        else:
            value, sep = node.expressions
            anchor.append(exp.DPipe(this=sep.copy(), expression=_path_text(
                value=_rebind(node=value, level=one, prior=None, child=None))).as_(column, quoted=True))
            parent_path = exp.DPipe(this=exp.column(column, table=prior_t, quoted=True), expression=sep.copy())
            text = _path_text(value=_rebind(node=value, level=next_level, prior=prior_t, child=child_t))
            step.append(exp.DPipe(this=parent_path, expression=text).as_(column, quoted=True))
        node.replace(exp.Column(this=_quoted(name=column), table=me.copy()))
    for column in list(select.find_all(exp.Column)):
        if column.find_ancestor(exp.Connect) is not None or column.find_ancestor(exp.Select) is not select:
            continue
        if _bare(column=column, name="LEVEL"):
            column.replace(exp.Column(this=_quoted(name=_CB_LEVEL), table=me.copy()))
        elif _bare(column=column, name="ROWNUM"):
            column.replace(exp.Window(this=exp.RowNumber(), order=exp.Order(
                expressions=[exp.Ordered(this=exp.Column(this=_quoted(name=_CB_ORDER), table=me.copy()))])))
        elif _bare(column=column, name="CONNECT_BY_ISLEAF"):
            child_rows = exp.select("1").from_(exp.Table(this=_quoted(name=name), alias=exp.TableAlias(
                this=_quoted(name=f"{name}_child")))).where(exp.EQ(
                    this=exp.column(_CB_PARENT, table=f"{name}_child", quoted=True),
                    expression=exp.Column(this=_quoted(name=_CB_ORDER), table=me.copy())))
            column.replace(exp.Case(ifs=[exp.If(this=exp.Exists(this=child_rows), true=exp.Literal.number(0))],
                                    default=exp.Literal.number(1)))

    anchor_select = exp.Select(expressions=anchor).from_(source.copy())
    if start is not None:
        anchor_select = anchor_select.where(start.copy())
    if limit is not None:
        anchor_select = exp.select("*").from_(exp.Subquery(this=anchor_select.limit(limit), alias=exp.TableAlias(
            this=_quoted(name=f"{name}_start"))))
    join_on = _rebind(node=condition, level=next_level, prior=prior_t, child=child_t)
    if child_key is not None:
        repeats = exp.Anonymous(this="list_contains", expressions=[
            exp.column(_CB_VISITED, table=prior_t, quoted=True), child_key.copy()])
        join_on = exp.and_(join_on, exp.Not(this=exp.Coalesce(this=repeats, expressions=[exp.false()])))
    step_select = exp.Select(expressions=step).from_(exp.Table(this=_quoted(name=name),
                                                              alias=exp.TableAlias(this=_quoted(name=prior_t))))
    if dual:
        step_select = step_select.where(join_on)
    else:
        step_select = step_select.join(source.copy(), on=join_on)
    cte = exp.CTE(this=exp.Union(this=anchor_select, expression=step_select, distinct=False),
                  alias=exp.TableAlias(this=_quoted(name=name)))
    select.set("connect", None)
    select.set(_FROM, exp.From(this=exp.Table(this=_quoted(name=name), alias=exp.TableAlias(this=me.copy()))))
    with_ = select.args.get(_WITH)
    if with_ is None:
        select.set(_WITH, exp.With(expressions=[cte], recursive=True))
    else:
        with_.append("expressions", cte)
        with_.set("recursive", True)


def _is_null(node: exp.Expression | None) -> bool:
    return isinstance(node, exp.Null) or (isinstance(node, exp.Cast) and isinstance(node.this, exp.Null))


# Oracle pseudo-columns: values only the source database can produce, with the type a NULL
# stand-in gets. Both are reserved words, so an unquoted reference is never a real column.
_ORACLE_PSEUDO_COLUMNS = {"ORA_ROWSCN": "DECIMAL(38,0)", "ROWID": "VARCHAR"}


def _oracle_rewrites(body: exp.Expression, pseudo_columns: str = "fail") -> exp.Expression:
    """Oracle-only constructs with a well-defined DuckDB equivalent."""
    for column in list(body.find_all(exp.Column)):
        name = column.name.upper()
        if name in _ORACLE_PSEUDO_COLUMNS and not column.this.quoted:
            if pseudo_columns != "null":
                raise ViewTranslationError(
                    f"uses the Oracle pseudo-column {name}, which has no meaning in a copy; set "
                    "views.source_pseudo_columns: null to keep the column as NULL, or add an override")
            column.replace(exp.Cast(this=exp.null(), to=exp.DataType.build(_ORACLE_PSEUDO_COLUMNS[name])))
    # SYS_CONTEXT(namespace, parameter): a value of the Oracle session (application contexts, USERENV).
    # A copy has no such session; NULL is what Oracle returns when the context isn't set.
    for node in list(body.find_all(exp.Anonymous)):
        if str(node.this).upper() == "SYS_CONTEXT":
            if pseudo_columns != "null":
                raise ViewTranslationError(
                    "uses SYS_CONTEXT, a value of the Oracle session that a copy doesn't have; set "
                    "views.source_pseudo_columns: null to read it as NULL (what Oracle returns when the "
                    "context isn't set), or add an override")
            node.replace(exp.Cast(this=exp.null(), to=exp.DataType.build("VARCHAR")))
    # TO_DATE / TO_TIMESTAMP of NULL is NULL whatever the format (sqlglot emits STRPTIME(x) with no
    # format for TO_DATE(x), which DuckDB rejects). An Oracle DATE keeps seconds: TIMESTAMP_S.
    for node in list(body.find_all(exp.StrToDate, exp.StrToTime)):
        if _is_null(node=node.this):
            node.replace(exp.Cast(this=exp.null(), to=exp.DataType.build(
                "TIMESTAMP_S" if isinstance(node, exp.StrToDate) else "TIMESTAMP", dialect="duckdb")))

    for node in sorted(body.find_all(exp.Window), key=lambda n: -n.depth):
        if node.args.get("over") == "KEEP":
            node.replace(_keep(node=node))

    # innermost first: a rewrite copies its arguments, and copies of a nested call wouldn't be visited
    for node in sorted(body.find_all(exp.StrPosition), key=lambda n: -n.depth):
        replacement = _instr(node=node)
        if replacement is not None:
            node.replace(replacement)

    # (+) outer joins -> ANSI LEFT JOINs. sqlglot's generator would otherwise drop the (+)
    # silently, turning outer joins into inner joins.
    body = transforms.eliminate_join_marks(body)
    if any(c.args.get("join_mark") for c in body.find_all(exp.Column)):
        raise ViewTranslationError("Oracle (+) outer-join syntax that can't be rewritten as ANSI joins")

    for node in list(body.find_all(exp.ToChar)):
        fmt = node.args.get("format")
        if isinstance(fmt, exp.Literal) and fmt.is_string and not node.args.get("nlsparam"):
            replacement = _to_char(value=node.this, fmt=fmt.this)
            if replacement is not None:
                node.replace(replacement)

    # REGEXP_SUBSTR(s, p [, 1, 1 [, 'i'|'c' [, group]]]) -> regexp_extract; Oracle's no-match is NULL.
    # (Both engines take the leftmost alternative that matches, checked against Oracle.)
    for node in list(body.find_all(exp.RegexpSubstr)):
        replacement = _regexp_substr(node=node)
        if replacement is None:
            raise ViewTranslationError("REGEXP_SUBSTR with a position, occurrence or match option other than "
                                       "1, 1, 'i' or 'c' has no exact DuckDB equivalent")
        node.replace(replacement)

    # TO_NUMBER(x) -> exact decimal, like unconstrained NUMBER columns (sqlglot would emit
    # CAST(x AS DOUBLE)). Forms with a format, NLS parameter or DEFAULT stay untranslatable.
    for node in list(body.find_all(exp.ToNumber)):
        if not any(node.args.get(k) for k in ("format", "nlsparam", "default", "precision", "scale")):
            node.replace(exp.Cast(this=node.this, to=exp.DataType.build("DECIMAL(38,10)")))

    hierarchical = [s for s in body.find_all(exp.Select) if s.args.get("connect")]
    for i, select in enumerate(sorted(hierarchical, key=lambda s: -s.depth)):
        _connect_by(select=select, name=f"__cb{i + 1}")

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


_PLACEHOLDER = re.compile(r"\{(\w+)\}")


def _user_functions(body: exp.Expression, functions: dict[str, str]) -> exp.Expression:
    """views.functions: calls of source functions (e.g. PL/SQL) -> the configured DuckDB SQL, with {1},
    {2}, ... standing for the call's arguments by position and {name} for named ones (p_name => x).
    Package-qualified calls (pkg.fn) are matched as pkg.fn."""
    templates = {name.upper(): template for name, template in functions.items()}
    for node in list(body.find_all(exp.Anonymous)):
        name = str(node.this).upper()
        if isinstance(node.parent, exp.Dot) and node.parent.expression is node:
            name = f"{node.parent.this.name.upper()}.{name}"
            target = node.parent
        else:
            target = node
        template = templates.get(name)
        if template is None:
            continue
        args: dict[str, exp.Expression] = {}
        for i, arg in enumerate(node.expressions, start=1):
            if isinstance(arg, (exp.Kwarg, exp.PropertyEQ)):
                args[arg.this.name.upper()] = arg.expression
            else:
                args[str(i)] = arg
        missing = sorted({m.upper() for m in _PLACEHOLDER.findall(template)} - set(args))
        if missing:
            raise ViewTranslationError(f"views.functions[{name}] uses {{{missing[0].lower()}}}, which this "
                                       f"call of {name} doesn't pass")
        sql = _PLACEHOLDER.sub(lambda m: f"__arg_{m.group(1).upper()}__", template)
        try:
            replacement = _fill(template=sql, value=exp.null(),
                                more={f"__arg_{k}__": v for k, v in args.items()})
        except sqlglot.errors.ParseError as e:
            raise ViewTranslationError(f"views.functions[{name}] isn't valid DuckDB SQL: {e}") from e
        target.replace(exp.Paren(this=replacement))
    return body


def translate_view(definition: str, source_dialect: str, view_db_schema: str, target_db_schema: str,
                   target_view: str, name_map: dict[tuple[str, str], tuple[str, str]], case: str,
                   source_columns: list[str] | None = None, target_catalog: str | None = None,
                   pseudo_columns: str = "fail", time_zone: str | None = None,
                   column_types: dict[tuple[str, str], dict[str, str]] | None = None,
                   functions: dict[str, str] | None = None) -> str:
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
    if functions:
        body = _user_functions(body=body, functions=functions)
    if source_dialect == "oracle":
        body = _oracle_rewrites(body=body, pseudo_columns=pseudo_columns)
        body = _oracle_dates(body=body, time_zone=time_zone, column_types=column_types or {},
                             default_schema=view_db_schema)
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

    # Oracle OUTER/CROSS APPLY: sqlglot emits LEFT/INNER JOIN LATERAL with no ON clause, which
    # DuckDB rejects. [LEFT] JOIN LATERAL (...) ON TRUE is exactly what APPLY means.
    for join in body.find_all(exp.Join):
        lateral = join.this
        if isinstance(lateral, exp.Lateral) and lateral.args.get("cross_apply") is not None \
                and not join.args.get("on") and not join.args.get("using"):
            outer = lateral.args["cross_apply"] is False
            lateral.set("cross_apply", None)
            if outer:
                join.set("side", "LEFT")
            join.set("on", exp.true())

    # Optimizer hints (/*+ FULL(t) */) mean nothing to DuckDB: the one thing safe to drop.
    for select in body.find_all(exp.Select):
        select.set("hint", None)

    # comments=False: dbt appends a per-run /* {"invocation_id": ...} */ comment, which would
    # otherwise change the definition hash on every dbt run. unsupported_level=RAISE: by
    # default sqlglot warns and generates anyway, dropping what DuckDB lacks, e.g.
    # TO_CHAR(dt, 'YYYY-MM') -> CAST(dt AS TEXT), or INSTR's occurrence argument.
    try:
        select_sql = body.sql(dialect="duckdb", pretty=True, comments=False, identify=True,
                              unsupported_level=ErrorLevel.RAISE)
    except UnsupportedError as e:
        raise ViewTranslationError("no faithful DuckDB translation: " + "; ".join(str(e).splitlines())) from e
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
