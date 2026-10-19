"""Turn the config's selection rules plus the source catalog into a Plan: which tables to copy
(with which columns and masks), which views to recreate, and under which target names."""

from __future__ import annotations

from .config import ColumnRule, ReplicationConfig
from .matching import PatternSet, TablePatternSet, apply_case
from .models import MATERIALIZED_VIEW, TABLE, VIEW, Plan, SelectedColumn, SourceObject, TableTask, ViewTask


class PlanError(Exception):
    pass


class _ColumnRuleUsage:
    """Tracks whether each exclude/include/mask/rename entry matched anything, to catch typos."""

    def __init__(self, rules: list[ColumnRule]) -> None:
        self.rules = rules
        self.compiled = []
        for rule in rules:
            self.compiled.append({
                "tables": TablePatternSet(patterns=rule.tables),
                "include": PatternSet(patterns=rule.include) if rule.include is not None else None,
                "exclude": PatternSet(patterns=rule.exclude),
                "mask": [(PatternSet(patterns=[p]), spec) for p, spec in rule.mask.items()],
                "ignore": PatternSet(patterns=rule.fingerprint_ignore),
            })
        self.matched: set[tuple[int, str, str]] = set()

    def select(self, obj: SourceObject) -> tuple[list[SelectedColumn], list[str]]:
        names = [c.name for c in obj.columns]
        included = {c.name for c in obj.columns if not c.unsupported}
        masks = {}
        ignored = set()
        renames: dict[str, str] = {}
        for i, rule in enumerate(self.compiled):
            if not rule["tables"].matches_table(schema=obj.db_schema, table=obj.name):
                continue
            if rule["include"] is not None:
                keep = set()
                for name in names:
                    p = rule["include"].first_match(name=name)
                    if p is not None:
                        keep.add(name)
                        self.matched.add((i, "include", p.text))
                included &= keep
            for name in names:
                p = rule["exclude"].first_match(name=name)
                if p is not None:
                    included.discard(name)
                    self.matched.add((i, "exclude", p.text))
            for pattern_set, spec in rule["mask"]:
                for name in names:
                    if pattern_set.matches(name=name):
                        masks[name] = spec
                        self.matched.add((i, "mask", pattern_set.patterns[0].text))
            for name in names:
                p = rule["ignore"].first_match(name=name)
                if p is not None:
                    ignored.add(name)
                    self.matched.add((i, "fingerprint_ignore", p.text))
            for source_name, target_name in self.rules[i].rename.items():
                for name in names:
                    if name.upper() == source_name.upper():
                        renames[name] = target_name
                        self.matched.add((i, "rename", source_name))
        selected = [SelectedColumn(column=c, target_name=renames.get(c.name, c.name), mask=masks.get(c.name),
                                   fingerprint=c.name not in ignored, renamed=c.name in renames)
                    for c in obj.columns if c.name in included]
        excluded = [c.name for c in obj.columns if c.name not in included]
        return selected, excluded

    def unmatched(self) -> list[str]:
        problems = []
        for i, rule in enumerate(self.rules):
            for kind, patterns in (("include", rule.include or []), ("exclude", rule.exclude),
                                   ("mask", list(rule.mask)), ("rename", list(rule.rename))):
                for p in patterns:
                    if (i, kind, p) not in self.matched:
                        problems.append(f"columns[{i}].{kind} pattern {p!r} (tables {rule.tables}) "
                                        "matched no column of any selected table")
        return problems


def _incremental_key(task: TableTask) -> tuple[list[str] | None, str | None]:
    """The target column names to merge on, or why the table is copied whole instead."""
    key = task.source.primary_key
    if not key:
        return None, "no primary key"
    selected = {c.column.name: c for c in task.columns}
    for name in key:
        column = selected.get(name)
        if column is None:
            return None, f"primary-key column {name} is excluded"
        if column.mask is not None and column.mask.strategy != "hash":
            # null and redact make keys collide; an expression may too. A salted hash stays unique.
            return None, f"primary-key column {name} is masked ({column.mask.strategy})"
    return [selected[name].target_name for name in key], None


def _configured_key(obj: SourceObject, keys: list, used: set[str]) -> list[str] | None:
    """The refresh.keys entry for a table (first matching pattern), as the source's column names."""
    for patterns, pattern, columns in keys:
        if patterns.matches_table(schema=obj.db_schema, table=obj.name):
            used.add(pattern)
            by_upper = {c.name.upper(): c.name for c in obj.columns}
            missing = [c for c in columns if c.upper() not in by_upper]
            if missing:
                raise PlanError(f"refresh.keys[{pattern!r}]: {obj.qualified_name} has no column(s) "
                                f"{', '.join(missing)}")
            return [by_upper[c.upper()] for c in columns]
    return None


