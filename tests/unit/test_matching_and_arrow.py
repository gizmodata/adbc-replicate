import pyarrow as pa
import pytest

from adbc_replicate.arrowutil import coalesce, transform
from adbc_replicate.matching import Pattern, PatternSet, TablePatternSet


def test_globs_are_case_insensitive_and_anchored():
    p = PatternSet(patterns=["emp*"])
    assert p.matches(name="EMPLOYEES")
    assert not p.matches(name="X_EMP")


def test_regex_patterns_search():
    p = PatternSet(patterns=["/_(BAK|TMP)$/"])
    assert p.matches(name="ORDERS_BAK")
    assert not p.matches(name="ORDERS")


def test_invalid_regex_is_reported():
    with pytest.raises(ValueError, match="invalid pattern"):
        Pattern.parse(text="/([/")


def test_table_patterns_with_and_without_schema():
    p = TablePatternSet(patterns=["HR.EMP*", "AUDIT_LOG"])
    assert p.matches_table(schema="hr", table="employees")
    assert not p.matches_table(schema="sales", table="employees")
    assert p.matches_table(schema="anything", table="audit_log")


def _reader(batches, schema):
    return pa.RecordBatchReader.from_batches(schema=schema, batches=iter(batches))


def test_coalesce_respects_both_caps():
    schema = pa.schema([pa.field("a", pa.int64())])
    batches = [pa.record_batch([pa.array(range(10))], schema=schema) for _ in range(10)]
    out = list(coalesce(reader=_reader(batches=batches, schema=schema), max_rows=35, max_bytes=1 << 30))
    assert [b.num_rows for b in out] == [30, 30, 30, 10]
    out = list(coalesce(reader=_reader(batches=batches, schema=schema), max_rows=1000, max_bytes=200))
    assert all(b.nbytes <= 200 for b in out) and sum(b.num_rows for b in out) == 100


@pytest.mark.skipif(not hasattr(pa, "json_"), reason="canonical arrow.json type needs pyarrow >= 19")
def test_transform_renames_strips_extensions_widens_and_casts():
    schema = pa.schema([pa.field("ID", pa.string()), pa.field("TS", pa.timestamp(unit="s")),
                        pa.field("J", pa.json_(pa.string()))])
    batch = pa.record_batch([pa.array(["1.50"]), pa.array([0], type=pa.timestamp(unit="s")),
                             pa.array(['{"a":1}'], type=pa.json_(pa.string()))], schema=schema)
    out = transform(reader=_reader(batches=[batch], schema=schema), names=["id", "ts", "j"],
                    casts={0: pa.decimal128(precision=10, scale=2)})
    table = out.read_all()
    assert table.schema.names == ["id", "ts", "j"]
    assert table.schema.field("id").type == pa.decimal128(precision=10, scale=2)
    assert table.schema.field("ts").type == pa.timestamp(unit="us")
    assert table.schema.field("j").type == pa.string()


def test_transform_empty_stream_yields_one_schema_batch():
    schema = pa.schema([pa.field("A", pa.int32())])
    batches = list(transform(reader=_reader(batches=[], schema=schema), names=["a"]))
    assert len(batches) == 1 and batches[0].num_rows == 0 and batches[0].schema.names == ["a"]


def test_strict_decimal_cast_fails_loudly():
    schema = pa.schema([pa.field("N", pa.string())])
    batch = pa.record_batch([pa.array(["1.123456789012"])], schema=schema)
    out = transform(reader=_reader(batches=[batch], schema=schema), names=["n"],
                    casts={0: pa.decimal128(precision=38, scale=10)})
    with pytest.raises(pa.ArrowInvalid, match="data loss"):
        out.read_all()
