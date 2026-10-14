"""The replication config: a versioned YAML (or JSON) document validated by pydantic.

The same models produce the JSON Schema (`adbc-replicate schema`), so a UI can generate a
config that the CLI accepts byte for byte. Secrets are never meant to live in the file:
any string value may reference the environment as ``${VAR}`` or ``${VAR:-default}``, and
substitution happens after YAML parsing, so a password containing YAML syntax can't change
the document's structure.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CONFIG_VERSION = 1

_ENV_REF = re.compile(r"\$\$|\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class ConfigError(Exception):
    """The config file is missing, unreadable, or fails validation."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---- Connections ------------------------------------------------------------------------

class _SourceBase(_Model):
    options: dict[str, str] = Field(
        default_factory=dict,
        description="Extra ADBC database options (db_kwargs) passed to the driver as-is.",
    )
    consistency: Literal["none", "serializable", "snapshot"] = Field(
        default="none",
        description="none: each table is read as of its own query. serializable: every table is read "
                    "in one transaction on one connection (forces parallelism 1). snapshot: every "
                    "worker reads the same point in time (Oracle AS OF SCN, PostgreSQL exported "
                    "snapshot, Snowflake time travel).",
    )


class OracleSource(_SourceBase):
    type: Literal["oracle"]
    uri: str = Field(description="e.g. oracle://host:1521/SERVICE_NAME")
    username: str | None = None
    password: str | None = None
    unconstrained_number: Literal["decimal", "double", "string"] = Field(
        default="decimal",
        description="How NUMBER columns declared without a precision are read. decimal casts them to "
                    "NUMBER(38,10): up to 28 integer digits, rounded to 10 fractional digits; a longer "
                    "integer part fails loudly. double is the driver default (float64, lossy above 15 "
                    "significant digits). string keeps the exact text.",
    )


class PostgresSource(_SourceBase):
    type: Literal["postgresql"]
    uri: str = Field(description="e.g. postgresql://host:5432/dbname")
    username: str | None = None
    password: str | None = None
    unconstrained_numeric: Literal["decimal", "double", "string"] = Field(
        default="decimal",
        description="How numeric columns declared without a precision are converted. decimal: "
                    "decimal128(38,10), rounded to 10 fractional digits in the source (a longer integer part "
                    "fails loudly). double: float64. string: the exact text.",
    )


class SnowflakeSource(_SourceBase):
    type: Literal["snowflake"]
    uri: str | None = Field(default=None, description="Optional snowflake:// URI; most setups use options.")
    result_queue_size: int = Field(
        default=4,
        description="Record batches the driver queues ahead (adbc.rpc.result_queue_size). The driver "
                    "default of 200 can hold ~1 GB in flight on wide tables.",
    )
    prefetch_concurrency: int = Field(default=2, description="adbc.snowflake.rpc.prefetch_concurrency")


class DuckDBSource(_SourceBase):
    type: Literal["duckdb"]
    path: str = Field(default=":memory:", description="DuckDB database file.")


SourceConfig = Annotated[
    OracleSource | PostgresSource | SnowflakeSource | DuckDBSource,
    Field(discriminator="type"),
]


class _TargetBase(_Model):
    options: dict[str, str] = Field(default_factory=dict, description="Extra ADBC database options.")
    checkpoint: bool = Field(
        default=True,
        description="Run CHECKPOINT before and after a load so the WAL is folded into the database "
                    "file. Needs the admin role on GizmoSQL; a failure is only a warning.",
    )


class GizmoSQLTarget(_TargetBase):
    type: Literal["gizmosql"]
    uri: str = Field(description="e.g. gizmosql://host:31337 (TLS) or gizmosql://host:31337?transport=tcp")
    username: str | None = None
    password: str | None = None
    tls_skip_verify: bool = False
    catalog: str | None = Field(default=None, description="Target catalog (attached database) to load into. "
                                       "Default: the server's current catalog.")
    max_msg_size: int = Field(
        default=32 * 1024 * 1024,
        description="gRPC message size the driver allows itself (bytes). Must exceed ingest.batch_bytes "
                    "plus framing; kept modest because the driver's memory grows with it.",
    )


class DuckDBTarget(_TargetBase):
    type: Literal["duckdb"]
    path: str = Field(description="DuckDB database file to load into.")
    catalog: str | None = None


TargetConfig = Annotated[GizmoSQLTarget | DuckDBTarget, Field(discriminator="type")]


