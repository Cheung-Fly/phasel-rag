# 前端（Vue 3 + Vite）

阶段一 RAG 知识库问答的浏览器端。**与 FastAPI 后端是两个完全独立的进程**：
后端只提供 `/api`、`/health` 这些接口，不再负责发送任何 HTML/CSS/JS。

## 目录结构

```
frontend/
├── index.html              入口 HTML（含防主题闪烁的内联脚本）
├── vite.config.js          开发代理 + 生产构建配置
├── Dockerfile              多阶段：node 构建 → nginx 托管
├── package.json
└── src/
    ├── main.js             挂载入口
    ├── App.vue             布局外壳 + 应用级初始化
    ├── components/         视图层（纯结构，不含业务逻辑）
    │   ├── AppSidebar.vue      左侧栏容器
    │   ├── BrandBar.vue        标题 + 主题按钮
    │   ├── UploadPanel.vue     上传区（点击 / 拖拽）
    │   ├── DocumentList.vue    已入库文档
    │   ├── SessionList.vue     会话历史
    │   ├── HealthStatus.vue    后端状态
    │   ├── ChatPanel.vue       对话区容器
    │   ├── MessageList.vue     消息滚动区（自动滚动策略在这里）
    │   ├── MessageBubble.vue   单条消息
    │   ├── SourceList.vue      引用片段
    │   ├── ChatComposer.vue    输入区
    │   └── ToastBar.vue        全局提示
    ├── composables/        状态与业务逻辑（无 DOM 依赖，除 useComposer）
    │   ├── useChat.js          消息列表 + 流式问答
    │   ├── useSessions.js      会话列表 + 当前会话
    │   ├── useDocuments.js     文档上传 / 列表 / 删除
    │   ├── useHealth.js        健康状态 + 后端下发的运行参数
    │   ├── useTheme.js         主题三态
    │   ├── useToast.js         轻提示
    │   └── useComposer.js      「请聚焦输入框」信号
    ├── lib/                与框架无关的工具层
    │   ├── markdown.js         Markdown 渲染（自研，先转义后解析）
    │   ├── sse.js              SSE 流式客户端（fetch + ReadableStream）
    │   ├── api.js              后端接口的唯一出口
    │   └── storage.js          localStorage 读写 + 会话索引校正
    └── styles/main.css     全局样式（配色变量 / 布局 / 断点）
```

## 开发

```bash
cd frontend
npm install
npm run dev          # http://localhost:5173
```

开发服务器会把 `/api`、`/health`、`/docs`、`/openapi.json` 代理到后端，
所以浏览器眼里所有请求都是同源，**不需要配 CORS**。

后端默认目标地址是 `http://127.0.0.1:8000`。如果后端跑在服务器上
（本项目的常态），先开一条隧道，再启动开发服务器：

```bash
# 另开一个终端，把服务器的 8000 映射到本地
ssh -i <密钥> -L 8000:127.0.0.1:8000 azureuser@<服务器IP> -N

# 然后（默认目标就是 127.0.0.1:8000，通常无需额外设置）
npm run dev
```

要指向别的地址：

```bash
VITE_API_TARGET=http://192.168.1.20:8000 npm run dev
```

## 生产构建

```bash
npm run build        # 产出 dist/
npm run preview      # 本地预览 dist/（不含后端代理，仅供看界面）
```

线上实际由 `frontend/Dockerfile` 多阶段构建：

```bash
docker compose up -d --build frontend
```

产物打进镜像，由 nginx 托管静态文件并把 `/api` 反代回 api 容器。
站点配置在 `deploy/nginx-frontend.conf`（运行时挂载，改完
`docker compose exec frontend nginx -s reload` 即可生效，无需重建镜像）。

## 两条必须记住的约定

1. **改前端源码后，宿主机上的文件不会自动生效。** 生产环境跑的是
   镜像里的构建产物，必须 `--build` 重建。开发期的即时反馈靠
   `npm run dev` 的 HMR，两者分工不同。
2. **所有后端调用都用相对路径**（`/api/...`），由代理层转发。
   不要在代码里写死 `http://某主机:8000`，否则开发 / 生产两套地址
   会分叉，且会重新引入跨域问题。

## 一个必须知道的构建坑（已规避，但别再踩回去）

`Dockerfile` 里**故意只 `COPY package.json`，不拷 `package-lock.json`**。

原因：本机的 lock 文件是在 Windows 上生成的，而 rollup 4 / esbuild 的
原生加速包是按平台分包的（`@rollup/rollup-linux-x64-musl`、
`@esbuild/linux-x64` 等）。Windows 那次安装不会把 Linux 的原生包写进
lock 的包列表，于是容器里 `npm ci` 会**报告成功**，但 rollup 找不到
原生模块，直到真正执行 `vite build` 才抛：

```
Error: Cannot find module '@rollup/rollup-linux-x64-musl'
npm has a bug related to optional dependencies (npm/cli#4828)
```

这是「安装成功、构建才炸」的典型，很难查。不拷 lock 让 `npm install`
在容器内按 linux/musl 重新解析，装的必然是正确的那一份。

代价是失去 lock 的严格版本锁定 —— 本项目只有 3 个直接依赖，可以接受。
若将来确实需要可复现构建，正确做法是**在 Linux 容器里生成 lock 再提交**，
而不是把 Windows 生成的 lock 拿来复用。
