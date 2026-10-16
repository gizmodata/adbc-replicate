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
        column.replace(node.copy() if id(node) in used else node)    # one node may stand for two names
        used.add(id(node))
    return tree


def _to_char(value: exp.Expression, fmt: str) -> exp.Expression | None:
    if fmt == "DAY":        # English day name, upper case, padded to 9 like Oracle
        return _fill(template="RPAD(UPPER(STRFTIME(__x__, '%A')), 9, ' ')", value=value)
    if fmt == "fmDAY":
        return _fill(template="UPPER(STRFTIME(__x__, '%A'))", value=value)
    if re.fullmatch(r"0+", fmt):                            # 000: zero-padded after a sign position
        k = len(fmt)
        padded = f"LPAD(CAST(CAST(ABS(ROUND(__x__)) AS BIGINT) AS VARCHAR), {k}, '0')"
        return _fill(template=f"CASE WHEN ABS(ROUND(__x__)) >= {10 ** k} THEN REPEAT('#', {k + 1}) "
                              f"WHEN __x__ < 0 THEN '-' || {padded} ELSE ' ' || {padded} END", value=value)
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

    def __init__(self, column_types: dict, default_schema: str, function_types: dict | None = None) -> None:
        self.column_types = column_types
        self.default_schema = default_schema
        self.function_types = function_types or {}      # views.functions NAME -> type class (RETURN ...)
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
            if name in self.function_types:
                return self.function_types[name]
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
            return self._projection(query=source.this, name=column.name, alias=source.args.get("alias"))
        if isinstance(source, exp.Table):
            cte = self._cte(node=select, name=source.name) if not source.db else None
            if cte is not None:
                return self._projection(query=cte.this, name=column.name, alias=cte.args.get("alias"))
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

    def _projection(self, query: exp.Expression, name: str, alias: exp.TableAlias | None = None) -> str | None:
        while isinstance(query, exp.SetOperation):
            query = query.this       # UNION branches share the first branch's column types
        if not isinstance(query, exp.Select):
            return None
        columns = [c.name.upper() for c in alias.args.get("columns") or []] if alias is not None else []
        if columns:                  # WITH c (a, b) AS (...) / (...) AS x(a, b): by position
            if name.upper() not in columns or columns.index(name.upper()) >= len(query.expressions):
                return None
            projection = query.expressions[columns.index(name.upper())]
            return self.of(projection.this if isinstance(projection, exp.Alias) else projection)
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
                  default_schema: str, function_types: dict | None = None) -> exp.Expression:
    types = _OracleTypes(column_types=column_types, default_schema=default_schema, function_types=function_types)
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
        if id(column) in done or column.find_ancestor(exp.Select) or column.meta.get("param"):
            continue
        if _bare(column=column, name="LEVEL"):
            column.replace(level.copy())
        elif child and not column.table:
            column.set("table", _quoted(name=child))
    return node.this if wrapped else node


def _path_text(value: exp.Expression) -> exp.Expression:
    """How SYS_CONNECT_BY_PATH turns a value into text: as Oracle converts it implicitly, NULL as ''."""
    return exp.Coalesce(this=_oracle_text(value=value, kind=None), expressions=[exp.Literal.string("")])


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
    leaf_join = False
    for column in list(select.find_all(exp.Column)):
        if column.find_ancestor(exp.Connect) is not None or column.find_ancestor(exp.Select) is not select:
            continue
        if _bare(column=column, name="LEVEL"):
            column.replace(exp.Column(this=_quoted(name=_CB_LEVEL), table=me.copy()))
        elif _bare(column=column, name="ROWNUM"):
            column.replace(exp.Window(this=exp.RowNumber(), order=exp.Order(
                expressions=[exp.Ordered(this=exp.Column(this=_quoted(name=_CB_ORDER), table=me.copy()))])))
        elif _bare(column=column, name="CONNECT_BY_ISLEAF"):
            # a leaf is a row no other row names as its parent: a LEFT JOIN to the distinct parents
            # (EXISTS over the recursive CTE trips a DuckDB binder error when the query is itself a
            # correlated subquery, e.g. inside a views.functions template)
            leaf_join = True
            column.replace(exp.Case(ifs=[exp.If(this=exp.Is(this=exp.column(_CB_PARENT, table=f"{name}_parents",
                                                                            quoted=True), expression=exp.null()),
                                                true=exp.Literal.number(1))], default=exp.Literal.number(0)))

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
    if leaf_join:
        parents = exp.select(exp.column(_CB_PARENT, quoted=True)).distinct().from_(exp.Table(this=_quoted(name=name)))
        select.join(exp.Subquery(this=parents, alias=exp.TableAlias(this=_quoted(name=f"{name}_parents"))),
                    on=exp.EQ(this=exp.column(_CB_PARENT, table=f"{name}_parents", quoted=True),
                              expression=exp.Column(this=_quoted(name=_CB_ORDER), table=me.copy())),
                    join_type="LEFT", copy=False)
    with_ = select.args.get(_WITH)
    if with_ is None:
        select.set(_WITH, exp.With(expressions=[cte], recursive=True))
    else:
        with_.append("expressions", cte)
        with_.set("recursive", True)


# How Oracle turns a value into text where none is asked for (TO_CHAR(x), ||, CONCAT): numbers
# without trailing zeros or a leading 0 before the point (1.50 -> '1.5', 0.5 -> '.5'), a DATE in
# NLS_DATE_FORMAT and a TIMESTAMP in NLS_TIMESTAMP_FORMAT, at their defaults (DD-MON-RR,
# DD-MON-RR HH.MI.SSXFF AM, English month names).
_NUMBER_TEXT = ("regexp_replace(regexp_replace(regexp_replace(CAST(__x__ AS VARCHAR), '(\\.\\d*?)0+$', '\\1'), "
                "'\\.$', ''), '^(-?)0\\.', '\\1.')")
_DATE_TEXT = "upper(strftime(__x__, '%d-%b-%y'))"
_TIMESTAMP_TEXT = "upper(strftime(__x__, '%d-%b-%y %I.%M.%S.%f %p'))"