def _watermark(task: TableTask, column: str | None) -> tuple[str | None, str | None]:
    """The source column marking changed rows for a keyed table, or why it has none."""
    if not column:
        return None, "the source can't tell which rows changed (set refresh.watermark_column)"
    found = next((c for c in task.source.columns if c.name.upper() == column.upper()), None)
    if found is None:
        return None, f"no {column.upper()} column (refresh.watermark_column)"
    if "TIMESTAMP" not in found.data_type and found.data_type != "DATE":
        return None, f"{found.name} is {found.data_type}, not a timestamp (refresh.watermark_column)"
    return found.name, None


def build_plan(config: ReplicationConfig, source, conn) -> Plan:
    case = config.naming.case
    plan = Plan(tables=[], views=[])
    seen: set[tuple[str | None, str, str]] = set()
    schema_cache: dict[str | None, list[str]] = {}
    object_cache: dict[tuple[str | None, tuple[str, ...]], list[SourceObject]] = {}

    candidates: list[tuple[SourceObject, str, str, str]] = []   # (obj, role, target_db_schema, target_name)
    config_keys = [(TablePatternSet(patterns=[pattern]), pattern, columns)
                   for pattern, columns in config.refresh.keys.items()]
    used_keys: set[str] = set()
    append = TablePatternSet(patterns=config.refresh.append)
    used_append: set[str] = set()
    for index, rule in enumerate(config.objects):
        source.check_catalog(conn=conn, catalog=rule.catalog)
        if rule.catalog not in schema_cache:
            schema_cache[rule.catalog] = source.list_db_schemas(conn=conn, catalog=rule.catalog)
        schema_patterns = PatternSet(patterns=rule.db_schemas)
        db_schemas = [s for s in schema_cache[rule.catalog] if schema_patterns.matches(name=s)]
        for p in schema_patterns.patterns:
            if not any(p.matches(name=s) for s in schema_cache[rule.catalog]):
                plan.warnings.append(f"objects[{index}].db_schemas pattern {p.text!r} matched no db_schema")
        if not db_schemas:
            continue
        key = (rule.catalog, tuple(db_schemas))
        if key not in object_cache:
            object_cache[key] = source.list_objects(conn=conn, catalog=rule.catalog, db_schemas=db_schemas)
            if config.refresh.mode == "incremental":
                keys = source.primary_keys(conn=conn, catalog=rule.catalog, db_schemas=db_schemas)
                for obj in object_cache[key]:
                    obj.primary_key = keys.get((obj.db_schema, obj.name))
                    configured = _configured_key(obj=obj, keys=config_keys, used=used_keys)
                    if configured is not None:
                        obj.primary_key = configured

        tables, exclude_tables = PatternSet(patterns=rule.tables), PatternSet(patterns=rule.exclude_tables)
        views, exclude_views = PatternSet(patterns=rule.views), PatternSet(patterns=rule.exclude_views)
        materialize = PatternSet(patterns=rule.materialize_views)
        for obj in object_cache[key]:
            ident = (obj.catalog, obj.db_schema, obj.name)
            if ident in seen:
                continue   # an earlier rule already decided this object
            if obj.kind in (TABLE, MATERIALIZED_VIEW):
                if not tables.matches(name=obj.name) or exclude_tables.matches(name=obj.name):
                    continue
                role = "table"
            elif obj.kind == VIEW:
                if not views.matches(name=obj.name) or exclude_views.matches(name=obj.name):
                    continue
                qualified = f"{obj.db_schema}.{obj.name}"
                role = "materialize" if (materialize.matches(name=obj.name) or materialize.matches(name=qualified)) \
                    else "view"
            else:
                continue
            seen.add(ident)
            template = rule.target_db_schema or "{db_schema}"
            target_db_schema = apply_case(name=template.replace("{db_schema}", obj.db_schema), case=case)
            candidates.append((obj, role, target_db_schema, apply_case(name=obj.name, case=case)))

    # Resolve target-name conflicts: a copied table wins over a view of the same target name
    # (e.g. a table copied from another db_schema to replace a view that wraps it).
    by_target: dict[tuple[str, str], tuple[SourceObject, str, str, str]] = {}
    for cand in candidates:
        obj, role, target_db_schema, target_name = cand
        key = (target_db_schema, target_name)
        existing = by_target.get(key)
        if existing is None:
            by_target[key] = cand
            continue
        existing_is_table = existing[1] != "view"
        if role != "view" and existing_is_table:
            raise PlanError(f"{existing[0].qualified_name} and {obj.qualified_name} would both be copied to "
                            f"{target_db_schema}.{target_name}")
        winner, loser = (cand, existing) if (role != "view" and not existing_is_table) else (existing, cand)
        by_target[key] = winner
        plan.warnings.append(f"skipping {loser[1]} {loser[0].qualified_name}: replaced by "
                             f"{winner[0].qualified_name} at {target_db_schema}.{target_name}")

    usage = _ColumnRuleUsage(rules=config.columns)
    lossy: list[str] = []
    unsupported = [f"{obj.qualified_name}.{c.name} ({c.unsupported})"
                   for obj, _, _, _ in by_target.values() for c in obj.columns if c.unsupported]
    if unsupported:
        plan.warnings.append("skipping columns that can't be copied: " + "; ".join(unsupported))
    for obj, role, target_db_schema, target_name in by_target.values():
        columns, excluded = usage.select(obj=obj)
        for c in columns:
            if not c.renamed:
                c.target_name = apply_case(name=c.column.name, case=case)
        target_names = [c.target_name for c in columns]
        if len(set(target_names)) != len(target_names):
            raise PlanError(f"{obj.qualified_name}: column names collide after naming.case={case!r}; "
                            "use naming.case: preserve, or exclude one of the columns")
        unsafe = [(c, reason) for c in columns if c.mask is None
                  and (reason := source.lossy_type(column=c.column)) is not None]
        if role == "view":
            fallback = TableTask(source=obj, target_db_schema=target_db_schema, target_table=target_name,
                                 columns=columns, excluded_columns=excluded, note="view materialized after a failure")
            if unsafe and columns and config.views.on_failure == "materialize":
                plan.warnings.append(f"{obj.qualified_name} won't be materialized if it can't be created as a view: "
                                     + "; ".join(f"{c.column.name}: {reason}" for c, reason in unsafe))
            plan.views.append(ViewTask(source=obj, target_db_schema=target_db_schema, target_view=target_name,
                                       columns_task=fallback if columns and not unsafe else None))
        else:
            if not obj.columns:
                plan.warnings.append(f"skipping {obj.qualified_name}: the source reported no columns "
                                     "(no privilege on it?)")
                continue
            if not columns:
                plan.warnings.append(f"skipping {obj.qualified_name}: every column is excluded")
                continue
            if unsafe:
                lossy.extend(f"{obj.qualified_name}.{c.column.name} ({c.column.data_type}): {reason}"
                             for c, reason in unsafe)
                continue
            note = "view materialized as a table" if role == "materialize" else None
            task = TableTask(source=obj, target_db_schema=target_db_schema, target_table=target_name,
                             columns=columns, excluded_columns=excluded, note=note)
            if config.refresh.mode == "incremental":
                task.incremental_key, task.incremental_note = _incremental_key(task=task)
                if not source.tracks_row_changes and append.matches_table(schema=obj.db_schema, table=obj.name):
                    used_append.add(obj.qualified_name)
                    task.incremental_key, task.incremental_note = [], None     # append-only, no key
                if task.incremental_key is not None and not source.tracks_row_changes:
                    task.watermark_column, note = _watermark(task=task, column=config.refresh.watermark_column)
                    if task.watermark_column is None:
                        task.incremental_key, task.incremental_note = None, note
            plan.tables.append(task)
            renamed = {c.column.name.upper(): c.target_name for c in columns if c.renamed}
            if renamed:
                plan.column_renames[(obj.db_schema.upper(), obj.name.upper())] = renamed
        plan.name_map[(obj.db_schema.upper(), obj.name.upper())] = (target_db_schema, target_name)
        if excluded and role != "view":
            plan.excluded_columns[(obj.db_schema.upper(), obj.name.upper())] = {c.upper() for c in excluded}

    if lossy:
        raise PlanError(
            "columns whose values the source driver can't hand over unchanged (they'd be copied wrong, "
            "with no error):\n  - " + "\n  - ".join(lossy)
            + "\nCopy each as text with a mask expression, e.g. columns: [{tables: [DB_SCHEMA.TABLE], mask: "
              "{COLUMN: {strategy: expression, sql: \"CAST({column} AS VARCHAR)\"}}}], or leave it out with "
              "exclude: [COLUMN].")
    for _, pattern, _ in config_keys:
        if pattern not in used_keys:
            plan.warnings.append(f"refresh.keys pattern {pattern!r} matched no selected table")
    if config.refresh.append and not used_append:
        plan.warnings.append(f"refresh.append patterns {config.refresh.append} matched no selected table")
    problems = usage.unmatched()
    if problems:
        if config.fail_on_unmatched_column_rules:
            raise PlanError("column rules that match nothing (a typo here could leak a column):\n  - "
                            + "\n  - ".join(problems)
                            + "\nFix the pattern, or set fail_on_unmatched_column_rules: false.")
        plan.warnings.extend(problems)

    plan.tables.sort(key=lambda t: (t.target_db_schema, t.target_table))
    plan.views.sort(key=lambda v: (v.target_db_schema, v.target_view))
    for objects in object_cache.values():
        for obj in objects:
            plan.column_types[(obj.db_schema.upper(), obj.name.upper())] = {
                c.name.upper(): c.data_type for c in obj.columns}
            plan.column_names[(obj.db_schema.upper(), obj.name.upper())] = {c.name.lower(): c.name for c in obj.columns}
    if plan.views and config.views.source_time_zone:
        warning = source.time_zone_warning(conn=conn, time_zone=config.views.source_time_zone)
        if warning:
            plan.warnings.append(warning)
    return plan
