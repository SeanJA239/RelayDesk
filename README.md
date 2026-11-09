# 客户反馈入口试点（第一阶段）

内部分配的客户账号登录 Baserow App Builder 页面提交工单；桥接服务为每张工单在对应单位的 GitLab 项目中创建且仅创建一个 Issue。

## 组成

- `docker-compose.yml`：Baserow 2.4.1 与 GitLab CE（仅本地试点用）。
- `scripts/`：初始化脚本，按顺序执行一次。
- `src/feedback_bridge/`：桥接服务（Webhook 快速通道 + 定期对账）。
- `tests/bridge/`：同步逻辑单元测试；`tests/live/`：对运行中环境的隔离与同步测试。

## 使用

```sh
uv sync
docker compose --env-file .env.local up -d   # 首次需先有 .env.local 中的 GITLAB_ROOT_PASSWORD
uv run scripts/seed.py           # 表结构与两个单位、四个测试账号
uv run scripts/build_app.py      # 发布 http://portal.localhost:8090
uv run scripts/setup_gitlab.py   # 单位项目与桥接用组访问令牌
uv run scripts/connect_bridge.py # Baserow 令牌与 Webhook
./scripts/run_bridge.sh          # 桥接服务，端口 8787
uv run pytest                    # 全部测试；tests/live 需上述环境在运行
```

e2e 测试建议以 `BRIDGE_RECONCILE_SECONDS=2 BRIDGE_BASE_BACKOFF_SECONDS=2 ./scripts/run_bridge.sh` 启动桥接服务。凭据均写入 `.env.local`（已忽略）。
