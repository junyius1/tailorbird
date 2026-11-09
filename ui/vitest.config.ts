import { defineConfig } from "vitest/config"
import { tanstackStart } from "@tanstack/react-start/plugin/vite"
import viteReact from "@vitejs/plugin-react"

// 测试配置:jsdom 环境(React 组件测试)。
// tanstackStart 必须保留:它把 server-function 调用改写成客户端 fetch,
// 否则 createIsomorphicFn 解析到 server 变体,agents api client 在测试里
// 拿到 undefined origin 或直接抛 StartEvent 错误。
export default defineConfig({
  plugins: [tanstackStart(), viteReact()],
  resolve: {
    alias: {
      "@": new URL("./src", import.meta.url).pathname,
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    // jsdom 对 opaque origin(未设 url)拒绝 localStorage,必须给显式 origin。
    environmentOptions: { jsdom: { url: "http://localhost:3000" } },
    setupFiles: ["./vitest.setup.ts"],
  },
})
