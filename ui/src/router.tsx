// 路由器工厂:TanStack Start 约定(服务端/客户端各调一次)。
import { createRouter as createTanStackRouter } from "@tanstack/react-router"

import { makeQueryClient } from "./lib/query"
import { routeTree } from "./routeTree.gen"

// 创建路由(带 queryClient 上下文)
export function getRouter() {
  // TanStack Query 客户端(会话/线程数据源)
  const queryClient = makeQueryClient()
  // 路由实例
  const router = createTanStackRouter({
    routeTree,
    context: { queryClient },
    // 滚动位置恢复
    scrollRestoration: true,
    // 悬停即预载路由(体验优化)
    defaultPreload: "intent",
  })
  return router
}

// 类型注册:让 useRouter/useRoute 获得强类型
declare module "@tanstack/react-router" {
  interface Register {
    router: ReturnType<typeof getRouter>
  }
}
