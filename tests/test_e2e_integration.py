from common import *
import pytest
import decimal
from mysql_ch_replicator import config
from mysql_ch_replicator import mysql_api
from mysql_ch_replicator import clickhouse_api


@pytest.mark.parametrize('config_file', [
    CONFIG_FILE,
    CONFIG_FILE_MARIADB,
])
def test_e2e_regular(config_file):
    cfg = config.Settings()
    cfg.load(config_file)

    mysql = mysql_api.MySQLApi(
        database=None,
        mysql_settings=cfg.mysql,
    )

    ch = clickhouse_api.ClickhouseApi(
        database=TEST_DB_NAME,
        clickhouse_settings=cfg.clickhouse,
    )

    prepare_env(cfg, mysql, ch)

    mysql.execute(f'''
CREATE TABLE `{TEST_TABLE_NAME}` (
    id int NOT NULL AUTO_INCREMENT,
    name varchar(255) COMMENT 'Dân tộc, ví dụ: Kinh',
    age int COMMENT 'CMND Cũ',
    field1 text,
    field2 blob,
    PRIMARY KEY (id)
); 
    ''')

    mysql.execute(
        f"INSERT INTO `{TEST_TABLE_NAME}` (name, age, field1, field2) VALUES ('Ivan', 42, 'test1', 'test2');",
        commit=True,
    )
    mysql.execute(f"INSERT INTO `{TEST_TABLE_NAME}` (name, age) VALUES ('Peter', 33);", commit=True)

    binlog_replicator_runner = BinlogReplicatorRunner(cfg_file=config_file)
    binlog_replicator_runner.run()
    db_replicator_runner = DbReplicatorRunner(TEST_DB_NAME, cfg_file=config_file)
    db_replicator_runner.run()

    assert_wait(lambda: TEST_DB_NAME in ch.get_databases())

    ch.execute_command(f'USE `{TEST_DB_NAME}`')

    assert_wait(lambda: TEST_TABLE_NAME in ch.get_tables())
    assert_wait(lambda: len(ch.select(TEST_TABLE_NAME)) == 2)

    # Check for custom partition_by configuration when using CONFIG_FILE_MARIADB
    if config_file == CONFIG_FILE_MARIADB:
        create_query = ch.show_create_table(TEST_TABLE_NAME)
        assert 'PARTITION BY intDiv(id, 1000000)' in create_query, f"Custom partition_by not found in CREATE TABLE query: {create_query}"

    mysql.execute(f"INSERT INTO `{TEST_TABLE_NAME}` (name, age) VALUES ('Filipp', 50);", commit=True)
    assert_wait(lambda: len(ch.select(TEST_TABLE_NAME)) == 3)
    assert_wait(lambda: ch.select(TEST_TABLE_NAME, where="name='Filipp'")[0]['age'] == 50)


    mysql.execute(f"ALTER TABLE `{TEST_TABLE_NAME}` ADD `last_name` varchar(255); ")
    mysql.execute(f"ALTER TABLE `{TEST_TABLE_NAME}` ADD `price` decimal(10,2) DEFAULT NULL; ")

    mysql.execute(f"ALTER TABLE `{TEST_TABLE_NAME}` ADD UNIQUE INDEX prise_idx (price)")
    mysql.execute(f"ALTER TABLE `{TEST_TABLE_NAME}` DROP INDEX prise_idx, ADD UNIQUE INDEX age_idx (age)")

    mysql.execute(f"INSERT INTO `{TEST_TABLE_NAME}` (name, age, last_name, price) VALUES ('Mary', 24, 'Smith', 3.2);", commit=True)

    assert_wait(lambda: len(ch.select(TEST_TABLE_NAME)) == 4)
    assert_wait(lambda: ch.select(TEST_TABLE_NAME, where="name='Mary'")[0]['last_name'] == 'Smith')

    assert_wait(lambda: ch.select(TEST_TABLE_NAME, where="field1='test1'")[0]['name'] == 'Ivan')
    assert_wait(lambda: ch.select(TEST_TABLE_NAME, where="field2='test2'")[0]['name'] == 'Ivan')


    mysql.execute(
        f"ALTER TABLE `{TEST_DB_NAME}`.`{TEST_TABLE_NAME}` "
        f"ADD COLUMN country VARCHAR(25) DEFAULT '' NOT NULL AFTER name;"
    )

    mysql.execute(
        f"INSERT INTO `{TEST_TABLE_NAME}` (name, age, last_name, country) "
        f"VALUES ('John', 12, 'Doe', 'USA');", commit=True,
    )

    mysql.execute(
        f"ALTER TABLE `{TEST_DB_NAME}`.`{TEST_TABLE_NAME}` "
        f"CHANGE COLUMN country origin VARCHAR(24) DEFAULT '' NOT NULL",
    )

    assert_wait(lambda: len(ch.select(TEST_TABLE_NAME)) == 5)
    assert_wait(lambda: ch.select(TEST_TABLE_NAME, where="name='John'")[0].get('origin') == 'USA')

    mysql.execute(
        f"ALTER TABLE `{TEST_DB_NAME}`.`{TEST_TABLE_NAME}` "
        f"CHANGE COLUMN origin country VARCHAR(24) DEFAULT '' NOT NULL",
    )
    assert_wait(lambda: ch.select(TEST_TABLE_NAME, where="name='John'")[0].get('origin') is None)
    assert_wait(lambda: ch.select(TEST_TABLE_NAME, where="name='John'")[0].get('country') == 'USA')

    mysql.execute(f"ALTER TABLE `{TEST_DB_NAME}`.`{TEST_TABLE_NAME}` DROP COLUMN country")
    assert_wait(lambda: ch.select(TEST_TABLE_NAME, where="name='John'")[0].get('country') is None)

    assert_wait(lambda: ch.select(TEST_TABLE_NAME, where="name='Filipp'")[0].get('last_name') is None)

    mysql.execute(f"UPDATE `{TEST_TABLE_NAME}` SET last_name = '' WHERE last_name IS NULL;")
    mysql.execute(f"ALTER TABLE `{TEST_TABLE_NAME}` MODIFY `last_name` varchar(1024) NOT NULL")

    assert_wait(lambda: ch.select(TEST_TABLE_NAME, where="name='Filipp'")[0].get('last_name') == '')


    mysql.execute(f'''
    CREATE TABLE {TEST_TABLE_NAME_2} (
        id int NOT NULL AUTO_INCREMENT,
        name varchar(255),
        age int,
        PRIMARY KEY (id)
    ); 
        ''')

    assert_wait(lambda: TEST_TABLE_NAME_2 in ch.get_tables())

    mysql.execute(f"INSERT INTO `{TEST_TABLE_NAME_2}` (name, age) VALUES ('Ivan', 42);", commit=True)
    assert_wait(lambda: len(ch.select(TEST_TABLE_NAME_2)) == 1)


    mysql.execute(f'''
    CREATE TABLE `{TEST_TABLE_NAME_3}` (
        id int NOT NULL AUTO_INCREMENT,
        `name` varchar(255),
        age int,
        PRIMARY KEY (`id`)
    ); 
        ''')

    assert_wait(lambda: TEST_TABLE_NAME_3 in ch.get_tables())

    mysql.execute(f"INSERT INTO `{TEST_TABLE_NAME_3}` (name, `age`) VALUES ('Ivan', 42);", commit=True)
    assert_wait(lambda: len(ch.select(TEST_TABLE_NAME_3)) == 1)

    mysql.execute(f'DROP TABLE `{TEST_TABLE_NAME_3}`')
    assert_wait(lambda: TEST_TABLE_NAME_3 not in ch.get_tables())

    db_replicator_runner.stop()


