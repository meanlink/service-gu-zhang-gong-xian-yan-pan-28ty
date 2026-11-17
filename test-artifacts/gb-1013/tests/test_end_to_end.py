"""端到端：演示 seed、worker 定时归档、HTTP 登录与容量管理、进程重启恢复。"""
import os
import time

from sqlalchemy import select

from src.config.database import Session
from src.config import settings as app_settings
from src.models import Delivery, Report, ReclaimRecord
from src.services import archives
from src.constants.enums import DeliveryStatus


def artifact_dir():
    return app_settings.ARTIFACT_DIR


def test_seed_creates_visible_archived_delivery(db, tmp_path):
    from scripts.seed_demo import seed
    counts = seed()
    assert counts['deliveries'] >= 1
    with Session() as s:
        d = s.scalar(select(Delivery).order_by(Delivery.id))
        assert d.status == DeliveryStatus.stored.value
        assert set(d.written_kinds) == {'csv', 'json'}
        for suffix in ('', '.json'):
            assert os.path.exists(os.path.join(
                artifact_dir(), d.filename + suffix))
        # 占用登记按真实字节
        used = archives.used_bytes(s)
        assert used > 0


def test_worker_tick_archives_due_report_and_recovers(db):
    from scripts.seed_demo import seed
    seed()
    import src.worker as worker
    # 让计划立即到期
    with Session() as s:
        r = s.scalar(select(Report))
        r.next_run = time.time() - 1
        s.commit()
    worker.tick()
    with Session() as s:
        new_d = s.scalars(select(Delivery).order_by(Delivery.id.desc())).first()
        assert new_d.status in (DeliveryStatus.stored.value, DeliveryStatus.queued.value)
        assert new_d.report_id == r.id
    # 模拟进程崩溃留下 writing 中间态，重启恢复后续传成功
    with Session() as s:
        target = s.get(Delivery, new_d.id)
        target.status = DeliveryStatus.writing.value
        target.note = '崩溃现场'
        s.commit()
    recovered = worker.archives.recover_interrupted(actor='worker')
    assert any(did == new_d.id for _, did, _ in recovered)
    worker.archives.run_delivery(new_d.id)
    with Session() as s:
        final = s.get(Delivery, new_d.id)
        assert final.status == DeliveryStatus.stored.value


def test_http_login_and_storage_management(db):
    from scripts.seed_demo import seed
    seed()
    from fastapi.testclient import TestClient
    from src.main import app
    client = TestClient(app)
    login = client.post('/auth/login', json={'email': 'admin@demo.local',
                                             'password': 'Demo123!'})
    assert login.status_code == 200
    token = login.json()['access_token']
    h = {'Authorization': 'Bearer ' + token}

    stats = client.get('/storage', headers=h).json()
    assert stats['used_bytes'] > 0
    assert stats['capacity_limit_bytes'] >= stats['used_bytes']

    new_limit = stats['used_bytes'] + 4096
    r = client.put('/storage', json={'capacity_limit_bytes': new_limit}, headers=h)
    assert r.status_code == 200 and r.json()['capacity_limit_bytes'] == new_limit

    # 收件箱列表带状态、字节与排队原因字段
    deliveries = client.get('/deliveries', headers=h).json()
    assert deliveries and all(
        {'status', 'size_bytes', 'visible', 'note'} <= set(x) for x in deliveries)

    # 保留后回收记录接口仍可查询，且保留项不会被自动回收
    target_id = deliveries[0]['id']
    assert client.post(f'/storage/deliveries/{target_id}/retain',
                       headers=h).json()['retained'] is True
    assert client.get('/storage/reclaims', headers=h).status_code == 200
    with Session() as s:
        assert s.get(Delivery, target_id).status == DeliveryStatus.retained.value


def test_over_limit_rejection_recorded_with_reason(db):
    from scripts.seed_demo import seed
    seed()
    # 把上限压到单份附件以下，新归档必须被明确拒绝并落回收原因记录
    with Session() as s:
        archives.set_limit(s, 1, 'admin'); s.commit()
    with Session() as s:
        r = s.scalar(select(Report))
        d = archives.create_delivery(s, r); s.commit(); did = d.id
    result = archives.run_delivery(did)
    assert result.status == DeliveryStatus.rejected.value
    assert '超过容量上限' in result.note
    with Session() as s:
        rec = s.scalar(select(ReclaimRecord).where(ReclaimRecord.delivery_id == did))
        assert rec is not None and rec.reason == 'rejected'
        assert archives.usage_for(s, did) == []
