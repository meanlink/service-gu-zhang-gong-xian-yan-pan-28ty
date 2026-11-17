"""归档容量生命周期验收测试：覆盖容量上限、实际字节计费、回收/排队、
保留与回收并发、下载读租约、写入与回收的中断恢复、幂等重试与页面接口权限。"""
import os
import time

from sqlalchemy import select

from src.config.database import Session
from src.config import settings as app_settings
from src.constants.enums import DeliveryStatus, ReclaimReason
from src.models import (
    Delivery, ArchiveUsage, ArchiveRead, ReclaimRecord, Report,
)
from src.services import archives
from src.services.archives import build_payloads, ATTACHMENT_KINDS


def artifact_dir():
    return app_settings.ARTIFACT_DIR


def register_delivery(report):
    with Session() as db:
        r = db.get(Report, report.id)
        d = archives.create_delivery(db, r)
        db.commit()
        return d.id, d.filename


def get_delivery(delivery_id):
    with Session() as db:
        return db.get(Delivery, delivery_id)


def sizes_of(delivery_id):
    with Session() as db:
        return {u.kind: u.size_bytes for u in archives.usage_for(db, delivery_id)}


def files_of(filename):
    return [f for f in os.listdir(artifact_dir())
            if f == filename or f == filename + '.json']


def set_limit(limit):
    with Session() as db:
        archives.set_limit(db, limit, 'tester')
        db.commit()


def archive_size(delivery):
    """单个归档（两份附件）的实际字节。"""
    return sum(sizes_of(delivery.id).values())


def make_delivery(job_factory, **kw):
    j, r, admin = job_factory(**kw)
    delivery_id, filename = register_delivery(r)
    d = archives.run_delivery(delivery_id)
    return d, filename, r, admin


# 1. 按两份附件的实际字节计费，登记与文件一致
def test_usage_billed_by_actual_bytes_of_both_attachments(job_factory):
    d, filename, _, _ = make_delivery(job_factory)
    assert d.status == DeliveryStatus.stored.value
    sizes = sizes_of(d.id)
    assert set(sizes) == {'csv', 'json'}
    for kind in ATTACHMENT_KINDS:
        path = os.path.join(artifact_dir(), filename + ('.json' if kind == 'json' else ''))
        assert os.path.exists(path)
        assert sizes[kind] == os.path.getsize(path)
    with Session() as db:
        assert archives.used_bytes(db) == sum(sizes.values())
        stats = archives.storage_stats(db)
        assert stats['used_bytes'] == sum(sizes.values())
        assert stats['available_bytes'] == stats['capacity_limit_bytes'] - stats['used_bytes']


# 2. 空间不足先回收最早且未保留的归档
def test_eviction_picks_oldest_unretained(job_factory):
    d1, f1, _, _ = make_delivery(job_factory, name='早')
    d2, f2, _, _ = make_delivery(job_factory, name='中')
    unit = archive_size(d1)
    # 上限略低于两份完整归档；新归档体积更小，回收一份后即可容纳
    set_limit(2 * unit - 1)
    d3, f3, _, _ = make_delivery(job_factory, name='新', csv_rows=1)
    assert archive_size(d3) < unit
    if d3.status == DeliveryStatus.queued.value:
        archives.retry_queued()
    assert get_delivery(d1.id).status == DeliveryStatus.reclaimed.value
    assert get_delivery(d2.id).status == DeliveryStatus.stored.value
    assert get_delivery(d3.id).status == DeliveryStatus.stored.value
    assert files_of(f1) == []  # 两份附件文件均被移除
    with Session() as db:
        rec = db.scalar(select(ReclaimRecord).where(ReclaimRecord.delivery_id == d1.id))
        assert rec.reason == ReclaimReason.eviction.value
        assert rec.released_bytes == unit
        assert set(rec.removed_kinds) == {'csv', 'json'}


