"""启动文化项目拨付与成效追踪后端。

用法：
    python -m cultural_fund.server --host 127.0.0.1 --port 8080 [--demo]

--demo 会登记演示角色（打印 token）并录入 fixtures/seed.json 中的项目资料，
随后发布两版规则（含一次跨生效日的政策换版演示）。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .api import ApiContext, bootstrap_demo_users, create_server
from .clock import VirtualClock
from .eventstore import Actor
from .ingest import ingest_seed
from .services import ROLE_AUTHORITY, CulturalFundService


def main() -> None:
    parser = argparse.ArgumentParser(description="文化项目拨付与成效追踪后端")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--demo", action="store_true", help="录入演示数据与角色")
    parser.add_argument("--seed", default="fixtures/seed.json")
    args = parser.parse_args()

    ctx = ApiContext()
    if args.demo:
        tokens = bootstrap_demo_users(ctx)
        seed_path = Path(args.seed)
        if seed_path.exists():
            seed = json.loads(seed_path.read_text(encoding="utf-8"))
            # 演示录入用固定时钟，保证规则生效日期可预期
            demo_clock = VirtualClock("2026-01-05T09:00:00+00:00")
            demo_service = CulturalFundService(ctx.store, demo_clock, ctx.repo, ctx.vault)
            authority = Actor("u-office", ROLE_AUTHORITY, display_name="省文化产业推进办公室")
            ingest_seed(demo_service, authority, seed)
            print("演示角色令牌：", flush=True)
            for uid, token in tokens.items():
                print(f"  {uid}: {token}", flush=True)
        else:
            print(f"未找到种子文件 {seed_path}，跳过资料录入")

    server = create_server(args.host, args.port, ctx)
    print(f"服务已启动：http://{args.host}:{args.port}/api/v1")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
