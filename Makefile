# Tailorbird 自托管编程 agent 服务器:2024 运行时 + dashboard 服务器 + UI
PY := .venv/bin/python
LG := .venv/bin/langgraph

.PHONY: bootstrap install runtime dashboard ui dev stop

## 引导 3partylibs 源码(钉死上游 commit;仓库内不提交,见 .gitignore)
bootstrap:
	bash scripts/bootstrap_3partylibs.sh

## 安装依赖(先 bootstrap 3partylibs 源码,再 uv sync editable 安装)
install: bootstrap
	uv sync

## 2024 运行时:langgraph dev(inmem 运行时 + 两张图 + 挂载 dashboard app + 进程内 loopback webhook)
runtime:
	$(LG) dev --no-reload --port 2024

## dashboard 服务器:双进程拓扑下独立于运行时的 uvicorn 进程(:8001)
dashboard:
	.venv/bin/uvicorn agent.webapp:app --port 8001

## UI 开发服务器(:3000,经 vite 代理 /dashboard/api → :2024)
ui:
	cd ui && pnpm dev

## 同时起 2024 运行时 + :8001 dashboard
dev:
	$(MAKE) runtime & $(MAKE) dashboard
