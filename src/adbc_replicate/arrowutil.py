"""Streaming wrappers around a RecordBatchReader. Each one keeps the stream streamed: at most
one (coalesced) batch is held at a time, so client memory doesn't grow with table size."""

from __future__ import annotations

from collections.abc import Callable

import pyarrow as pa
import pyarrow.compute as pc


def _storage_type(data_type: pa.DataType) -> pa.DataType:
    """Strip Arrow extension types (arrow.json, arrow.opaque, ...) down to their storage type,
    recursively, and widen second/millisecond timestamps. Targets ingest the physical types;
    the extension metadata is source-specific."""
    if isinstance(data_type, pa.BaseExtensionType):
        return _storage_type(data_type=data_type.storage_type)
    if pa.types.is_list(data_type):
        return pa.list_(_storage_type(data_type=data_type.value_type))
    if pa.types.is_large_list(data_type):
        return pa.large_list(_storage_type(data_type=data_type.value_type))
    if pa.types.is_struct(data_type):
        return pa.struct([f.with_type(_storage_type(data_type=f.type)) for f in data_type])
    if pa.types.is_map(data_type):
        return pa.map_(_storage_type(data_type=data_type.key_type), _storage_type(data_type=data_type.item_type))
    if pa.types.is_timestamp(data_type) and data_type.unit in ("s", "ms"):
        # Widen to microseconds (lossless): DuckDB's native TIMESTAMP, rather than
        # TIMESTAMP_S / TIMESTAMP_MS, which some BI tools and drivers handle poorly.
        return pa.timestamp(unit="us", tz=data_type.tz)
    return data_type


def _strip_array(array: pa.Array, target: pa.DataType) -> pa.Array:
    if isinstance(array, pa.ExtensionArray):
        array = array.storage
    if array.type.equals(target):
        return array
    return array.cast(target_type=target)


def transform(reader: pa.RecordBatchReader, names: list[str],
              casts: dict[int, pa.DataType] | None = None) -> pa.RecordBatchReader:
    """Rename every column to `names` (zero-copy), strip extension types, and apply the
    per-column `casts` ({column index: type}; a strict Arrow cast, so precision loss raises).

    An empty source yields a single zero-row batch, because a Flight DoPut needs at least one
    message to carry the schema.
    """
    casts = casts or {}
    fields = []
    for i, f in enumerate(reader.schema):
        data_type = casts.get(i) or _storage_type(data_type=f.type)
        fields.append(pa.field(names[i], data_type, nullable=True))
    schema = pa.schema(fields)

    def convert(batch: pa.RecordBatch) -> pa.RecordBatch:
        arrays = []
        for i, array in enumerate(batch.columns):
            target = schema.field(i).type
            if isinstance(array, pa.ExtensionArray):
                array = array.storage
            if not array.type.equals(target):
                array = pc.cast(array, target_type=target) if i in casts else _strip_array(array=array, target=target)
            arrays.append(array)
        return pa.RecordBatch.from_arrays(arrays=arrays, schema=schema)

    def batches():
        yielded = False
        for batch in reader:
            yielded = True
            yield convert(batch=batch)
        if not yielded:
            yield pa.RecordBatch.from_arrays(arrays=[pa.array([], type=f.type) for f in schema], schema=schema)

    return pa.RecordBatchReader.from_batches(schema=schema, batches=batches())


def coalesce(reader: pa.RecordBatchReader, max_rows: int, max_bytes: int) -> pa.RecordBatchReader:
    """Combine consecutive small batches into batches of up to `max_rows` rows and `max_bytes`
    Arrow buffer bytes. A pending group is sent before a batch that would push it over either
    cap; a single source batch that is already bigger passes through unchanged."""
    schema = reader.schema

    def combine(pending: list[pa.RecordBatch]) -> pa.RecordBatch:
        if len(pending) == 1:
            return pending[0]
        return pa.Table.from_batches(batches=pending, schema=schema).combine_chunks().to_batches()[0]

    def batches():
        pending: list[pa.RecordBatch] = []
        rows = size = 0
        for batch in reader:
            if pending and (rows + batch.num_rows > max_rows or size + batch.nbytes > max_bytes):
                yield combine(pending=pending)
                pending, rows, size = [], 0, 0
            pending.append(batch)
            rows += batch.num_rows
            size += batch.nbytes
        if pending:
            yield combine(pending=pending)

    return pa.RecordBatchReader.from_batches(schema=schema, batches=batches())


def counting(reader: pa.RecordBatchReader, on_batch: Callable[[int], None]) -> pa.RecordBatchReader:
    """Call `on_batch(rows)` for every batch that passes through (for progress reporting)."""
    def batches():
        for batch in reader:
            on_batch(batch.num_rows)
            yield batch

    return pa.RecordBatchReader.from_batches(schema=reader.schema, batches=batches())
