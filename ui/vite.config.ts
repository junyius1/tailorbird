import { defineConfig } from "vite"
import { tanstackStart } from "@tanstack/react-start/plugin/vite"
import viteReact from "@vitejs/plugin-react"
import tailwindcss from "@tailwindcss/vite"
import { nitro } from "nitro/vite"

// Browser `/dashboard/api/*` calls are proxied to the tailorbird dashboard
// (:8002) by the server so the session cookie stays same-origin. `/dashboard/api`
// is the only prefix a deployed dashboard serves (protocol proxy lives behind it).
const BACKEND_PREFIXES = ["/dashboard/api"]

const IS_PRODUCTION = process.env.NODE_ENV === "production"

// shiki lazily imports one grammar per language on first render; these libs only
// live inside lazy route components, so Vite's startup scanner misses them.
// Pre-bundling avoids a first-navigation re-optimize + forced reload.
const SHIKI_LANGS = [
  "bash",
  "c",
  "cpp",
  "csharp",
  "css",
  "diff",
  "docker",
  "go",
  "graphql",
  "html",
  "java",
  "javascript",
  "json",
  "jsonc",
  "jsx",
  "kotlin",
  "lua",
  "make",
  "markdown",
  "php",
  "python",
  "ruby",
  "rust",
  "scala",
  "shellscript",
  "sql",
  "swift",
  "toml",
  "tsx",
  "typescript",
  "xml",
  "yaml",
]

const devRouteRules = IS_PRODUCTION
  ? {}
  : Object.fromEntries(
      BACKEND_PREFIXES.map((prefix) => [
        `${prefix}/**`,
        {
          proxy: {
            to: `${process.env.DASHBOARD_API_URL ?? "http://localhost:8002"}${prefix}/**`,
            fetchOptions: { redirect: "manual" as const },
          },
        },
      ])
    )

const config = defineConfig({
  base: "/",
  legacy: { skipWebSocketTokenCheck: true },
  resolve: { tsconfigPaths: true },
  optimizeDeps: {
    include: [
      "streamdown",
      "shiki",
      "@pierre/diffs",
      "@pierre/diffs/react",
      "@pierre/trees",
      "@pierre/trees/react",
      "@shikijs/themes/github-light",
      "@shikijs/themes/github-dark",
      ...SHIKI_LANGS.map((lang) => `@shikijs/langs/${lang}`),
    ],
  },
  worker: { format: "es" },
  plugins: [
    nitro({
      routeRules: devRouteRules,
      handlers: IS_PRODUCTION
        ? BACKEND_PREFIXES.map((prefix) => ({
            route: `${prefix}/**`,
            handler: "./server/backend-proxy.ts",
          }))
        : [],
      inlineDynamicImports: true,
    }),
    tailwindcss(),
    tanstackStart(),
    viteReact({ compiler: true }),
  ],
})

export default config