def test_e2e_multistatement():
    cfg = config.Settings()
    cfg.load(CONFIG_FILE)

    mysql = mysql_api.MySQLApi(
        database=None,
        mysql_settings=cfg.mysql,
    )

    ch = clickhouse_api.ClickhouseApi(
        database=TEST_DB_NAME,
        clickhouse_settings=cfg.clickhouse,
    )

    prepare_env(cfg, mysql, ch)

    mysql.execute(f'''
CREATE TABLE `{TEST_TABLE_NAME}` (
    id int NOT NULL AUTO_INCREMENT,
    name varchar(255),
    age int,
    PRIMARY KEY (id, `name`)
); 
    ''')

    mysql.execute(f"INSERT INTO `{TEST_TABLE_NAME}` (name, age) VALUES ('Ivan', 42);", commit=True)

    binlog_replicator_runner = BinlogReplicatorRunner()
    binlog_replicator_runner.run()
    db_replicator_runner = DbReplicatorRunner(TEST_DB_NAME)
    db_replicator_runner.run()

    assert_wait(lambda: TEST_DB_NAME in ch.get_databases())

    ch.execute_command(f'USE `{TEST_DB_NAME}`')

    assert_wait(lambda: TEST_TABLE_NAME in ch.get_tables())
    assert_wait(lambda: len(ch.select(TEST_TABLE_NAME)) == 1)

    mysql.execute(f"ALTER TABLE `{TEST_TABLE_NAME}` ADD `last_name` varchar(255), ADD COLUMN city varchar(255); ")
    mysql.execute(
        f"INSERT INTO `{TEST_TABLE_NAME}` (name, age, last_name, city) "
        f"VALUES ('Mary', 24, 'Smith', 'London');", commit=True,
    )

    assert_wait(lambda: len(ch.select(TEST_TABLE_NAME)) == 2)
    assert_wait(lambda: ch.select(TEST_TABLE_NAME, where="name='Mary'")[0].get('last_name') == 'Smith')
    assert_wait(lambda: ch.select(TEST_TABLE_NAME, where="name='Mary'")[0].get('city') == 'London')

    mysql.execute(f"ALTER TABLE `{TEST_TABLE_NAME}` DROP COLUMN last_name, DROP COLUMN city")
    assert_wait(lambda: ch.select(TEST_TABLE_NAME, where="name='Mary'")[0].get('last_name') is None)
    assert_wait(lambda: ch.select(TEST_TABLE_NAME, where="name='Mary'")[0].get('city') is None)

    mysql.execute(f"DELETE FROM `{TEST_TABLE_NAME}` WHERE name='Ivan';", commit=True)
    assert_wait(lambda: len(ch.select(TEST_TABLE_NAME)) == 1)

    mysql.execute(f"ALTER TABLE `{TEST_TABLE_NAME}` ADD factor NUMERIC(5, 2) DEFAULT NULL;")
    mysql.execute(f"INSERT INTO `{TEST_TABLE_NAME}` (name, age, factor) VALUES ('Snow', 31, 13.29);", commit=True)

    assert_wait(lambda: len(ch.select(TEST_TABLE_NAME)) == 2)
    assert_wait(lambda: ch.select(TEST_TABLE_NAME, where="name='Snow'")[0].get('factor') == decimal.Decimal('13.29'))

    mysql.execute(
        f"CREATE TABLE {TEST_TABLE_NAME_2} "
        f"(id int NOT NULL AUTO_INCREMENT, name varchar(255), age int, "
        f"PRIMARY KEY (id));"
    )

    assert_wait(lambda: TEST_TABLE_NAME_2 in ch.get_tables())

    db_replicator_runner.stop()
    binlog_replicator_runner.stop()


