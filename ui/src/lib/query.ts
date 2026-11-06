// TanStack Query 客户端工厂(SSR/客户端各建一个,dehydrated 状态由 Start 注入)。
import { QueryClient } from "@tanstack/react-query"

// 服务端/客户端共用的 queryClient;SSR 下请求经 /dashboard/api 同 origin 代理。
export function makeQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: {
        // 会话等基础数据:短 stale 时间,页面切换时刷新
        staleTime: 5_000,
        retry: 1,
      },
    },
  })
}
