"""Which DuckDB type names the plan refuses (see test_lossy_duckdb_types.py): a type, alone or nested,
never a STRUCT field name. No DuckDB needed, so this also runs where DuckDB has no wheels."""

import pytest

from adbc_replicate.sources.duckdb import lossy_duckdb_type


@pytest.mark.parametrize(("data_type", "lossy"), [
    ("TIME WITH TIME ZONE", True), ("TIME WITH TIME ZONE[]", True), ("BIT", True), ("BIT[3]", True),
    ("BIGNUM", True), ("VARINT", True), ("MAP(VARCHAR, BIT)", True), ("STRUCT(a INTEGER, x TIME WITH TIME ZONE)", True),
    ("TIME", False), ("TIMESTAMP WITH TIME ZONE", False), ("BLOB", False), ('STRUCT("bit" INTEGER)', False),
    ("STRUCT(bignum INTEGER, bitmask VARCHAR)", False), ("VARCHAR", False),
])
def test_lossy_type_names(data_type, lossy):
    assert (lossy_duckdb_type(data_type=data_type) is not None) == lossy
