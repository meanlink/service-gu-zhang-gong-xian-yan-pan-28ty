from sqlalchemy import inspect, text
from src.config import database as db_config
from src import models

def migrate():
    # 始终使用当前 engine（测试与多环境会重绑定）
    engine = db_config.engine
    # 新库或新表直接按当前模型建立
    db_config.Base.metadata.create_all(engine)
    inspector = inspect(engine)
    if 'deliveries' not in inspector.get_table_names():
        return
    existing = {c['name'] for c in inspector.get_columns('deliveries')}
    additions = {
        'status': "VARCHAR(20) DEFAULT 'stored'",
        'written_kinds': 'JSON',
        'note': "VARCHAR(500) DEFAULT ''",
        'attempts': 'INTEGER DEFAULT 0',
        'updated_at': 'FLOAT DEFAULT 0',
    }
    with engine.begin() as conn:
        for column, ddl in additions.items():
            if column not in existing:
                conn.execute(text(f'ALTER TABLE deliveries ADD COLUMN {column} {ddl}'))
        # 历史归档视为两份附件均已写入、收件箱可见
        if 'status' not in existing:
            conn.execute(text("UPDATE deliveries SET status='stored' WHERE status IS NULL OR status=''"))
        if 'written_kinds' not in existing:
            conn.execute(text("UPDATE deliveries SET written_kinds='[\"csv\", \"json\"]' WHERE written_kinds IS NULL"))
        # 回填历史归档的实际字节占用登记（按文件真实大小，两份都登记）
        rows = conn.execute(text('SELECT id, filename FROM deliveries')).fetchall()
        for delivery_id, filename in rows:
            done = conn.execute(text(
                'SELECT COUNT(*) FROM archive_usage WHERE delivery_id=:d'), {'d': delivery_id}).scalar()
            if done:
                continue
            _backfill_usage(conn, delivery_id, filename)
        # 确保存在默认容量配置
        has_setting = conn.execute(text('SELECT COUNT(*) FROM storage_settings')).scalar()
        if not has_setting:
            from src.config.settings import DEFAULT_ARCHIVE_LIMIT_BYTES
            import time
            conn.execute(text(
                'INSERT INTO storage_settings (id, capacity_limit_bytes, updated_by, updated_at) '
                'VALUES (1, :limit, :actor, :at)'),
                {'limit': DEFAULT_ARCHIVE_LIMIT_BYTES, 'actor': 'migration', 'at': time.time()})

def _backfill_usage(conn, delivery_id, filename):
    import os, time
    from src.config.settings import ARTIFACT_DIR
    for kind, suffix in (('csv', ''), ('json', '.json')):
        path = os.path.join(ARTIFACT_DIR, filename + suffix)
        if not os.path.exists(path):
            continue
        size = os.path.getsize(path)
        conn.execute(text(
            'INSERT INTO archive_usage (delivery_id, kind, filename, size_bytes, created_at) '
            'VALUES (:d, :kind, :f, :size, :at)'),
            {'d': delivery_id, 'kind': kind, 'f': filename, 'size': size, 'at': time.time()})

if __name__=='__main__':migrate()