@pytest.mark.parametrize('cfg_file', [CONFIG_FILE, 'tests/tests_config_parallel.yaml'])
def test_runner(cfg_file):
    cfg = config.Settings()
    cfg.load(cfg_file)

    mysql = mysql_api.MySQLApi(
        database=None,
        mysql_settings=cfg.mysql,
    )

    ch = clickhouse_api.ClickhouseApi(
        database=TEST_DB_NAME,
        clickhouse_settings=cfg.clickhouse,
    )

    mysql.drop_database(TEST_DB_NAME_2)
    ch.drop_database(TEST_DB_NAME_2)
    ch.drop_database(TEST_DB_NAME_2_DESTINATION)

    prepare_env(cfg, mysql, ch)

    mysql.execute(f'''
CREATE TABLE `{TEST_TABLE_NAME}` (
    id int NOT NULL AUTO_INCREMENT,
    name varchar(255),
    age int,
    rate decimal(10,4),
    coordinate point NOT NULL,
    KEY `IDX_age` (`age`),
    FULLTEXT KEY `IDX_name` (`name`),
    PRIMARY KEY (id),
    SPATIAL KEY `coordinate` (`coordinate`)
) ENGINE=InnoDB AUTO_INCREMENT=2478808 DEFAULT CHARSET=latin1; 
    ''', commit=True)


    mysql.execute(f'''
    CREATE TABLE `group` (
        id int NOT NULL AUTO_INCREMENT,
        name varchar(255) NOT NULL,
        age int,
        rate decimal(10,4),
        PRIMARY KEY (id)
    ); 
        ''', commit=True)


    mysql.execute(f"INSERT INTO `{TEST_TABLE_NAME}` (name, age, coordinate) VALUES ('Ivan', 42, POINT(10.0, 20.0));", commit=True)
    mysql.execute(f"INSERT INTO `{TEST_TABLE_NAME}` (name, age, coordinate) VALUES ('Peter', 33, POINT(10.0, 20.0));", commit=True)

    mysql.execute(f"INSERT INTO `group` (name, age, rate) VALUES ('Peter', 33, 10.2);", commit=True)

    run_all_runner = RunAllRunner(cfg_file=cfg_file)
    run_all_runner.run()

    assert_wait(lambda: TEST_DB_NAME in ch.get_databases())

    ch.execute_command(f'USE `{TEST_DB_NAME}`;')

    assert_wait(lambda: 'group' in ch.get_tables())

    mysql.drop_table('group')

    assert_wait(lambda: 'group' not in ch.get_databases())

    assert_wait(lambda: TEST_TABLE_NAME in ch.get_tables())
    assert_wait(lambda: len(ch.select(TEST_TABLE_NAME)) == 2)

    mysql.execute(f"INSERT INTO `{TEST_TABLE_NAME}` (name, age, coordinate) VALUES ('Xeishfru32', 50, POINT(10.0, 20.0));", commit=True)
    assert_wait(lambda: len(ch.select(TEST_TABLE_NAME)) == 3)
    assert_wait(lambda: ch.select(TEST_TABLE_NAME, where="name='Xeishfru32'")[0]['age'] == 50)

    # Test for restarting dead processes
    binlog_repl_pid = get_binlog_replicator_pid(cfg)
    db_repl_pid = get_db_replicator_pid(cfg, TEST_DB_NAME)

    kill_process(binlog_repl_pid)
    kill_process(db_repl_pid, force=True)

    mysql.execute(f"INSERT INTO `{TEST_TABLE_NAME}` (name, rate, coordinate) VALUES ('John', 12.5, POINT(10.0, 20.0));", commit=True)
    assert_wait(lambda: len(ch.select(TEST_TABLE_NAME)) == 4)
    assert_wait(lambda: ch.select(TEST_TABLE_NAME, where="name='John'")[0]['rate'] == 12.5)

    mysql.execute(f"DELETE FROM `{TEST_TABLE_NAME}` WHERE name='John';", commit=True)
    assert_wait(lambda: len(ch.select(TEST_TABLE_NAME)) == 3)

    mysql.execute(f"UPDATE `{TEST_TABLE_NAME}` SET age=66 WHERE name='Ivan'", commit=True)
    assert_wait(lambda: ch.select(TEST_TABLE_NAME, "name='Ivan'")[0]['age'] == 66)

    mysql.execute(f"UPDATE `{TEST_TABLE_NAME}` SET age=77 WHERE name='Ivan'", commit=True)
    assert_wait(lambda: ch.select(TEST_TABLE_NAME, "name='Ivan'")[0]['age'] == 77)

    mysql.execute(f"UPDATE `{TEST_TABLE_NAME}` SET age=88 WHERE name='Ivan'", commit=True)
    assert_wait(lambda: ch.select(TEST_TABLE_NAME, "name='Ivan'")[0]['age'] == 88)

    mysql.execute(f"INSERT INTO `{TEST_TABLE_NAME}` (name, age, coordinate) VALUES ('Vlad', 99, POINT(10.0, 20.0));", commit=True)

    assert_wait(lambda: len(ch.select(TEST_TABLE_NAME)) == 4)

    assert_wait(lambda: len(ch.select(TEST_TABLE_NAME, final=False)) == 4)

    mysql.execute(
        command=f"INSERT INTO `{TEST_TABLE_NAME}` (name, age, coordinate) VALUES (%s, %s, POINT(10.0, 20.0));",
        args=(b'H\xe4llo'.decode('latin-1'), 1912),
        commit=True,
    )

    assert_wait(lambda: TEST_DB_NAME in ch.get_databases())

    assert_wait(lambda: len(ch.select(TEST_TABLE_NAME)) == 5)
    assert_wait(lambda: ch.select(TEST_TABLE_NAME, "age=1912")[0]['name'] == 'Hällo')

    ch.drop_database(TEST_DB_NAME)
    ch.drop_database(TEST_DB_NAME_2)

    requests.get('http://localhost:9128/restart_replication')
    time.sleep(1.0)

    assert_wait(lambda: TEST_DB_NAME in ch.get_databases())

    assert_wait(lambda: len(ch.select(TEST_TABLE_NAME)) == 5)
    assert_wait(lambda: ch.select(TEST_TABLE_NAME, "age=1912")[0]['name'] == 'Hällo')

    mysql.create_database(TEST_DB_NAME_2)
    assert_wait(lambda: TEST_DB_NAME_2_DESTINATION in ch.get_databases())

    mysql.execute(f'''
    CREATE TABLE `group` (
        id int NOT NULL AUTO_INCREMENT,
        name varchar(255) NOT NULL,
        age int,
        rate decimal(10,4),
        PRIMARY KEY (id)
    ); 
        ''')

    assert_wait(lambda: 'group' in ch.get_tables())

    create_query = ch.show_create_table('group')
    assert 'INDEX name_idx name TYPE ngrambf_v1' in create_query

    run_all_runner.stop()


