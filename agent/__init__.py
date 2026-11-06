"""Tailorbird 自托管编程 agent 服务器(无 GitHub 变体)。

依据 open-swe 设计书实现的三个面:
- 2024 运行时(`langgraph dev`,inmem 运行时 + agent/scheduler 两图 + 挂载的 dashboard app);
- dashboard 服务器(`agent.webapp:app`,可同进程挂 :2024,也可独立 :8001);
- 本地 git 分支提交交付(沙箱 = 本地 Docker 容器,不持有任何 GitHub/云凭据)。
"""
