"""Source plugins, one per database type."""

from __future__ import annotations

from .base import Snapshot, Source


def make_source(config) -> Source:
    if config.type == "oracle":
        from .oracle import OracleSource
        return OracleSource(config=config)
    if config.type == "postgresql":
        from .postgresql import PostgresSource
        return PostgresSource(config=config)
    if config.type == "snowflake":
        from .snowflake import SnowflakeSource
        return SnowflakeSource(config=config)
    if config.type == "duckdb":
        from .duckdb import DuckDBSource
        return DuckDBSource(config=config)
    raise ValueError(f"unknown source type {config.type!r}")


__all__ = ["Snapshot", "Source", "make_source"]