def test_create_table_like():
    """
    Test that CREATE TABLE ... LIKE statements are handled correctly.
    The test creates a source table, then creates another table using LIKE,
    and verifies that both tables have the same structure in ClickHouse.
    """
    config_file = CONFIG_FILE
    cfg = config.Settings()
    cfg.load(config_file)

    mysql = mysql_api.MySQLApi(
        database=None,
        mysql_settings=cfg.mysql,
    )

    ch = clickhouse_api.ClickhouseApi(
        database=TEST_DB_NAME,
        clickhouse_settings=cfg.clickhouse,
    )

    prepare_env(cfg, mysql, ch)
    mysql.set_database(TEST_DB_NAME)

    # Create the source table with a complex structure
    mysql.execute(f'''
    CREATE TABLE `source_table` (
        id INT NOT NULL AUTO_INCREMENT,
        name VARCHAR(255) NOT NULL,
        age INT UNSIGNED,
        email VARCHAR(100) UNIQUE,
        status ENUM('active','inactive','pending') DEFAULT 'active',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        data JSON,
        PRIMARY KEY (id)
    );
    ''')
    
    # Get the CREATE statement for the source table
    source_create = mysql.get_table_create_statement('source_table')
    
    # Create a table using LIKE statement
    mysql.execute(f'''
    CREATE TABLE `derived_table` LIKE `source_table`;
    ''')

    # Set up replication
    binlog_replicator_runner = BinlogReplicatorRunner(cfg_file=config_file)
    binlog_replicator_runner.run()
    db_replicator_runner = DbReplicatorRunner(TEST_DB_NAME, cfg_file=config_file)
    db_replicator_runner.run()

    # Wait for database to be created and renamed from tmp to final
    assert_wait(lambda: TEST_DB_NAME in ch.get_databases(), max_wait_time=10.0)
    
    # Use the correct database explicitly
    ch.execute_command(f'USE `{TEST_DB_NAME}`')

    # Wait for tables to be created in ClickHouse with a longer timeout
    assert_wait(lambda: 'source_table' in ch.get_tables(), max_wait_time=10.0)
    assert_wait(lambda: 'derived_table' in ch.get_tables(), max_wait_time=10.0)

    # Insert data into both tables to verify they work
    mysql.execute("INSERT INTO `source_table` (name, age, email, status) VALUES ('Alice', 30, 'alice@example.com', 'active');", commit=True)
    mysql.execute("INSERT INTO `derived_table` (name, age, email, status) VALUES ('Bob', 25, 'bob@example.com', 'pending');", commit=True)

    # Wait for data to be replicated
    assert_wait(lambda: len(ch.select('source_table')) == 1, max_wait_time=10.0)
    assert_wait(lambda: len(ch.select('derived_table')) == 1, max_wait_time=10.0)

    # Compare structures by reading descriptions in ClickHouse
    source_desc = ch.execute_command("DESCRIBE TABLE source_table")
    derived_desc = ch.execute_command("DESCRIBE TABLE derived_table")

    # The structures should be identical
    assert source_desc == derived_desc
    
    # Verify the data in both tables
    source_data = ch.select('source_table')[0]
    derived_data = ch.select('derived_table')[0]
    
    assert source_data['name'] == 'Alice'
    assert derived_data['name'] == 'Bob'
    
    # Both tables should have same column types
    assert type(source_data['id']) == type(derived_data['id'])
    assert type(source_data['name']) == type(derived_data['name'])
    assert type(source_data['age']) == type(derived_data['age'])
    
    # Now test realtime replication by creating a new table after the initial replication
    mysql.execute(f'''
    CREATE TABLE `realtime_table` (
        id INT NOT NULL AUTO_INCREMENT,
        title VARCHAR(100) NOT NULL,
        description TEXT,
        price DECIMAL(10,2),
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (id)
    );
    ''')
    
    # Wait for the new table to be created in ClickHouse
    assert_wait(lambda: 'realtime_table' in ch.get_tables(), max_wait_time=10.0)
    
    # Insert data into the new table
    mysql.execute("""
    INSERT INTO `realtime_table` (title, description, price) VALUES 
    ('Product 1', 'First product description', 19.99),
    ('Product 2', 'Second product description', 29.99),
    ('Product 3', 'Third product description', 39.99);
    """, commit=True)
    
    # Wait for data to be replicated
    assert_wait(lambda: len(ch.select('realtime_table')) == 3, max_wait_time=10.0)
    
    # Verify the data in the realtime table
    realtime_data = ch.select('realtime_table')
    assert len(realtime_data) == 3
    
    # Verify specific values
    products = sorted([record['title'] for record in realtime_data])
    assert products == ['Product 1', 'Product 2', 'Product 3']
    
    prices = sorted([float(record['price']) for record in realtime_data])
    assert prices == [19.99, 29.99, 39.99]
    
    # Now create another table using LIKE after initial replication
    mysql.execute(f'''
    CREATE TABLE `realtime_like_table` LIKE `realtime_table`;
    ''')
    
    # Wait for the new LIKE table to be created in ClickHouse
    assert_wait(lambda: 'realtime_like_table' in ch.get_tables(), max_wait_time=10.0)
    
    # Insert data into the new LIKE table
    mysql.execute("""
    INSERT INTO `realtime_like_table` (title, description, price) VALUES 
    ('Service A', 'Premium service', 99.99),
    ('Service B', 'Standard service', 49.99);
    """, commit=True)
    
    # Wait for data to be replicated
    assert_wait(lambda: len(ch.select('realtime_like_table')) == 2, max_wait_time=10.0)
    
    # Verify the data in the realtime LIKE table
    like_data = ch.select('realtime_like_table')
    assert len(like_data) == 2
    
    services = sorted([record['title'] for record in like_data])
    assert services == ['Service A', 'Service B']
    
    # Clean up
    db_replicator_runner.stop()
    binlog_replicator_runner.stop()

