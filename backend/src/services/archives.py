"""归档容量生命周期：上限设置、按实际字节计费、容量不足回收最早未保留归档、
排队、保留/回收并发保护、下载读租约、写入与回收的中断恢复与幂等重试。"""
import os
import csv
import io
import json
import time
import tempfile
import logging
from fastapi import HTTPException
from sqlalchemy import select, func, exists, delete
from src.config.database import Session
from src.config import settings as app_settings
from src.config.settings import DEFAULT_ARCHIVE_LIMIT_BYTES
from src.constants.enums import DeliveryStatus, AttachmentKind, ReclaimReason, DOWNLOAD_LEASE_SECONDS
from src.models import (
    Job, Report, Delivery, StorageSetting, ArchiveUsage, ArchiveRead, ReclaimRecord,
)
from src.services.audit import record

ATTACHMENT_KINDS = (AttachmentKind.csv.value, AttachmentKind.json.value)
logger = logging.getLogger('archives')


# ---------- 基础工具 ----------
def now():
    return time.time()

def artifact_dir():
    """运行时读取，便于配置覆盖与测试隔离。"""
    return app_settings.ARTIFACT_DIR

def get_setting(db):
    """单行容量配置，不存在则按默认上限初始化。"""
    s = db.scalar(select(StorageSetting).where(StorageSetting.id == 1))
    if s is None:
        s = StorageSetting(id=1, capacity_limit_bytes=DEFAULT_ARCHIVE_LIMIT_BYTES,
                           updated_by='system', updated_at=now())
        db.add(s)
        db.flush()
    return s

def set_limit(db, limit_bytes, actor):
    if not isinstance(limit_bytes, int) or limit_bytes <= 0:
        raise HTTPException(422, '容量上限必须为正整数字节')
    if limit_bytes > 10 ** 15:
        raise HTTPException(422, '容量上限超出允许范围')
    s = get_setting(db)
    old = s.capacity_limit_bytes
    s.capacity_limit_bytes = limit_bytes
    s.updated_by = actor
    s.updated_at = now()
    # 调低上限不主动回收任何归档（含未保留项），仅在下一次写入需要空间时才回收
    record(db, actor, 'archive.limit', {'old_bytes': old, 'new_bytes': limit_bytes})
    return s

def used_bytes(db):
    return int(db.scalar(select(func.coalesce(func.sum(ArchiveUsage.size_bytes), 0))) or 0)

def usage_for(db, delivery_id):
    return list(db.scalars(select(ArchiveUsage).where(ArchiveUsage.delivery_id == delivery_id)))

def _has_active_read(db, delivery_id):
    return db.scalar(select(
        exists().where(ArchiveRead.delivery_id == delivery_id,
                       ArchiveRead.expires_at > now())))

def prune_expired_reads(db):
    db.execute(delete(ArchiveRead).where(ArchiveRead.expires_at <= now()))

def _path_for(filename, kind):
    return os.path.join(artifact_dir(), filename + ('.json' if kind == AttachmentKind.json.value else ''))

