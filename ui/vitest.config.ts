import { defineConfig } from "vitest/config"
import react from "@vitejs/plugin-react"

// 测试配置:jsdom 环境(React 组件测试)。
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": new URL("./src", import.meta.url).pathname,
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./vitest.setup.ts"],
  },
})
