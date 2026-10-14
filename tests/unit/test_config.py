import json

import pytest

from adbc_replicate.config import ConfigError, json_schema, load_config, parse_config


def base(**overrides):
    data = {
        "version": 1,
        "source": {"type": "duckdb", "path": "/tmp/x.duckdb"},
        "target": {"type": "duckdb", "path": "/tmp/y.duckdb"},
        "objects": [{"db_schemas": ["main"]}],
    }
    data.update(overrides)
    return data


def test_minimal_config_gets_defaults():
    config = parse_config(data=base())
    assert config.naming.case == "lower"
    assert config.refresh.mode == "changed"
    assert config.parallelism == 1
    assert config.objects[0].tables == ["*"]
    assert config.state.db_schema == "_adbc_replicate"
    assert config.ingest.batch_bytes == 12 * 1024 * 1024


def test_env_substitution_after_yaml_parsing(monkeypatch, tmp_path):
    # A password full of YAML syntax must not change the document's structure.
    monkeypatch.setenv("PW", "a: b, [c] # d")
    path = tmp_path / "c.yaml"
    path.write_text("""
version: 1
source: {type: oracle, uri: "oracle://h:1521/S", username: u, password: "${PW}"}
target: {type: gizmosql, uri: "gizmosql://h:31337", password: "${MISSING_OK:-fallback}"}
objects: [{db_schemas: [HR]}]
""")
    config = load_config(path=path)
    assert config.source.password == "a: b, [c] # d"
    assert config.target.password == "fallback"


def test_config_file_is_read_as_utf8(tmp_path):
    # Windows defaults to cp1252; a comment or name with non-ASCII text must still load.
    path = tmp_path / "c.yaml"
    path.write_bytes("""
version: 1
name: "Réplication 🚀 Müller"   # ✅
source: {type: duckdb, path: /tmp/x.duckdb}
target: {type: duckdb, path: /tmp/y.duckdb}
objects: [{db_schemas: [main]}]
""".encode())
    assert load_config(path=path).name == "Réplication 🚀 Müller"


def test_missing_env_var_fails_closed(monkeypatch):
    monkeypatch.delenv("NOPE", raising=False)
    with pytest.raises(ConfigError, match="NOPE"):
        parse_config(data=base(source={"type": "postgresql", "uri": "postgresql://h/db", "password": "${NOPE}"}))


def test_dollar_escape():
    config = parse_config(data=base(name="cost $$5"))
    assert config.name == "cost $5"


def test_unknown_keys_rejected():
    with pytest.raises(ConfigError, match="extra"):
        parse_config(data=base(objects=[{"db_schemas": ["main"], "tabels": ["*"]}]))


def test_serializable_forbids_parallelism():
    with pytest.raises(ConfigError, match="parallelism must be 1"):
        parse_config(data=base(source={"type": "oracle", "uri": "oracle://h/s", "consistency": "serializable"},
                               parallelism=4))


def test_mask_shorthand():
    config = parse_config(data=base(columns=[{"tables": ["*"], "mask": {"SSN": "hash", "PHONE": {"strategy": "null"}}}]))
    assert config.columns[0].mask["SSN"].strategy == "hash"
    assert config.columns[0].mask["PHONE"].strategy == "null"


def test_expression_mask_needs_sql():
    with pytest.raises(ConfigError, match="needs 'sql'"):
        parse_config(data=base(columns=[{"mask": {"SSN": {"strategy": "expression"}}}]))


def test_batch_must_fit_message_size():
    with pytest.raises(ConfigError, match="max_msg_size"):
        parse_config(data=base(target={"type": "gizmosql", "uri": "gizmosql://h:1", "max_msg_size": 1000},
                               ingest={"batch_bytes": 1000}))


def test_json_schema_is_valid_json_with_discriminators():
    schema = json_schema()
    text = json.dumps(schema)
    assert schema["title"] == "adbc-replicate config"
    for name in ("oracle", "postgresql", "snowflake", "gizmosql", "db_schemas", "target_db_schema"):
        assert name in text
