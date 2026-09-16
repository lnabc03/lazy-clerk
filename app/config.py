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
    """极简 .env 加载（不引入 python-dotenv 依赖）。

    合并两个文件：根 .env（本地开发）打底，server/.env（公网版部署约定）
    覆盖同名键；真实环境变量优先级最高。
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    merged: dict[str, str] = {}
    for path in (os.path.join(root, ".env"), os.path.join(root, "server", ".env")):
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key, value = key.strip(), value.strip()
                if key:
                    merged[key] = value
    for key, value in merged.items():
        os.environ.setdefault(key, value)


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
    # 代理逃生通道（仅公网版配 mihomo sidecar 时使用）：空 = 直连
    proxy_url: str = field(default_factory=lambda: os.environ.get("PROXY_URL", ""))
    # mihomo API：探针用它查当前出口（直连/节点），热力图据此分色
    mihomo_api: str = field(default_factory=lambda: os.environ.get("MIHOMO_API", ""))
    mihomo_secret: str = field(default_factory=lambda: os.environ.get("MIHOMO_SECRET", ""))
    proxy_group: str = field(default_factory=lambda: os.environ.get("PROXY_GROUP", "hospital"))

    @property
    def db_path(self) -> str:
        return os.path.join(self.data_dir, "lazy-clerk.db")


settings = Settings()