# 3. 保留成功的附件不被回收；全部被保留时新写入排队
def test_retained_deliveries_never_evicted_and_then_queue(job_factory):
    d1, _, _, _ = make_delivery(job_factory, name='保留项')
    d2, _, _, _ = make_delivery(job_factory, name='普通项')
    archives.set_retained(d1.id, True, 'admin')
    unit = archive_size(d1)
    # 上限略低于两份完整归档：新写入需回收，但 d1 被保留，只能回收 d2
    set_limit(2 * unit - 1)
    d3, f3, r3, _ = make_delivery(job_factory, name='触发回收', csv_rows=1)
    if d3.status == DeliveryStatus.queued.value:
        archives.retry_queued()
    assert get_delivery(d1.id).status == DeliveryStatus.retained.value
    assert get_delivery(d2.id).status == DeliveryStatus.reclaimed.value  # 跳过保留项回收次早项
    assert get_delivery(d3.id).status == DeliveryStatus.stored.value
    # 现有归档全部保留，再来一份普通大小写入：无候选可回收，只能排队
    archives.set_retained(d3.id, True, 'admin')
    # 放宽到恰好两份完整容量：两份保留项仍占住空间，新完整归档放不下
    set_limit(2 * unit)
    j4, r4, _ = job_factory(name='排队项')
    did4, _ = register_delivery(r4)
    d4 = archives.run_delivery(did4)
    assert d4.status == DeliveryStatus.queued.value
    assert '排队' in d4.note
    # 解除保留并释放空间后，worker 回收 d3 即可写入
    archives.set_retained(d3.id, False, 'admin')
    archives.retry_queued()
    assert get_delivery(did4).status == DeliveryStatus.stored.value


# 4. 正在下载（读租约有效）的附件读完前不回收
def test_active_read_lease_blocks_eviction(job_factory):
    d1, f1, _, admin = make_delivery(job_factory, name='下载中')
    with Session() as db:
        live = db.get(Delivery, d1.id)
        lease = archives.open_read_lease(db, live, 'csv', 'viewer')
        db.commit()
        lease_id = lease.id
    unit = archive_size(d1)
    # 上限略低于两份完整归档：新的完整归档需要回收，但最早归档正在读取
    set_limit(2 * unit - 1)
    d2, _, _, _ = make_delivery(job_factory, name='等待空间')
    # 最早归档正在读取 → 不回收，新写入排队
    assert get_delivery(d1.id).status == DeliveryStatus.stored.value
    assert get_delivery(d2.id).status == DeliveryStatus.queued.value
    # 读完释放租约后重试，回收恢复
    archives.close_read_lease(lease_id)
    archives.retry_queued()
    assert get_delivery(d1.id).status == DeliveryStatus.reclaimed.value
    assert get_delivery(d2.id).status == DeliveryStatus.stored.value
    # 过期租约同样不阻塞回收
    with Session() as db:
        stale = ArchiveRead(delivery_id=d2.id, kind='csv', reader='v',
                            expires_at=time.time() - 1, created_at=time.time())
        db.add(stale); db.commit()
    assert archives._pick_eviction_candidate(Session()) is not None


# 4b. 行锁下二次确认：候选看似可回收，但若此刻存在有效读租约则拒绝回收
def test_reclaim_refuses_when_lease_appears_under_lock(job_factory):
    d, filename, _, _ = make_delivery(job_factory, name='锁内下载')
    with Session() as db:
        live = db.get(Delivery, d.id)
        archives.open_read_lease(db, live, 'json', 'viewer')
        db.commit()
    with Session() as db:
        rec = archives._reclaim_by_id(
            db, d.id, ReclaimReason.eviction.value, 'worker', '锁内竞争')
        db.rollback()
    assert rec is None  # 正在下载，不回收
    assert get_delivery(d.id).status == DeliveryStatus.stored.value
    assert files_of(filename)  # 两份附件仍在


