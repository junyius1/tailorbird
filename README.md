# Tailorbird

本地自托管的编程 agent 服务器(OpenSWE 的 **无 GitHub / 无云 / 纯本地** 变体)。

- **LangGraph 运行时** `:2024`:两张图(`agent` / `scheduler`),挂载 FastAPI dashboard,进程内 loopback 完成 webhook。
- **FastAPI dashboard** `agent.webapp:app`:浏览器侧 `/dashboard/api/**`(认证、线程、计划、调度、技能、指令、画像、健康)。
- **UI** `ui/`(Vite + TanStack Start + Nitro):本地 token 登录、线程界面、本地 git diff、懒加载推理,只反代 `/dashboard/api/**`。

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

## 运行

```bash
make runtime              # :2024 运行时(挂载 dashboard)
# 或双进程:
make dev                  # :2024 运行时 + :8001 dashboard
make ui                   # :3000 前端(代理 /dashboard/api → :2024)
```

> 本地开发若运行时不在默认 `:2024`,用 `LANGGRAPH_URL` 指向实际端口。
> 认证:浏览器侧用「共享 token + 用户名」登录换取 `osw_session` cookie;
> 进程间用 `Authorization: Bearer <token>`(+ 可选 `X-OpenSWE-User`)。
