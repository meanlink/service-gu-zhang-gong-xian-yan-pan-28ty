import hashlib, json
def checksum(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
def dataset_dict(d):
    return {'id':d.id, 'name':d.name, 'source':d.source, 'rows':len(d.records), 'checksum':d.checksum, 'created_at':d.created_at}
def job_dict(j):
    return {k:getattr(j,k) for k in ['id','name','dataset_id','params','status','result','error','approved','created_at','finished_at']}
def report_dict(r):
    return {k:getattr(r,k) for k in ['id','name','job_id','destination','interval_seconds','next_run','enabled']}
def delivery_dict(d):
    # 完整口径由 services.archives._delivery_dict 提供（含实际字节与状态）；
    # 此函数仅用于历史兼容兜底。
    return {'id':d.id, 'report_id':d.report_id, 'created_at':getattr(d,'created_at',0),
            'checksum':getattr(d,'checksum',''), 'filename':getattr(d,'filename',''),
            'destination':d.destination, 'status':getattr(d,'status','stored'),
            'visible':getattr(d,'status','stored') in ('stored','retained'),
            'note':getattr(d,'note',''), 'size_bytes':0, 'written_kinds':getattr(d,'written_kinds',[])}
