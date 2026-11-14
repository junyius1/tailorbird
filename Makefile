# Tailorbird 自托管编程 agent 服务器:2026 运行时 + 8002 dashboard 服务器 + 3002 UI
#
# 双进程拓扑(推荐,与本 Makefile 对齐):
#   浏览器 :3002 (UI) --/dashboard/api--> :8002 (dashboard 服务器, agent.webapp:app)
#                                              --bearer+身份头--> :2026 (运行时, langgraph dev, 挂载同 app)
#   运行时 :2026 自身也挂载了同一份 dashboard app(单进程形态可用,同端口直连)。
#
# 端口可用环境变量覆盖:RUNTIME_PORT / DASHBOARD_PORT / UI_PORT。
PY := .venv/bin/python
LG := .venv/bin/langgraph

RUNTIME_PORT ?= 2026
DASHBOARD_PORT ?= 8002
UI_PORT ?= 3002

.PHONY: bootstrap install runtime dashboard ui dev all stop

## 引导 3partylibs 源码(钉死上游 commit;仓库内不提交,见 .gitignore)
bootstrap:
	bash scripts/bootstrap_3partylibs.sh

## 安装依赖(先 bootstrap 3partylibs 源码,再 uv sync editable 安装)
install: bootstrap
	uv sync

## 运行时:langgraph dev(:2026,inmem 运行时 + 两张图 + 挂载 dashboard app + 进程内 loopback webhook)
## 预置 LANGGRAPH_URL 指向自身端口:同进程 dashboard 代理必须打到同端口(否则 CLI 默认会指向 2024)
runtime:
	LANGGRAPH_URL=http://localhost:$(RUNTIME_PORT) LANGGRAPH_PORT=$(RUNTIME_PORT) $(LG) dev --no-reload --port $(RUNTIME_PORT)

## 自研运行时(替代 langgraph dev):独立 FastAPI 进程,读路径+store+认证
## 验证期用 RUNTIME_PORT=2027 与 :2026 并存做差分;Phase 3 切换后顶替 :2026
runtime-new:
	RUNTIME_PORT=$(RUNTIME_PORT) $(PY) -m agent.runtime

## dashboard 服务器:独立 uvicorn 进程(:8002),浏览器只连它;内部以复合 bearer 调运行时
## .env 由 app 自行 load_dotenv 加载;LANGGRAPH_URL 指向运行时端口
dashboard:
	LANGGRAPH_URL=http://localhost:$(RUNTIME_PORT) $(PY) -m uvicorn agent.webapp:app --port $(DASHBOARD_PORT)

## UI 开发服务器(:3002,经 vite 代理 /dashboard/api → dashboard 服务器 :8002)
ui:
	cd ui && DASHBOARD_API_URL=http://localhost:$(DASHBOARD_PORT) pnpm dev

## 同时起运行时 + dashboard(推荐;UI 另开终端 `make ui`)
dev:
	$(MAKE) runtime & $(MAKE) dashboard

## 三个进程全起(前台;Ctrl-C 仅停最后启动的,建议分开终端)
all:
	$(MAKE) runtime & $(MAKE) dashboard & $(MAKE) ui

stop:
	@echo "kill 各端口的 langgraph / uvicorn / vite 进程"