def test_resume_realtime_after_deleting_state_pckl():
    cfg = config.Settings()
    cfg.load(CONFIG_FILE)

    mysql = mysql_api.MySQLApi(
        database=None,
        mysql_settings=cfg.mysql,
    )

    ch = clickhouse_api.ClickhouseApi(
        database=TEST_DB_NAME,
        clickhouse_settings=cfg.clickhouse,
    )

    prepare_env(cfg, mysql, ch)

    mysql.execute(f'''
CREATE TABLE `{TEST_TABLE_NAME}` (
    id int NOT NULL AUTO_INCREMENT,
    name varchar(255),
    age int,
    PRIMARY KEY (id)
);
    ''')

    mysql.execute(
        f"INSERT INTO `{TEST_TABLE_NAME}` (name, age) VALUES ('before-reset', 10);",
        commit=True,
    )

    binlog_replicator_runner = BinlogReplicatorRunner(cfg_file=CONFIG_FILE)
    binlog_replicator_runner.run()
    db_replicator_runner = DbReplicatorRunner(TEST_DB_NAME, cfg_file=CONFIG_FILE)
    db_replicator_runner.run()

    assert_wait(lambda: TEST_DB_NAME in ch.get_databases())
    ch.execute_command(f'USE `{TEST_DB_NAME}`')
    assert_wait(lambda: TEST_TABLE_NAME in ch.get_tables())
    assert_wait(lambda: len(ch.select(TEST_TABLE_NAME)) == 1)

    db_replicator_runner.stop()

    state_file = os.path.join(cfg.binlog_replicator.data_dir, TEST_DB_NAME, 'state.pckl')
    assert os.path.exists(state_file), f'{state_file} should exist before deletion'
    os.remove(state_file)
    assert not os.path.exists(state_file), 'state.pckl should be deleted'

    db_replicator_runner = DbReplicatorRunner(
        TEST_DB_NAME,
        additional_arguments='--skip_initial_replication',
        cfg_file=CONFIG_FILE,
    )
    db_replicator_runner.run()

    # Give db_replicator enough time to finish skip-initial bootstrap and enter realtime mode.
    time.sleep(3.0)

    mysql.execute(
        f"INSERT INTO `{TEST_TABLE_NAME}` (name, age) VALUES ('after-reset', 20), ('after-reset-2', 30);",
        commit=True,
    )

    assert_wait(lambda: len(ch.select(TEST_TABLE_NAME)) >= 2)
    assert_wait(lambda: ch.select(TEST_TABLE_NAME, where="name='after-reset-2'")[0]['age'] == 30)

    db_replicator_runner.stop()
    binlog_replicator_runner.stop()


