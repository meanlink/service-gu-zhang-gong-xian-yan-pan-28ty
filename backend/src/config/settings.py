import os
DATABASE_URL = os.getenv('DATABASE_URL', 'sqlite:///./local.db')
REDIS_URL = os.getenv('REDIS_URL', 'redis://redis:6379/0')
JWT_SECRET = os.environ.get('JWT_SECRET', 'local-development-change-before-deploy')
ARTIFACT_DIR = os.getenv('ARTIFACT_DIR', '/artifacts')
# 归档容量默认上限（字节），负责人可在页面调整
DEFAULT_ARCHIVE_LIMIT_BYTES = int(os.getenv('ARCHIVE_DEFAULT_LIMIT_BYTES', '10485760'))
