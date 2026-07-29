"""
Unit tests for cluster mode SQL generation.

Covers:
1. ``ClickhouseApi.build_sharding_key`` -- generates cityHash64(primary_key)
   expressions so that the same primary key always routes to the same shard.
2. ``ClickhouseApi.get_distributed_table_schema`` -- emits the Distributed
   table DDL with column definitions + sharding_key placeholder.
3. ``ClickhouseApi.get_max_record_version`` -- in cluster mode uses
   ``clusterAllReplicas`` to query the local Replicated table on every
   replica in parallel, bypassing the Distributed table for a true global
   max.
"""

import pytest
from unittest.mock import MagicMock

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
    pytest.param(['id'], 'cityHash64(`id`)', id='single-column-pk'),
    pytest.param(['user_id'], 'cityHash64(`user_id`)', id='single-column-string-name'),
    pytest.param(['tenant_id', 'order_id'], 'cityHash64(`tenant_id`, `order_id`)', id='two-column-composite-pk'),
    pytest.param(['a', 'b', 'c'], 'cityHash64(`a`, `b`, `c`)', id='three-column-composite-pk'),
    pytest.param([], 'rand()', id='empty-pk-falls-back-to-rand'),
])
def test_build_sharding_key(primary_keys, expected):
    api = _make_api()
    assert api.build_sharding_key(primary_keys) == expected


def test_build_sharding_key_is_deterministic():
    api = _make_api()
    assert api.build_sharding_key(['id']) == api.build_sharding_key(['id'])


def test_distributed_table_ddl_with_fields_and_cityhash64():
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
        "ENGINE = Distributed('barn_cluster', 'mydb', 'users', cityHash64(`id`));"
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
    assert "cityHash64(`tenant_id`, `order_id`)" in sql
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
        sharding_key='cityHash64(`id`)',
        fields=fields_no_version,
    ).strip()
    assert "`_version`" not in sql
    fields_with_version = fields_no_version + ",\n    `_version` UInt64"
    sql_full = api.get_distributed_table_schema(
        'users', 'mydb',
        sharding_key='cityHash64(`id`)',
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