def test_auto_backfill_when_local_binlog_files_missing():
    cfg = config.Settings()
    cfg.load(CONFIG_FILE)

    mysql = mysql_api.MySQLApi(
        database=None,
        mysql_settings=cfg.mysql,
    )

    ch = clickhouse_api.ClickhouseApi(
        database=TEST_DB_NAME,
        clickhouse_settings=cfg.clickhouse,
    )

    prepare_env(cfg, mysql, ch)

    mysql.execute(f'''
CREATE TABLE `{TEST_TABLE_NAME}` (
    id int NOT NULL AUTO_INCREMENT,
    name varchar(255),
    age int,
    PRIMARY KEY (id)
);
    ''')
    mysql.execute(f"INSERT INTO `{TEST_TABLE_NAME}` (name, age) VALUES ('seed', 1);", commit=True)

    binlog_replicator_runner = BinlogReplicatorRunner(cfg_file=CONFIG_FILE)
    binlog_replicator_runner.run()
    db_replicator_runner = DbReplicatorRunner(TEST_DB_NAME, cfg_file=CONFIG_FILE)
    db_replicator_runner.run()

    assert_wait(lambda: TEST_DB_NAME in ch.get_databases())
    ch.execute_command(f'USE `{TEST_DB_NAME}`')
    assert_wait(lambda: len(ch.select(TEST_TABLE_NAME)) == 1)

    mysql.execute(
        f"INSERT INTO `{TEST_TABLE_NAME}` (name, age) VALUES ('before-gap', 99);",
        commit=True,
    )
    assert_wait(lambda: len(ch.select(TEST_TABLE_NAME, where="name='before-gap'")) == 1)

    db_replicator_runner.stop()

    db_binlog_dir = os.path.join(cfg.binlog_replicator.data_dir, TEST_DB_NAME)
    state_file = os.path.join(db_binlog_dir, 'state.pckl')
    assert os.path.exists(state_file)
    saved_state = DbReplicatorState(state_file)
    assert saved_state.last_processed_transaction is not None

    for file_name in os.listdir(db_binlog_dir):
        if file_name.endswith('.bin'):
            os.remove(os.path.join(db_binlog_dir, file_name))

    mysql.execute(f"INSERT INTO `{TEST_TABLE_NAME}` (name, age) VALUES ('after-backfill', 2);", commit=True)

    db_replicator_runner = DbReplicatorRunner(TEST_DB_NAME, cfg_file=CONFIG_FILE)
    db_replicator_runner.run()

    assert_wait(
        lambda: len(ch.select(TEST_TABLE_NAME, where="name='after-backfill'")) == 1,
        max_wait_time=60.0,
    )
    assert_wait(lambda: ch.select(TEST_TABLE_NAME, where="name='after-backfill'")[0]['age'] == 2)

    db_replicator_runner.stop()
    binlog_replicator_runner.stop()


