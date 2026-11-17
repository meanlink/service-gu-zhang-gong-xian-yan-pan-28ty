from enum import Enum
class Role(str, Enum):
    admin = 'admin'
    analyst = 'analyst'
    viewer = 'viewer'
class JobStatus(str, Enum):
    queued = 'queued'
    running = 'running'
    completed = 'completed'
    failed = 'failed'
# 归档写入状态机：writing（逐附件写入中）→ stored（两份附件均登记，收件箱可见）；
# 空间不足或写入中断时 queued/rejected/reclaiming；retained 为负责人保留标记。
class DeliveryStatus(str, Enum):
    queued = 'queued'          # 空间不足排队，等待 worker 恢复重试
    writing = 'writing'        # 正在逐份写入两份附件
    stored = 'stored'          # 两份附件均已写入并登记，收件箱可见
    retained = 'retained'      # 负责人保留，回收跳过（文件仍在）
    reclaiming = 'reclaiming'  # 回收处理中（中断可恢复）
    reclaimed = 'reclaimed'    # 两份附件均移除，容量已释放
    rejected = 'rejected'      # 单份附件超过容量上限，明确拒绝
class AttachmentKind(str, Enum):
    csv = 'csv'
    json = 'json'
class ReclaimReason(str, Enum):
    eviction = 'eviction'      # 空间不足自动回收最早未保留归档
    rejected = 'rejected'      # 单份超上限拒绝登记
    manual = 'manual'          # 预留：人工处置
# 默认归档容量上限（字节），可由负责人在页面调整；可用 ARCHIVE_DEFAULT_LIMIT_BYTES 覆盖
DEFAULT_ARCHIVE_LIMIT_BYTES = 10 * 1024 * 1024
# 下载读租约最长保留秒数，超时视为读取已结束，可重新参与回收
DOWNLOAD_LEASE_SECONDS = 300
