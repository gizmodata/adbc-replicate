"""Run a replication: plan, copy changed tables (streamed, optionally in parallel), recreate
views, and record everything in the target's sync state."""

from __future__ import annotations

import concurrent.futures
import dataclasses
import datetime
import hashlib
import threading
import time
import uuid
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from functools import partial

from . import __version__
from .arrowutil import coalesce, counting, transform
from .config import ReplicationConfig
from .events import Reporter, fmt_rows, fmt_seconds
from .matching import TablePatternSet
from .models import Plan, TableResult, TableTask, ViewTask
from .notify import build_payload, deliver, outcome_of
from .planner import PlanError, build_plan
from .sources import make_source
from .state import StateStore, column_signature
from .targets import make_target
from .targets.duckdb import DuplicateKeyError, is_transient
from .views import ViewTranslationError, create_view_sql, load_override, translate_view

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_CONFIG = 2
EXIT_VIEW_ERRORS = 3
EXIT_INTERRUPTED = 130      # the CLI reports 128 + the signal number (130 SIGINT, 143 SIGTERM)

PROGRESS_EVERY_SECONDS = 30.0


@dataclass
class RunOptions:
    full_refresh: bool = False
    row_limit: int | None = None
    only: list[str] = field(default_factory=list)      # DB_SCHEMA.NAME patterns
    parallelism: int | None = None
    skip_views: bool = False


@dataclass
class RunSummary:
    run_id: str
    status: str
    stats: dict
    view_failures: dict[str, str]
    table_failures: dict[str, str]
    exit_code: int


def _filter_plan(plan: Plan, only: list[str]) -> Plan:
    if not only:
        return plan
    patterns = TablePatternSet(patterns=only)
    plan.tables = [t for t in plan.tables if patterns.matches_table(schema=t.source.db_schema, table=t.source.name)]
    plan.views = [v for v in plan.views if patterns.matches_table(schema=v.source.db_schema, table=v.source.name)]
    return plan


