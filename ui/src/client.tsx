// 客户端入口:hydrate(与参考一致,SSR 渲染后接管)。
import { StartClient } from "@tanstack/react-start/client"
import { hydrateRoot } from "react-dom/client"

hydrateRoot(document, <StartClient />)