def test_recover_after_transaction_not_found_by_deleting_state():
    cfg = config.Settings()
    cfg.load(CONFIG_FILE)

    mysql = mysql_api.MySQLApi(
        database=None,
        mysql_settings=cfg.mysql,
    )

    ch = clickhouse_api.ClickhouseApi(
        database=TEST_DB_NAME,
        clickhouse_settings=cfg.clickhouse,
    )

    prepare_env(cfg, mysql, ch)

    mysql.execute(f'''
CREATE TABLE `{TEST_TABLE_NAME}` (
    id int NOT NULL AUTO_INCREMENT,
    name varchar(255),
    age int,
    PRIMARY KEY (id)
);
    ''')
    mysql.execute(f"INSERT INTO `{TEST_TABLE_NAME}` (name, age) VALUES ('seed', 1);", commit=True)

    binlog_replicator_runner = BinlogReplicatorRunner(cfg_file=CONFIG_FILE)
    binlog_replicator_runner.run()
    db_replicator_runner = DbReplicatorRunner(TEST_DB_NAME, cfg_file=CONFIG_FILE)
    db_replicator_runner.run()

    assert_wait(lambda: TEST_DB_NAME in ch.get_databases())
    ch.execute_command(f'USE `{TEST_DB_NAME}`')
    assert_wait(lambda: len(ch.select(TEST_TABLE_NAME)) == 1)

    db_replicator_runner.stop()

    state_file = os.path.join(cfg.binlog_replicator.data_dir, TEST_DB_NAME, 'state.pckl')
    broken_state = DbReplicatorState(state_file)
    broken_state.last_processed_transaction = ('mysql-bin.999999', 99999999)
    broken_state.save()

    db_replicator_runner = DbReplicatorRunner(TEST_DB_NAME, cfg_file=CONFIG_FILE)
    db_replicator_runner.run()
    assert_wait(lambda: db_replicator_runner.process.poll() is not None, max_wait_time=10.0)
    assert db_replicator_runner.process.returncode != 0

    os.remove(state_file)
    db_replicator_runner = DbReplicatorRunner(
        TEST_DB_NAME,
        additional_arguments='--skip_initial_replication',
        cfg_file=CONFIG_FILE,
    )
    db_replicator_runner.run()

    time.sleep(2.0)
    mysql.execute(f"INSERT INTO `{TEST_TABLE_NAME}` (name, age) VALUES ('recovered', 2);", commit=True)

    assert_wait(lambda: len(ch.select(TEST_TABLE_NAME, where="name='recovered'")) == 1, max_wait_time=30.0)
    assert_wait(lambda: ch.select(TEST_TABLE_NAME, where="name='recovered'")[0]['age'] == 2, max_wait_time=30.0)

    db_replicator_runner.stop()
    binlog_replicator_runner.stop()