def _number_literal_text(literal: exp.Literal) -> str:
    import decimal
    text = format(decimal.Decimal(literal.this).normalize(), "f")
    return re.sub(r"^(-?)0\.", r"\1.", text)


def _oracle_text(value: exp.Expression, kind: str | None) -> exp.Expression:
    """`value` as text the way Oracle converts it implicitly; `kind` is its inferred type class
    (unknown: decided at run time from the DuckDB type, numbers only)."""
    if isinstance(value, exp.Literal):
        return value if value.is_string else exp.Literal.string(_number_literal_text(literal=value))
    if kind == "STRING":
        return value
    template = {"NUMBER": _NUMBER_TEXT, "DATE": _DATE_TEXT, "TIMESTAMP": _TIMESTAMP_TEXT}.get(kind or "")
    if template is None:
        template = (f"CASE WHEN typeof(__x__) LIKE 'DECIMAL%' OR typeof(__x__) IN ('DOUBLE', 'FLOAT') "
                    f"THEN {_NUMBER_TEXT} ELSE CAST(__x__ AS VARCHAR) END")
    return _fill(template=template, value=value)


def _concat_operands(node: exp.Expression) -> list[exp.Expression]:
    if isinstance(node, exp.DPipe):
        return _concat_operands(node=node.this) + _concat_operands(node=node.expression)
    return [node]


def _oracle_concat(body: exp.Expression, types: _OracleTypes) -> None:
    """Oracle a || b and CONCAT(a, b) treat NULL as '' and give NULL only when the result is empty
    ('' is NULL in Oracle); DuckDB's || gives NULL if either side is NULL. -> NULLIF(CONCAT(...), '')
    with each operand converted to text as Oracle does. Checked against Oracle."""
    tops = [n for n in body.find_all(exp.DPipe, exp.Concat)
            if not (isinstance(n, exp.DPipe) and isinstance(n.parent, exp.DPipe))]
    work = []
    for node in tops:
        operands = node.expressions if isinstance(node, exp.Concat) else _concat_operands(node=node)
        work.append((node, [(op, types.of(op)) for op in operands]))     # types before anything moves
    for node, operands in sorted(work, key=lambda w: -w[0].depth):
        parts = [_oracle_text(value=op, kind=kind) for op, kind in operands if not isinstance(op, exp.Null)]
        concat = exp.Anonymous(this="CONCAT", expressions=parts) if parts else exp.Literal.string("")
        node.replace(exp.Nullif(this=concat, expression=exp.Literal.string("")))


def _substr(node: exp.Substring, kind: str | None) -> exp.Expression:
    """Oracle SUBSTR(s, position [, length]): position 0 counts as 1, fractions are truncated, a length
    below 1 or a negative position before the start gives NULL, an empty result is NULL, and a number
    is converted to text first. Checked against Oracle."""
    text = node.this if kind in ("STRING", None) else _oracle_text(value=node.this, kind=kind)
    start, length = node.args.get("start"), node.args.get("length")
    pos = _int_literal(node=start, default=1)
    if pos is not None:
        start = exp.Literal.number(1 if pos == 0 else pos)
    else:
        start = _fill(template="CASE WHEN TRUNC(__x__) = 0 THEN 1 ELSE CAST(TRUNC(__x__) AS BIGINT) END", value=start)
    guard = None
    if length is not None:
        n = _int_literal(node=length, default=1)
        if n is not None and n < 1:
            return exp.Cast(this=exp.null(), to=exp.DataType.build("VARCHAR"))
        if n is None:
            guard = length.copy()
            length = _fill(template="CAST(TRUNC(__x__) AS BIGINT)", value=length)
    raw_start = node.args.get("start")
    result = exp.Nullif(this=exp.Substring(this=text, start=start, length=length), expression=exp.Literal.string(""))
    if guard is not None:
        result = _fill(template="CASE WHEN TRUNC(__x__) < 1 THEN NULL ELSE __y__ END", value=guard, second=result)
    # a negative position before the start of the string is NULL in Oracle (DuckDB clamps it)
    if pos is not None and pos < 0:
        result = _fill(template=f"CASE WHEN LENGTH(__x__) < {-pos} THEN NULL ELSE __y__ END", value=text.copy(),
                       second=result)
    elif pos is None:
        result = _fill(template="CASE WHEN TRUNC(__x__) < 0 AND -TRUNC(__x__) > LENGTH(__z__) THEN NULL ELSE __y__ END",
                       value=raw_start.copy(), second=result, more={"__z__": text.copy()})
    return result


# String functions: Oracle converts a number or date argument to text first, and an empty result is NULL.
_TEXT_FUNCTIONS = (exp.Trim, exp.Upper, exp.Lower, exp.Length, exp.Pad, exp.Replace, exp.Translate, exp.Initcap)
_EMPTY_IS_NULL = (exp.Trim, exp.Pad, exp.Replace, exp.Translate)


def _text_functions(body: exp.Expression, types: _OracleTypes) -> None:
    """TRIM/LTRIM/RTRIM, UPPER, LOWER, LENGTH, LPAD/RPAD, REPLACE, TRANSLATE, INITCAP of a number or a
    date: of its text, as Oracle converts it; and '' results (all trimmed or replaced away) are NULL,
    as in Oracle. Checked against Oracle."""
    for node in sorted(body.find_all(*_TEXT_FUNCTIONS), key=lambda n: -n.depth):
        kind = types.of(node.this)
        if kind in ("NUMBER", "DATE", "TIMESTAMP"):
            node.set("this", _oracle_text(value=node.this, kind=kind))
        if isinstance(node, _EMPTY_IS_NULL) and not isinstance(node.parent, exp.Nullif):
            node.replace(exp.Nullif(this=node.copy(), expression=exp.Literal.string("")))


_HASHES = {"SHA1": "sha1", "SHA256": "sha256", "MD5": "md5"}


