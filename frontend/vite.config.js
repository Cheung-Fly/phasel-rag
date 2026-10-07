/* ============================================================
   vite.config.js —— 开发服务器 + 生产构建
   ------------------------------------------------------------
   一套配置同时服务两个阶段：

     npm run dev    开发：起一个带 HMR 的开发服务器（默认 5173），
                    并把 /api、/health 等后端路径代理到 FastAPI。
     npm run build  生产：把 src/ 打包成纯静态产物 dist/，
                    交给 nginx 托管（见 Dockerfile）。

   ★ 为什么开发期一定要用「代理」而不是让前端直连 8000：
     浏览器视角下 5173 与 8000 是「跨域」，直连就必须在后端配 CORS
     白名单、还可能有预检请求的坑。配了代理之后，浏览器眼里所有请求
     都是同源（都打到 5173），由 Vite 在 Node 侧转发 —— 于是开发期
     和生产期的「同源」拓扑完全一致，行为不会到上线才变。
     后端已有的 CORS_ORIGINS 因此只是一个可选的兜底，不是必需项。

   ★ 代理目标用环境变量 VITE_API_TARGET 覆盖：
     本机开发时后端多半跑在服务器上，需要一条 SSH 隧道把服务器的
     8000 映射到本地，然后：
         VITE_API_TARGET=http://127.0.0.1:8000 npm run dev
   ============================================================ */
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig(({ mode }) => {
  // 第三个参数传 '' 才能读到不带 VITE_ 前缀的变量；这里只需要 VITE_ 开头的，
  // 但显式写出来可以避免以后误以为「普通 .env 变量读不到」。
  const target = process.env.VITE_API_TARGET || 'http://127.0.0.1:8000'

  // 需要被转发到后端的路径前缀。改这里要同步改 nginx-frontend.conf，
  // 否则会出现「开发期好使、上线 404」这类只在生产暴露的问题。
  const backendPaths = ['/api', '/health', '/docs', '/openapi.json']

  const proxy = {}
  for (const p of backendPaths) {
    proxy[p] = {
      target,
      changeOrigin: true,
      // ★ SSE 必须不被中间层攒批。Vite 用的 http-proxy 默认不缓冲流式响应，
      //   这里显式禁掉压缩中间件的干扰项，避免逐字输出变成一次性吐出。
      headers: { Connection: 'keep-alive' },
    }
  }

  return {
    plugins: [vue()],

    server: {
      // 默认只监听 localhost。开发服务器没有任何鉴权，绑 0.0.0.0 等于把
      // 你本地的源码目录开放给同网段；需要从别的机器访问时显式设
      // VITE_DEV_HOST=0.0.0.0 并自己想清楚后果，再用 SSH 隧道更稳妥。
      host: process.env.VITE_DEV_HOST || 'localhost',
      port: 5173,
      // 端口被占用时直接报错退出，而不是「悄悄换到 5174」——
      // 后者会让浏览器书签、前端里写死的地址全部失效，且不易察觉。
      strictPort: true,
      proxy,
    },

    build: {
      outDir: 'dist',
      // 每次构建前清空 dist，避免上一版残留文件被一起发出去
      emptyOutDir: true,
      // 生产不开 sourcemap：产物部署在公网可达的静态目录，
      // sourcemap 会把源码结构一并公开，对教学项目也无必要。
      sourcemap: false,
      chunkSizeWarningLimit: 800,
    },
  }
})