# ---- What to replicate ------------------------------------------------------------------

Patterns = list[str]
_PATTERN_HELP = ("Case-insensitive glob patterns (*, ?, [abc]); wrap a pattern in slashes for a "
                 "regular expression, e.g. /^TMP_.*$/.")


class ObjectRule(_Model):
    """One selection rule: which db_schemas, and which tables and views inside them."""

    catalog: str | None = Field(
        default=None,
        description="Source catalog (database) to read from. Default: the connection's current catalog. "
                    "Only sources that can query across catalogs (Snowflake, DuckDB) accept another one.",
    )
    db_schemas: Patterns = Field(description="Source db_schemas. " + _PATTERN_HELP)
    tables: Patterns = Field(default_factory=lambda: ["*"], description="Tables to copy. " + _PATTERN_HELP)
    exclude_tables: Patterns = Field(default_factory=list)
    views: Patterns = Field(default_factory=lambda: ["*"],
                            description="Views to recreate in the target. Empty list = no views.")
    exclude_views: Patterns = Field(default_factory=list)
    materialize_views: Patterns = Field(
        default_factory=list,
        description="Views to copy as tables (their data) instead of recreating them as views.",
    )
    target_db_schema: str | None = Field(
        default=None,
        description="Target db_schema name; {db_schema} is replaced by the source db_schema name. "
                    "Default: the source db_schema name (after naming.case).",
    )


class MaskSpec(_Model):
    strategy: Literal["null", "hash", "redact", "expression"]
    salt: str | None = Field(default=None, description="hash: secret prepended before hashing (SHA-256, hex).")
    value: str = Field(default="****", description="redact: the replacement text.")
    sql: str | None = Field(
        default=None,
        description="expression: SQL in the SOURCE dialect, evaluated in the source database so the raw "
                    "value never leaves it. {column} is replaced by the quoted column name.",
    )

    @model_validator(mode="after")
    def _check(self) -> MaskSpec:
        if self.strategy == "expression" and not self.sql:
            raise ValueError("mask strategy 'expression' needs 'sql'")
        return self


class ColumnRule(_Model):
    """Column filtering and masking, applied to the tables the rule matches (in order)."""

    tables: Patterns = Field(
        default_factory=lambda: ["*"],
        description="Tables the rule applies to, as DB_SCHEMA.TABLE patterns (a pattern without a dot "
                    "matches the table name in any db_schema). " + _PATTERN_HELP,
    )
    include: Patterns | None = Field(default=None, description="Allow-list: copy only these columns.")
    exclude: Patterns = Field(default_factory=list, description="Columns to leave out.")
    mask: dict[str, MaskSpec] = Field(
        default_factory=dict,
        description="Column pattern -> mask. Masks run in the source database, so the raw values never "
                    "cross the network. Shorthand: 'SSN: hash', 'SALARY: null'.",
    )
    fingerprint_ignore: Patterns = Field(
        default_factory=list,
        description="Columns copied but ignored by change detection (e.g. dbt run-metadata stamps).",
    )

    @field_validator("mask", mode="before")
    @classmethod
    def _mask_shorthand(cls, value: Any) -> Any:
        if isinstance(value, dict):
            # 'SSN: hash' -> {strategy: hash}; a bare YAML null ('SALARY: null') means the null mask.
            return {k: ({"strategy": v} if isinstance(v, str) else {"strategy": "null"} if v is None else v)
                    for k, v in value.items()}
        return value


class NamingConfig(_Model):
    case: Literal["lower", "upper", "preserve"] = Field(
        default="lower",
        description="Case of target db_schema, table, view and column names (and of identifiers inside "
                    "translated views).",
    )


class ViewsConfig(_Model):
    overrides_dir: str | None = Field(
        default=None,
        description="Directory of hand-written view bodies named <DB_SCHEMA>.<VIEW>.sql (case-insensitive), "
                    "written in the target dialect against target names. Used instead of translating.",
    )
    on_failure: Literal["report", "materialize"] = Field(
        default="report",
        description="What to do with a view that can't be created: report it, or copy its data as a table.",
    )


class RefreshConfig(_Model):
    mode: Literal["changed", "full"] = Field(
        default="changed",
        description="changed: skip tables whose content is unchanged since the last run (metadata "
                    "timestamp where the source has one, else a COUNT + content fingerprint computed in "
                    "the source). full: copy every table on every run.",
    )