def _standard_hash(node: exp.StandardHash, kind: str | None) -> None:
    """STANDARD_HASH(text [, 'SHA1'|'SHA256'|'MD5']) -> the digest of the UTF-8 text (a RAW: unhex of
    DuckDB's hex digest); RAWTOHEX(STANDARD_HASH(...)) -> the upper-case hex digest. Oracle hashes a
    number or date by its internal bytes, which DuckDB can't reproduce: refused."""
    method = node.args.get("expression")
    name = "SHA1" if method is None else (method.this.upper() if isinstance(method, exp.Literal) else None)
    function = _HASHES.get(name or "")
    if function is None:
        raise ViewTranslationError(f"STANDARD_HASH with method {method.sql() if method else ''} (SHA1, SHA256 and "
                                   "MD5 are translated)")
    if kind != "STRING":
        raise ViewTranslationError("STANDARD_HASH of a value that isn't text (Oracle hashes its internal bytes)")
    digest = exp.Anonymous(this=function, expressions=[node.this])
    parent = node.parent
    if isinstance(parent, exp.Anonymous) and str(parent.this).upper() == "RAWTOHEX" and len(parent.expressions) == 1:
        parent.replace(exp.Upper(this=digest))
    else:
        node.replace(exp.Anonymous(this="unhex", expressions=[digest]))


_DS_UNITS = {"DAY": 86_400_000_000, "HOUR": 3_600_000_000, "MINUTE": 60_000_000, "SECOND": 1_000_000}


def _interval_functions(body: exp.Expression) -> None:
    """NUMTODSINTERVAL(n, 'unit') -> an interval of exact microseconds; FROM_TZ(ts, zone) -> the
    TIMESTAMPTZ of that wall-clock time in that zone; RAWTOHEX(raw) -> upper-case hex."""
    for node in list(body.find_all(exp.Anonymous)):
        name = str(node.this).upper()
        if name == "NUMTODSINTERVAL" and len(node.expressions) == 2:
            unit = node.expressions[1]
            factor = _DS_UNITS.get(unit.this.upper()) if isinstance(unit, exp.Literal) and unit.is_string else None
            if factor is None:
                raise ViewTranslationError("NUMTODSINTERVAL with a unit other than a literal DAY/HOUR/MINUTE/SECOND")
            node.replace(_fill(template=f"to_microseconds(CAST(ROUND(CAST(__x__ AS DOUBLE) * {factor}) AS BIGINT))",
                               value=node.expressions[0]))
        elif name == "FROM_TZ" and len(node.expressions) == 2:
            node.replace(exp.Anonymous(this="timezone", expressions=[node.expressions[1], node.expressions[0]]))
        elif name == "RAWTOHEX" and len(node.expressions) == 1:
            node.replace(exp.Upper(this=exp.Anonymous(this="hex", expressions=[node.expressions[0]])))


_COMPARISONS = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)


def _all_any_lists(body: exp.Expression) -> None:
    """x op ALL (a, b, ...) -> x op a AND x op b ...; x op ANY|SOME (a, b, ...) -> ... OR ... (Oracle
    defines them this way, NULLs included). Subqueries are left alone (DuckDB has those)."""
    for node in list(body.find_all(*_COMPARISONS)):
        right = node.expression
        if isinstance(right, exp.Anonymous) and str(right.this).upper() in ("ALL", "ANY", "SOME"):
            quantifier, items = str(right.this).upper(), right.expressions
        elif isinstance(right, (exp.All, exp.Any)) and isinstance(right.this, (exp.Tuple, exp.Paren)):
            quantifier = "ALL" if isinstance(right, exp.All) else "ANY"
            items = right.this.expressions if isinstance(right.this, exp.Tuple) else [right.this.this]
        else:
            continue
        if not items or any(isinstance(i, (exp.Select, exp.Subquery)) for i in items):
            continue
        parts = [node.__class__(this=node.this.copy(), expression=item) for item in items]
        combined = exp.and_(*parts) if quantifier == "ALL" else exp.or_(*parts)
        node.replace(exp.Paren(this=combined))


def _recursive_ctes(body: exp.Expression) -> None:
    """Oracle's recursive WITH needs no keyword; DuckDB's needs WITH RECURSIVE."""
    for with_ in body.find_all(exp.With):
        for cte in with_.expressions:
            if any(not t.db and t.name.upper() == cte.alias.upper() for t in cte.this.find_all(exp.Table)):
                with_.set("recursive", True)


def _source_columns(source: exp.Expression, types: _OracleTypes, default_schema: str) -> list[str] | None:
    if isinstance(source, exp.Subquery) and isinstance(source.this, exp.Select):
        names = [p.alias_or_name for p in source.this.expressions]
        return None if any(not n or n == "*" for n in names) else names
    if isinstance(source, exp.Table):
        columns = types.column_types.get(((source.db or default_schema).upper(), source.name.upper()))
        return list(columns) if columns else None
    return None


def _unpivot_columns(node: exp.Expression) -> list[exp.Expression]:
    return list(node.expressions) if isinstance(node, exp.Tuple) else [node]


def _multi_column_unpivots(body: exp.Expression, types: _OracleTypes, default_schema: str) -> None:
    """UNPIVOT with several value columns -> UNION ALL, one branch per IN group. DuckDB's own
    multi-column UNPIVOT drops a row when ANY value is NULL (Oracle: when ALL are) and has one name
    column only. Output columns in Oracle's order: the others, the FOR columns, the values."""
    for pivot in list(body.find_all(exp.Pivot)):
        if not pivot.args.get("unpivot"):
            continue
        values = [c for e in pivot.expressions for c in _unpivot_columns(node=e)]
        field = pivot.args["fields"][0] if pivot.args.get("fields") else None
        if field is None or (len(values) < 2 and not isinstance(field.this, exp.Tuple)):
            continue                                    # single-column UNPIVOT: DuckDB matches Oracle
        source = pivot.parent
        columns = _source_columns(source=source, types=types, default_schema=default_schema)
        if columns is None:
            raise ViewTranslationError("multi-column UNPIVOT over a source whose columns aren't known")
        names = [c.name for c in _unpivot_columns(node=field.this)]
        groups = []
        for entry in field.expressions:
            inputs = _unpivot_columns(node=entry.this if isinstance(entry, exp.PivotAlias) else entry)
            labels = _unpivot_columns(node=entry.args["alias"]) if isinstance(entry, exp.PivotAlias) else \
                [exp.Literal.string("_".join(c.name for c in inputs).upper())]
            if len(inputs) != len(values) or len(labels) != len(names):
                raise ViewTranslationError("UNPIVOT with mismatched column groups")
            groups.append((inputs, labels))
        used = {c.name.upper() for inputs, _ in groups for c in inputs}
        others = [c for c in columns if c.upper() not in used]
        bare = source.copy()
        bare.set("pivots", None)
        bare.set("alias", None)
        branches = []
        for inputs, labels in groups:
            projections = [exp.column(c) for c in others] \
                + [lbl.copy().as_(n) for lbl, n in zip(labels, names, strict=True)] \
                + [i.copy().as_(v.name) for i, v in zip(inputs, values, strict=True)]
            select = exp.Select(expressions=projections).from_(bare.copy())
            if not pivot.args.get("include_nulls"):
                select = select.where(exp.not_(exp.and_(*[exp.Is(this=i.copy(), expression=exp.null())
                                                          for i in inputs])))
            branches.append(select)
        union = branches[0]
        for branch in branches[1:]:
            union = exp.Union(this=union, expression=branch, distinct=False)
        alias = pivot.args.get("alias") or source.args.get("alias") or exp.TableAlias(this=_quoted(name="unpivoted"))
        source.replace(exp.Subquery(this=union, alias=alias.copy()))