# 5. 写入、占用登记与收件箱可见状态一致：两份齐才可见
def test_visibility_only_after_both_attachments_registered(job_factory):
    j, r, _ = job_factory()
    delivery_id, filename = register_delivery(r)
    partial = get_delivery(delivery_id)
    assert partial.status == DeliveryStatus.queued.value
    assert partial.status not in ('stored', 'retained')  # 单份未齐不可见
    # 模拟第一份附件已独立提交
    payloads, _ = build_payloads(j)
    archives._atomic_write(archives._path_for(filename, 'csv'), payloads['csv'])
    with Session() as db:
        d = db.get(Delivery, delivery_id)
        db.add(ArchiveUsage(delivery_id=d.id, kind='csv', filename=filename,
                            size_bytes=len(payloads['csv']), created_at=time.time()))
        d.written_kinds = ['csv']
        d.status = DeliveryStatus.queued.value
        db.commit()
    listing = archives._delivery_dict(get_delivery(delivery_id))
    assert listing['visible'] is False  # 单份不可见
    archives.run_delivery(delivery_id)
    final = get_delivery(delivery_id)
    assert final.status == DeliveryStatus.stored.value
    assert archives._delivery_dict(final)['visible'] is True
    assert set(final.written_kinds) == {'csv', 'json'}


# 6. 回收两份附件都移除才释放容量，并写回收记录说明原因
def test_capacity_released_only_after_both_removed(job_factory):
    d, filename, _, _ = make_delivery(job_factory)
    with Session() as db:
        before = archives.used_bytes(db)
        size = sum(u.size_bytes for u in archives.usage_for(db, d.id))
    rec = archives.reclaim_oldest(size)
    assert rec is not None and set(rec['removed_kinds']) == {'csv', 'json'}
    assert rec['released_bytes'] == size
    assert rec['reason'] == ReclaimReason.eviction.value and rec['detail']
    with Session() as db:
        assert archives.used_bytes(db) == before - size
        assert archives.usage_for(db, d.id) == []
        rows = archives.list_reclaims(db)
        assert any(x['delivery_id'] == d.id for x in rows)
    assert files_of(filename) == []


# 7. 写入中断后可恢复，接着处理剩余附件，重试不重复占用
def test_write_interruption_resumes_without_double_charge(job_factory, monkeypatch):
    j, r, _ = job_factory()
    payloads, _ = build_payloads(j)
    delivery_id, filename = register_delivery(r)
    calls = {'n': 0}
    real_write = archives._atomic_write

    def fail_on_json(path, data):
        if path.endswith('.json') and calls['n'] == 0:
            calls['n'] += 1
            raise OSError('模拟写入中断')
        return real_write(path, data)
    monkeypatch.setattr(archives, '_atomic_write', fail_on_json)
    d = archives.run_delivery(delivery_id)
    # CSV 已落盘登记，JSON 中断 → 保持排队续传
    assert d.status == DeliveryStatus.queued.value
    assert d.written_kinds == ['csv']
    monkeypatch.undo()
    # 中断已在写入边界落为排队态；recover 负责清理临时文件，且不影响已登记的 CSV
    recovered = archives.recover_interrupted()
    assert get_delivery(delivery_id).status == DeliveryStatus.queued.value
    assert get_delivery(delivery_id).written_kinds == ['csv']
    assert isinstance(recovered, list)
    d2 = archives.run_delivery(delivery_id)
    assert d2.status == DeliveryStatus.stored.value
    sizes = sizes_of(delivery_id)
    assert sizes == {'csv': len(payloads['csv']), 'json': len(payloads['json'])}
    # 再重试一次：已存储直接返回，不产生重复占用
    again = archives.run_delivery(delivery_id)
    assert again.status == DeliveryStatus.stored.value
    assert sizes_of(delivery_id) == sizes
    with Session() as db:
        assert db.scalar(select(ArchiveUsage).where(
            ArchiveUsage.delivery_id == delivery_id,
            ArchiveUsage.kind == 'csv'))  # 每种附件仅一条登记


