"""Target plugins. Every target must support ADBC bulk ingest."""

from .duckdb import DuckDBFamilyTarget, make_target

__all__ = ["DuckDBFamilyTarget", "make_target"]
