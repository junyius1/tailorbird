# Tailorbird

本地自托管的编程 agent 服务器(OpenSWE 的 **无 GitHub / 无云 / 纯本地** 变体)。

- **LangGraph 运行时** `:2026`:两张图(`agent` / `scheduler`),挂载 FastAPI dashboard,进程内 loopback 完成 webhook。
- **FastAPI dashboard 服务器** `:8002`(`agent.webapp:app` 独立进程):浏览器侧 `/dashboard/api/**`(认证、线程、计划、调度、技能、指令、画像、健康)。
- **UI** `:3002`(Vite + TanStack Start + Nitro):本地 token 登录、线程界面、本地 git diff、懒加载推理,只反代 `/dashboard/api/**`。

核心约束:无 GitHub / PR / OAuth / Slack / Linear / Notion / 云沙箱;交付默认**仅本地 git 分支提交**;持久化**仅 Postgres**(缺失/不可达 → 启动 fail-closed);单事件循环(沙箱绑定该循环);主模型为**本地 vLLM**;推理单独落 Postgres 并从 checkpoint 剥离、UI 懒加载。

## 目录

- `agent/` — 运行时 + dashboard 业务(FastAPI)
- `langgraph.json` — 图 / checkpointer / store / auth / webhook 配置
- `ui/` — 前端(见 `ui/README.md`)
- `scripts/bootstrap_3partylibs.sh` — 引导第三方源码依赖
- `3partylibs/` — langchain / langgraph / deepagents **源码**(editable 依赖,不入仓库)

## 全新机器安装

1. **引导 3partylibs 源码**(按钉死的上游 commit,仓库内不提交):
   ```bash
   bash scripts/bootstrap_3partylibs.sh
   ```
2. **配置环境**:
   ```bash
   cp .env.example .env   # 填 DATABASE_URI / OPEN_SWE_LOCAL_AUTH_TOKEN / RUN_COMPLETE_WEBHOOK_SECRET 等
   ```
   需要可达的 Postgres(`DATABASE_URI`)与本地 vLLM(`LOCAL_LLM_BASE_URL`)。
3. **安装依赖**:
   ```bash
   make install            # = bootstrap + uv sync
   ```

## 启动(三个服务,三个终端)

```bash
make runtime               # 终端 1:运行时 :2026(langgraph dev;自身也挂载 dashboard app)
make dashboard             # 终端 2:dashboard 服务器 :8002(浏览器只连这个)
make ui                    # 终端 3:UI :3002(vite 反代 /dashboard/api → :8002)
```

端口拓扑:

```
浏览器 :3002 (UI)
   └─ /dashboard/api/** ──► :8002 (dashboard 服务器, agent.webapp:app)
                                └─ bearer+身份头 ──► :2026 (运行时, langgraph dev)
```

说明:

- 运行时 `:2026` 自身**也挂载**同一份 dashboard app(单进程形态:直接连 `:2026/dashboard/api` 也能用);
  推荐的浏览器入口是 `:8002`(dashboard 服务器),因为它不依赖运行时 worker 空闲。
- 端口可用环境变量覆盖:`RUNTIME_PORT=2030 DASHBOARD_PORT=8030 UI_PORT=3030 make runtime`(三个目标同理)。
- `make dev` = 同时起运行时 + dashboard;`make all` = 三个全起(前台)。
- 认证:浏览器侧用「共享 token + 用户名」登录换取 `osw_session` cookie;
  进程间用 `Authorization: Bearer <token>`(+ 可选 `X-OpenSWE-User`)。
- `.env` 优先于进程环境(app 启动时 `load_dotenv(override=True)`)。
