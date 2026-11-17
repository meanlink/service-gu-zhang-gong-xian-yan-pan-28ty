import os
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import select
from src.config.database import get_db
from src.models import Report, Delivery
from src.schemas.report import ReportInput
from src.middlewares.auth import current_user, admin_only
from src.services import reports, archives
from src.services.audit import record
router=APIRouter()
@router.get('/reports')
def listing(db=Depends(get_db),u=Depends(current_user)):
    from src.utils.serialization import report_dict
    return [report_dict(r) for r in db.scalars(select(Report).order_by(Report.id.desc()))]
@router.post('/reports',status_code=201)
def create(data:ReportInput,db=Depends(get_db),u=Depends(admin_only)):
    r=reports.create(db,data); record(db,u.email,'report.schedule',{'id':r.id,'destination':r.destination}); db.commit()
    from src.utils.serialization import report_dict
    return report_dict(r)
@router.post('/reports/{id}/toggle')
def toggle(id:int,db=Depends(get_db),u=Depends(admin_only)):
    r=db.get(Report,id)
    if not r:raise HTTPException(404,'分发计划不存在')
    r.enabled=not r.enabled;record(db,u.email,'report.toggle',{'id':id,'enabled':r.enabled});db.commit()
    from src.utils.serialization import report_dict
    return report_dict(r)
@router.get('/deliveries')
def deliveries(db=Depends(get_db),u=Depends(current_user)):
    """全部归档（含排队、拒绝、回收），visible 标记收件箱是否可见。"""
    rows=db.scalars(select(Delivery).order_by(Delivery.id.desc()).limit(200)).all()
    usage_map={}
    for d in rows:
        usage_map[d.id]=archives.usage_for(db,d.id)
    return [archives._delivery_dict(d,used=usage_map[d.id]) for d in rows]
@router.get('/deliveries/{id}/download')
def download(id:int,format:str='csv',db=Depends(get_db),u=Depends(current_user)):
    d=db.get(Delivery,id)
    if not d:raise HTTPException(404,'归档不存在')
    if format not in ('csv','json'):raise HTTPException(422,'支持 CSV 或 JSON')
    if d.status not in ('stored','retained'):
        raise HTTPException(409,'该归档不在收件箱（排队中或已回收），无法下载')
    kind=format
    filename=d.filename+('.json' if format=='json' else '')
    p=os.path.join(archives.artifact_dir(),filename)
    if not os.path.exists(p):raise HTTPException(404,'归档文件不存在，请重新安排分发')
    # 打开读租约：租约有效期间回收不会移除正在下载的附件，读完后释放
    lease=archives.open_read_lease(db,d,kind,u.email)
    record(db,u.email,'archive.download',{'id':id,'format':format,'lease':lease.id})
    db.commit()
    response=FileResponse(p,filename=filename,media_type='application/json' if format=='json' else 'text/csv; charset=utf-8')
    from starlette.background import BackgroundTask
    response.background=BackgroundTask(archives.close_read_lease,lease.id)
    return response
