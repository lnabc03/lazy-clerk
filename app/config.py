"""环境变量读取。所有配置集中于此，其余模块只 import settings。"""
from __future__ import annotations

import os
import secrets
import sys
from dataclasses import dataclass, field


def _default_data_dir() -> str:
    """打包成 exe 时数据目录固定在 exe 旁边（计划任务的 CWD 不可靠）。"""
    if getattr(sys, "frozen", False):
        return os.path.join(os.path.dirname(sys.executable), "data")
    return "data"


def _load_dotenv() -> None:
    """极简 .env 加载（不引入 python-dotenv 依赖），已存在的环境变量优先。

    候选位置：server/.env（公网版部署约定）→ 项目根 .env（本地开发）。
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for path in (os.path.join(root, "server", ".env"), os.path.join(root, ".env")):
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key, value = key.strip(), value.strip()
                if key and key not in os.environ:
                    os.environ[key] = value
        return


_load_dotenv()


@dataclass(frozen=True)
class Settings:
    admin_password: str = field(default_factory=lambda: os.environ.get("ADMIN_PASSWORD", ""))
    serverchan_sendkey: str = field(default_factory=lambda: os.environ.get("SERVERCHAN_SENDKEY", ""))
    # 未配置时随机生成——进程重启后所有 session 失效，仅影响体验，不阻断运行
    session_secret: str = field(
        default_factory=lambda: os.environ.get("SESSION_SECRET") or secrets.token_hex(32)
    )
    tz: str = field(default_factory=lambda: os.environ.get("TZ", "Asia/Shanghai"))
    data_dir: str = field(default_factory=lambda: os.environ.get("DATA_DIR") or _default_data_dir())

    @property
    def db_path(self) -> str:
        return os.path.join(self.data_dir, "lazy-clerk.db")


settings = Settings()
