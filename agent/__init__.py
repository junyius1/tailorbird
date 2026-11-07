"""Tailorbird 自托管编程 agent 服务器(无 GitHub 变体)。

依据 open-swe 设计书实现的三个面:
 - 运行时(缺省 :2026)(`langgraph dev` :2026,inmem 运行时 + agent/scheduler 两图 + 挂载的 dashboard app);
- dashboard 服务器(`agent.webapp:app`,可同进程挂运行时(缺省 :2026),也可独立 :8002);
- 本地 git 分支提交交付(沙箱 = 本地 Docker 容器,不持有任何 GitHub/云凭据)。
"""
