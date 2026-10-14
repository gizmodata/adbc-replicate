"""Oracle objects that can't be read like ordinary tables: external tables (files have no SCN,
so AS OF fails) and BFILE columns (pointers to files on the server, which the driver can't read).
Found replicating a real schema. Seeded in their own schema, so other tests' counts don't change."""

import time

import pytest
from conftest import (
    ORACLE_SYSTEM_PASSWORD,
    ORACLE_USERNAME,
    execute,
    make_config,
    oracle_connect,
    oracle_source,
    query,
    run,
)

from adbc_replicate.engine import EXIT_OK

pytestmark = pytest.mark.integration

SCHEMA = "EXTSRC"


@pytest.fixture(scope="module")
def ext_source(oracle):
    system = oracle_connect(username="system", password=ORACLE_SYSTEM_PASSWORD)
    execute(conn=system, statements=[
        f"BEGIN EXECUTE IMMEDIATE 'DROP USER {SCHEMA} CASCADE'; EXCEPTION WHEN OTHERS THEN NULL; END;",
        f"CREATE USER {SCHEMA} IDENTIFIED BY extsrc QUOTA UNLIMITED ON users",
        "CREATE OR REPLACE DIRECTORY adbc_replicate_ext AS '/tmp'",
        f"GRANT READ, WRITE ON DIRECTORY adbc_replicate_ext TO {SCHEMA}",
        f"GRANT READ ON DIRECTORY adbc_replicate_ext TO {ORACLE_USERNAME}",
        # The external table's data file, written from inside the database (no shell on the server).
        "DECLARE f UTL_FILE.FILE_TYPE; BEGIN "
        "f := UTL_FILE.FOPEN('ADBC_REPLICATE_EXT', 'adbc_replicate_regions.csv', 'w'); "
        "UTL_FILE.PUT_LINE(f, '1,North'); UTL_FILE.PUT_LINE(f, '2,South'); UTL_FILE.PUT_LINE(f, '3,West'); "
        "UTL_FILE.FCLOSE(f); END;",
        f"CREATE TABLE {SCHEMA}.regions_ext (id NUMBER(10), name VARCHAR2(20)) ORGANIZATION EXTERNAL ("
        "TYPE ORACLE_LOADER DEFAULT DIRECTORY adbc_replicate_ext ACCESS PARAMETERS ("
        "RECORDS DELIMITED BY NEWLINE NOLOGFILE NOBADFILE NODISCARDFILE FIELDS TERMINATED BY ',') "
        "LOCATION ('adbc_replicate_regions.csv'))",
        f"CREATE TABLE {SCHEMA}.documents (id NUMBER(10), title VARCHAR2(50), file_ref BFILE)",
        f"INSERT INTO {SCHEMA}.documents VALUES (1, 'Regions', BFILENAME('ADBC_REPLICATE_EXT', 'adbc_replicate_regions.csv'))",
        f"INSERT INTO {SCHEMA}.documents VALUES (2, 'Nothing', NULL)",
        "COMMIT",
        f"GRANT SELECT ON {SCHEMA}.regions_ext TO {ORACLE_USERNAME}",
        f"GRANT SELECT ON {SCHEMA}.documents TO {ORACLE_USERNAME}",
        f"GRANT FLASHBACK ON {SCHEMA}.documents TO {ORACLE_USERNAME}",
    ])
    system.close()
    # Oracle can't flashback-read a table in the first seconds after it's created (ORA-01466).
    conn = oracle_connect()
    deadline = time.monotonic() + 30
    while True:
        scn = query(conn=conn, sql="SELECT TO_CHAR(DBMS_FLASHBACK.GET_SYSTEM_CHANGE_NUMBER) FROM dual")[0][0]
        try:
            query(conn=conn, sql=f"SELECT COUNT(*) FROM {SCHEMA}.documents AS OF SCN {scn}")
            break
        except Exception:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.5)
    conn.close()
    yield SCHEMA


def ext_config(prefix, **source_extra):
    return make_config(prefix=prefix, source=oracle_source(consistency="snapshot", **source_extra),
                       objects=[{"db_schemas": [SCHEMA], "target_db_schema": f"{prefix}_ext"}])


def test_external_table_and_bfile_column_under_snapshot(ext_source, gizmosql, prefix):
    # Before the fix: the external table failed with "AS OF" errors (ORA-08188 / ORA-01466) and the
    # BFILE table with "data type BFILE is not supported".
    summary, out = run(config=ext_config(prefix=prefix))
    assert summary.exit_code == EXIT_OK, out
    assert summary.stats["tables_copied"] == 2, out
    assert sorted(query(conn=gizmosql, sql=f"SELECT id, name FROM {prefix}_ext.regions_ext")) == \
        [(1, "North"), (2, "South"), (3, "West")]
    assert [r[0] for r in query(conn=gizmosql, sql=f"DESCRIBE {prefix}_ext.documents")] == ["id", "title"]
    assert "skipping columns that can't be copied: EXTSRC.DOCUMENTS.FILE_REF (BFILE" in out, out
    assert "(excluded FILE_REF)" in out, out


def test_external_tables_can_be_excluded(ext_source, gizmosql, prefix):
    summary, out = run(config=ext_config(prefix=prefix, external_tables="exclude"))
    assert summary.exit_code == EXIT_OK, out
    assert summary.stats["tables_copied"] == 1, out
    assert query(conn=gizmosql, sql=f"SELECT table_name FROM duckdb_tables() WHERE schema_name = '{prefix}_ext'") == \
        [("documents",)]
