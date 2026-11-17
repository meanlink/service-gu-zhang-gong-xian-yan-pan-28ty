from sqlalchemy import String, Double, ForeignKey, Boolean, BigInteger, JSON
from sqlalchemy.orm import Mapped, mapped_column
from src.config.database import Base

class Report(Base):
    __tablename__ = 'reports'
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    job_id: Mapped[int] = mapped_column(ForeignKey('jobs.id'))
    destination: Mapped[str] = mapped_column(String(120))
    interval_seconds: Mapped[int]
    next_run: Mapped[float] = mapped_column(Double)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

class Delivery(Base):
    """一次简报归档：两份附件（CSV+JSON）作为同一容量单元。"""
    __tablename__ = 'deliveries'
    id: Mapped[int] = mapped_column(primary_key=True)
    report_id: Mapped[int] = mapped_column(ForeignKey('reports.id'))
    created_at: Mapped[float] = mapped_column(Double)
    checksum: Mapped[str] = mapped_column(String(64), default='')
    filename: Mapped[str] = mapped_column(String(160), default='')
    destination: Mapped[str] = mapped_column(String(120))
    # 写入状态机：queued → writing → stored/retained → reclaiming → reclaimed；异常 → queued/rejected
    status: Mapped[str] = mapped_column(String(20), default='queued', index=True)
    # 已成功写入并登记占用的附件类型（csv/json），同次操作重试据此跳过，避免重复占用容量
    written_kinds: Mapped[list] = mapped_column(JSON, default=list)
    # 排队或拒绝原因（容量不足排队、单份超上限拒绝等），供页面查询
    note: Mapped[str] = mapped_column(String(500), default='')
    attempts: Mapped[int] = mapped_column(default=0)
    updated_at: Mapped[float] = mapped_column(Double, default=0.0)

class StorageSetting(Base):
    """单行配置：归档容量上限（实际字节计费）。"""
    __tablename__ = 'storage_settings'
    id: Mapped[int] = mapped_column(primary_key=True)
    capacity_limit_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    updated_by: Mapped[str] = mapped_column(String(150), default='system')
    updated_at: Mapped[float] = mapped_column(Double, default=0.0)

class ArchiveUsage(Base):
    """逐附件实际字节占用登记；delivery 两份附件都回收后才释放对应容量。"""
    __tablename__ = 'archive_usage'
    id: Mapped[int] = mapped_column(primary_key=True)
    delivery_id: Mapped[int] = mapped_column(ForeignKey('deliveries.id'), index=True)
    kind: Mapped[str] = mapped_column(String(10))          # csv / json
    filename: Mapped[str] = mapped_column(String(160))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[float] = mapped_column(Double)

class ArchiveRead(Base):
    """下载读租约：租约有效期内对应附件不参与回收。"""
    __tablename__ = 'archive_reads'
    id: Mapped[int] = mapped_column(primary_key=True)
    delivery_id: Mapped[int] = mapped_column(ForeignKey('deliveries.id'), index=True)
    kind: Mapped[str] = mapped_column(String(10))
    reader: Mapped[str] = mapped_column(String(150))
    expires_at: Mapped[float] = mapped_column(Double, index=True)
    created_at: Mapped[float] = mapped_column(Double)

class ReclaimRecord(Base):
    """回收记录：两份附件都移除才落一条成功记录并释放容量；失败也记录原因。"""
    __tablename__ = 'reclaim_records'
    id: Mapped[int] = mapped_column(primary_key=True)
    delivery_id: Mapped[int] = mapped_column(ForeignKey('deliveries.id'), index=True)
    reason: Mapped[str] = mapped_column(String(20))        # eviction / rejected / manual
    released_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    removed_kinds: Mapped[list] = mapped_column(JSON, default=list)
    actor: Mapped[str] = mapped_column(String(150), default='worker')
    detail: Mapped[str] = mapped_column(String(500), default='')
    created_at: Mapped[float] = mapped_column(Double)
