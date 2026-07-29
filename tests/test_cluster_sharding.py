"""
Unit tests for cluster mode SQL generation and G3 version patch.

Covers:
1. ``ClickhouseApi.build_sharding_key`` -- generates sipHash64(primary_key)
   expressions so that the same primary key always routes to the same shard.
2. ``ClickhouseApi.get_distributed_table_schema`` -- emits the Distributed
   table DDL with column definitions + sharding_key placeholder.
3. ``ClickhouseApi.get_max_record_version`` -- in cluster mode uses
   ``clusterAllReplicas`` to query the local Replicated table on every
   replica in parallel, bypassing the Distributed table for a true global
   max.
4. G3 _version patch -- insert() assigns 13-digit millisecond timestamps
   as _version, and the version counter key is consistent across batches.
"""

import pytest
import sys
from unittest.mock import MagicMock

# __init__.py 的导入链会拉进 binlog_replicator → pymysqlreplication →
# dlopen 一个 native .dylib（跨平台编译问题）。测试只用到 clickhouse_api
# 和 config，先把重依赖 stub 掉再 import。
for _mod in [
    'mysql_ch_replicator.pymysqlreplication',
    'mysql_ch_replicator.binlog_replicator',
]:
    sys.modules.setdefault(_mod, MagicMock())

from mysql_ch_replicator import clickhouse_api
from mysql_ch_replicator.config import ClickhouseSettings


def _make_api(cluster='test_cluster', database='mydb'):
    api = clickhouse_api.ClickhouseApi.__new__(clickhouse_api.ClickhouseApi)
    api.clickhouse_settings = ClickhouseSettings(
        host='localhost',
        port=8123,
        user='default',
        password='',
        cluster=cluster,
        erase_batch_size=1000,
    )
    api.database = database
    api.version_initial_value = 0
    api.tables_last_record_version = {}
    api.stats = clickhouse_api.GeneralStats()
    api.client = MagicMock()
    return api


@pytest.mark.parametrize("primary_keys, expected", [
    pytest.param(['id'], 'sipHash64(`id`)', id='single-column-pk'),
    pytest.param(['user_id'], 'sipHash64(`user_id`)', id='single-column-string-name'),
    pytest.param(['tenant_id', 'order_id'], 'sipHash64(`tenant_id`, `order_id`)', id='two-column-composite-pk'),
    pytest.param(['a', 'b', 'c'], 'sipHash64(`a`, `b`, `c`)', id='three-column-composite-pk'),
    pytest.param([], 'rand()', id='empty-pk-falls-back-to-rand'),
])
def test_build_sharding_key(primary_keys, expected):
    api = _make_api()
    assert api.build_sharding_key(primary_keys) == expected


def test_build_sharding_key_is_deterministic():
    api = _make_api()
    assert api.build_sharding_key(['id']) == api.build_sharding_key(['id'])


def test_distributed_table_ddl_with_fields_and_siphash64():
    api = _make_api(cluster='barn_cluster')
    fields = "    `id` UInt64,\n    `name` String,\n    `_version` UInt64"
    sql = api.get_distributed_table_schema(
        'users', 'mydb',
        sharding_key=api.build_sharding_key(['id']),
        fields=fields,
    ).strip()
    expected = (
        "CREATE TABLE  `mydb`.`users_distributed` ON CLUSTER barn_cluster\n"
        "(\n"
        "    `id` UInt64,\n"
        "    `name` String,\n"
        "    `_version` UInt64\n"
        ")\n"
        "ENGINE = Distributed('barn_cluster', 'mydb', 'users', sipHash64(`id`));"
    )
    assert sql == expected


def test_distributed_table_ddl_with_fields_and_composite_pk():
    api = _make_api(cluster='barn_cluster')
    fields = "    `tenant_id` UInt64,\n    `order_id` UInt64,\n    `_version` UInt64"
    sql = api.get_distributed_table_schema(
        'orders', 'mydb',
        sharding_key=api.build_sharding_key(['tenant_id', 'order_id']),
        fields=fields,
    ).strip()
    assert "ENGINE = Distributed('barn_cluster', 'mydb', 'orders'" in sql
    assert "sipHash64(`tenant_id`, `order_id`)" in sql
    assert "rand()" not in sql
    assert "`tenant_id` UInt64" in sql


def test_distributed_table_ddl_without_fields_falls_back_to_empty():
    api = _make_api(cluster='barn_cluster')
    sql = api.get_distributed_table_schema(
        'legacy', 'mydb', sharding_key='rand()',
    ).strip()
    assert "ENGINE = Distributed" in sql
    assert "rand()" in sql


def test_distributed_table_ddl_falls_back_to_rand_without_pk():
    api = _make_api(cluster='barn_cluster')
    sk = api.build_sharding_key([])
    assert sk == 'rand()'
    sql = api.get_distributed_table_schema('logs', 'mydb', sharding_key=sk).strip()
    assert "rand()" in sql


def test_distributed_table_ddl_default_sharding_key_is_rand():
    api = _make_api(cluster='barn_cluster')
    sql = api.get_distributed_table_schema('legacy', 'mydb').strip()
    expected = (
        "CREATE TABLE  `mydb`.`legacy_distributed` ON CLUSTER barn_cluster\n"
        "(\n"
        "\n"
        ")\n"
        "ENGINE = Distributed('barn_cluster', 'mydb', 'legacy', rand());"
    )
    assert sql == expected


