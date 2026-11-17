# 归档容量生命周期验收测试

对应需求：简报长期归档的容量上限、按附件实际字节计费、容量不足回收/排队、
保留与回收并发保护、下载读租约、写入与回收中断恢复、幂等重试，以及页面接口。

## 运行

依赖与后端一致（见 `backend/requirements.txt`），另需 `pytest`。

```bash
pip install -r backend/requirements.txt pytest
cd test-artifacts/gb-1013/tests
python3 -m pytest
```

测试使用独立临时 SQLite 与临时归档目录，不依赖 Docker、Postgres、Redis 或外部服务；
每个用例重建数据库与产物目录，互不串扰。

## 用例索引

| 文件 | 覆盖点 |
|---|---|
| `test_archive_lifecycle.py` | 实际字节计费、最早未保留回收、排队、保留/解除、读租约、可见性一致、双附件释放、写入/回收中断恢复、幂等、超上限拒绝、调低上限不动保留项、查询口径、孤儿文件清理、HTTP 权限 |
| `test_end_to_end.py` | 演示 seed 幂等、worker 定时归档与重启恢复、HTTP 登录与容量管理、拒绝原因落库 |
| `test_migration_upgrade.py` | 旧版 deliveries 表补列、占用回填、默认容量配置、迁移幂等 |

## 关键实现位置

- 后端生命周期服务：`backend/src/services/archives.py`
- 模型与状态机：`backend/src/models/report.py`、`backend/src/constants/enums.py`
- 容量/保留接口：`backend/src/routes/archive.py`
- 下载读租约：`backend/src/routes/reports.py`
- worker 恢复与排队重试：`backend/src/worker.py`
- 前端页面：`frontend/src/pages/reports.js`
