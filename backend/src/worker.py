import time, json, logging
from pathlib import Path
from sqlalchemy import select
from redis import Redis
from src.config.database import Session
from src.config.settings import REDIS_URL
from src.models import Job, Dataset, Report
from src.services.business import analyze
from src.services import archives
from src.services.audit import record
logging.basicConfig(level=logging.INFO)
def execute(job_id):
    with Session() as db:
        j=db.get(Job,job_id)
        if not j or j.status!='queued':return
        j.status='running'; db.commit()
        try:
            d=db.get(Dataset,j.dataset_id)
            result=analyze(d.records,j.params)
            json.dumps(result,allow_nan=False)
            j.result=result;j.status='completed';j.finished_at=time.time()
            record(db,'worker','job.completed',{'id':j.id})
        except Exception as e:
            logging.exception('Analysis failed')
            j.status='failed';j.error=str(e)[:990];j.finished_at=time.time()
            record(db,'worker','job.failed',{'id':j.id,'error':j.error})
        db.commit()
def tick():
    # 先重试排队中的归档（同次操作重试不重复占用或释放容量）
    archives.retry_queued(actor='worker')
    with Session() as db:
        due=list(db.scalars(select(Report).where(Report.enabled==True,Report.next_run<=time.time())))
        for r in due:
            j=db.get(Job,r.job_id)
            if not j or not j.approved:
                r.next_run=time.time()+r.interval_seconds
                record(db,'worker','report.skipped',{'id':r.id,'reason':'关联结果未批准'})
                continue
            # 先登记并提交，再在独立事务写入：写入中断后可按记录恢复续传
            d=archives.create_delivery(db,r)
            r.next_run=time.time()+r.interval_seconds
            delivery_id=d.id
            record(db,'worker','report.scheduled',{'id':r.id,'delivery':delivery_id})
            db.commit()
            result=archives.run_delivery(delivery_id,actor='worker')
            if result is not None:
                with Session() as log_db:
                    record(log_db,'worker','report.delivered',
                           {'id':r.id,'delivery':delivery_id,'status':result.status})
                    log_db.commit()
        db.commit()
def main():
    r=Redis.from_url(REDIS_URL,socket_timeout=3)
    # 单 worker；崩溃不得遗留 running 任务或写入/回收中间态
    with Session() as db:
        for j in db.scalars(select(Job).where(Job.status=='running')):
            j.status='queued';record(db,'worker','job.recovered',{'id':j.id})
        db.commit()
    recovered=archives.recover_interrupted(actor='worker')
    for phase,delivery_id,extra in recovered:
        logging.info('Recovered delivery %s phase=%s extra=%s',delivery_id,phase,extra)
    while True:
        Path('/tmp/heartbeat').touch()
        try:
            item=r.blpop('analysis:queue',timeout=1)
            if item:execute(int(item[1]))
            tick()
        except Exception:
            logging.exception('Worker retry');time.sleep(2)
if __name__=='__main__':main()