def test_distributed_table_ddl_requires_version_column_in_fields():
    """Contract: create_table() appends `_version` UInt64 to fields so that
    INSERT (which appends _version to each row) doesn't fail with
    'Insert data column count does not match column names'."""
    api = _make_api(cluster='barn_cluster')
    fields_no_version = "    `id` UInt64,\n    `name` String"
    sql = api.get_distributed_table_schema(
        'users', 'mydb',
        sharding_key='sipHash64(`id`)',
        fields=fields_no_version,
    ).strip()
    assert "`_version`" not in sql
    fields_with_version = fields_no_version + ",\n    `_version` UInt64"
    sql_full = api.get_distributed_table_schema(
        'users', 'mydb',
        sharding_key='sipHash64(`id`)',
        fields=fields_with_version,
    ).strip()
    assert "`_version` UInt64" in sql_full


def test_get_max_record_version_cluster_uses_cluster_all_replicas():
    api = _make_api(cluster='barn_cluster', database='mydb')
    api.client.query.return_value = MagicMock(result_rows=[(12345,)])
    result = api.get_max_record_version('users')
    assert result == 12345
    sent_sql = api.client.query.call_args[0][0]
    expected = (
        "SELECT MAX(_version) AS global_max "
        "FROM clusterAllReplicas('barn_cluster', `mydb`.`users`)"
    )
    assert sent_sql == expected
    assert '_distributed' not in sent_sql


def test_get_max_record_version_non_cluster_uses_simple_select():
    api = _make_api(cluster='', database='mydb')
    api.client.query.return_value = MagicMock(result_rows=[(99,)])
    result = api.get_max_record_version('users')
    assert result == 99
    sent_sql = api.client.query.call_args[0][0]
    assert sent_sql == "SELECT MAX(_version) FROM `mydb`.`users`"
    assert 'clusterAllReplicas' not in sent_sql


@pytest.mark.parametrize("result_rows, expected", [
    pytest.param([(None,)], None, id='empty-table'),
    pytest.param([], None, id='no-rows'),
])
def test_get_max_record_version_returns_none_for_empty(result_rows, expected):
    api = _make_api(cluster='barn_cluster')
    api.client.query.return_value = MagicMock(result_rows=result_rows)
    assert api.get_max_record_version('users') is expected


def test_get_max_record_version_handles_client_exception():
    api = _make_api(cluster='barn_cluster')
    api.client.query.side_effect = RuntimeError('connection refused')
    assert api.get_max_record_version('users') is None


# ---------------------------------------------------------------------------
# G3: _version = 13-digit millisecond timestamp
# ---------------------------------------------------------------------------

def test_insert_version_is_millisecond_timestamp():
    """G3: _version should be a 13-digit millisecond timestamp (≥ 1e12),
    not a small incrementing integer starting from 1."""
    api = _make_api(cluster='barn_cluster')
    api.client.insert = MagicMock()
    api.insert('users', [['alice'], ['bob']])
    # records_to_insert is the data= kwarg of client.insert()
    rows = api.client.insert.call_args[1]['data']
    for row in rows:
        version = row[-1]  # _version is the last column
        assert version >= 1_000_000_000_000, (
            f"_version {version} is not a 13-digit millisecond timestamp"
        )


def test_insert_version_increases_within_batch():
    """Within a single batch, consecutive rows get strictly increasing
    _version values (current_version += 1 in the loop)."""
    api = _make_api(cluster='barn_cluster')
    api.client.insert = MagicMock()
    api.insert('users', [['a'], ['b'], ['c']])
    rows = api.client.insert.call_args[1]['data']
    versions = [row[-1] for row in rows]
    assert versions == sorted(versions)
    assert len(set(versions)) == len(versions), "versions must be unique"


def test_insert_version_monotonic_across_batches():
    """G3 key-mismatch fix: the second batch must NOT restart from 1.
    Version counter must persist across insert() calls with the same
    (unsuffixed) table name."""
    api = _make_api(cluster='barn_cluster')
    api.client.insert = MagicMock()

    api.insert('users', [['a']])
    rows1 = api.client.insert.call_args[1]['data']
    v1 = rows1[0][-1]

    api.insert('users', [['b']])
    rows2 = api.client.insert.call_args[1]['data']
    v2 = rows2[0][-1]

    assert v2 > v1, (
        f"second batch version {v2} must be greater than first batch {v1}"
    )


def test_insert_version_key_consistent_in_cluster_mode():
    """G3: in cluster mode, table_name gets '_distributed' appended before
    the actual CH insert, but set_last_used_version must store under the
    ORIGINAL (unsuffixed) key so get_last_used_version can read it back."""
    api = _make_api(cluster='barn_cluster')
    api.client.insert = MagicMock()

    api.insert('orders', [['x']])

    # The counter must be stored under 'orders', NOT 'orders_distributed'
    assert 'orders' in api.tables_last_record_version
    assert 'orders_distributed' not in api.tables_last_record_version
