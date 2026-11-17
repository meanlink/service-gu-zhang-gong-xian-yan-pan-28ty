from fastapi import APIRouter, Depends, HTTPException
from src.config.database import get_db
from src.models import Delivery
from src.schemas.archive import LimitInput
from src.middlewares.auth import current_user, admin_only
from src.services import archives
from src.services.audit import record
router=APIRouter(prefix='/storage')

@router.get('')
def storage(db=Depends(get_db),u=Depends(current_user)):
    """实际占用、容量上限与各状态归档数量。"""
    return archives.storage_stats(db)

@router.put('')
def set_limit(data:LimitInput,db=Depends(get_db),u=Depends(admin_only)):
    """负责人设置容量上限（字节）。调低不回收保留项，也不立即触发回收。"""
    archives.set_limit(db,data.capacity_limit_bytes,u.email); db.commit()
    return archives.storage_stats(db)

@router.get('/reclaims')
def reclaims(db=Depends(get_db),u=Depends(current_user)):
    """回收记录（含原因、释放字节、移除附件）。"""
    return archives.list_reclaims(db)

@router.post('/deliveries/{id}/retain')
def retain(id:int,db=Depends(get_db),u=Depends(admin_only)):
    return archives.set_retained(id,True,u.email)

@router.post('/deliveries/{id}/release')
def release(id:int,db=Depends(get_db),u=Depends(admin_only)):
    return archives.set_retained(id,False,u.email)

@router.post('/deliveries/{id}/retry')
def retry(id:int,db=Depends(get_db),u=Depends(admin_only)):
    """排队归档手动重试；已完成/已回收/已拒绝不会被重复处理。"""
    d=db.get(Delivery,id)
    if not d: raise HTTPException(404,'归档不存在')
    if d.status not in ('queued','writing'):
        raise HTTPException(409,'仅排队或中断恢复中的归档可以重试')
    record(db,u.email,'archive.retry',{'id':id}); db.commit()
    result=archives.run_delivery(id,actor=u.email)
    if result is None: raise HTTPException(404,'归档不存在')
    return archives._delivery_dict(result)