class IngestConfig(_Model):
    batch_rows: int = Field(default=65_536, description="Coalesce small source batches up to this many rows.")
    batch_bytes: int = Field(
        default=12 * 1024 * 1024,
        description="...and this many Arrow buffer bytes. DuckDB's insert buffers every incoming batch "
                    "pre-sized for 2,048 rows, so many tiny batches cost the server several times the "
                    "data; 12 MiB keeps the client's driver memory modest too.",
    )


class StateConfig(_Model):
    db_schema: str = Field(default="_adbc_replicate",
                           description="Target db_schema holding sync state and the run log.")


class ReplicationConfig(_Model):
    version: Literal[1] = Field(description="Config format version.")
    name: str | None = Field(default=None, description="A label for this replication, recorded in the run log.")
    source: SourceConfig
    target: TargetConfig
    objects: list[ObjectRule] = Field(min_length=1)
    columns: list[ColumnRule] = Field(default_factory=list)
    naming: NamingConfig = Field(default_factory=NamingConfig)
    views: ViewsConfig = Field(default_factory=ViewsConfig)
    refresh: RefreshConfig = Field(default_factory=RefreshConfig)
    ingest: IngestConfig = Field(default_factory=IngestConfig)
    state: StateConfig = Field(default_factory=StateConfig)
    parallelism: int = Field(default=1, ge=1, le=64, description="Tables copied at once.")
    on_table_error: Literal["abort", "continue"] = Field(
        default="abort",
        description="abort: stop scheduling tables and skip views after a table fails (views would be built "
                    "on incomplete data). continue: copy the remaining tables, then fail the run.",
    )
    fail_on_unmatched_column_rules: bool = Field(
        default=True,
        description="Fail when an exclude/mask pattern matches no column in any selected table. A masking "
                    "rule that matches nothing is usually a typo that would leak the column.",
    )

    @model_validator(mode="after")
    def _check(self) -> ReplicationConfig:
        if self.source.consistency == "serializable" and self.parallelism > 1:
            raise ValueError("consistency 'serializable' reads every table in one transaction on one "
                             "connection, so parallelism must be 1 (use consistency 'snapshot' to copy "
                             "tables in parallel from one point in time)")
        if self.source.consistency == "snapshot" and self.source.type == "duckdb":
            raise ValueError("consistency 'snapshot' isn't supported for a DuckDB source")
        if self.ingest.batch_bytes * 2 > getattr(self.target, "max_msg_size", 1 << 62):
            raise ValueError("target.max_msg_size must be at least twice ingest.batch_bytes")
        return self


# ---- Loading ----------------------------------------------------------------------------

def _substitute(value: Any, missing: set[str]) -> Any:
    if isinstance(value, str):
        def repl(match: re.Match) -> str:
            if match.group(0) == "$$":
                return "$"
            name, default = match.group(1), match.group(2)
            if name in os.environ:
                return os.environ[name]
            if default is not None:
                return default
            missing.add(name)
            return ""
        return _ENV_REF.sub(repl, value)
    if isinstance(value, dict):
        return {k: _substitute(value=v, missing=missing) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute(value=v, missing=missing) for v in value]
    return value


def parse_config(data: dict, base_dir: Path | None = None) -> ReplicationConfig:
    """Validate an already-parsed config document (after ${VAR} substitution)."""
    missing: set[str] = set()
    data = _substitute(value=data, missing=missing)
    if missing:
        raise ConfigError(f"environment variable(s) referenced by the config are not set: {', '.join(sorted(missing))}")
    try:
        config = ReplicationConfig.model_validate(data)
    except Exception as e:
        raise ConfigError(str(e)) from e
    if base_dir is not None and config.views.overrides_dir and not os.path.isabs(config.views.overrides_dir):
        config.views.overrides_dir = str((base_dir / config.views.overrides_dir).resolve())
    return config


def load_config(path: str | Path) -> ReplicationConfig:
    path = Path(path)
    try:
        text = path.read_text()
    except OSError as e:
        raise ConfigError(f"can't read config {path}: {e}") from e
    try:
        data = json.loads(text) if path.suffix == ".json" else yaml.safe_load(text)
    except Exception as e:
        raise ConfigError(f"can't parse {path}: {e}") from e
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a mapping at the top level")
    return parse_config(data=data, base_dir=path.parent)


def json_schema() -> dict:
    schema = ReplicationConfig.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["title"] = "adbc-replicate config"
    return schema
