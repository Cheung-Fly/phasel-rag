# ============================================================
#  rag-frontend —— Vue 3 构建 + nginx 托管（多阶段）
#
#  为什么用多阶段构建，而不是「宿主机先 npm run build，再挂载 dist」：
#    - 宿主机不需要装 Node，构建环境与运行环境一起被版本化在镜像里，
#      换机器、重装系统都不会出现「我这台能构建、你那台报错」；
#    - dist 被打进镜像，部署产物不可被意外修改（镜像层只读）；
#    - 一条命令完成「构建 + 上线」：docker compose up -d --build frontend
#
#  代价（必须知道）：改了前端源码必须重新 build 镜像才会生效，
#  不再有「改宿主机文件即时生效」的便利。
#  这是选择构建链的必然结果 —— 开发期的即时反馈由 Vite dev server
#  （npm run dev，带 HMR）承担，两者分工不同。
# ============================================================

# ---------------- 阶段 1：构建 ----------------
# 固定到 node:22-alpine 而不是 node:latest：
# 构建产物直接决定线上表现，基础镜像漂移会让「同一次提交在不同时间构建出
# 不同结果」，排查起来极其痛苦。
FROM node:22-alpine AS build

WORKDIR /app

# ★ 只拷 package.json —— **故意不拷 package-lock.json**。
#
#   原因是一个真实踩到的坑，不是偷懒：
#   lock 文件是在 Windows 上生成的，而 rollup 4 / esbuild 的原生加速包
#   按平台分包（@rollup/rollup-linux-x64-musl、@esbuild/linux-x64 等），
#   Windows 那次安装不会把 Linux 的原生包写进 lock 的包列表。
#   于是容器里即使 `npm ci` 报告成功，rollup 也找不到自己的原生模块，
#   直到真正跑 vite build 才抛：
#       Cannot find module '@rollup/rollup-linux-x64-musl'
#       npm has a bug related to optional dependencies (npm/cli#4828)
#   这类「安装成功、构建才炸」的错最难查，所以从源头避开。
#
#   不拷 lock 之后，npm install 会在容器内按当前平台（linux/musl）重新解析，
#   装的必然是正确的那一份。代价是失去了 lock 的严格版本锁定 ——
#   对只有 3 个直接依赖（vue / vite / @vitejs/plugin-vue）的本项目可以接受。
#   ★ 若将来确实需要可复现构建，正确做法是在 Linux 容器里生成 lock 再提交，
#     而不是把 Windows 生成的 lock 拿来复用。
COPY package.json ./

RUN npm install --no-audit --no-fund

# 再拷源码。这一步之后的层才会因源码变动而重建。
# 注意 .dockerignore 里排除了 node_modules 与 dist，所以这次 COPY
# 不会把宿主机的依赖或旧产物带进来。
COPY . .

# ★ 构建即是验证：rollup 原生包缺失、Vue 模板写错、import 路径不存在，
#   都会在这一步直接失败。宁可让 docker build 报错，
#   也不要产出一个「构建成功、打开却是白屏」的镜像。
RUN npm run build


# ---------------- 阶段 2：运行 ----------------
# 用 nginx-unprivileged 而不是官方 nginx：
#   官方镜像以 root 启动 master 进程（为了绑 80 端口、写 /var/run），
#   与本机「容器一律非 root」的约定冲突。unprivileged 变体以 uid 101
#   运行、默认监听 8080，无需任何额外权限。
FROM nginxinc/nginx-unprivileged:1.27-alpine

# 把构建产物放进 nginx 的站点根目录。
# 这里用 COPY --from 而不是挂载：产物属于镜像的一部分，随版本一起发布。
COPY --from=build /app/dist /usr/share/nginx/html

# 站点配置不在这里 COPY，而是由 compose 从 ./deploy/nginx-frontend.conf
# 只读挂载 —— 这样调缓存头、加一段 location 只需改文件 + nginx -s reload，
# 不必重新构建镜像。

EXPOSE 8080

# 不再需要 CMD：基础镜像自带的前台启动命令就是我们要的。
