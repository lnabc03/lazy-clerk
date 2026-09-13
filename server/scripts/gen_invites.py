"""预生成邀请码：明文打印到 stdout（由部署者线下分发），数据库只存 SHA-256 哈希。

用法：python scripts/gen_invites.py [--count 50]
（Web 管理页也提供同样的生成功能，见 /admin）
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app import db, models  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser(description="生成邀请码")
    p.add_argument("--count", type=int, default=50)
    args = p.parse_args()

    db.init()
    codes = [models.generate_invite() for _ in range(args.count)]
    print(f"已生成 {args.count} 个邀请码。明文如下，也可在管理页「未使用邀请码」中随时查看：\n")
    for c in codes:
        print(c)


if __name__ == "__main__":
    main()