def _atomic_write(path, data):
    """临时文件 + fsync + 原子替换：写入中断不会留下半截正式文件。"""
    os.makedirs(artifact_dir(), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.tmp-archive-', dir=artifact_dir())
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

def build_payloads(job):
    """渲染两份附件的实际字节，返回 (payloads, csv_checksum)。"""
    out = io.StringIO()
    out.write('﻿')
    table = job.result.get('table', {})
    cols = table.get('columns', [])
    writer = csv.writer(out)
    def safe(v):
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return v
        text = str(v)
        return "'" + text if text.startswith(('=', '+', '-', '@', '\t', '\r')) else text
    writer.writerow([safe(c['label']) for c in cols])
    for row in table.get('rows', []):
        writer.writerow([safe(row.get(c['key'], '')) for c in cols])
    csv_bytes = out.getvalue().encode('utf-8')
    json_bytes = json.dumps(
        {'job': job.name, 'params': job.params, 'result': job.result},
        ensure_ascii=False, allow_nan=False).encode('utf-8')
    import hashlib
    checksum = hashlib.sha256(csv_bytes).hexdigest()
    return {AttachmentKind.csv.value: csv_bytes,
            AttachmentKind.json.value: json_bytes}, checksum


# ---------- 下载读租约 ----------
def open_read_lease(db, delivery, kind, reader):
    if delivery.status not in (DeliveryStatus.stored.value, DeliveryStatus.retained.value):
        raise HTTPException(409, '该归档当前不在收件箱（排队中或已回收），无法下载')
    lease = ArchiveRead(delivery_id=delivery.id, kind=kind, reader=reader,
                        expires_at=now() + DOWNLOAD_LEASE_SECONDS, created_at=now())
    db.add(lease)
    db.flush()
    return lease

def close_read_lease(lease_id):
    with Session() as db:
        db.execute(delete(ArchiveRead).where(ArchiveRead.id == lease_id))
        db.commit()


# ---------- 回收 ----------
def _pick_eviction_candidate(db):
    """最早创建、未保留、未在下载读取中的已存储归档。"""
    blocked = exists().where(ArchiveRead.delivery_id == Delivery.id,
                             ArchiveRead.expires_at > now())
    stmt = (select(Delivery)
            .where(Delivery.status == DeliveryStatus.stored.value,
                   ~blocked)
            .order_by(Delivery.created_at.asc(), Delivery.id.asc())
            .limit(1)
            .with_for_update())
    return db.scalar(stmt)

def _finalize_reclaim(db, d, reason, actor, detail=''):
    """删除两份附件文件与占用登记，两份都移除后才写回收记录、释放容量。"""
    rows = usage_for(db, d.id)
    removed, released = [], 0
    for u in rows:
        path = _path_for(u.filename, u.kind)
        try:
            if os.path.exists(path):
                os.unlink(path)
        except OSError:
            logger.exception('回收删除文件失败 delivery=%s kind=%s', d.id, u.kind)
        removed.append(u.kind)
        released += int(u.size_bytes)
        db.delete(u)
    for kind in ATTACHMENT_KINDS:
        if kind not in removed:
            # 占用登记缺失但文件可能残留（历史中断），尽力清理
            path = _path_for(d.filename, kind)
            try:
                if os.path.exists(path):
                    os.unlink(path)
                    removed.append(kind)
            except OSError:
                logger.exception('回收清理残留文件失败 delivery=%s', d.id)
    d.status = DeliveryStatus.reclaimed.value
    d.updated_at = now()
    rec = ReclaimRecord(delivery_id=d.id, reason=reason, released_bytes=released,
                        removed_kinds=sorted(set(removed)), actor=actor,
                        detail=detail, created_at=now())
    db.add(rec)
    record(db, actor, 'archive.reclaimed',
           {'id': d.id, 'reason': reason, 'released_bytes': released, 'kinds': rec.removed_kinds})
    return rec

def reclaim_oldest(need_bytes, actor='worker', reason=ReclaimReason.eviction.value,
                   detail=''):
    """在独立事务中回收一份最早未保留归档；无候选（全部保留或下载中）返回 None。"""
    with Session() as pdb:
        with pdb.begin():
            prune_expired_reads(pdb)
    with Session() as db:
        d = _pick_eviction_candidate(db)
        # 行锁后再次确认读租约，挡住与下载并发的时间窗
        if d is None or _has_active_read(db, d.id):
            return None
        cid = d.id
    with Session() as db:
        with db.begin():
            rec = _reclaim_by_id(db, cid, reason, actor,
                                 detail or f'容量不足，需腾出 {need_bytes} 字节，按最早未保留归档回收')
            return _reclaim_record_dict(rec) if rec else None


# ---------- 写入 ----------
def create_delivery(db, report):
    d = Delivery(report_id=report.id, created_at=now(), destination=report.destination,
                 status=DeliveryStatus.queued.value, written_kinds=[],
                 note='等待写入两份附件', attempts=0, updated_at=now())
    db.add(d)
    db.flush()
    d.filename = f'report-{report.id}-delivery-{d.id}.csv'
    d.checksum = ''
    return d

def _reclaim_by_id(db, delivery_id, reason, actor, detail):
    """在当前事务内回收指定归档；两份附件都移除才释放容量。
    行锁下再次确认状态与读租约，挡住与下载/保留并发的时间窗。"""
    d = db.get(Delivery, delivery_id, with_for_update=True)
    if d is None or d.status not in (DeliveryStatus.stored.value,
                                     DeliveryStatus.reclaiming.value):
        return None
    if d.status == DeliveryStatus.stored.value and _has_active_read(db, d.id):
        return None  # 正在下载的附件读完前不回收
    d.status = DeliveryStatus.reclaiming.value
    d.updated_at = now()
    return _finalize_reclaim(db, d, reason, actor, detail)

def run_delivery(delivery_id, actor='worker'):
    """推进一次归档写入。附件逐份原子落盘并独立提交；中断重入只处理未登记附件，
    同次操作重试不会重复占用或释放容量。返回刷新后的 Delivery。"""
    # 阶段一：准备并做单份超上限校验（独立事务）
    with Session() as db:
        with db.begin():
            d = db.get(Delivery, delivery_id, with_for_update=True)
            if d is None:
                return None
            if d.status not in (DeliveryStatus.queued.value, DeliveryStatus.writing.value):
                return d
            d.attempts += 1
            d.status = DeliveryStatus.writing.value
            d.updated_at = now()
            report = db.get(Report, d.report_id)
            job = db.get(Job, report.job_id) if report else None
            if not report or not job or not job.approved:
                d.status = DeliveryStatus.queued.value
                d.note = '关联分析结果不可用，等待下次重试'
                record(db, actor, 'archive.queued', {'id': d.id, 'reason': d.note})
                return d
            payloads, checksum = build_payloads(job)
            limit = get_setting(db).capacity_limit_bytes
            # 单份附件超上限：明确拒绝，不写入、不占用容量
            for kind, data in payloads.items():
                if len(data) > limit:
                    d.status = DeliveryStatus.rejected.value
                    d.note = f'{kind} 附件 {len(data)} 字节超过容量上限 {limit} 字节，已拒绝写入'
                    d.updated_at = now()
                    db.add(ReclaimRecord(delivery_id=d.id, reason=ReclaimReason.rejected.value,
                                         released_bytes=0, removed_kinds=[], actor=actor,
                                         detail=d.note, created_at=now()))
                    record(db, actor, 'archive.rejected', {'id': d.id, 'note': d.note})
                    return d
            missing = [k for k in ATTACHMENT_KINDS if k not in set(d.written_kinds or [])]
            filename = d.filename

    # 阶段二：容量不足时逐份回收最早未保留、未在下载的归档；仍不足则排队
    need = sum(len(payloads[k]) for k in missing)
    while True:
        # 读取与决策在独立短事务，结束后才开启写事务，避免锁嵌套
        with Session() as pdb:
            with pdb.begin():
                prune_expired_reads(pdb)
        with Session() as db:
            enough = used_bytes(db) + need <= limit
            candidate = None if enough else _pick_eviction_candidate(db)
            blocked = candidate is not None and _has_active_read(db, candidate.id)
            cid = candidate.id if candidate is not None else None
        if enough:
            break
        if cid is None or blocked:
            note = ('最早归档正在下载读取，回收暂缓，本次写入排队等待' if blocked
                    else f'空间不足（需 {need} 字节，上限 {limit} 字节，'
                         '可回收归档均被保留或正在下载），已排队等待空间释放')
            with Session() as qdb:
                with qdb.begin():
                    qd = qdb.get(Delivery, delivery_id, with_for_update=True)
                    qd.status = DeliveryStatus.queued.value
                    qd.note = note
                    qd.updated_at = now()
                    record(qdb, actor, 'archive.queued', {'id': delivery_id, 'reason': note})
                    return qdb.get(Delivery, delivery_id)
        with Session() as rdb:
            with rdb.begin():
                rec = _reclaim_by_id(rdb, cid, ReclaimReason.eviction.value, actor,
                                     f'为归档 {delivery_id} 腾出空间，回收最早未保留归档')
        if rec is None:
            # 候选在锁定瞬间被保留或开始下载：本次写入排队，等待下轮重试
            with Session() as qdb:
                with qdb.begin():
                    qd = qdb.get(Delivery, delivery_id, with_for_update=True)
                    qd.status = DeliveryStatus.queued.value
                    qd.note = '目标归档被保留或正在下载，回收暂缓，本次写入排队等待'
                    qd.updated_at = now()
                    record(qdb, actor, 'archive.queued',
                           {'id': delivery_id, 'reason': qd.note})
                    return qdb.get(Delivery, delivery_id)

    # 阶段三：逐附件原子写入；每份附件独立事务提交，崩溃后可从下一份续传
    for kind in missing:
        with Session() as db:
            current = db.get(Delivery, delivery_id)
            if current is None or kind in set(current.written_kinds or []):
                continue  # 重入：已登记附件跳过，不重复占用容量
        try:
            _atomic_write(_path_for(filename, kind), payloads[kind])
        except OSError:
            # 写入中断：已登记附件保留，本份未登记不占容量；标记排队，下次只续剩余附件
            logger.exception('附件 %s 写入中断 delivery=%s', kind, delivery_id)
            with Session() as edb:
                with edb.begin():
                    ed = edb.get(Delivery, delivery_id, with_for_update=True)
                    ed.status = DeliveryStatus.queued.value
                    ed.note = f'{kind} 附件写入中断，已排队从该附件续传'
                    ed.updated_at = now()
                    record(edb, actor, 'archive.queued',
                           {'id': delivery_id, 'reason': ed.note})
                return edb.get(Delivery, delivery_id)
        with Session() as db:
            with db.begin():
                d = db.get(Delivery, delivery_id, with_for_update=True)
                if kind not in set(d.written_kinds or []):
                    db.add(ArchiveUsage(delivery_id=d.id, kind=kind, filename=filename,
                                        size_bytes=len(payloads[kind]), created_at=now()))
                    d.written_kinds = sorted(set(d.written_kinds or []) | {kind})
                d.updated_at = now()
                if set(d.written_kinds) == set(ATTACHMENT_KINDS):
                    # 两份附件均已登记才转为收件箱可见，保证状态一致
                    d.status = DeliveryStatus.stored.value
                    d.checksum = checksum
                    d.note = ''
                    record(db, actor, 'archive.stored',
                           {'id': d.id, 'bytes': sum(len(v) for v in payloads.values())})
                else:
                    d.status = DeliveryStatus.queued.value
                    d.note = '部分附件已写入，剩余附件排队续传'

    # 兜底：两份附件实际已齐（例如崩溃于状态切换前）则恢复可见
    with Session() as db:
        with db.begin():
            d = db.get(Delivery, delivery_id, with_for_update=True)
            if d is not None and set(d.written_kinds or []) == set(ATTACHMENT_KINDS) \
                    and d.status not in (DeliveryStatus.stored.value, DeliveryStatus.retained.value):
                d.status = DeliveryStatus.stored.value
                d.checksum = checksum
                d.note = ''
                d.updated_at = now()
        return db.get(Delivery, delivery_id)


def retry_queued(actor='worker', limit=5):
    """worker 周期调用：排队归档按最早顺序重试，同次操作重试不重复占用容量。"""
    with Session() as db:
        ids = list(db.scalars(
            select(Delivery.id)
            .where(Delivery.status == DeliveryStatus.queued.value)
            .order_by(Delivery.created_at.asc(), Delivery.id.asc()).limit(limit)))
    results = []
    for delivery_id in ids:
        d = run_delivery(delivery_id, actor)
        if d is not None:
            results.append(d.id)
    return results


# ---------- 保留 / 解除 ----------
def set_retained(delivery_id, retained, actor):
    with Session() as db:
        with db.begin():
            d = db.get(Delivery, delivery_id, with_for_update=True)
            if d is None:
                raise HTTPException(404, '归档不存在')
            if retained:
                if d.status not in (DeliveryStatus.stored.value, DeliveryStatus.retained.value):
                    raise HTTPException(409, '仅收件箱中已存储的归档可以保留')
                if d.status == DeliveryStatus.stored.value:
                    d.status = DeliveryStatus.retained.value
                    d.updated_at = now()
                    record(db, actor, 'archive.retain', {'id': d.id})
            else:
                if d.status != DeliveryStatus.retained.value:
                    raise HTTPException(409, '该归档未处于保留状态')
                d.status = DeliveryStatus.stored.value
                d.updated_at = now()
                record(db, actor, 'archive.release', {'id': d.id})
            payload = _delivery_dict(d, used=usage_for(db, d.id))
        return payload


# ---------- 中断恢复 ----------
def recover_interrupted(actor='worker'):
    """进程重启后：写入中→排队续传；回收中→完成两份附件移除并释放容量；清理残留临时/孤儿文件。"""
    recovered = []
    with Session() as db:
        with db.begin():
            prune_expired_reads(db)
            for d in db.scalars(select(Delivery).where(
                    Delivery.status.in_([DeliveryStatus.writing.value,
                                         DeliveryStatus.reclaiming.value]))):
                if d.status == DeliveryStatus.reclaiming.value:
                    rec = _finalize_reclaim(db, d, ReclaimReason.eviction.value, actor,
                                            '回收中断后恢复：完成两份附件移除并释放容量')
                    recovered.append(('reclaimed', d.id, rec.released_bytes))
                else:
                    d.status = DeliveryStatus.queued.value
                    d.note = '写入中断后恢复：已登记附件保留，剩余附件排队续传'
                    d.updated_at = now()
                    record(db, actor, 'archive.recovered', {'id': d.id, 'phase': 'writing'})
                    recovered.append(('queued', d.id, 0))
    _cleanup_orphan_files()
    return recovered

def _cleanup_orphan_files():
    """文件与登记一致性兜底：删除临时文件及已回收/已拒绝归档残留的附件。"""
    base_dir = artifact_dir()
    try:
        if not os.path.isdir(base_dir):
            return
        names = os.listdir(base_dir)
    except OSError:
        logger.exception('扫描归档目录失败')
        return
    with Session() as db:
        for name in names:
            if name.startswith('.tmp-archive-'):
                try:
                    os.unlink(os.path.join(base_dir, name))
                except OSError:
                    pass
                continue
            if not (name.endswith('.csv') or name.endswith('.json')):
                continue
            base = name[:-5] if name.endswith('.json') else name[:-4]
            parts = base.split('-delivery-')
            if len(parts) != 2 or not parts[1].isdigit():
                continue
            d = db.get(Delivery, int(parts[1]))
            if d is None or d.status in (DeliveryStatus.reclaimed.value,
                                         DeliveryStatus.rejected.value):
                try:
                    os.unlink(os.path.join(base_dir, name))
                except OSError:
                    pass


# ---------- 查询序列化 ----------
def _delivery_dict(d, used=None):
    if used is None:
        with Session() as db:
            used = usage_for(db, d.id)
    kinds = sorted(u.kind for u in used)
    size = sum(int(u.size_bytes) for u in used)
    return {
        'id': d.id, 'report_id': d.report_id, 'created_at': d.created_at,
        'updated_at': d.updated_at, 'checksum': d.checksum, 'filename': d.filename,
        'destination': d.destination, 'status': d.status,
        'retained': d.status == DeliveryStatus.retained.value,
        'visible': d.status in (DeliveryStatus.stored.value, DeliveryStatus.retained.value),
        'written_kinds': list(d.written_kinds or []),
        'attachment_kinds': kinds, 'size_bytes': size,
        'note': d.note or '', 'attempts': d.attempts,
    }

def _reclaim_record_dict(r):
    return {'id': r.id, 'delivery_id': r.delivery_id, 'reason': r.reason,
            'released_bytes': r.released_bytes, 'removed_kinds': list(r.removed_kinds or []),
            'actor': r.actor, 'detail': r.detail or '', 'created_at': r.created_at}

def storage_stats(db):
    s = get_setting(db)
    used = used_bytes(db)
    rows = list(db.execute(select(Delivery.status, func.count(Delivery.id))
                           .group_by(Delivery.status)).all())
    by_status = {status: count for status, count in rows}
    return {
        'capacity_limit_bytes': int(s.capacity_limit_bytes),
        'used_bytes': used,
        'available_bytes': max(0, int(s.capacity_limit_bytes) - used),
        'over_limit': used > int(s.capacity_limit_bytes),
        'attachment_count': db.scalar(select(func.count()).select_from(ArchiveUsage)),
        'updated_by': s.updated_by, 'updated_at': s.updated_at,
        'deliveries': {st.value: by_status.get(st.value, 0) for st in DeliveryStatus},
    }

def list_reclaims(db, limit=100):
    rows = db.scalars(select(ReclaimRecord).order_by(ReclaimRecord.id.desc()).limit(limit))
    return [_reclaim_record_dict(r) for r in rows]