# 8. 回收中断后恢复：完成两份附件移除并释放容量
def test_reclaim_interruption_completes_on_recovery(job_factory):
    d, filename, _, _ = make_delivery(job_factory)
    with Session() as db:
        size = sum(u.size_bytes for u in archives.usage_for(db, d.id))
        target = db.get(Delivery, d.id)
        target.status = DeliveryStatus.reclaiming.value
        db.commit()
    recovered = archives.recover_interrupted()
    assert any(phase == 'reclaimed' and did == d.id and extra == size
               for phase, did, extra in recovered)
    assert get_delivery(d.id).status == DeliveryStatus.reclaimed.value
    assert files_of(filename) == []
    with Session() as db:
        assert archives.usage_for(db, d.id) == []


# 9. 同次操作重试不重复占用或释放容量
def test_retry_is_idempotent(job_factory):
    d, _, _, _ = make_delivery(job_factory)
    with Session() as db:
        used = archives.used_bytes(db)
    archives.run_delivery(d.id)  # stored 状态重入
    archives.run_delivery(d.id)
    with Session() as db:
        assert archives.used_bytes(db) == used
    rec = archives.reclaim_oldest(1)
    archives.recover_interrupted()  # 已 reclaimed，无中间态可处理
    with Session() as db:
        records = list(db.scalars(select(ReclaimRecord).where(
            ReclaimRecord.delivery_id == d.id)))
    assert len(records) == 1  # 回收只释放一次


# 10. 单份附件超上限明确拒绝，不写文件不占容量
def test_single_attachment_over_limit_rejected(job_factory):
    set_limit(10)
    d, filename, _, _ = make_delivery(job_factory, json_pad=500)
    assert d.status == DeliveryStatus.rejected.value
    assert '超过容量上限' in d.note
    assert files_of(filename) == []
    with Session() as db:
        assert archives.usage_for(db, d.id) == []
        rec = db.scalar(select(ReclaimRecord).where(ReclaimRecord.delivery_id == d.id))
        assert rec.reason == ReclaimReason.rejected.value and rec.released_bytes == 0


# 11. 调低上限不动保留项，也不立即回收
def test_lowering_limit_keeps_retained_items(job_factory):
    d, _, _, _ = make_delivery(job_factory)
    archives.set_retained(d.id, True, 'admin')
    with Session() as db:
        used = archives.used_bytes(db)
    set_limit(1)  # 远低于当前占用
    assert get_delivery(d.id).status == DeliveryStatus.retained.value
    with Session() as db:
        assert archives.used_bytes(db) == used
        assert archives.storage_stats(db)['over_limit'] is True


# 12. 排队原因、实际占用与回收记录可查询
def test_queue_reason_usage_and_reclaim_records_queryable(job_factory):
    d1, _, _, _ = make_delivery(job_factory)
    unit = archive_size(d1)
    # 上限略低于两份完整归档；已有归档保留后，新归档放大到放不下 → 无可回收项，排队
    archives.set_retained(d1.id, True, 'admin')
    set_limit(2 * unit - 1)
    j2, r2, _ = job_factory(name='排队', json_pad=unit)
    did2, _ = register_delivery(r2)
    d2 = archives.run_delivery(did2)
    assert d2.note and d2.status == DeliveryStatus.queued.value
    with Session() as db:
        stats = archives.storage_stats(db)
        assert stats['deliveries']['queued'] >= 1
        payload = archives._delivery_dict(db.get(Delivery, did2))
        assert payload['note'] == d2.note
    # 解除保留后回收 d1，排队项重试写入，并检查回收记录
    archives.set_retained(d1.id, False, 'admin')
    archives.retry_queued()
    with Session() as db:
        reclaims = archives.list_reclaims(db)
        assert reclaims and all('reason' in x and 'detail' in x for x in reclaims)
        assert archives.storage_stats(db)['capacity_limit_bytes'] == 2 * unit - 1


