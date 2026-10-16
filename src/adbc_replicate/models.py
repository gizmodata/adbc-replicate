"""Plain data objects passed between discovery, planning and the engine."""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field

from .config import MaskSpec

TABLE = "TABLE"
VIEW = "VIEW"
MATERIALIZED_VIEW = "MATERIALIZED VIEW"


@dataclass
class Column:
    name: str
    data_type: str                 # source type name, upper-case (NUMBER, VARCHAR2, numeric, ...)
    precision: int | None = None
    scale: int | None = None
    unsupported: str | None = None # why this column can't be copied (it's skipped, and the plan says so)


@dataclass
class SourceObject:
    catalog: str | None
    db_schema: str
    name: str
    kind: str                      # TABLE | VIEW | MATERIALIZED VIEW
    columns: list[Column] = field(default_factory=list)
    last_altered: datetime.datetime | None = None   # set only where it reliably tracks data changes
    external: bool = False         # data lives outside the database (files): no snapshot reads
    primary_key: list[str] | None = None   # source column names, in key order (refresh.mode: incremental)

    @property
    def qualified_name(self) -> str:
        return f"{self.db_schema}.{self.name}"


@dataclass
class SelectedColumn:
    column: Column
    target_name: str
    mask: MaskSpec | None = None
    fingerprint: bool = True       # False: copied, but ignored by change detection
    renamed: bool = False          # target_name came from a rename rule: naming.case doesn't apply


@dataclass
class TableTask:
    source: SourceObject
    target_db_schema: str
    target_table: str
    columns: list[SelectedColumn]
    excluded_columns: list[str] = field(default_factory=list)
    note: str | None = None        # e.g. "view materialized as a table"
    incremental_key: list[str] | None = None   # target column names to merge on; [] appends (refresh.append);
                                               # None: whole-table copies
    incremental_note: str | None = None        # why a table isn't incremental (refresh.mode: incremental)
    watermark_column: str | None = None        # source column marking changed rows (refresh.watermark_column)

    @property
    def target_key(self) -> tuple[str, str]:
        return (self.target_db_schema, self.target_table)


@dataclass
class ViewTask:
    source: SourceObject
    target_db_schema: str
    target_view: str
    columns_task: TableTask | None = None   # used when the view falls back to materialization

    @property
    def target_key(self) -> tuple[str, str]:
        return (self.target_db_schema, self.target_view)


@dataclass
class Plan:
    tables: list[TableTask]
    views: list[ViewTask]
    warnings: list[str] = field(default_factory=list)
    # (source db_schema, source name) upper-cased  ->  (target db_schema, target name), for every
    # object the run creates in the target; used to re-point table references inside views.
    name_map: dict[tuple[str, str], tuple[str, str]] = field(default_factory=dict)
    # (source db_schema, name) upper-cased -> {column upper-cased: source data type}, for every
    # discovered table and view; lets view translation tell a date from a number.
    column_types: dict[tuple[str, str], dict[str, str]] = field(default_factory=dict)
    # (source db_schema, name) upper-cased -> {column lower-cased: column name exactly as in the source}; view
    # translation uses it to find references that match a column only when case is ignored.
    column_names: dict[tuple[str, str], dict[str, str]] = field(default_factory=dict)
    # (source db_schema, name) upper-cased -> {source column upper-cased: target name}, for columns
    # renamed by column rules; views reading them are re-pointed.
    column_renames: dict[tuple[str, str], dict[str, str]] = field(default_factory=dict)
    # (source db_schema, name) upper-cased -> source column names upper-cased that column rules leave
    # out; a view's * EXCLUDE (...) naming one of them would otherwise fail to bind in the target.
    excluded_columns: dict[tuple[str, str], set[str]] = field(default_factory=dict)


@dataclass
class TableResult:
    task: TableTask
    action: str                    # copied | merged | skipped | failed
    reason: str = ""
    rows: int | None = None
    row_count: int | None = None
    fingerprint: str | None = None
    seconds: float = 0.0
    error: str | None = None
    change_token: str | None = None   # where the next incremental read starts (e.g. an Oracle SCN)
    deleted: int | None = None        # rows removed by an incremental merge's delete detection
