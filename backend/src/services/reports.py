import time
from fastapi import HTTPException
from src.models import Job, Report
from src.services import archives
def create(db, data):
    j=db.get(Job,data.job_id)
    if not j or j.status!='completed' or not j.approved:
        raise HTTPException(409,'请先选择已完成且获批准的分析结果')
    r=Report(name=data.name, job_id=j.id, destination=data.destination, interval_seconds=data.interval_seconds, next_run=time.time()+data.delay_seconds, enabled=True)
    db.add(r); db.flush()
    return r
def deliver(db, report):
    """登记一条归档（两份附件），并立即尝试写入；容量不足则排队，由 worker 续处理。"""
    j=db.get(Job,report.job_id)
    if not j or not j.approved: return None
    delivery=archives.create_delivery(db, report)
    db.flush()
    delivery_id=delivery.id
    # 写入在独立事务中完成，保证写入、占用登记、收件箱可见状态一致
    db.commit()
    return archives.run_delivery(delivery_id)