def _rename_columns(body: exp.Expression, renames: dict, types: _OracleTypes, default_schema: str) -> None:
    """Columns renamed by column rules (`rename`) -> their target names, wherever a view reads them from
    the renamed table (by alias, or unqualified when only that table has the column). A bare selected
    column keeps its old name as an alias, so outer queries still find it."""
    for column in list(body.find_all(exp.Column)):
        select = column.find_ancestor(exp.Select)
        if select is None or isinstance(column.this, exp.Star):
            continue
        from_ = select.args.get(_FROM)
        sources = ([from_.this] if from_ is not None else []) + [j.this for j in select.args.get("joins") or []]
        if column.table:
            sources = [s for s in sources if s.alias_or_name.upper() == column.table.upper()]
        elif any(not isinstance(s, exp.Table) for s in sources):
            continue                                   # an unqualified column that a subquery may provide
        tables = [s for s in sources if isinstance(s, exp.Table)
                  and (s.db or not _OracleTypes._cte(node=select, name=s.name))]
        if not column.table:
            tables = [t for t in tables if column.name.upper() in types.column_types.get(
                ((t.db or default_schema).upper(), t.name.upper()), {})]
        if len(tables) != 1:
            continue
        table = tables[0]
        target = renames.get(((table.db or default_schema).upper(), table.name.upper()), {}).get(column.name.upper())
        if target is None:
            continue
        old = column.name
        ident = exp.to_identifier(target, quoted=True)
        ident.meta["final"] = True                      # already the target's name: no naming.case
        if isinstance(column.parent, exp.Select):
            renamed = column.copy()
            renamed.set("this", ident)
            column.replace(exp.alias_(renamed, old))
        else:
            column.set("this", ident)


def _is_null(node: exp.Expression | None) -> bool:
    return isinstance(node, exp.Null) or (isinstance(node, exp.Cast) and isinstance(node.this, exp.Null))


# Oracle pseudo-columns: values only the source database can produce, with the type a NULL
# stand-in gets. Both are reserved words, so an unquoted reference is never a real column.
_ORACLE_PSEUDO_COLUMNS = {"ORA_ROWSCN": "DECIMAL(38,0)", "ROWID": "VARCHAR"}
# an Oracle TIMESTAMP literal: date [time [fraction]] [offset]
_TIMESTAMP_LITERAL = re.compile(r"\s*(\d{4}-\d{2}-\d{2}(?: \d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?)?)"
                                r"\s*([+-]\d{1,2}:\d{2})?\s*")


# TO_DATE(TO_CHAR(date), 'format'): TO_CHAR without a format writes NLS_DATE_FORMAT (DD-MON-RR, '16-OCT-26'),
# and TO_DATE reads it back leniently: fields by position, any separator matching any punctuation, MM accepting
# a month name, elements after the end of the text ignored. So TO_DATE(TO_CHAR(SYSDATE), 'YYYY-MM-DD') is year
# 16, October 26 (measured on Oracle 23ai). The element names below: sqlglot's strftime codes, or Oracle's when
# sqlglot left them as written.
_NLS_ELEMENTS = {"%d": "day", "DD": "day", "%m": "month", "MM": "month", "%b": "month name", "MON": "month name",
                 "%B": "month name", "MONTH": "month name", "%Y": "YYYY", "YYYY": "YYYY", "SYYYY": "YYYY",
                 "%y": "YY", "YY": "YY", "RR": "RR", "RRRR": "RR"}
_NLS_TRAILING = {"%H", "%I", "%M", "%S", "%p", "AM", "PM", "HH", "HH12", "HH24", "MI", "SS"}
# the fields of '16-OCT-26': what each holds, as DuckDB SQL over the date (__x__)
_NLS_FIELDS = [("number", "DAY(__x__)"), ("name", "MONTH(__x__)"), ("number", "(YEAR(__x__) % 100)")]