# 13. 孤儿/临时文件与登记一致性清理
def test_orphan_and_temp_file_cleanup(job_factory):
    d, filename, _, _ = make_delivery(job_factory)
    tmp = os.path.join(artifact_dir(), '.tmp-archive-abc')
    open(tmp, 'w').write('x')
    orphan = os.path.join(artifact_dir(), filename + '.json')
    archives.reclaim_oldest(1)  # 记录已删除
    assert not os.path.exists(os.path.join(artifact_dir(), filename))
    archives._cleanup_orphan_files()
    assert not os.path.exists(tmp)
    assert not os.path.exists(orphan)


# 14. 接口权限与页面所需数据（admin 可设上限/保留/解除，其他角色拒绝）
def test_http_routes_and_role_permissions(job_factory, auth_headers):
    from fastapi.testclient import TestClient
    from src.main import app
    client = TestClient(app)
    admin = auth_headers('admin')
    analyst = auth_headers('analyst')
    viewer = auth_headers('viewer')

    r = client.get('/storage', headers=viewer)
    assert r.status_code == 200 and 'used_bytes' in r.json()
    r = client.put('/storage', json={'capacity_limit_bytes': 12345}, headers=analyst)
    assert r.status_code == 403
    r = client.put('/storage', json={'capacity_limit_bytes': 12345}, headers=admin)
    assert r.status_code == 200 and r.json()['capacity_limit_bytes'] == 12345
    r = client.put('/storage', json={'capacity_limit_bytes': 0}, headers=admin)
    assert r.status_code == 422

    d, filename, _, _ = make_delivery(job_factory)
    r = client.post(f'/storage/deliveries/{d.id}/retain', headers=viewer)
    assert r.status_code == 403
    r = client.post(f'/storage/deliveries/{d.id}/retain', headers=admin)
    assert r.status_code == 200 and r.json()['retained'] is True
    r = client.post(f'/storage/deliveries/{d.id}/retain', headers=admin)
    assert r.status_code == 200 and r.json()['retained'] is True  # 重复保留幂等
    r = client.post(f'/storage/deliveries/{d.id}/release', headers=analyst)
    assert r.status_code == 403
    r = client.post(f'/storage/deliveries/{d.id}/release', headers=admin)
    assert r.status_code == 200 and r.json()['retained'] is False

    r = client.get('/storage/reclaims', headers=viewer)
    assert r.status_code == 200
    r = client.get('/deliveries', headers=viewer)
    assert r.status_code == 200
    assert any(x['id'] == d.id and 'size_bytes' in x and 'status' in x
               for x in r.json())


# 15. 下载接口受读租约保护，回收中的归档不可下载
def test_download_endpoint_lease_and_visibility(job_factory, auth_headers):
    from fastapi.testclient import TestClient
    from src.main import app
    client = TestClient(app)
    headers = auth_headers('viewer')
    d, filename, _, _ = make_delivery(job_factory)
    r = client.get(f'/deliveries/{d.id}/download?format=csv', headers=headers)
    assert r.status_code == 200 and r.content
    with Session() as db:
        assert db.scalar(select(ArchiveRead).where(
            ArchiveRead.delivery_id == d.id)) is None  # 响应结束即释放租约
    archives.reclaim_oldest(1)
    r = client.get(f'/deliveries/{d.id}/download?format=csv', headers=headers)
    assert r.status_code == 409


# 16. 保留与回收并发：保留成功后回收跳过该归档
def test_retain_concurrent_with_eviction_loop(job_factory):
    d1, _, _, _ = make_delivery(job_factory, name='并发保留')
    d2, _, _, _ = make_delivery(job_factory, name='后到')
    with Session() as db:
        total = archives.used_bytes(db)
    set_limit(total + 1)
    # 在回收发生前完成保留
    archives.set_retained(d1.id, True, 'admin')
    j3, r3, _ = job_factory(name='触发')
    did3, _ = register_delivery(r3)
    d3 = archives.run_delivery(did3)
    assert get_delivery(d1.id).status == DeliveryStatus.retained.value
    assert get_delivery(d2.id).status == DeliveryStatus.reclaimed.value
    assert d3.status == DeliveryStatus.stored.value
