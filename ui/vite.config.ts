import { defineConfig } from "vite"
import { tanstackStart } from "@tanstack/react-start/plugin/vite"
import viteReact from "@vitejs/plugin-react"
import { nitro } from "nitro/vite"

// 浏览器只访问 /dashboard/api/**;该前缀反代到 dashboard 服务器(缺省 :8002)。
// 同 origin 保持 → osw_session cookie 天然可用,UI 不手填 Authorization。
const API_PREFIX = "/dashboard/api"
const DASHBOARD_API_URL = process.env.DASHBOARD_API_URL ?? "http://localhost:8002"
const IS_PRODUCTION = process.env.NODE_ENV === "production"

export default defineConfig({
  base: "/",
  resolve: { tsconfigPaths: true },
  plugins: [
    nitro({
      // dev:routeRule 反代(localhost 缺省);prod:显式 handler 反代
      routeRules: IS_PRODUCTION
        ? {}
        : {
            [`${API_PREFIX}/**`]: {
              proxy: {
                to: `${DASHBOARD_API_URL}${API_PREFIX}/**`,
                fetchOptions: { redirect: "manual" as const },
              },
            },
          },
      handlers: IS_PRODUCTION
        ? [
            {
              route: `${API_PREFIX}/**`,
              handler: "./server/backend-proxy.ts",
            },
          ]
        : [],
      // 服务端单 chunk:避免 CommonJS 依赖循环跨 chunk 初始化失败
      inlineDynamicImports: true,
    }),
    tanstackStart(),
    viteReact(),
  ],
})
