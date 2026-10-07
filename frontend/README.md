# frontend/ —— 前端（可独立替换）

纯 HTML/CSS/JS，无构建步骤。被 api 容器只读挂载到 `/app/frontend`（见 `docker-compose.yml`），
由 `app/main.py` 挂到 `/ui`；若启用了 nginx 服务，则同时由 nginx 在 `127.0.0.1:8080` 独立托管。

## 文件

| 文件 | 作用 |
|---|---|
| `index.html` | 页面骨架 |
| `style.css` | 样式（含明/暗主题变量） |
| `markdown.js` | 把模型返回的 Markdown 渲染成 HTML（先转义再渲染，防 XSS） |
| `sse.js` | 流式请求封装（POST + SSE 解析） |
| `store.js` | localStorage：会话索引、当前会话、主题偏好 |
| `app.js` | 主逻辑（编排以上模块） |

## 怎么改（不需要重建任何镜像）

```bash
cd ~/phase1-rag/frontend
nano style.css          # 改完直接刷新浏览器即可
```

api 容器是**只读绑定挂载**这个目录，所以宿主机一改、下一次请求就是新内容 ——
不用 `docker compose build`，也不用重启容器。

刷新页面看到旧内容时，先确认不是浏览器缓存：`Ctrl+Shift+R` 强刷。

## 怎么整体替换（例如换成 Vue3 构建产物）

1. 构建出 `dist/`；
2. 把 `dist/` 里的内容覆盖到本目录（保留原文件备份）；
3. 确认入口是 `index.html`（`_resolve_frontend_dir()` 以此为判据）；
4. 若用了 nginx，`docker compose exec frontend nginx -s reload`。

前端调用的接口全部是**根相对路径**（`/api/...`、`/health`），
所以只要保证前端与 api 同源即可，不需要改任何接口地址、也不需要配 CORS。
若将来前端单独部署到别的域名，才需要在 `.env` 里设 `CORS_ORIGINS`。

## 硬性约定

- **接口地址一律用根相对路径**，不要写 `http://20.89.90.58:8000` 这类绝对地址 ——
  改端口或加反代时会全线失效。
- **不要在前端写死后端配置值**（上传上限、切片上限、模型名…）。
  这些应通过 `GET /health` 读取，例：`max_upload_mb`、`ingest_max_chunks`、
  `llm_model`、`memory_backend`。写死的后果是界面显示假信息
  （本次就修过一处「界面说 20MB、后端其实收 50MB」）。
- 容器内本目录是 `Read-only file system`，前端**不能**做任何本地文件写入。