def test_new_table_synced_after_realtime_replication_started():
    new_table_name = 'new_table_after_sync'

    cfg = config.Settings()
    cfg.load(CONFIG_FILE)

    mysql = mysql_api.MySQLApi(
        database=None,
        mysql_settings=cfg.mysql,
    )

    ch = clickhouse_api.ClickhouseApi(
        database=TEST_DB_NAME,
        clickhouse_settings=cfg.clickhouse,
    )

    prepare_env(cfg, mysql, ch)

    mysql.execute(f'''
CREATE TABLE `{TEST_TABLE_NAME}` (
    id int NOT NULL AUTO_INCREMENT,
    name varchar(255),
    age int,
    PRIMARY KEY (id)
);
    ''')
    mysql.execute(
        f"INSERT INTO `{TEST_TABLE_NAME}` (name, age) VALUES ('initial', 1);",
        commit=True,
    )

    binlog_replicator_runner = BinlogReplicatorRunner(cfg_file=CONFIG_FILE)
    binlog_replicator_runner.run()
    db_replicator_runner = DbReplicatorRunner(TEST_DB_NAME, cfg_file=CONFIG_FILE)
    db_replicator_runner.run()

    assert_wait(lambda: TEST_DB_NAME in ch.get_databases())
    ch.execute_command(f'USE `{TEST_DB_NAME}`')
    assert_wait(lambda: TEST_TABLE_NAME in ch.get_tables())
    assert_wait(lambda: len(ch.select(TEST_TABLE_NAME)) == 1)

    # Confirm realtime replication is active before creating a new table.
    state_file = os.path.join(cfg.binlog_replicator.data_dir, TEST_DB_NAME, 'state.pckl')
    assert_wait(lambda: DbReplicatorState(state_file).status.value == 3, max_wait_time=30.0)

    mysql.execute(
        f"INSERT INTO `{TEST_TABLE_NAME}` (name, age) VALUES ('realtime-row', 99);",
        commit=True,
    )
    assert_wait(lambda: len(ch.select(TEST_TABLE_NAME, where="name='realtime-row'")) == 1, max_wait_time=30.0)
    assert_wait(lambda: ch.select(TEST_TABLE_NAME, where="name='realtime-row'")[0]['age'] == 99, max_wait_time=30.0)

    assert new_table_name not in ch.get_tables()

    mysql.execute(f'''
CREATE TABLE `{new_table_name}` (
    id int NOT NULL AUTO_INCREMENT,
    title varchar(255) NOT NULL,
    amount decimal(10,2),
    PRIMARY KEY (id)
);
    ''')

    assert_wait(lambda: new_table_name in ch.get_tables(), max_wait_time=30.0)

    mysql.execute(
        f"INSERT INTO `{new_table_name}` (title, amount) VALUES ('new-table-row', 12.34);",
        commit=True,
    )

    assert_wait(lambda: len(ch.select(new_table_name)) == 1, max_wait_time=30.0)
    assert_wait(
        lambda: len(ch.select(new_table_name, where="title='new-table-row'")) == 1,
        max_wait_time=30.0,
    )
    assert_wait(
        lambda: float(ch.select(new_table_name, where="title='new-table-row'")[0]['amount']) == 12.34,
        max_wait_time=30.0,
    )

    db_replicator_runner.stop()
    binlog_replicator_runner.stop()

