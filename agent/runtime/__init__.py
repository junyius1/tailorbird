"""自研运行时(逐步替换 ``langgraph_api`` + ``langgraph_runtime_inmem``)。

Phase 1(A+B+D):读路径(threads/state/history)+ 存储(store/thread 注册表)
+ 认证 + app 形态。Phase 2(C):V2 run 引擎(commands/stream/events)。
"""