def _to_date_of_nls_text(value: exp.Expression, fmt: str) -> exp.Expression:
    """TO_DATE(TO_CHAR(value), fmt) for a DATE value, as Oracle evaluates it (see _NLS_ELEMENTS). Raises
    ViewTranslationError for a format Oracle always rejects for this text, or one this doesn't read exactly."""
    refuse = ViewTranslationError(f"TO_DATE(TO_CHAR(date), '{fmt}') reads the date's DD-MON-RR text with a format "
                                  "that has no exact translation here")
    fields, glued = [], False         # fields: element lists between separators
    for token, separator in re.findall(r'(S%Y|%[A-Za-z]|"[^"]*"|[A-Za-z0-9]+)|([^A-Za-z0-9%"]+)', fmt):
        if token.startswith('"'):     # literal text: past the end of the input it is ignored like a separator
            if len(fields) < 3:
                raise refuse
            glued = False
        elif separator:
            glued = False
        elif glued:
            fields[-1].append(token)
        else:
            fields.append([token])
            glued = True
    if len(fields) < 3 or any(len(f) > 1 for f in fields[:3]):
        raise refuse
    for extra in fields[3:]:          # after the text ends: time of day, AM/PM, quoted literals
        if not all(t.upper() in _NLS_TRAILING or t in _NLS_TRAILING for t in extra):
            raise refuse
    parts = {}
    for (kind, field), (token,) in zip(_NLS_FIELDS, fields[:3], strict=True):
        element = _NLS_ELEMENTS.get(token if token.startswith("%") else token.upper().replace("S%Y", "SYYYY"))
        if element is None or (kind == "name") != (element in ("month", "month name")) \
                or (kind == "number" and element == "month name"):
            raise refuse             # Oracle: ORA-01858 / ORA-01843 on every row, or not an element we know
        role = {"month name": "month", "YYYY": "year", "YY": "year", "RR": "year"}.get(element, element)
        if role in parts:
            raise refuse             # ORA-01810 / ORA-01816: an element given twice
        if element == "YY":          # the current century
            field = f"(YEAR(__y__) // 100) * 100 + {field}"
        elif element == "RR":        # the century that puts the year nearest the current one (two-digit RR window)
            field = (f"CASE WHEN {field} < 50 THEN (YEAR(__y__) // 100 + CASE WHEN YEAR(__y__) % 100 < 50 THEN 0 "
                     f"ELSE 1 END) * 100 ELSE (YEAR(__y__) // 100 - CASE WHEN YEAR(__y__) % 100 < 50 THEN 1 "
                     f"ELSE 0 END) * 100 END + {field}")
        parts[role] = field
    # MAKE_TIMESTAMP fails on an impossible date (day 31 of a 30-day month), where Oracle raises ORA-01847
    return _fill(template=f"CAST(MAKE_TIMESTAMP({parts['year']}, {parts['month']}, {parts['day']}, 0, 0, 0) "
                          "AS TIMESTAMP_S)",
                 value=value, second=exp.CurrentTimestamp(sysdate=True))


# where an Oracle '' literal is a value or a comparison operand, so NULL (see _oracle_rewrites)
_EMPTY_AS_TEXT = (exp.Replace, exp.RegexpReplace, exp.DPipe, exp.Concat, exp.ConcatWs, exp.Anonymous)
_EMPTY_IS_NULL_PARENTS = (exp.Coalesce, exp.DecodeCase, exp.Alias, exp.Select, exp.Greatest, exp.Least, exp.In,
                          exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Like, exp.Between)


