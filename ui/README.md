# Tailorbird UI

Vite + TanStack Start(React 19 + Nitro SSR)+ TanStack Router/Query 的本地自托管前端。

- **只反代 `/dashboard/api/**`**:浏览器与 UI 同 origin,`osw_session` cookie 天然可用,
  不手填 `Authorization`,身份全部来自会话 cookie。
- **本地 token 登录**:`username` + 共享 token → `osw_session`。
- **线程界面**:对话(轮询 dashboard state)、输入框(busy 入队 / idle run.start)、
  本地 git diff 面板(`local/<slug>` 分支 + working-tree diff)。
- **推理懒加载**:不在打开线程时拉历史推理;每条 AI 消息旁「▸ 推理」按需调
  `GET /dashboard/api/threads/{id}/reasoning/{message_id}`。
- **无 GitHub / PR / 云 / 连接** 等参考实现的表面。

## 开发

```bash
pnpm install
pnpm dev            # :3002(make ui 注入 --port;vite 代理 /dashboard/api → DASHBOARD_API_URL,缺省 :8002)
```

dashboard 服务器不在默认端口时:

```bash
DASHBOARD_API_URL=http://localhost:8002 pnpm dev --port 3002
```

## 校验

```bash
pnpm typecheck      # tsc --noEmit
pnpm test           # vitest(run)
pnpm build          # 生产构建(nitro node-server)
```
