"""旧库升级：为已有 deliveries 表补列、回填占用登记与默认容量配置。"""
import os
import time

from sqlalchemy import inspect, text, create_engine, event

from src.config import database as db_config
from src.config import settings as app_settings


def test_migration_upgrades_legacy_delivery_schema(tmp_path):
    # 独立引擎与产物目录，且先不迁移，手工搭出“旧版”库
    db_config.engine.dispose()
    db_path = tmp_path / 'legacy.db'
    art = tmp_path / 'artifacts'
    art.mkdir()
    app_settings.ARTIFACT_DIR = str(art)
    engine = create_engine('sqlite:///' + str(db_path), connect_args={'timeout': 30})

    @event.listens_for(engine, 'connect')
    def _pragmas(conn, _):
        cur = conn.cursor()
        cur.execute('PRAGMA journal_mode=WAL')
        cur.execute('PRAGMA busy_timeout=30000')
        cur.close()

    db_config.engine = engine
    db_config.Session.configure(bind=engine)

    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE deliveries ("
            "id INTEGER PRIMARY KEY, report_id INTEGER, created_at FLOAT, "
            "checksum VARCHAR(64), filename VARCHAR(160), destination VARCHAR(120))"))
        conn.execute(text(
            "INSERT INTO deliveries (id, report_id, created_at, checksum, filename, destination) "
            "VALUES (1, 1, :at, 'abc', 'report-1-delivery-1.csv', '旧收件箱')"),
            {'at': time.time()})
    # 旧版会有两份实体文件，回填按真实字节登记
    with open(os.path.join(str(art), 'report-1-delivery-1.csv'), 'wb') as f:
        f.write(b'x' * 123)
    with open(os.path.join(str(art), 'report-1-delivery-1.csv.json'), 'wb') as f:
        f.write(b'y' * 456)

    from migrations.initial import migrate
    migrate()  # 应幂等补列并回填

    cols = {c['name'] for c in inspect(engine).get_columns('deliveries')}
    assert {'status', 'written_kinds', 'note', 'attempts', 'updated_at'} <= cols
    with db_config.Session() as s:
        row = s.execute(text(
            "SELECT status, written_kinds FROM deliveries WHERE id=1")).fetchone()
        assert row[0] == 'stored'
        usage = s.execute(text(
            "SELECT kind, size_bytes FROM archive_usage WHERE delivery_id=1 "
            "ORDER BY kind")).fetchall()
        assert dict(usage) == {'csv': 123, 'json': 456}
        setting = s.execute(text(
            "SELECT capacity_limit_bytes FROM storage_settings WHERE id=1")).scalar()
        assert setting and setting > 0

    # 再次迁移必须幂等，不报错、不重复回填
    migrate()
    with db_config.Session() as s:
        n = s.execute(text(
            "SELECT COUNT(*) FROM archive_usage WHERE delivery_id=1")).scalar()
        assert n == 2
    engine.dispose()