def _oracle_rewrites(body: exp.Expression, pseudo_columns: str = "fail", column_types: dict | None = None,
                     default_schema: str = "", context_params: list[str] | None = None,
                     function_types: dict | None = None) -> exp.Expression:
    """Oracle-only constructs with a well-defined DuckDB equivalent."""
    types = _OracleTypes(column_types=column_types or {}, default_schema=default_schema, function_types=function_types)
    # type classes first: the rewrites below replace the nodes they are inferred from
    for node in body.find_all(exp.DPipe, exp.Concat, exp.ToChar, exp.StandardHash, exp.Substring, *_TEXT_FUNCTIONS):
        for operand in (_concat_operands(node=node) if isinstance(node, exp.DPipe) else
                        node.expressions if isinstance(node, exp.Concat) else [node.this]):
            types.of(operand)
    # '' is NULL in Oracle: as a value (CASE ... THEN '', NVL(x, ''), DECODE, a select item), compared with
    # (x <> '' is never true) or passed to a function (LENGTH('') is NULL) it becomes a typed NULL. Functions
    # for which Oracle reads NULL as '' (REPLACE(s, 'x', ''), ||) keep it for the rewrites below.
    for literal in list(body.find_all(exp.Literal)):
        if literal.is_string and literal.this == "" and (
                isinstance(literal.parent, _EMPTY_IS_NULL_PARENTS)
                or (isinstance(literal.parent, exp.If) and literal.arg_key == "true")
                or (isinstance(literal.parent, exp.Case) and literal.arg_key == "default")
                or (isinstance(literal.parent, exp.Nvl2) and literal.arg_key in ("true", "false"))
                or (isinstance(literal.parent, exp.Func) and not isinstance(literal.parent, _EMPTY_AS_TEXT))):
            literal.replace(exp.Cast(this=exp.null(), to=exp.DataType.build("VARCHAR")))
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
        if str(node.this).upper() == "SYS_CONTEXT" and context_params is not None:
            # a parameterized view's macro: SYS_CONTEXT(namespace, 'ATTRIBUTE') -> parameter attribute
            attribute = node.expressions[1] if len(node.expressions) == 2 else None
            if not (isinstance(attribute, exp.Literal) and attribute.is_string
                    and re.fullmatch(r"[A-Za-z_]\w*", attribute.this)):
                raise ViewTranslationError("SYS_CONTEXT with an attribute that isn't a literal name")
            name = attribute.this.upper()
            if name not in context_params:
                context_params.append(name)
            param = exp.column(name)
            param.meta["param"] = True
            node.replace(param)
            continue
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
        if isinstance(node.this, exp.Column) and node.this.meta.get("param") and node.args.get("format") is None:
            node.replace(exp.Cast(this=node.this, to=exp.DataType.build(
                "TIMESTAMP_S" if isinstance(node, exp.StrToDate) else "TIMESTAMP", dialect="duckdb")))
            continue
        if _is_null(node=node.this):
            node.replace(exp.Cast(this=exp.null(), to=exp.DataType.build(
                "TIMESTAMP_S" if isinstance(node, exp.StrToDate) else "TIMESTAMP", dialect="duckdb")))

    # TO_DATE(TO_CHAR(date), 'format'): before TO_CHAR(x) without a format becomes text below
    for node in list(body.find_all(exp.StrToDate, exp.StrToTime)):
        inner, fmt = node.this, node.args.get("format")
        if isinstance(inner, exp.ToChar) and inner.args.get("format") is None and not inner.args.get("nlsparam") \
                and types.of(inner.this) == "DATE" and isinstance(fmt, exp.Literal) and fmt.is_string:
            node.replace(_to_date_of_nls_text(value=inner.this, fmt=fmt.this))

    _multi_column_unpivots(body=body, types=types, default_schema=default_schema)
    for node in list(body.find_all(exp.StandardHash)):
        _standard_hash(node=node, kind=types.of(node.this))
    _oracle_concat(body=body, types=types)
    # TO_CHAR(x) without a format: Oracle's implicit text conversion (sqlglot: CAST(x AS TEXT))
    for node in list(body.find_all(exp.ToChar)):
        if node.args.get("format") is None and not node.args.get("nlsparam"):
            node.replace(_oracle_text(value=node.this, kind=types.of(node.this)))
    for node in sorted(body.find_all(exp.Substring), key=lambda n: -n.depth):
        node.replace(_substr(node=node, kind=types.of(node.this)))
    _text_functions(body=body, types=types)
    _all_any_lists(body=body)
    _interval_functions(body=body)
    # TIMESTAMP 'yyyy-mm-dd hh:mi:ss[.ff] [zone]' literals: sqlglot emits STRPTIME(..., '...%S.%f'), which
    # fails (when the view is queried) without a fraction; Oracle's literal doesn't need one.
    for node in list(body.find_all(exp.StrToTime)):
        fmt, value = node.args.get("format"), node.this
        if isinstance(fmt, exp.Literal) and fmt.this == "%Y-%m-%d %H:%M:%S.%f" and isinstance(value, exp.Literal) \
                and value.is_string:
            match = _TIMESTAMP_LITERAL.fullmatch(value.this)
            if match:      # DuckDB wants the offset attached: 08:30:15-05:00
                text, zone = match.group(1), match.group(2)
                node.replace(exp.Cast(this=exp.Literal.string(text + (zone or "")),
                                      to=exp.DataType.build("TIMESTAMPTZ" if zone else "TIMESTAMP")))
    # TO_DATE(SYSDATE) round-trips through NLS_DATE_FORMAT (default DD-MON-RR): midnight of that day
    for node in list(body.find_all(exp.StrToDate)):
        if node.args.get("format") is None and isinstance(node.this, _NOW_NODES):
            node.replace(_fill(template="CAST(DATE_TRUNC('day', __x__) AS TIMESTAMP_S)", value=node.this))

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
            if isinstance(cond, (exp.LTE, exp.LT, exp.EQ)) and isinstance(cond.this, exp.Column) \
                    and cond.this.name.upper() == "ROWNUM" and not cond.this.table \
                    and isinstance(cond.expression, exp.Literal) and cond.expression.is_int \
                    and (not isinstance(cond, exp.EQ) or cond.expression.name == "1"):    # ROWNUM = 1: LIMIT 1
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
    # DUAL still referenced (joined, e.g. FROM dual OUTER APPLY (...)): a one-row stand-in
    for table in list(body.find_all(exp.Table)):
        if not table.db and table.name.upper() == "DUAL" and not _OracleTypes._cte(node=table, name="DUAL"):
            table.replace(exp.Subquery(this=exp.select(exp.Literal.string("X").as_("DUMMY")),
                                       alias=exp.TableAlias(this=_quoted(name=table.alias_or_name))))
    _recursive_ctes(body=body)
    if any(isinstance(c, exp.Column) and c.name.upper() == "ROWNUM" and not c.table
           for c in body.find_all(exp.Column)):
        raise ViewTranslationError("ROWNUM used other than as a top-level 'ROWNUM <= n' (or = 1) filter")
    # GREATEST / LEAST: NULL if any argument is NULL in Oracle; DuckDB's skip NULLs (and sqlglot marks Oracle's
    # as ignoring them too), so GREATEST(d, NULL) would be d instead of NULL.
    for node in sorted(body.find_all(exp.Greatest, exp.Least), key=lambda n: -n.depth):
        arguments = [node.this, *node.expressions]
        if len(arguments) > 1:
            present = exp.and_(*(exp.Not(this=exp.Is(this=a.copy(), expression=exp.null())) for a in arguments))
            node.replace(exp.Case(ifs=[exp.If(this=present, true=node.copy())]))
    return body


_PLACEHOLDER = re.compile(r"\{(\w+)\}")


_SIGNATURE = re.compile(r"\s*([\w.$#]+)\s*(?:\(([^)]*)\))?\s*(?:RETURN\s+(\w+(?:\s*\([\d, ]+\))?))?\s*",
                        re.IGNORECASE)


def _function_types(functions: dict[str, str]) -> dict[str, str]:
    """views.functions keys declaring RETURN <type>: function name -> type class, for date arithmetic
    and text conversions on their results."""
    types = {}
    for key in functions:
        match = _SIGNATURE.fullmatch(key)
        if match and match.group(3):
            kind = _OracleTypes._class(data_type=match.group(3))
            if kind:
                types[match.group(1).upper()] = kind
    return types


def _user_functions(body: exp.Expression, functions: dict[str, str]) -> exp.Expression:
    """views.functions: calls of source functions (e.g. PL/SQL) -> the configured DuckDB SQL, with {1},
    {2}, ... standing for the call's arguments by position and {name} for named ones (p_name => x).
    A key may list the parameters, NAME(p1, p2), so that positional and named calls both fill {p1}
    and {1}. Package-qualified calls (pkg.fn) are matched as pkg.fn."""
    templates: dict[str, tuple[list[str], str]] = {}
    for key, template in functions.items():
        match = _SIGNATURE.fullmatch(key)
        if match is None:
            raise ViewTranslationError(f"views.functions key {key!r} isn't NAME [(param, ...)] [RETURN type]")
        params = [p.strip().upper() for p in (match.group(2) or "").split(",") if p.strip()]
        templates[match.group(1).upper()] = (params, template)
    for node in list(body.find_all(exp.Anonymous)):
        name = str(node.this).upper()
        if isinstance(node.parent, exp.Dot) and node.parent.expression is node:
            name = f"{node.parent.this.name.upper()}.{name}"
            target = node.parent
        else:
            target = node
        if name not in templates:
            continue
        params, template = templates[name]
        args: dict[str, exp.Expression] = {}
        for i, arg in enumerate(node.expressions, start=1):
            if isinstance(arg, (exp.Kwarg, exp.PropertyEQ)):
                key = arg.this.name.upper()
                args[key] = arg.expression
                if key in params:
                    args[str(params.index(key) + 1)] = arg.expression
            else:
                args[str(i)] = arg
                if i <= len(params):
                    args[params[i - 1]] = arg
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


