// 本地(桌面)线程路由:本地变体无桌面运行时 → 恒跳 /agents。
import { Navigate, createFileRoute } from "@tanstack/react-router"

export const Route = createFileRoute("/agents/local/$sessionId")({
  component: LocalAgentThreadPage,
})

function LocalAgentThreadPage() {
  return <Navigate to="/agents" />
}
