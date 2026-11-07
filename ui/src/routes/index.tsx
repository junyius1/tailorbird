// 根路由:访问 / 直接重定向到 /agents(真正的首页;未登录时登录门再转 /login)。
import { createFileRoute, redirect } from "@tanstack/react-router"

export const Route = createFileRoute("/")({
  beforeLoad: () => {
    throw redirect({ to: "/agents" })
  },
})