def _star_sources(star: exp.Expression) -> list[exp.Table] | None:
    """The tables a SELECT * (or t.*) reads, or None when one of them isn't a plain table
    (a CTE, subquery or table function), whose columns we can't know here."""
    select = star.find_ancestor(exp.Select)
    if select is None:
        return None
    sources = []
    from_ = select.args.get(_FROM)
    if from_ is not None:
        sources.append(from_.this)
    sources.extend(join.this for join in select.args.get("joins") or [])
    qualifier = star.table if isinstance(star, exp.Column) else ""
    if qualifier:
        sources = [s for s in sources if s.alias_or_name.upper() == qualifier.upper()]
    if not sources or not all(isinstance(s, exp.Table) and s.args.get("db") for s in sources):
        return None
    return sources


def _drop_excluded_from_star_except(body: exp.Expression, excluded: dict[tuple[str, str], set[str]],
                                    column_types: dict[tuple[str, str], dict[str, str]]) -> None:
    """SELECT * EXCLUDE (c) over a table copied without column c fails to bind in the target
    ("Column c in EXCLUDE list not found"). The target table already lacks c, so drop it from the
    list (and drop an emptied list)."""
    for node in list(body.find_all(exp.Star)):
        names = node.args.get("except_")
        if not names:
            continue
        star = node.parent if isinstance(node.parent, exp.Column) else node
        sources = _star_sources(star=star)
        if sources is None:
            continue
        keys = [(t.db.upper(), t.name.upper()) for t in sources]
        if not any(k in excluded for k in keys):
            continue

        def gone(name: str, keys: list[tuple[str, str]] = keys) -> bool:
            # left out of every source that had it, so the target reads no such column
            had = [k for k in keys if name in column_types.get(k, {})]
            return bool(had) and all(name in excluded.get(k, set()) for k in had)

        kept = [c for c in names if not gone(name=c.name.upper())]
        node.set("except_", kept or None)


def _natural_column_list(body: exp.Expression, column_list: list[str],
                         column_types: dict[tuple[str, str], dict[str, str]]) -> bool:
    """True when a view's column list only restates the names its SELECT * produces anyway.

    Snowflake's GET_DDL always writes the column list out, and Oracle's comes from the dictionary.
    Over a star that list names every source column, including ones the copy left out (excluded
    columns, here or in a view it reads), so the target sees more aliases than columns. When every
    listed name is a star source's own column name or an explicit select item's name, the list
    renames nothing and can go; a list that renames columns positionally is kept."""
    selects = body.selects if isinstance(body, exp.Query) else []
    stars = [e for e in selects if isinstance(e, exp.Star)
             or (isinstance(e, exp.Column) and isinstance(e.this, exp.Star))]
    if not stars:
        return False
    natural = {e.alias_or_name.upper() for e in selects if e not in stars and e.alias_or_name}
    for star in stars:
        sources = _star_sources(star=star)
        if sources is None:
            return True        # a CTE or subquery: its names are its own; keep what the star yields
        for table in sources:
            natural |= set(column_types.get((table.db.upper(), table.name.upper()), {}))
    return all(name.upper() in natural for name in column_list)


def _qualify_without_window(body: exp.Expression) -> None:
    """Snowflake accepts QUALIFY with no window function (e.g. QUALIFY rn = 1 over a CTE's rn
    column); DuckDB requires one in the SELECT list or the QUALIFY clause. With no window function
    and no aggregation the predicate filters plain rows, which is exactly WHERE: QUALIFY runs after
    GROUP BY/HAVING/windows, none of which exist here, and before DISTINCT, which a row filter
    commutes with. SELECT-list aliases it uses are inlined, since WHERE can't see them reliably."""
    for select in list(body.find_all(exp.Select)):
        qualify = select.args.get("qualify")
        if qualify is None or qualify.find(exp.Window):
            continue
        if any(e.find(exp.Window) for e in select.expressions):
            continue
        if select.args.get("group") or select.args.get("having") \
                or any(e.find(exp.AggFunc) for e in select.expressions):
            continue
        predicate = qualify.this.copy()
        aliases = {e.alias.upper(): e.this for e in select.expressions
                   if isinstance(e, exp.Alias) and e.alias}
        for column in list(predicate.find_all(exp.Column)):
            if column.table or column.name.upper() not in aliases:
                continue
            source = aliases[column.name.upper()]
            if isinstance(source, exp.Column) and source.name.upper() == column.name.upper():
                continue
            replacement = exp.Paren(this=source.copy())
            if column is predicate:
                predicate = replacement
            else:
                column.replace(replacement)
        select.set("qualify", None)
        select.where(predicate, copy=False)


def _case_sensitive_references(body: exp.Expression, column_names: dict[tuple[str, str], dict[str, str]],
                                default_schema: str) -> None:
    """Refuse an unqualified column that matches a column of a table in its own FROM only when case is ignored.

    After normalize_identifiers a reference is spelled as the source database resolves it (Oracle: unquoted
    partno -> PARTNO). A table created with quoted names ("PartNo") doesn't have that column, so the source
    resolves the name in an outer query (or fails), while DuckDB, whose names ignore case, would bind it to that
    table: `partno IN (SELECT partno FROM orderdet)` turns from always-true into a real filter, silently."""
    for column in body.find_all(exp.Column):
        if column.table or not isinstance(column.this, exp.Identifier):
            continue
        select = column.find_ancestor(exp.Select)
        if select is None:
            continue
        sources = [select.args.get(_FROM).this] if select.args.get(_FROM) is not None else []
        sources += [join.this for join in select.args.get("joins") or []]
        for source in sources:
            if not isinstance(source, exp.Table) or not source.name:
                continue
            exact = column_names.get(((source.db or default_schema).upper(), source.name.upper()), {})
            spelled = exact.get(column.name.lower())
            if spelled is not None and spelled != column.name and column.name not in exact.values():
                raise ViewTranslationError(
                    f"{column.name} doesn't match {source.name}'s column \"{spelled}\" in the source (quoted names "
                    "are case-sensitive there), so the source resolves it elsewhere (an outer query); in the target "
                    "it would match that column. Add a view override that says which one is meant")


