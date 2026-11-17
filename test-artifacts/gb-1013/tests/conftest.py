import os
import tempfile
import time

import pytest

# 基础配置必须在导入应用模块前就绪；具体库与产物目录每个用例单独重建
_ROOT = tempfile.mkdtemp(prefix='archive-tests-')
os.environ.setdefault('DATABASE_URL', 'sqlite:///:memory:')  # 运行时按用例重写
os.environ['JWT_SECRET'] = 'test-secret'
os.environ['ARCHIVE_DEFAULT_LIMIT_BYTES'] = '100000'
os.environ['REDIS_URL'] = 'redis://127.0.0.1:6390/0'  # 限流中间件连接失败会被静默忽略
os.environ['SEED_DEMO'] = 'false'  # 验收测试使用自建数据，不跑演示 seed

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..', 'backend'))

from migrations.initial import migrate
from sqlalchemy import create_engine
import src.config.database as db_config
from src.config import settings as app_settings
from src.models import User, Dataset, Job, Report
from src.utils.security import hash_password, token_for


def make_table(csv_rows=6, json_pad=0):
    """生成可控体积的结果表，附件字节数随行数变化。"""
    cols = [{'key': 'pair', 'label': '服务对'}, {'key': 'rate', 'label': '跟随率'}]
    rows = [{'pair': 'cache->api-%02d' % i, 'rate': 0.83} for i in range(csv_rows)]
    table = {'columns': cols, 'rows': rows}
    result = {'table': table}
    if json_pad:
        result['padding'] = 'x' * json_pad
    return result


@pytest.fixture
def db(tmp_path):
    # 每用例独立 SQLite 文件与独立归档目录，彻底隔离状态
    db_config.engine.dispose()
    db_path = tmp_path / 'test.db'
    artifact_dir = tmp_path / 'artifacts'
    artifact_dir.mkdir()
    app_settings.ARTIFACT_DIR = str(artifact_dir)
    from sqlalchemy import event
    new_engine = create_engine('sqlite:///' + str(db_path),
                               connect_args={'timeout': 30})
    @event.listens_for(new_engine, 'connect')
    def _pragmas(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute('PRAGMA journal_mode=WAL')
        cur.execute('PRAGMA busy_timeout=30000')
        cur.close()
    db_config.engine = new_engine
    db_config.Session.configure(bind=new_engine)
    migrate()
    with db_config.Session() as session:
        yield session
        session.rollback()
    new_engine.dispose()


@pytest.fixture
def job_factory(db):
    from sqlalchemy import select
    def _make(name='复核任务', csv_rows=6, json_pad=0, approved=True):
        admin = db.scalar(select(User).where(User.email == 'admin@t.local'))
        if admin is None:
            admin = User(email='admin@t.local', name='负责人', role='admin',
                         password=hash_password('pw'))
            db.add(admin); db.flush()
        ds = Dataset(name='批次-' + name + '-' + str(time.time_ns()),
                     source='t.csv', records=[{'event_id': 'e1'}],
                     checksum='c', created_at=time.time())
        db.add(ds); db.flush()
        j = Job(name=name, dataset_id=ds.id, params={'window_min': 5, 'min_pairs': 3},
                status='completed', result=make_table(csv_rows, json_pad),
                approved=approved, created_by=admin.id,
                created_at=time.time(), finished_at=time.time())
        db.add(j); db.flush()
        r = Report(name='简报-' + name, job_id=j.id, destination='本地收件箱',
                   interval_seconds=3600, next_run=time.time() + 3600, enabled=True)
        db.add(r); db.flush()
        db.commit()
        return j, r, admin
    return _make


@pytest.fixture
def auth_headers(db):
    def _headers(role='admin'):
        from sqlalchemy import select
        u = db.scalar(select(User).where(User.role == role))
        if u is None:
            u = User(email=role + '@t.local', name=role, role=role,
                     password=hash_password('pw'))
            db.add(u); db.flush(); db.commit()
        return {'Authorization': 'Bearer ' + token_for(u)}
    return _headers