class Replicator:
    def __init__(self, config: ReplicationConfig, reporter: Reporter | None = None,
                 options: RunOptions | None = None) -> None:
        self.config = config
        self.reporter = reporter or Reporter()
        self.options = options or RunOptions()
        self.source = make_source(config=config.source)
        self.target = make_target(config=config.target)
        self._local = threading.local()
        self._stop = threading.Event()
        self._worker_connections: list = []
        self._worker_lock = threading.Lock()
        self._inflight: set = set()          # cursors with a statement running, cancelled by cancel()
        self.interrupted: str | None = None   # set by cancel(): why the run is stopping (e.g. "SIGTERM")

    # ---- cancellation ---------------------------------------------------------------------

    @contextmanager
    def track(self, cursor):
        """Register a cursor while its statement runs, so cancel() can interrupt it."""
        with self._worker_lock:
            self._inflight.add(cursor)
        try:
            if self.interrupted:
                raise RuntimeError(f"run interrupted ({self.interrupted})")
            yield cursor
        finally:
            with self._worker_lock:
                self._inflight.discard(cursor)

    def cancel(self, reason: str) -> None:
        """Stop the run from another thread (a signal handler): no new tables start, and every
        statement in flight, in any worker, is cancelled, so its target transaction rolls back and
        no server session is left holding locks."""
        self.interrupted = reason
        self._stop.set()
        with self._worker_lock:
            cursors = list(self._inflight)
        for cursor in cursors:
            with suppress(Exception):
                cursor.adbc_cancel()

    # ---- helpers --------------------------------------------------------------------------

    def emit(self, event: str, text: str | None = None, **fields) -> None:
        self.reporter.emit(event=event, text=text, **fields)

    def signature(self, task: TableTask) -> str:
        return column_signature(entries=[
            [c.column.name, c.target_name, self.source.column_expr(column=c.column, mask=c.mask), c.fingerprint]
            for c in task.columns])

    def _connections(self, main_source, main_target):
        """The (source, target) connections for the current thread."""
        if self._parallelism() == 1:
            return main_source, main_target
        conns = getattr(self._local, "conns", None)
        if conns is None:
            conns = (self.source.worker_connection(conn=main_source), self.target.worker_connection(conn=main_target))
            self._local.conns = conns
            with self._worker_lock:
                self._worker_connections.extend(conns)
        return conns

    def _parallelism(self) -> int:
        return self.options.parallelism or self.config.parallelism

    # ---- planning -------------------------------------------------------------------------

    def start_consistency(self, conn) -> str | None:
        mode = self.config.source.consistency
        if mode == "serializable":
            self.source.begin_serializable(conn=conn)
            return "serializable transaction"
        if mode == "snapshot":
            return self.source.begin_snapshot(conn=conn).description
        return None

    def build_plan(self, source_conn) -> Plan:
        return _filter_plan(plan=build_plan(config=self.config, source=self.source, conn=source_conn),
                            only=self.options.only)

    # ---- tables ---------------------------------------------------------------------------

    def _fingerprint(self, conn, task: TableTask) -> tuple[int | None, str | None]:
        sql = self.source.fingerprint_sql(task=task)
        if sql is None:
            return None, None
        with conn.cursor() as cur, self.track(cursor=cur):
            cur.execute(operation=sql)
            rows = cur.fetchall()
        count, fingerprint = rows[0]
        return int(float(count)), (str(fingerprint) if fingerprint is not None else None)

    def sync_table(self, task: TableTask, previous: dict | None, exists_as_table: bool, exists_as_view: bool,
                   main_source, main_target) -> TableResult:
        """Copy one table if (and only if) it changed. Runs in a worker thread when parallel."""
        if self._stop.is_set():
            return TableResult(task=task, action="not_started", reason="run aborted after a table failure")
        started = time.monotonic()
        sconn, tconn = self._connections(main_source=main_source, main_target=main_target)
        name = task.source.qualified_name
        refresh_full = self.options.full_refresh or self.config.refresh.mode == "full"
        marker = self.source.change_marker(obj=task.source)
        row_count = fingerprint = None
        # refresh.mode incremental, for a table with a usable primary key: merge the changed rows
        incremental = (self.config.refresh.mode == "incremental" and task.incremental_key is not None
                       and self.options.row_limit is None and not self.options.full_refresh)
        token = self.source.change_token() if incremental and not task.watermark_column else None
        try:
            signature = self.signature(task=task)
            if self.options.row_limit is not None:
                reason = f"sample of at most {self.options.row_limit:,} rows"
            elif refresh_full:
                reason = "full refresh"
            elif not exists_as_table:
                reason = "missing in target"
            elif previous is None:
                reason = "no sync state"
            elif previous["column_signature"] != signature:
                reason = "column selection changed"
            elif marker is not None and previous["change_marker"] == marker:
                return TableResult(task=task, action="skipped", reason="unchanged (change marker)",
                                   row_count=previous["row_count"], fingerprint=previous["fingerprint"],
                                   seconds=time.monotonic() - started,
                                   change_token=self._skip_token(conn=sconn, task=task, previous=previous,
                                                                 token=token, incremental=incremental))
            elif incremental and task.watermark_column and previous.get("change_token") and \
                    (token := self._watermark(conn=sconn, task=task)) is not None:
                since = previous["change_token"]
                appending = not task.incremental_key
                where = self.source.watermark_filter(
                    task=task, previous=since,
                    lookback_minutes=0 if appending else self.config.refresh.watermark_lookback_minutes)
                how = "appending" if appending else f"key {', '.join(task.incremental_key)}"
                self.emit("table_started", f"  🔄 {'Appending to' if appending else 'Merging'} {name} → "
                          f"{task.target_db_schema}.{task.target_table} "
                          f"(rows with {task.watermark_column} after {since}, {how})",
                          table=name, target=f"{task.target_db_schema}.{task.target_table}", reason="incremental",
                          since=since)
                result = self._with_retries(task=task, tconn=tconn, load=lambda: self._merge(
                    task=task, sconn=sconn, tconn=tconn, where=where))
                if result is not None:
                    merged, deleted, row_count = result
                    return TableResult(task=task, action="merged", reason=f"incremental since {since}", rows=merged,
                                       deleted=deleted, row_count=row_count, change_token=token,
                                       seconds=time.monotonic() - started)
                reason = "row count differs from the source after the merge"
            elif token and previous.get("change_token") and \
                    (where := self.source.incremental_filter(previous_token=previous["change_token"])) is not None:
                since = previous["change_token"]
                self.emit("table_started", f"  🔄 Merging {name} → {task.target_db_schema}.{task.target_table} "
                          f"(rows changed since {since}, key {', '.join(task.incremental_key)})", table=name,
                          target=f"{task.target_db_schema}.{task.target_table}", reason="incremental", since=since)
                result = self._with_retries(task=task, tconn=tconn, load=lambda: self._merge(
                    task=task, sconn=sconn, tconn=tconn, where=where))
                if result is not None:
                    merged, deleted, row_count = result
                    return TableResult(task=task, action="merged", reason=f"incremental since {since}", rows=merged,
                                       deleted=deleted, row_count=row_count, change_token=token,
                                       seconds=time.monotonic() - started)
                reason = "row count differs from the source after the merge"
            else:
                row_count, fingerprint = self._fingerprint(conn=sconn, task=task)
                if fingerprint is not None and previous["fingerprint"] == fingerprint \
                        and previous["row_count"] == row_count:
                    # unchanged as of this run's snapshot, so the next incremental read can start here
                    return TableResult(task=task, action="skipped",
                                       reason=f"unchanged (fingerprint, {fmt_rows(row_count)} rows)",
                                       row_count=row_count, fingerprint=fingerprint,
                                       seconds=time.monotonic() - started,
                                       change_token=self._skip_token(conn=sconn, task=task, previous=previous,
                                                                     token=token, incremental=incremental))
                reason = "content changed" if fingerprint is not None else "no fingerprint for this table"

            # Fingerprint BEFORE the copy: if the table changes in between, the next run sees a
            # different fingerprint and copies again, never the other way round. (Incremental
            # tables track changes with the change token instead.)
            if fingerprint is None and self.options.row_limit is None and not incremental \
                    and self.config.refresh.mode in ("changed", "incremental"):
                row_count, fingerprint = self._fingerprint(conn=sconn, task=task)

            if incremental and task.watermark_column and token is None:
                # taken BEFORE the copy: rows stamped while it runs are read again next time
                token = self._watermark(conn=sconn, task=task)
            self.emit("table_started", f"  📦 Copying {name} → {task.target_db_schema}.{task.target_table} ({reason})",
                      table=name, target=f"{task.target_db_schema}.{task.target_table}", reason=reason)
            rows = self._with_retries(task=task, tconn=tconn, load=lambda: self._copy(
                task=task, sconn=sconn, tconn=tconn, exists_as_view=exists_as_view))
            return TableResult(task=task, action="copied", reason=reason, rows=rows, row_count=row_count,
                               fingerprint=fingerprint, seconds=time.monotonic() - started, change_token=token)
        except Exception as e:
            with suppress(Exception):
                tconn.rollback()
            error = f"{type(e).__name__}: {e}"
            hint = self.source.error_hint(error=error)
            return TableResult(task=task, action="failed", error=error + (f" — hint: {hint}" if hint else ""),
                               seconds=time.monotonic() - started)

    def _with_retries(self, task: TableTask, tconn, load):
        """Re-run a load that failed on a transient connection error. Safe to repeat: a copy or a
        merge is one target transaction, and the source re-reads at the same snapshot."""
        retries, wait = self.config.ingest.retries, self.config.ingest.retry_wait_seconds
        for attempt in range(retries + 1):
            try:
                return load()
            except Exception as e:
                if attempt == retries or not is_transient(error=e) or self._stop.is_set() or self.interrupted:
                    raise
                with suppress(Exception):
                    tconn.rollback()
                delay = wait * (2 ** attempt)
                name = task.source.qualified_name
                self.emit("table_retry", f"  🔁 {name}: {type(e).__name__}: {str(e).splitlines()[0]} — retrying in "
                          f"{fmt_seconds(delay)} (attempt {attempt + 2} of {retries + 1})", level="warning",
                          table=name, attempt=attempt + 2, error=f"{type(e).__name__}: {e}")
                time.sleep(delay)
        raise AssertionError("unreachable")

    def _copy(self, task: TableTask, sconn, tconn, exists_as_view: bool) -> int:
        if exists_as_view:
            # A view of the same name would block the ingest's CREATE OR REPLACE TABLE.
            self.target.drop(conn=tconn, db_schema=task.target_db_schema, name=task.target_table, kind="VIEW")
        copied = self._load(task=task, sql=self.source.select_sql(task=task, row_limit=self.options.row_limit),
                            sconn=sconn, tconn=tconn, db_schema=task.target_db_schema, table=task.target_table)
        tconn.commit()
        return copied

    def _skip_token(self, conn, task: TableTask, previous: dict, token: str | None, incremental: bool) -> str | None:
        """The change token to keep for an unchanged table: the run's token (Oracle SCN), or for a
        watermark table its previous watermark, read now if it has none yet (e.g. state from a run in
        refresh mode changed), so the next change can be merged instead of copied."""
        if not (incremental and task.watermark_column):
            return token
        return previous.get("change_token") or self._watermark(conn=conn, task=task)

    def _watermark(self, conn, task: TableTask) -> str | None:
        rows = self.source.fetch_all(conn=conn, sql=self.source.watermark_sql(task=task))
        return str(rows[0][0]) if rows and rows[0][0] is not None else None

    def _merge(self, task: TableTask, sconn, tconn, where: str) -> tuple[int, int, int] | None:
        """Stage the rows changed since the last run (and, for delete detection, every current key)
        in the state db_schema, then merge them into the table in the same target transaction.
        Without delete detection, the merged table's row count must equal the source's; if it doesn't
        (rows were deleted, or a key isn't unique), the merge is rolled back and None returned, so
        the caller copies the table whole."""
        staging = self.config.state.db_schema
        suffix = hashlib.sha256("|".join(task.target_key).encode()).hexdigest()[:8]
        changes, keys = f"_changes_{suffix}", f"_keys_{suffix}"
        self._load(task=task, sql=self.source.select_sql(task=task, where=where), sconn=sconn, tconn=tconn,
                   db_schema=staging, table=changes)
        detect_deletes = self.config.refresh.detect_deletes and bool(task.incremental_key)
        if detect_deletes:
            key_task = dataclasses.replace(task, columns=[c for c in task.columns
                                                          if c.target_name in task.incremental_key])
            self._load(task=key_task, sql=self.source.select_sql(task=key_task), sconn=sconn, tconn=tconn,
                       db_schema=staging, table=keys, quiet=True)
        try:
            if not task.incremental_key:
                merged, deleted = self.target.append(conn=tconn, db_schema=task.target_db_schema,
                                                     table=task.target_table, staging_db_schema=staging,
                                                     changes=changes), 0
            else:
                merged, deleted = self.target.merge(conn=tconn, db_schema=task.target_db_schema,
                                                    table=task.target_table, key=task.incremental_key,
                                                    staging_db_schema=staging, changes=changes,
                                                    keys=keys if detect_deletes else None)
        except DuplicateKeyError as e:
            tconn.rollback()
            self.emit("table_merge_mismatch", f"  ⚠️  {task.source.qualified_name}: {e}; copying it whole",
                      level="warning", table=task.source.qualified_name, error=str(e))
            return None
        row_count = self.target.query(conn=tconn, sql="SELECT count(*) FROM " + self.target.qualified(
            db_schema=task.target_db_schema, name=task.target_table))[0][0]
        if not detect_deletes:
            source_count = int(float(self.source.fetch_all(conn=sconn, sql=self.source.count_sql(task=task))[0][0]))
            if source_count != int(row_count):
                tconn.rollback()
                self.emit("table_merge_mismatch", f"  ⚠️  {task.source.qualified_name}: {fmt_rows(int(row_count))} rows "
                          f"after the merge, {fmt_rows(source_count)} in the source; copying it whole",
                          level="warning", table=task.source.qualified_name, target_rows=int(row_count),
                          source_rows=source_count)
                return None
        tconn.commit()
        return merged, deleted, int(row_count)

    def _load(self, task: TableTask, sql: str, sconn, tconn, db_schema: str, table: str, quiet: bool = False) -> int:
        """Stream a source SELECT into a (new copy of a) target table, in the caller's transaction."""
        name = task.source.qualified_name
        copied = 0
        started = last_report = time.monotonic()

        def progress(rows: int) -> None:
            nonlocal copied, last_report
            copied += rows
            now = time.monotonic()
            if now - last_report >= PROGRESS_EVERY_SECONDS and not quiet:
                last_report = now
                self.emit("table_progress", f"     ⏳ {name}: {fmt_rows(copied)} rows ({fmt_seconds(now - started)})",
                          table=name, rows=copied, seconds=round(now - started, 1))

        with sconn.cursor() as cur, self.track(cursor=cur):
            self.source.prepare_cursor(cursor=cur)
            cur.execute(operation=sql)
            reader = cur.fetch_record_batch()
            reader = transform(reader=reader, names=[c.target_name for c in task.columns],
                               casts=self.source.arrow_casts(task=task, schema=reader.schema))
            reader = coalesce(reader=reader, max_rows=self.config.ingest.batch_rows,
                              max_bytes=self.config.ingest.batch_bytes)
            reader = counting(reader=reader, on_batch=progress)
            self.target.ingest(conn=tconn, db_schema=db_schema, table=table, reader=reader, track=self.track)
        return copied

    def copy_tables(self, plan: Plan, store: StateStore, run_id: str, table_state: dict,
                    existing_tables: set, existing_views: set, main_source, main_target) -> list[TableResult]:
        results: list[TableResult] = []

        def handle(result: TableResult) -> None:
            task = result.task
            name = task.source.qualified_name
            target_name = f"{task.target_db_schema}.{task.target_table}"
            results.append(result)
            if result.action == "copied":
                self.emit("table_copied", f"  ✅ {name}: {fmt_rows(result.rows)} rows in {fmt_seconds(result.seconds)}"
                          + (f" (excluded {', '.join(task.excluded_columns)})" if task.excluded_columns else ""),
                          table=name, target=target_name, rows=result.rows, seconds=round(result.seconds, 2))
                if self.options.row_limit is not None:
                    store.forget_table(db_schema=task.target_db_schema, table=task.target_table)
                else:
                    store.save_table(task=task, change_marker=self.source.change_marker(obj=task.source),
                                     fingerprint=result.fingerprint, row_count=result.row_count,
                                     column_signature=self.signature(task=task), copied=True, run_id=run_id,
                                     change_token=result.change_token)
            elif result.action == "merged":
                self.emit("table_merged", f"  ✅ {name}: {fmt_rows(result.rows)} changed rows merged, "
                          f"{fmt_rows(result.deleted)} deleted in {fmt_seconds(result.seconds)} "
                          f"({fmt_rows(result.row_count)} rows now)", table=name, target=target_name,
                          rows=result.rows, deleted=result.deleted, row_count=result.row_count,
                          seconds=round(result.seconds, 2))
                store.save_table(task=task, change_marker=self.source.change_marker(obj=task.source),
                                 fingerprint=None, row_count=result.row_count,
                                 column_signature=self.signature(task=task), copied=True, run_id=run_id,
                                 change_token=result.change_token)
            elif result.action == "skipped":
                self.emit("table_skipped", f"  ⏭️  {name}: {result.reason}", table=name, target=target_name,
                          reason=result.reason)
                store.save_table(task=task, change_marker=self.source.change_marker(obj=task.source),
                                 fingerprint=result.fingerprint, row_count=result.row_count,
                                 column_signature=self.signature(task=task), copied=False, run_id=run_id,
                                 change_token=result.change_token)
            elif result.action == "failed":
                self.emit("table_failed", f"  ❌ {name}: {result.error}", level="error", table=name,
                          target=target_name, error=result.error)
                if self.config.on_table_error == "abort":
                    self._stop.set()
            store.record_object(run_id=run_id, kind="table", source_name=name, db_schema=task.target_db_schema,
                                name=task.target_table, action=result.action, reason=result.reason or None,
                                rows=result.rows, seconds=round(result.seconds, 3), error=result.error)

        def run_one(task: TableTask) -> TableResult:
            return self.sync_table(task=task, previous=table_state.get(task.target_key),
                                   exists_as_table=task.target_key in existing_tables,
                                   exists_as_view=task.target_key in existing_views,
                                   main_source=main_source, main_target=main_target)

        if self._parallelism() == 1:
            for task in plan.tables:
                handle(result=run_one(task=task))
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=self._parallelism(),
                                                       thread_name_prefix="replicate") as pool:
                futures = [pool.submit(run_one, task) for task in plan.tables]
                for future in concurrent.futures.as_completed(futures):
                    handle(result=future.result())
        return results

    # ---- views ----------------------------------------------------------------------------

    def create_views(self, plan: Plan, store: StateStore, run_id: str, view_state: dict, existing_tables: set,
                     existing_views: set, source_conn, target_conn, table_state: dict) -> tuple[int, int, dict]:
        if not plan.views:
            return 0, 0, {}
        copied_keys = {t.target_key for t in plan.tables}
        # Leftover tables where views now belong (e.g. views that an older run materialized).
        for view in plan.views:
            if view.target_key in existing_tables and view.target_key not in copied_keys:
                self.target.drop(conn=target_conn, db_schema=view.target_db_schema, name=view.target_view,
                                 kind="TABLE")
        target_conn.commit()

        definitions: dict = {}
        for catalog in sorted({v.source.catalog for v in plan.views}, key=lambda c: c or ""):
            in_catalog = [v.source for v in plan.views if v.source.catalog == catalog]
            definitions.update(self.source.view_definitions(conn=source_conn, catalog=catalog, views=in_catalog))

        failures: dict[str, str] = {}
        statements: dict[tuple[str, str], str] = {}
        macros: dict[tuple[str, str], str] = {}
        for view in plan.views:
            name = view.source.qualified_name
            try:
                override = load_override(overrides_dir=self.config.views.overrides_dir,
                                         db_schema=view.source.db_schema, view=view.source.name)
                if override is not None:
                    statements[view.target_key] = create_view_sql(target_db_schema=view.target_db_schema,
                                                                  target_view=view.target_view, body=override,
                                                                  target_catalog=self.target.catalog)
                    continue
                definition = definitions.get((view.source.db_schema, view.source.name))
                if not definition:
                    raise ViewTranslationError("the source returned no definition (no privilege on it?)")
                translate = partial(translate_view,
                    definition=definition, source_dialect=self.source.dialect,
                    view_db_schema=view.source.db_schema, target_db_schema=view.target_db_schema,
                    target_view=view.target_view, name_map=plan.name_map, case=self.config.naming.case,
                    source_columns=[c.name for c in view.source.columns], target_catalog=self.target.catalog,
                    pseudo_columns=self.config.views.source_pseudo_columns,
                    time_zone=self.config.views.source_time_zone, column_types=plan.column_types,
                    column_names=plan.column_names,
                    functions=self.config.views.functions, column_renames=plan.column_renames,
                    excluded_columns=plan.excluded_columns)
                statements[view.target_key] = translate()
            except Exception as e:
                failures[name] = f"translation: {e}"
                continue
            # a parameterized view (SYS_CONTEXT) also gets a table macro of the same name
            if self.config.views.sys_context_macros and self.source.dialect == "oracle":
                try:
                    macro = translate(as_macro=True)
                except Exception as e:
                    self.emit("view_macro_skipped", f"  ⚠️  {name}: no parameterized macro ({e})",
                              level="warning", view=name)
                    macro = None
                if macro:
                    macros[view.target_key] = macro

        created = skipped = 0
        pending = [v for v in plan.views if v.target_key in statements]
        # Views can depend on other views: retry the failures until a pass makes no progress.
        while pending:
            remaining = []
            for view in pending:
                name = view.source.qualified_name
                sql = statements[view.target_key]
                macro = macros.get(view.target_key)
                definition_hash = hashlib.sha256((sql + ("\n" + macro if macro else "")).encode()).hexdigest()
                full = self.options.full_refresh or self.config.refresh.mode == "full"
                # Same definition as last time AND still valid: a table it reads may have changed shape
                # (e.g. a column newly excluded), which leaves an unchanged view broken.
                unchanged = view.target_key in existing_views and view_state.get(view.target_key) == definition_hash \
                    and self._view_binds(conn=target_conn, view=view)
                if not full and unchanged:
                    skipped += 1
                    store.record_object(run_id=run_id, kind="view", source_name=name, db_schema=view.target_db_schema,
                                        name=view.target_view, action="skipped", reason="unchanged definition")
                    continue
                try:
                    self.target.execute(conn=target_conn, sql=sql)
                    target_conn.commit()
                except Exception as e:
                    with suppress(Exception):
                        target_conn.rollback()
                    failures[name] = str(e)
                    remaining.append(view)
                    continue
                if macro:
                    try:
                        self.target.execute(conn=target_conn, sql=macro)
                        target_conn.commit()
                        self.emit("view_macro_created", f"  🧩 {view.target_db_schema}.{view.target_view}(...): "
                                  "table macro for the parameterized view", view=name)
                    except Exception as e:
                        with suppress(Exception):
                            target_conn.rollback()
                        self.emit("view_macro_skipped", f"  ⚠️  {name}: parameterized macro failed: "
                                  f"{str(e).splitlines()[0][:200]}", level="warning", view=name)
                        definition_hash = hashlib.sha256(sql.encode()).hexdigest()    # retried next run
                store.save_view(db_schema=view.target_db_schema, view=view.target_view,
                                definition_hash=definition_hash, run_id=run_id)
                store.record_object(run_id=run_id, kind="view", source_name=name, db_schema=view.target_db_schema,
                                    name=view.target_view, action="created")
                failures.pop(name, None)
                created += 1
                self.emit("view_created", f"  🪟 {name} → {view.target_db_schema}.{view.target_view}",
                          view=name, target=f"{view.target_db_schema}.{view.target_view}")
            if len(remaining) == len(pending):
                break
            pending = remaining

        if failures and self.config.views.on_failure == "materialize":
            by_name = {v.source.qualified_name: v for v in plan.views}
            for name in list(failures):
                view: ViewTask = by_name[name]
                if view.columns_task is None:
                    continue
                self.emit("view_materializing", f"  🧱 {name}: can't be created as a view, copying its data instead "
                          f"({failures[name].splitlines()[0][:200]})", view=name)
                try:
                    # On the main connections: worker connections are closed by now.
                    rows = self._copy(task=view.columns_task, sconn=source_conn, tconn=target_conn, exists_as_view=True)
                except Exception as e:
                    with suppress(Exception):
                        target_conn.rollback()
                    failures[name] += f"; materializing failed too: {type(e).__name__}: {e}"
                    continue
                failures.pop(name)
                created += 1
                self.emit("view_materialized", f"  ✅ {name}: materialized {fmt_rows(rows)} rows", view=name, rows=rows)
                store.record_object(run_id=run_id, kind="view", source_name=name, db_schema=view.target_db_schema,
                                    name=view.target_view, action="materialized", rows=rows)

        by_name = {v.source.qualified_name: v for v in plan.views}
        for name, error in failures.items():
            view = by_name[name]
            # A view that can't be (re)created mustn't linger in the target as an older version that no
            # longer matches the source (or no longer binds): drop it and forget its state.
            if view.target_key in existing_views:
                with suppress(Exception):
                    self.target.drop(conn=target_conn, db_schema=view.target_db_schema, name=view.target_view,
                                     kind="VIEW")
                    target_conn.commit()
                    self.emit("view_dropped", f"  🗑️  {view.target_db_schema}.{view.target_view}: dropped the "
                              "previous version (it couldn't be recreated)", level="warning", view=name)
            with suppress(Exception):           # its parameterized macro, if it had one
                self.target.drop(conn=target_conn, db_schema=view.target_db_schema, name=view.target_view,
                                 kind="MACRO TABLE")
                target_conn.commit()
            with suppress(Exception):
                store.forget_view(db_schema=view.target_db_schema, view=view.target_view)
            store.record_object(run_id=run_id, kind="view", source_name=name, db_schema=view.target_db_schema,
                                name=view.target_view, action="failed", error=error)
        return created, skipped, failures

    def _view_binds(self, conn, view: ViewTask) -> bool:
        """True if the target view still binds (all the tables and columns it reads exist)."""
        try:
            self.target.query(conn=conn, sql="SELECT * FROM " + self.target.qualified(
                db_schema=view.target_db_schema, name=view.target_view) + " LIMIT 0")
            conn.commit()
            return True
        except Exception:
            with suppress(Exception):
                conn.rollback()
            return False

    # ---- the run --------------------------------------------------------------------------

    def run(self) -> RunSummary:
        """Run the replication, then call the configured webhooks with the outcome (also when the run
        fails before or while connecting)."""
        run_id = str(uuid.uuid4())
        stats: dict = {}
        started_at, started = datetime.datetime.now(tz=datetime.UTC), time.monotonic()
        summary, error, exit_code = None, None, EXIT_FAILED
        try:
            summary = self._recorded_run(run_id=run_id, stats=stats)
            exit_code = summary.exit_code
            return summary
        except PlanError as e:
            error, exit_code = str(e), EXIT_CONFIG
            raise
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            raise
        finally:
            if self.config.notifications:
                payload = build_payload(
                    outcome=outcome_of(exit_code=exit_code), exit_code=exit_code, run_id=run_id,
                    replication=self.config.name, source=self.config.source.type, target=self.config.target.type,
                    catalog=self.target.catalog, started_at=started_at, seconds=time.monotonic() - started,
                    stats=summary.stats if summary else stats,
                    table_failures=summary.table_failures if summary else {},
                    view_failures=summary.view_failures if summary else {}, error=error,
                    include_errors=all(n.include_errors for n in self.config.notifications))
                deliver(notifications=self.config.notifications, payload=payload, emit=self.emit)

    def _recorded_run(self, run_id: str, stats: dict) -> RunSummary:
        snapshot_desc = None
        refresh_mode = "full" if self.options.full_refresh else self.config.refresh.mode
        target_conn = self.target.connect()
        try:
            store = StateStore(target=self.target, conn=target_conn, db_schema=self.config.state.db_schema)
            store.ensure()
            store.start_run(run_id=run_id, replication=self.config.name, tool_version=__version__,
                            refresh_mode=refresh_mode, row_limit=self.options.row_limit)
            self.emit("run_started", f"🚀 adbc-replicate {__version__}: {self.config.source.type} → "
                      f"{self.config.target.type} (catalog {self.target.catalog}), run {run_id}"
                      + (f", {refresh_mode} refresh" if refresh_mode == "full" else "")
                      + (f", row limit {self.options.row_limit:,}" if self.options.row_limit else ""),
                      run_id=run_id, source=self.config.source.type, target=self.config.target.type,
                      catalog=self.target.catalog, refresh_mode=refresh_mode, row_limit=self.options.row_limit)
            try:
                summary = self._run(run_id=run_id, store=store, target_conn=target_conn, stats=stats)
                snapshot_desc = summary.stats.get("snapshot")
                error = None
                if summary.table_failures:
                    error = "; ".join(f"{k}: {v}" for k, v in summary.table_failures.items())
                store.finish_run(run_id=run_id, status=summary.status, stats=summary.stats, snapshot=snapshot_desc,
                                 error=error)
                return summary
            except Exception as e:
                with suppress(Exception):
                    target_conn.rollback()
                store.finish_run(run_id=run_id, status="failed", stats=stats, snapshot=stats.get("snapshot"),
                                 error=f"{type(e).__name__}: {e}")
                raise
        finally:
            with suppress(Exception):
                target_conn.close()

    def _run(self, run_id: str, store: StateStore, target_conn, stats: dict) -> RunSummary:
        source_conn = self.source.connect()
        try:
            snapshot_desc = self.start_consistency(conn=source_conn)
            if snapshot_desc:
                stats["snapshot"] = snapshot_desc
                self.emit("snapshot", f"📸 Consistent read: {snapshot_desc}", snapshot=snapshot_desc)

            started = time.monotonic()
            self.emit("discovering", "🔍 Discovering source objects…")
            plan = self.build_plan(source_conn=source_conn)
            for warning in plan.warnings:
                self.emit("warning", f"⚠️  {warning}", level="warning", message=warning)
            stats.update(tables_total=len(plan.tables), views_total=len(plan.views))
            self.emit("planned", f"🗺️  {len(plan.tables)} table(s) and {len(plan.views)} view(s) selected "
                      f"({fmt_seconds(time.monotonic() - started)})", tables=len(plan.tables), views=len(plan.views))

            if self.config.target.checkpoint:
                error = self.target.checkpoint(conn=target_conn)
                if error:
                    self.emit("warning", f"⚠️  CHECKPOINT before load failed: {error}", level="warning")

            table_state = store.load_tables()
            view_state = store.load_views()
            existing_tables, existing_views = self.target.existing_objects(conn=target_conn)
            target_db_schemas = {t.target_db_schema for t in plan.tables} | {v.target_db_schema for v in plan.views}
            for db_schema in sorted(target_db_schemas):
                self.target.ensure_db_schema(conn=target_conn, db_schema=db_schema)

            self.emit("tables_started", f"📦 Copying tables (parallelism {self._parallelism()})…")
            tables_started = time.monotonic()
            try:
                results = self.copy_tables(plan=plan, store=store, run_id=run_id, table_state=table_state,
                                           existing_tables=existing_tables, existing_views=existing_views,
                                           main_source=source_conn, main_target=target_conn)
            finally:
                for conn in self._worker_connections:
                    with suppress(Exception):
                        conn.close()
                self._worker_connections.clear()
            copied = [r for r in results if r.action == "copied"]
            merged = [r for r in results if r.action == "merged"]
            failed = [r for r in results if r.action == "failed"]
            stats.update(tables_copied=len(copied), tables_skipped=sum(r.action == "skipped" for r in results),
                         tables_failed=len(failed), rows_copied=sum(r.rows or 0 for r in copied))
            if merged:
                stats.update(tables_merged=len(merged), rows_merged=sum(r.rows or 0 for r in merged),
                             rows_deleted=sum(r.deleted or 0 for r in merged))
            self.emit("tables_finished", f"📊 Tables: {len(copied)} copied ({fmt_rows(stats['rows_copied'])} rows), "
                      + (f"{len(merged)} merged ({fmt_rows(stats['rows_merged'])} changed rows, "
                         f"{fmt_rows(stats['rows_deleted'])} deleted), " if merged else "")
                      + f"{stats['tables_skipped']} unchanged, {len(failed)} failed "
                      f"in {fmt_seconds(time.monotonic() - tables_started)}",
                      copied=len(copied), merged=len(merged), skipped=stats["tables_skipped"], failed=len(failed),
                      rows=stats["rows_copied"])
            table_failures = {r.task.source.qualified_name: r.error for r in failed}

            view_failures: dict[str, str] = {}
            views_created = views_skipped = 0
            if self.interrupted:
                self.emit("views_skipped", f"🪟 Views skipped: the run was interrupted ({self.interrupted})",
                          level="warning")
            elif self.options.skip_views:
                self.emit("views_skipped", "🪟 Views skipped (--skip-views)")
            elif failed and self.config.on_table_error == "abort":
                self.emit("views_skipped", "🪟 Views skipped: a table failed, so they'd be built on incomplete data",
                          level="warning")
            elif plan.views:
                self.emit("views_started", f"🪟 Creating {len(plan.views)} view(s)…")
                views_created, views_skipped, view_failures = self.create_views(
                    plan=plan, store=store, run_id=run_id, view_state=view_state, existing_tables=existing_tables,
                    existing_views=existing_views, source_conn=source_conn, target_conn=target_conn,
                    table_state=table_state)
                self.emit("views_finished", f"🪟 Views: {views_created} created, {views_skipped} unchanged, "
                          f"{len(view_failures)} failed",
                          created=views_created, skipped=views_skipped, failed=len(view_failures))
                for name, error in view_failures.items():
                    self.emit("view_failed", f"  ❌ {name}: {error}", level="error", view=name, error=error)
            stats.update(views_created=views_created, views_skipped=views_skipped, views_failed=len(view_failures))

            if self.config.target.checkpoint and (copied or merged or views_created) and not self.interrupted:
                error = self.target.checkpoint(conn=target_conn)
                if error:
                    self.emit("warning", f"⚠️  CHECKPOINT after load failed: {error}", level="warning")
        finally:
            with suppress(Exception):
                source_conn.rollback()
            with suppress(Exception):
                source_conn.close()

        if self.interrupted:
            status, code = "interrupted", EXIT_INTERRUPTED
        elif table_failures:
            status, code = "failed", EXIT_FAILED
        elif view_failures:
            status, code = "completed_with_view_errors", EXIT_VIEW_ERRORS
        else:
            status, code = "completed", EXIT_OK
        icon = {"completed": "🏁", "completed_with_view_errors": "⚠️ ", "failed": "💥", "interrupted": "🛑"}[status]
        self.emit("run_finished", f"{icon} Run {status.replace('_', ' ')}", status=status, run_id=run_id, **stats)
        return RunSummary(run_id=run_id, status=status, stats=stats, view_failures=view_failures,
                          table_failures=table_failures, exit_code=code)