def translate_view(definition: str, source_dialect: str, view_db_schema: str, target_db_schema: str,
                   target_view: str, name_map: dict[tuple[str, str], tuple[str, str]], case: str,
                   source_columns: list[str] | None = None, target_catalog: str | None = None,
                   pseudo_columns: str = "fail", time_zone: str | None = None,
                   column_types: dict[tuple[str, str], dict[str, str]] | None = None,
                   functions: dict[str, str] | None = None,
                   column_renames: dict[tuple[str, str], dict[str, str]] | None = None,
                   excluded_columns: dict[tuple[str, str], set[str]] | None = None,
                   column_names: dict[tuple[str, str], dict[str, str]] | None = None,
                   as_macro: bool = False) -> str | None:
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
    if column_names:
        _case_sensitive_references(body=body, column_names=column_names, default_schema=view_db_schema)
    function_types = _function_types(functions=functions or {})
    if column_renames:
        _rename_columns(body=body, renames=column_renames, default_schema=view_db_schema,
                        types=_OracleTypes(column_types=column_types or {}, default_schema=view_db_schema))
    context_params: list[str] | None = [] if as_macro else None
    if source_dialect == "oracle":
        body = _oracle_rewrites(body=body, pseudo_columns=pseudo_columns, column_types=column_types,
                                default_schema=view_db_schema, context_params=context_params,
                                function_types=function_types)
    if as_macro and not context_params:
        return None                                # not a parameterized view
    if as_macro:
        # A macro parameter shadows an unqualified column of the same name: qualify such a column with
        # its query's only source, or refuse when there are several.
        for column in list(body.find_all(exp.Column)):
            if column.meta.get("param") or column.table or column.name.upper() not in context_params:
                continue
            select = column.find_ancestor(exp.Select)
            from_ = select.args.get(_FROM) if select is not None else None
            if from_ is None or select.args.get("joins") or not from_.this.alias_or_name:
                raise ViewTranslationError(f"SYS_CONTEXT attribute {column.name.upper()} is also a column name "
                                           "in a query with several sources; a macro parameter would shadow it")
            column.set("table", exp.to_identifier(from_.this.alias_or_name))
    if source_dialect == "oracle":
        body = _oracle_dates(body=body, time_zone=time_zone, column_types=column_types or {},
                             default_schema=view_db_schema, function_types=function_types)
    if functions:      # DuckDB SQL: after the source-dialect rewrites, which mustn't touch it
        body = _user_functions(body=body, functions=functions)
    body = qualify_tables(body, db=_quoted(name=view_db_schema), dialect=source_dialect)
    if column_list and _natural_column_list(body=body, column_list=column_list, column_types=column_types or {}):
        column_list = None
    if excluded_columns:
        _drop_excluded_from_star_except(body=body, excluded=excluded_columns, column_types=column_types or {})
    if source_dialect == "snowflake":
        _qualify_without_window(body=body)

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
        if id(ident) not in table_nodes and not ident.meta.get("final"):
            ident.set("this", apply_case(name=ident.this, case=case))
            ident.set("quoted", True)

    # Oracle OUTER/CROSS APPLY: sqlglot emits LEFT/INNER JOIN LATERAL with no ON clause, which
    # DuckDB rejects. [LEFT] JOIN LATERAL (...) ON TRUE is exactly what APPLY means.
    for join in body.find_all(exp.Join):
        lateral = join.this
        # APPLY over a parenthesized table name (OUTER APPLY (t) x): just that table, joined ON TRUE
        if isinstance(lateral, exp.Lateral) and lateral.args.get("cross_apply") is not None \
                and isinstance(lateral.this, exp.Subquery) and isinstance(lateral.this.this, exp.Table) \
                and not join.args.get("on"):
            table = lateral.this.this.copy()
            if lateral.args.get("alias"):
                table.set("alias", lateral.args["alias"].copy())
            if lateral.args["cross_apply"] is False:
                join.set("side", "LEFT")
            join.set("this", table)
            join.set("on", exp.true())
            continue
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
    columns = [apply_case(name=c, case=case) for c in column_list] if column_list else None
    if as_macro:
        return create_macro_sql(target_db_schema=target_db_schema, target_view=target_view, body=select_sql,
                                columns=columns, params=[apply_case(name=p, case=case) for p in context_params],
                                target_catalog=target_catalog)
    return create_view_sql(target_db_schema=target_db_schema, target_view=target_view, body=select_sql,
                           columns=columns, target_catalog=target_catalog)


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


def create_macro_sql(target_db_schema: str, target_view: str, body: str, params: list[str],
                     columns: list[str] | None = None, target_catalog: str | None = None) -> str:
    """A table macro named like the view: SELECT * FROM view(param := value, ...); every parameter
    defaults to NULL, which is what the view itself reads."""
    view_sql = create_view_sql(target_db_schema=target_db_schema, target_view=target_view, body="SELECT 1",
                               target_catalog=target_catalog)
    name = view_sql[len("CREATE OR REPLACE VIEW "):view_sql.index(" AS\n")]
    signature = ", ".join(f"{exp.to_identifier(p, quoted=True).sql(dialect='duckdb')} := NULL" for p in params)
    query = body.strip().rstrip(";")
    if columns:
        aliases = ", ".join(exp.to_identifier(c, quoted=True).sql(dialect="duckdb") for c in columns)
        query = f"SELECT * FROM (\n{query}\n) AS \"_view\"({aliases})"
    return f"CREATE OR REPLACE MACRO {name}({signature}) AS TABLE\n{query}"


def load_override(overrides_dir: str | None, db_schema: str, view: str) -> str | None:
    """A hand-written view body from overrides_dir/<DB_SCHEMA>.<VIEW>.sql (case-insensitive)."""
    if not overrides_dir or not os.path.isdir(overrides_dir):
        return None
    wanted = f"{db_schema}.{view}.sql".lower()
    for entry in os.listdir(overrides_dir):
        if entry.lower() == wanted:
            return Path(overrides_dir, entry).read_text(encoding="utf-8")
    return None
