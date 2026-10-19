# 个人 AI 助手（自部署）

部署在你自己服务器上的个人助手：通过网页对话，Agent 可以在隔离环境里执行终端命令、操作浏览器、检索知识库，并能加载 Skill 和调用 MCP 工具。模型由你自己选择并可随时切换。

## 能力

| 能力 | 说明 |
| --- | --- |
| 对话与知识库 | RAG 检索、会话记忆，支持 PDF、Markdown、TXT、HTML、Word、PowerPoint、Excel 文档上传入库 |
| 终端 | 独立非特权容器，无 Docker socket；只读命令直接执行，危险命令需审批；容器可联网，带 Node，仅允许 `npx skills`（查询直接执行，安装/更新需审批） |
| 浏览器 | 独立容器运行 agent-browser，按用户隔离会话；验证码、滑块、短信验证由你本人完成，不绕过 |
| Skill | 把 `skills/<name>/SKILL.md` 作为工作流说明加载 |
| MCP | 支持 Streamable HTTP 和 stdio；配置按用户加密保存；只读工具自动调用，变更类工具需审批 |
| 模型 | 前端添加多个 OpenAI 兼容端点，一键切换，密钥加密保存 |
| 审批与审计 | 所有会改变外部状态的操作走统一审批状态机，并留有审计时间线 |
| 运维 | `/health`、`/metrics`（Prometheus）、API 限流、浏览器/MCP 熔断、CI |

## 快速开始

要求：Docker 与 Docker Compose。

```bash
cp .env.example .env && chmod 600 .env
# 编辑 .env，至少填写：
#   POSTGRES_PASSWORD            强口令（16 位以上）
#   CREDENTIAL_ENCRYPTION_KEY    python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
#   LLM_API_KEY / LLM_BASE_URL / LLM_MODEL   任意 OpenAI 兼容端点（也可启动后在前端“模型”面板添加）

bash scripts/preflight.sh      # 启动前安全检查
docker compose up -d --build
```

打开 <http://127.0.0.1:8080>。

> `CREDENTIAL_ENCRYPTION_KEY` 请离线备份：它加密凭证、MCP 配置和模型密钥，丢失后这些数据无法恢复。

## 安全模型（请先读）

- 默认 `AUTH_MODE=local`：**没有登录**，所有请求都视为同一个主人。所有端口只绑定 `127.0.0.1`，这是它安全的前提。
- 远程访问自己的服务器：设 `AUTH_MODE=password`、`ACCESS_PASSWORD`（12 位以上）、`AUTH_COOKIE_SECURE=true`，再设 `PUBLIC_DOMAIN`（域名已解析到本机）并执行 `docker compose --profile https up -d caddy`，Caddy 会自动签发 HTTPS 证书。打开网址输入口令即可；改口令后重启 api，旧登录自动失效。口令模式下登录失败有限流。
- 也可以继续用 SSH 隧道；不要把 `AUTH_MODE=local` 暴露到公网。
- 多人使用请改 `AUTH_MODE=session` 并设置 `AUTH_COOKIE_SECURE=true`。
- 不要把端口改成 `0.0.0.0` 暴露到公网。
- Agent 读取的网页内容和 MCP 返回内容都被视为不可信数据，不会作为指令执行。
- 密码、Cookie、Token 不写入日志、聊天历史或向量库。
- 变更类操作（危险命令、危险点击、变更类 MCP 工具）必须你在界面上批准，批准有时效且只能用一次。

## 使用

- 普通对话：直接提问。
- 使用 MCP：`/mcp 查询当前状态`；使用 Skill：`/skill 按技能完成任务`。
- 前端侧栏的“模型”面板管理模型档案；“扩展能力”面板管理 Skill 与 MCP。
- 可选私有网页搜索：在 `.env` 设置随机 `SEARXNG_SECRET`，运行 `docker compose --profile web-search up -d searxng mcp-searxng`，再在“扩展能力 → MCP”添加 `http://mcp-searxng:3000/mcp` 并测试。之后用 `/mcp 搜索……` 调用。服务不发布宿主机端口；搜索词会发送给 SearXNG 使用的上游搜索引擎，请只搜索可外发的信息。

## 配置

- 环境变量说明见 [.env.example](.env.example)。
- `ENABLED_PLUGINS=campus` 加载示例校园站点；留空即为通用助手。

### 部署成通用助手

默认是个人知识库（个人 RAG）。想换成别的场景，在 `.env` 里改这几项后重启 `api`：

| 变量 | 默认 | 作用 |
|---|---|---|
| `ENABLED_PLUGINS` | `campus` | 留空则不预置任何站点 |
| `ASSISTANT_DOMAIN` | `个人` | 写进知识库回答的提示词，如“公司”“产品” |
| `KNOWLEDGE_TAB_LABEL` | `个人RAG` | 聊天顶部知识库栏目的名字 |
| `ASSISTANT_TAB_LABEL` | `个人助手` | 聊天顶部助手栏目的名字 |

说明：
- `ASSISTANT_DOMAIN` 只保留文字、数字、连字符和空格，最长 20 个字；栏目名最长 12 个字。
- 关闭插件只影响新库的预置内容。已经写入数据库的站点是用户数据，不会被自动删除，请在“站点”面板里手动删。
- Skill 放在 `skills/<名称>/SKILL.md`；MCP 可在侧栏“扩展能力”面板添加（Streamable HTTP），或用 `MCP_SERVERS_JSON` 配置（含 stdio）。
- 可选搜索集成使用独立容器 [SearXNG](https://github.com/searxng/searxng)（AGPL-3.0）与 [mcp-searxng](https://github.com/ihor-sokoliuk/mcp-searxng)（MIT）；不启用 `web-search` profile 时不会启动或改变默认对话行为。
- Word、PowerPoint、Excel 文件由 [Microsoft MarkItDown](https://github.com/microsoft/markitdown)（MIT）转换为 Markdown 后入库；转换插件关闭，仅处理上传的本地文件。Office 文件解压体积上限为 100MB。
- 已保存的 HTML 网页由 [Trafilatura](https://github.com/adbar/trafilatura)（Apache-2.0）提取正文后入库；只解析用户上传的本地文件，不主动抓取 URL。

## 开发与测试

```bash
docker compose run --rm --no-deps -v "$PWD/backend/tests:/tmp/tests:ro" api \
  sh -lc 'PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s /tmp/tests -p "test_*.py"'
cd frontend && npm ci && npm run build
```

## 已知限制

- 模型切换目前只对单用户本地模式生效；后台 worker 仍使用环境变量中的模型。
- 模型端点必须是 HTTPS；本地 llama.cpp 这类 HTTP 端点只能通过环境变量配置。
- 变更类 MCP 工具按名称和描述中的关键词判断，不是形式化的安全模型，请只连接你信任的 MCP 服务。
- stdio MCP 在 API 容器内运行，只继承少量白名单环境变量。

## 许可证

本项目使用 [MIT 许可证](./LICENSE)。
