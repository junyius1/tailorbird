// 生产构建:/dashboard/api/** 反代到运行时(同 origin 保持,cookie 可用)。
// 目标地址由运行时环境变量 DASHBOARD_API_URL 决定(部署属性,不写死)。
import { proxyRequest } from "h3"

// 后端 origin(按请求读取;未配置 → 502)
function backendOrigin(): string {
  const configured = (process.env.DASHBOARD_API_URL ?? "http://localhost:8002").replace(/\/$/, "")
  return configured
}

// 反代处理器:透传方法/头/body/cookie,流式回传(SSE 可用)。
export default async function backendProxy(
  event: Parameters<typeof proxyRequest>[0]
) {
  const url = new URL(event.req.url ?? "/dashboard/api", "http://localhost")
  return proxyRequest(event, `${backendOrigin()}${url.pathname}${url.search}`, {
    // manual:3xx 原样回给浏览器,不自动跟随
    fetchOptions: { redirect: "manual" },
  })
}
