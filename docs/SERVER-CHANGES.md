# 服务器变更记录（SERVER CHANGES）

> 记录时间：2026-10-07 17:20 – 17:45（Asia/Shanghai）
> 目标机：`rag-vm` / `azureuser@20.89.90.58`
> 机型：Azure **Standard_B2as_v2** — 2 vCPU / 8 GiB / CPU 基线 40% / japaneast / AMD EPYC 7763
> 系统：Ubuntu 24.04.5 LTS，Docker 29.1.3 + Compose v2
> 记录人：WorkBuddy（受用户委托执行）
>
> 本文档用途：说明**改了什么、为什么这么改、怎么回滚**。所有改动前状态已快照留档，
> 见 §1。未执行项与待决策项见 §5。

---

## 0. 总览

| # | 变更 | 风险 | 结果 |
|---|---|---|---|
| 1 | 容器内存上限 `600m/1600m` → `4g/5g` | 低 | 已生效，容器 healthy |
| 2 | 新增全局 Docker 日志上限 `/etc/docker/daemon.json` | 低 | 已生效，docker 已重启 |
| 3 | `vm.swappiness` 60 → 10（持久化） | 低 | 已生效 |
| 4 | 时区 `Etc/UTC` → `Asia/Shanghai` | 低 | 已生效 |
| 5 | CORS `["*"]` → 环境变量白名单（默认仅同源） | 中 | 已生效，需重建镜像 |
| 6 | 上传上限 `20MB` → `50MB`（可配置） | 中 | 已生效 |
| 7 | 知识库数据每日备份 + cron | 低 | 已部署并首跑成功 |
| 8 | 磁盘回收 | 低 | 实际几乎无可回收（见 §6 更正） |
| 9 | SSH 加固 | — | **检查后确认无需改动**（见 §3） |

配套的代码改动（CORS / 上传上限 / 过时注释）已同步进仓库，见 §7 的提交。

---

## 1. 改动前留档（回滚依据）

路径：`~/phase1-rag/data/_backup_20261007/pre-change/`

```
docker-compose.yml.bak     改动前的编排文件
main.py.bak                改动前的应用入口
Dockerfile.bak             改动前的镜像定义
env.bak                    改动前的环境变量（含密钥，权限 600）
sshd-effective.txt         改动前 sshd 生效配置
sysctl-values.txt          改动前内核参数
timedatectl.txt            改动前时区
docker-version.txt / docker-ps.txt / docker-images.txt
rag-api-inspect.json       改动前容器完整 inspect
free-before.txt            改动前内存/磁盘
docker-system-df-before.txt 改动前 Docker 空间占用
```

另有历史遗留文件备份目录：`~/phase1-rag/data/_backup_20261007/`（含知识库被替换的测试文档 `9ac9cb801e8a.md` 等）。
镜像回滚点：`phase1-rag-api:0.1.0-backup-20261007`。

---

## 2. 逐条变更

### 2.1 容器内存上限 600m → 4g

**改前**：`docker-compose.yml` 里写死 `mem_limit: 600m` / `memswap_limit: 1600m`，
注释说明是"1 GiB 机器时代的保命配置"（系统常驻 250MB，剩 600MB 给业务）。

**为什么改**：机型已由 `B2ts_v2`（2 vCPU / **1 GiB**）升级为 `B2as_v2`（2 vCPU / **8 GiB**），
但编排文件没跟着调整。结果是**在 8 GiB 的机器上只敢用 600MB**：
解析稍大的 PDF 或并发问答就会触发 OOM Kill（容器退出码 137），
属于纯粹的自我设限，且故障表现（进程被杀）容易被误判为代码 bug。

**怎么改**：`mem_limit: 4g`（约占机器一半），`memswap_limit: 5g`；
同时把文件头部和内存段的过时注释（B2ts_v2 / 1 GiB / 20% 基线）一并订正为实况。

**为什么是 4g 而不是更大**：留一半内存给宿主 OS、Docker daemon、页缓存；
单容器吃满 8 GiB 会把宿主拖进 swap，反而更糟。

**验证**：
```
MemLimit=4294967296  MemSwap=5368709120   # 即 4 GiB / 5 GiB
rag-api  Up (healthy)
```

**回滚**：`cp data/_backup_20261007/pre-change/docker-compose.yml.bak docker-compose.yml && docker compose up -d`

---

### 2.2 新增全局 Docker 日志上限

**改前**：`/etc/docker/daemon.json` **不存在**（连 `/etc/docker` 目录都没有）。
`rag-api` 自身在 compose 里设了 `max-size: 10m / max-file: 3`，
但**这是按容器单独生效的**。

**为什么改**：阶段二/三还会起 Redis、Chroma、Agent 等新容器。
没有 daemon 级默认值，这些容器会以 `json-file` 无上限写日志 ——
一个死循环打日志的服务能在几天内把 29 GB 系统盘写满，
而磁盘写满会连带 SQLite/Chroma 写失败，故障面远大于日志本身。

**怎么改**：新建 `/etc/docker/daemon.json`：
```json
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "10m", "max-file": "3" }
}
```
写入后先 `python3 -m json.tool` 校验 JSON 再重启 docker
（daemon.json 写坏会导致 Docker 起不来，必须先校验）。

**为什么需要重启而不是 reload**：`log-opts` 不属于 Docker 支持热重载的配置项，
必须重启 daemon。重启期间 `rag-api` 因 `restart: unless-stopped` 自动恢复，实测无需人工介入。

**验证**：
```
docker info → LoggingDriver: json-file
新起容器 inspect → {"Type":"json-file","Config":{"max-file":"3","max-size":"10m"}}
重启后 rag-api 自动恢复：Up (healthy)
```

**回滚**：`sudo rm /etc/docker/daemon.json && sudo systemctl restart docker`

---

### 2.3 vm.swappiness 60 → 10

**为什么改**：`swappiness=60` 是桌面发行版默认值，倾向把匿名页换出到 swap。
这台机器上最热的数据是 Chroma 索引和 Python 堆 —— 被换出后每次检索都要从磁盘换回，
表现为**响应延迟毫无规律地抖动**，而且 `free` 看起来"内存很空"（都进了 swap），非常难排查。
内存已有 8 GiB，swap 应当只作极端兜底。

**怎么改**：写入 `/etc/sysctl.d/99-rag-tuning.conf`（而非直接改 `/etc/sysctl.conf`，
便于日后识别是本次调优引入的），再 `sysctl --system` 生效。

**验证**：`sysctl vm.swappiness` → `10`，`/proc/sys/vm/swappiness` → `10`

**回滚**：`sudo rm /etc/sysctl.d/99-rag-tuning.conf && sudo sysctl --system`

---

### 2.4 时区 UTC → Asia/Shanghai

**为什么改**：此前 `docker logs` 的时间戳比北京时间**快 8 小时**（UTC），
排查问题时需要心算换算，容易把"刚刚发生的错误"看成"8 小时前的旧错误"。

**验证**：`timedatectl` → `Time zone: Asia/Shanghai (CST, +0800)`

**回滚**：`sudo timedatectl set-timezone Etc/UTC`

---

### 2.5 CORS 白名单：`["*"]` → 环境变量驱动

**改前**：`backend/app/main.py` 里 `allow_origins=["*"]`。

**为什么改**：`allow_origins=["*"]` 意味着**任意网站**都能用访客的浏览器
跨域调用这个 API（例如访客在浏览恶意页面时，该页面顺手把本 API 的问答结果读走）。
服务当前只监听 `127.0.0.1`，风险被"没有公网入口"挡住了；
但这层保护是**外部条件**，一旦挂 Nginx / 开公网就立刻失效 ——
安全设置不应该依赖"没人能访问到"。

**为什么可以安全收紧**：前端页面由本服务自己托管（`/ui`），与 API **同源**，
同源请求根本不走 CORS 检查。因此白名单留空不影响正常使用。

**怎么改**：新增配置项 `CORS_ORIGINS`（逗号分隔），
`config.py` 提供 `cors_origin_list` 属性做解析，`main.py` 读取它。
`CORS_ORIGINS` 留空 = 不允许任何跨域。

**验证**：
```
带 Origin: https://evil.example.com  → 响应中 Access-Control-Allow-Origin 数量 = 0  ✓
不带 Origin 的正常请求               → HTTP 200                                     ✓
```

**回滚**：`git revert` 对应提交，或恢复 `pre-change/main.py.bak` 后重建镜像。

---

### 2.6 上传上限 20MB → 50MB（可配置）

**改前**：`MAX_UPLOAD_BYTES = 20 * 1024 * 1024`，注释写明"1 GiB 机器解析大 PDF 会吃满内存"。

**为什么改**：这个 20MB 同样是按 1 GiB 机型定的。容器内存已放宽到 4 GiB，
20MB 的上限开始变成正常使用中的阻碍（一份带图的 PDF 报告很容易超过）。

**为什么不是设很大**：PDF 解析是 CPU + 内存双高，本机只有 2 vCPU 且 CPU 额度非无限。
50MB 是"够用且仍安全"的折中，且改为环境变量 `MAX_UPLOAD_MB` 后可按需调整而不必改代码。

**验证**：
```
上传 51MB 文件 → HTTP 413 {"detail":"文件超过 50MB 限制"}   ✓（在解析前拦截，无额外开销）
```

---

### 2.7 知识库数据每日备份

**为什么改**：`data/chroma`（向量库）与 `data/uploads`（原始文档）是**唯一不可再生的数据** ——
代码在 GitHub 上有，数据没有。此前**零备份**，一次误删或磁盘故障即彻底丢失全部知识库。

**怎么改**：新增 `scripts/backup-data.sh`，`tar.gz` 打包上述两个目录到 `~/backups/`，
按时间倒序滚动保留最近 14 份；每日 03:00（北京时间）由 cron 触发，日志追加到 `~/backups/backup.log`。

**验证**：手动执行成功，产物 3.6 MB，`tar -tzf` 确认内含 `chroma/` 与 `uploads/`。

**⚠️ 已知局限（重要）**：备份落在**同一块系统盘**上，只能防"误删/改坏"，
**防不了磁盘损坏或整机丢失**。真正的容灾必须做异地副本（Azure Blob / 对象存储），
见 §5 待决策项。

**回滚**：`crontab -r`（清空定时任务即可，脚本本身留着无害）

---

## 3. 检查过但确认无需改动

- **SSH 已加固，无需处理**。原计划要关闭密码登录，实测 `sshd -T` 显示
  `passwordauthentication no`、`kbdinteractiveauthentication no`、
  `permitrootlogin without-password`、`pubkeyauthentication yes` —— 已是仅密钥登录。
  `authorized_keys` 仅 1 条记录（Azure 生成的密钥），无冗余 key 需要清理。
  **不做无谓改动**是这里最正确的选择：改动 sshd 有把自己锁在门外的风险，收益为 0。
- **自动安全更新已启用**：`unattended-upgrades` 状态为 enabled + active，无需处理。

---

## 4. ⚠️ 执行过程中的一次事故与发现（如实记录）

**事故**：在验证"上传上限已放宽"时，我构造了一个 22MB 的随机文本文件作为探测样本上传。
文件顺利通过了大小检查，但随即进入真实解析流程 ——
22MB 文本被切成 **33,002 个片段**并开始逐个调用 embedding 接口（日志显示已嵌入 450 个），
同时容器因持续忙而健康检查失败。

**处置**：立即中止请求 → 重启容器切断残留任务 → 通过 API 删除该文档（清理 470 个向量）
→ 移动残留文件到备份目录 → 复核 Chroma 集合向量数 = **4**（仅剩正常的成都东软学院文档），确认无污染。

**发现（值得单独记一笔）**：`MAX_UPLOAD_BYTES` 只拦**文件体积**，不拦**切片数量**。
50MB 的纯文本约等于 75,000 个片段、约 7,500 次 embedding 调用 ——
既烧钱又耗时，还会把容器拖到健康检查失败。这不是本次改动引入的问题，
而是**一直存在、只是没人试过超大纯文本**。

**建议的后续修复**（我没有擅自改，因为它需要你确认阈值）：
在 `rag.ingest` 里对切片数量加硬上限（例如 `> 2000 片段直接拒绝并提示`），
或按扩展名区分处理（`.txt`/`.md` 的体积极限应远小于 `.pdf`）。

---

## 5. 未执行 / 需要你决策

| 项 | 为什么没做 |
|---|---|
| **异地备份**（Azure Blob） | 需要存储账号与凭据；当前备份同盘，容灾能力有限。建议用 `azcopy` + SAS，或 Azure Blob 生命周期策略 |
| **接入 Redis 持久记忆** | `memory_backend=inproc`，重启即丢对话历史。属于功能变更而非"完善"，需要你确认用 Upstash 免费版还是本机容器 |
| **Azure 成本/资源告警** | 必须在 Azure 门户操作，SSH 内无法完成。B2as_v2 非免费 B1s 档，会消耗学生额度 |
| **Nginx 反代 + HTTPS** | 需要域名。挂上后记得把该域名填进 `CORS_ORIGINS` |
| **切片数量上限** | 见 §4，需要你定阈值 |
| **Docker live-restore** | 可让 daemon 重启时容器不中断。对单机 compose 场景收益有限，暂缓 |
| **删除回滚镜像** | `phase1-rag-api:0.1.0-backup-20261007`（958MB）保留中，确认稳定运行几天后再删 |

---

## 6. 更正：上一版报告中的一处误报

上一版我写"build cache 817 MB 可回收"。**这是我误读了 `docker system df` 的输出**：
817.5 MB 是构建缓存的**总大小**，当列的可回收值只有 1.2 MB。
实际执行 `docker builder prune -f` 只回收了 **1.7 MB**，磁盘占用前后均为 6.2G / 29G（23%）。

真正处于"可回收"状态的是**未被容器使用的镜像**约 1.3 GB ——
即回滚镜像（958MB）与基础镜像 `python:3.12-slim`（191MB）。
这两个我**故意保留**：前者是回滚点，后者删掉会让下次构建多下载 190MB。
磁盘还有 22 GB 空闲，没有回收压力。

---

## 7. 当前盘面

```
容器    rag-api  Up (healthy)  127.0.0.1:8000->8000/tcp
内存    4 GiB 上限 / 实测约 400 MiB 占用
Swap    swappiness=10，未使用
时区    Asia/Shanghai (+0800)
日志    daemon 级 10m×3 轮转
备份    每日 03:00，保留 14 份，位于 ~/backups/
健康    /health → {"status":"ok","llm_model":"qwen-plus","embedding_model":"text-embedding-v4","memory_backend":"inproc"}
知识库  1 份文档：《成都东软学院-学校概况》（4 片段）
```

---

*本文档由 WorkBuddy 于 2026-10-07 生成，记录了本机当日的全部配置变更。*

---

# 2026-10-07（第二次）架构调整：单容器 → 四容器

> 承接本文件上一节。上一节解决的是「配置与实况脱节」（内存上限、日志、内核参数等），
> 本节是一次**结构性**调整：把挤在一个容器里的三件事拆成三个独立服务。

## 一、为什么改

改动前是「单容器 + 嵌入式 Chroma」。这个方案不是错的，但它的前提是 1 GiB 机型 ——
当初的原话是「多一个容器就多 200MB+ 常驻，嵌入式省掉一整个容器的开销」。
机型升到 **Standard_B2as_v2（2 vCPU / 8 GiB）** 后，前提不再成立，
而嵌入式方案的代价开始显现：

| 代价 | 具体表现 |
|---|---|
| 内存与 API 绑死 | HNSW 索引、sqlite 连接、chunk 缓存全在 API 进程内，文档一多 API 内存就涨，且**无法单独限额** |
| 并发被锁死在 1 worker | 多开 uvicorn worker 会各自打开同一份 sqlite，写冲突风险高 → 等于永久放弃水平并发 |
| 备份/升级不独立 | 向量库是 API 的一个"副作用"，没法单独快照、单独升级 |
| 阻碍阶段二 | Agent 需要多进程/多工具共享同一向量库，嵌入式做不到 |

另一条线是会话记忆：此前 `REDIS_URL` 留空 → 走进程内降级 → **容器一重启对话就没了**，
且 `redis` 包根本没装进镜像，Redis 分支从未真正执行过。

## 二、改成什么

```
                    ┌──────────────────────────┐
                    │  rag-api  (FastAPI)      │
                    │  mem_limit 2g            │
                    │  只留业务逻辑与文件解析     │
                    └───┬──────────┬───────────┘
              HTTP:8000 │          │
        ┌───────────────┘          └───────────────┐
        ▼                                          ▼
┌──────────────────┐  RESP          ┌──────────────────────────┐
│ rag-chroma       │◀───┐           │ rag-redis                │
│ chromadb/chroma  │    │           │ redis:7-alpine           │
│ 1.5.9            │    │           │ 512m / allkeys-lru / AOF │
│ 1.5g             │    │           │ = 会话热缓存（可丢）        │
│ 向量库（持久化）   │    │           └──────────────────────────┘
└──────────────────┘    │
                        │  SQL
                        │  ┌──────────────────────────┐
                        └──│ rag-postgres             │
                           │ postgres:16-alpine       │
                           │ 1g / shared_buffers 256M │
                           │ = 会话真相源（不可丢）     │
                           └──────────────────────────┘
```

内存预算（上限，非预留）合计 5.0 GiB，系统常驻约 0.9 GiB，总 7.7 GiB 下留约 1.8 GiB 余量。

| 容器 | 镜像 | mem_limit | 实测常驻 |
|---|---|---|---|
| rag-api | phase1-rag-api:0.1.0（自建） | 2g | 123 MiB |
| rag-chroma | chromadb/chroma:**1.5.9** | 1500m | 30 MiB |
| rag-postgres | postgres:16-alpine | 1g | 34 MiB |
| rag-redis | redis:7-alpine | 512m | 4 MiB |

> 版本必须对齐：API 里的 chromadb 客户端是 **1.5.9**，服务端镜像也用 1.5.9。
> Chroma 1.x 要求客户端与服务端主版本一致，错配会直接抛 API 版本错误。

## 三、为什么这么设计（关键取舍）

### 1. 会话存储：为什么不是「近期写 Redis、长期写 PG」

最初的想法是"热数据进 Redis、冷数据进 PG"。但这个说法有个隐含缺陷：**它把两者当成了
两个并列的数据源**，于是变成双写 —— 两边都可能失败，且没有权威副本，不一致时不知道该信谁。

实际采用的是**先定真相源，再让缓存派生**：

```
写入：先落 PostgreSQL（真相源）→ 再刷 Redis（缓存）
       PG 成功而 Redis 失败，只损失一点性能；反过来则会直接丢数据。
读取：Redis 命中即返回；未命中 → 从 PG 重建并回填缓存（cache-aside）
删除：PG 删（messages 走外键级联）+ Redis 删 + 进程内清
列表：从 PG 查（真相源），Redis 仅作兜底
```

分层职责因此变得明确：**Redis 里的一切都可以丢**，丢了下次读取自动从 PG 回填。
`INGEST`/`MEMORY_TTL_SECONDS`(7天) 只影响缓存，不影响 PG 里的永久存档。

降级链（任何一层挂掉服务都可用）：

```
PostgreSQL + Redis  →  仅 PG（无缓存，稍慢）  →  仅 Redis（无持久化）  →  进程内
```

### 2. 为什么改用本机 Redis 而不是 Upstash

原实现假设 Upstash 托管 Redis（公网 + TLS，有额度与延迟成本）。
既然这台 VM 已经有 8 GiB 内存，把 Redis 跑成本机容器更划算：
延迟从公网往返降到容器内网，且完全免费。`memory.py` 仍兼容 `redis://` 与 `rediss://`。

Redis 用 `allkeys-lru` + 384MB 上限：缓存写满时自动淘汰最久未用的会话，
而不是像默认 `noeviction` 那样直接报错。淘汰在这里无损（真相源在 PG）。

### 3. 入库闸门：从「文件体积」改到「工作量」

上一节的事故（22MB 文本被切成 33002 片、疯狂调 embedding、打挂健康检查）根因是
**闸门设错了维度**：`MAX_UPLOAD_MB` 拦的是文件体积，而决定成本与负载的是**切片数量**，
两者并不成正比。现在改为三道闸：

| 配置 | 默认值 | 作用与依据 |
|---|---|---|
| `INGEST_MAX_CHARS` | 2,000,000 | 解析后字符数。约等价 3000 片；在切片**之前**拦下明显过大的文件，省掉一次全量切片 |
| `INGEST_MAX_CHUNKS` | 1200 | 切片数硬上限。1200 片 = 120 次 embedding 调用（每批 10 行），按每次 0.5~1.5s 约 1~3 分钟 —— 已是交互能忍受的上限；再大应转后台队列（阶段二） |
| `INGEST_MAX_CONCURRENT` | 1 | 同时入库数。2 vCPU 上并行入库只会互相抢 CPU 并触发云端限流，串行更快更稳。用**非阻塞获取 + 明确报错**，而不是让第二个请求无限等待 |

报错信息会直接告诉用户"会被切成 N 片、约需 M 次 embedding 调用"，而不是一句笼统的"失败"。

### 4. 上传接口必须丢线程池

`rag.ingest` 是同步函数，一次入库要串行发起上百次 embedding 请求。
原先 `async def upload` 里直接同步调用它会**阻塞整个事件循环** ——
期间 `/health` 和别人的问答全部卡住，容器还会被健康检查判定为不健康。
现在改为 `await run_in_threadpool(rag.ingest, ...)`。

## 四、数据迁移：向量库怎么搬的

嵌入式与独立服务用的是**同一套持久化格式**（`chroma.sqlite3` + 一个 HNSW 索引目录），
所以迁移就是复制目录：

```bash
mkdir -p data/chroma-server
cp -a data/chroma/. data/chroma-server/
```

挂给 chroma 容器（`./data/chroma-server:/data`），**旧目录 `./data/chroma` 原地保留作冷备份**。

风险其实极低：当前知识库只有 1 份文档、4 个向量，最坏情况重新上传一次就恢复。
迁移后实测 `count = 4`，文档列表与问答均正常，无需重灌。

## 五、执行中遇到的问题与处置

### 1. Chroma 容器健康检查失败（unhealthy）

现象：`dependency failed to start: container rag-chroma is unhealthy`。
排查：`docker inspect` 显示 `exec: "python": executable file not found in $PATH`。

原因：**chromadb/chroma 官方镜像非常精简**，没有 `curl` / `wget` / `python` / `nc`，
只有 `bash`、`perl`、`awk`、`grep` 等基础工具。原先照抄 API 容器的 `python -c` 写法不成立。

处置：改用 bash 的 `/dev/tcp` 直接发 HTTP 请求并校验状态行：

```yaml
test:
  - CMD
  - bash
  - -c
  - 'exec 3<>/dev/tcp/127.0.0.1/8000 && printf "GET /api/v2/heartbeat HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n" >&3 && head -1 <&3 | grep -q " 200 "'
```

顺带纠正一个端点认知：Chroma 1.x 的心跳是 `/api/v2/heartbeat`（返回 200），
`/api/v1/heartbeat` 现在返回 **410**。

### 2. 重建镜像后接口 500 —— 依赖声明缺失

现象：`/api/documents` 报 `ModuleNotFoundError: No module named 'langchain_openai'`。
全面排查后发现 **`langchain-openai` 和 `pypdf` 两个包根本没写进 requirements.txt**，
而它们是 `llm.py` / `rag.py` **直接 import** 的。

旧镜像之所以能跑，是因为它们被 `langchain-community` 的传递依赖"顺带"装上了；
重建镜像时依赖解析结果变化，两个包一起消失，接口立刻挂掉。

处置：在 requirements.txt 里显式声明

```
langchain-openai>=1.0,<2
pypdf>=6,<7
```

并在文件里写下这条规则：**凡是代码里 import 的包，就必须在 requirements 里出现，不能靠别人带。**

### 3. 顺手修掉：删除文档不删原始文件

排查时发现 `uploads/` 里存在"孤儿文件" —— 删除文档只删了向量，落盘的原文一直留着，
每删一次就多一个永远不会被引用的文件。已修：`delete_document` 现在用 glob 匹配
`{doc_id}.*` 一并删除，让 uploads 始终与知识库一致。

（`ingest` 失败时也会清理刚落盘的文件，避免同类垃圾。）

## 六、验收结果（全部实测通过）

| # | 项目 | 结果 |
|---|---|---|
| 1 | 四容器状态 | 全部 `Up (healthy)` |
| 2 | `/health` | `status=ok`、`memory_backend=postgres+redis`、`chroma_status=ok` |
| 3 | 向量数据迁移 | collection `phase1_docs`、`count = 4`，文档列表正确 |
| 4 | 端到端问答 | 命中「张应辉 / 精勤博学，学以致用」「2003 年 / 软件工程」，带 `[来源N]` |
| 5 | 多轮记忆 | 第二轮「它有多少在校生？」正确理解指代 |
| 6 | **PG 落库** | `sessions` 1 行、`messages` 4 行（user/assistant 逐条） |
| 7 | **Redis 缓存** | key 存在、TTL 604800 |
| 8 | **缓存回填**（关键） | 手动 `DEL` 缓存后读历史 → 仍返回 2 轮，且缓存被自动重建 |
| 9 | **容器重启不丢会话** | `docker restart rag-api` 后历史仍为 2 轮 |
| 10 | 入库闸门 A（字符） | 2,100,000 字符 → `HTTP 400` + 可读原因 |
| 11 | 入库闸门 B（切片） | 1,462 片 → `HTTP 400`「约需 147 次 embedding 调用」 |
| 12 | 闸门不误伤 | 正常文件 → `HTTP 200`，4 chunks |
| 13 | 删除一致性 | 向量与落盘原文一并清除，uploads 无孤儿 |
| 14 | CORS | 外站 Origin 拿不到 ACAO 头 |
| 15 | 端口暴露面 | 8000/8001/6379/5432 **全部只绑 127.0.0.1** |
| 16 | 日志上限 | 四个容器全部继承 `max-size=10m, max-file=3` |
| 17 | 备份 | pg_dump + 向量/原文归档均非空，cron 每日 03:00 |

## 七、回滚方式

```bash
cd ~/phase1-rag

# 1) 代码与编排：改动前完整快照（含 app 全部源码、compose、chroma 数据副本）
ls data/_backup_20261007/pre-fanout/

# 2) 镜像回滚点
docker image ls | grep backup-20261007     # phase1-rag-api:0.1.0-backup-20261007

# 3) 若需退回单容器嵌入式方案
cp data/_backup_20261007/pre-fanout/docker-compose.yml .
cp -a data/_backup_20261007/pre-fanout/app-backup/. backend/app/
docker compose up -d --build

# 4) 数据层不受影响：旧向量目录 data/chroma 一直原地保留，
#    PostgreSQL / Redis 的卷目录删掉即可（会话存档需先确认是否要留）
```

## 八、遗留待办

| 项 | 说明 |
|---|---|
| 异地备份 | 备份与源数据同盘，防不了整机丢失。建议 cron 后追加一步推到 Azure Blob |
| Azure 成本告警 | B2as_v2 会消耗学生额度，需在门户对 CPU Credits / Cost Budget 设告警 |
| Chroma 认证 | 目前无鉴权，仅靠端口只绑本机 + 容器网络隔离。若将来跨机访问需开 token |
| 并发入库 | 当前串行（名额 1）。若将来要并发，正确做法是引入任务队列 + 状态查询接口，而不是调大名额 |
| 会话长期价值 | 存档已进 PG。若要让 Agent 跨会话检索历史对话（长期记忆），下一步是给 messages 加 pgvector 向量列 |

---

# 第四次变更：前端从镜像剥离，独立挂载（2026-10-07 晚）

## 1. 动机

前端 6 个文件（`index.html` / `app.js` / `style.css` / `markdown.js` / `sse.js` / `store.js`，共 1800 行）
原先被 `COPY app ./app` 打进 api 镜像。由此产生两个问题：

1. **改一行样式也要重建镜像**。重建约 30 秒，且必须重启 api 容器，问答服务短暂中断。
2. **「改了没生效」的幽灵问题**。容器跑的是几小时前构建的那一层，人在宿主机改文件、
   镜像里还是旧的，现象极难定位 —— 容易误判为浏览器缓存或代码写错。

同时，阶段二计划把前端换成 Vue3 构建产物，届时若前端仍焊在 API 镜像里，
前端发版将被迫与后端发版耦合。

## 2. 改动清单

| 项 | 改动 | 原因 |
|---|---|---|
| 目录 | 前端迁到仓库顶层 `frontend/`，从 `backend/app/static/` 移除 | 前端成为独立可替换单元 |
| 挂载 | api 容器新增 `./frontend:/app/frontend:ro` | 改宿主机文件即时生效，无需重建镜像 |
| 配置 | `config.py` 新增 `frontend_dir`（默认 `/app/frontend`） | 目录可配；回退到包内 `app/static` 以支持不开容器直接跑 uvicorn |
| 代码 | `main.py` 改为按 `frontend_dir` 解析，并要求目录内确有 `index.html`；新增 `/` → `/ui/` 跳转 | 缺前端时明确报错，而不是挂个空目录让浏览器收一堆 404 |
| 构建 | 新增 `backend/.dockerignore`，排除 `app/static/` | ★ 见下方「关键取舍」第 1 条 |
| 构建 | `Dockerfile` 预建 `/app/frontend` 空目录 | bind mount 目标若不存在，Docker 会以 root 创建，与非 root 约定冲突 |
| 编排 | 新增 `frontend` 服务（初版为 `profiles: ["nginx"]`，**第五次变更已改为默认常驻**） | 前端可独立成服务、独立发版 |
| 编排 | 新增 `deploy/nginx-frontend.conf` | 站点配置与前端产物分离，前端升级不会覆盖 nginx 配置 |
| 前端 | `index.html` 上传提示、`app.js` 体积预校验改为从 `/health` 读取 `max_upload_mb` | 原写死 20MB 与后端实际的 50MB 不符，属界面假信息 |
| 前端 | `app.js` 记忆后端提示语、`store.js` 注释更新 | 原文描述的是 `inproc` 时代的行为，已过时 |
| 接口 | `/health` 新增 `max_upload_mb`、`ingest_max_chunks` | 让前端能显示真实生效值，而非各自写死 |

## 3. 关键取舍（为什么这么做）

1. **必须用 `.dockerignore` 把 `app/static` 从镜像里彻底去掉，不能只靠挂载遮盖。**
   如果镜像里留着一份旧副本，挂载正常时看不出来，一旦挂载失效（路径写错、忘了挂）
   就会**静默回退到旧文件**，表现为「改了前端但页面没变」。宁可让它根本不存在，
   失败时直接报「缺少 index.html」。实测确认：新镜像内 `/app/app/` 只剩 `.py` 文件。

2. **挂载用 `:ro`（只读）而不是可写。** API 进程没有任何理由写前端文件，
   多一个写权限就多一条「被入侵后篡改页面」的路径。实测 `touch /app/frontend/x`
   返回 `Read-only file system`。

3. **nginx 用 `nginxinc/nginx-unprivileged:1.27-alpine`，不用 `nginx:alpine`。**
   官方 nginx 镜像以 root 启动 master（为了绑 80 端口、写 `/var/run`），
   与本机「容器一律非 root」的约定冲突。unprivileged 变体以 uid 101 运行、默认监听 8080。
   实测 `id` 输出 `uid=101(nginx)`。

4. **nginx 的反代地址必须写成「变量 + resolver」，不能写死 `proxy_pass http://api:8000`。**
   写死时 nginx 只在**启动那一刻**解析一次并把 IP 永久记住；而 api 容器被重建
   （换镜像、改环境变量都会触发）后会拿到新 IP，nginx 却仍往旧 IP 打，
   表现为 502，且重启 api 怎么都不好、只有重启 nginx 才行 —— 这类问题极难排查。
   改用 `resolver 127.0.0.11 valid=10s` + `set $api_upstream http://api:8000`
   后每次请求最多缓存 10 秒。

   附带一个易错点：`proxy_pass` 用变量时 nginx **不会**自动补上「去掉 location 前缀后的
   剩余路径」，必须显式写 `proxy_pass $api_upstream$request_uri`，
   否则所有 `/api/*` 都会被打到上游的 `/` 上。

5. **SSE 必须关掉 nginx 缓冲。** nginx 默认把上游响应攒进缓冲区再整块发出，
   逐字输出会被攒成一大坨一次性吐出，前端全程无输出、最后突然出现整段答案。
   因此 `/api/` 下显式 `proxy_buffering off`，并把读写超时提到 300 秒
   （入库要串行调 120 次 embedding，可达 1~3 分钟，默认 60 秒会在后端还在干活时就掐断连接）。

6. **nginx 服务起初放进 profile 而非默认启动 —— 这个决定后来被推翻了（见第五次变更）。**
   当时的判断是：访问方式是 SSH 隧道直连 api 的 8000，api 自己就能发 `/ui`，
   凭空多一个常驻容器只会多占内存与一个攻击面。
   实际用过才发现这个「可选」有害无益，第五次变更已去掉 profile 改为默认常驻。

7. **根路径加了 `/` → `/ui/` 的 307 跳转。** 纯属易用性：省掉手打 `/ui/`。
   注册在静态挂载之前，不会与挂载抢路径。

## 4. 验收（全部实测）

| # | 项目 | 结果 |
|---|---|---|
| 1 | `/ui/` 首页 | HTTP 200 / text/html / 3682 B |
| 2 | 根路径跳转 | `GET /` → HTTP 307，`Location: /ui/` |
| 3 | 6 个静态资源 | 全部 HTTP 200（index/app/style/markdown/sse/store） |
| 4 | 镜像内无旧前端 | `/app/app/static` → No such file or directory |
| 5 | 容器内不可写前端 | `touch /app/frontend/x` → Read-only file system |
| 6 | **热更新（核心）** | 宿主机给 index.html 追加一行注释 → 服务端立即返回新内容，全程未重建任何镜像 |
| 7 | `/health` 新字段 | `max_upload_mb=50`、`ingest_max_chunks=1200`、`memory_backend=postgres+redis` |
| 8 | nginx 首页 | HTTP 200，返回的是 index.html |
| 9 | nginx gzip | `style.css` 15665 B → 5361 B（`Content-Encoding: gzip`） |
| 10 | nginx 反代 | `/api/documents`、`/health`、`/docs`、`/openapi.json` 全部 200 |
| 11 | nginx 上传 | 经反代上传成功（`chunks=1`），验证 `client_max_body_size` 与反代上传链路 |
| 12 | **SSE 未缓冲（核心）** | 经 nginx 的流式问答：160 个 token 事件跨越 13.62 秒逐条到达，`Transfer-Encoding: chunked` |
| 13 | **nginx 动态解析（核心）** | api 容器名称一度消失后又恢复，nginx **未重启**，在 10 秒内自行恢复为 200 |
| 14 | 端到端问答 | 经 8080 提问成都东软学院校训，命中并带来源；`/health` = `postgres+redis` |
| 15 | 知识库未被污染 | 上传验证文档已删除，uploads 目录只剩 `4271c477f93f.md` |

最终容器状态（5 个全部 healthy）：

```
rag-frontend   Up (healthy)   127.0.0.1:8080->8080/tcp
rag-api        Up (healthy)   127.0.0.1:8000->8000/tcp
rag-chroma     Up (healthy)   127.0.0.1:8001->8000/tcp
rag-postgres   Up (healthy)   127.0.0.1:5432->5432/tcp
rag-redis      Up (healthy)   127.0.0.1:6379->6379/tcp
```

内存实测：frontend 3.2 MiB / api 115 MiB / chroma 30 MiB / postgres 36 MiB / redis 3.9 MiB，
合计约 188 MiB —— 上限是上限，实际常驻远低于此。

## 5. 执行中踩到的坑（如实记录）

1. **`docker network connect` 不会带上 compose 的服务别名。**
   为验证 nginx 的动态解析，我用「断开 + 重连网络」的方式想给 api 换 IP，
   结果 `Aliases` 变成空、`api` 这个域名解析不到了，nginx 侧立刻 502。
   这不是配置问题，是我的测试操作副作用：compose 会给容器打上
   `[容器名, 服务名]` 两个别名，手写的 `docker network connect` 只给容器名。
   改用 `docker compose up -d --force-recreate api` 恢复后，nginx 在 10 秒内自愈。
   **这个插曲反而真正验证了 resolver 方案的价值**：nginx 全程未重启，域名消失又恢复后能自己接上；
   若当初写死了 `proxy_pass http://api:8000`，它会一直往旧 IP 打、不会自愈。

2. **api 容器重建时 Docker 常常复用同一个 IP。**
   所以「重建容器看 IP 有没有变」并不能验证动态解析 —— 我第一次和第二次重建，
   IP 都是 `172.18.0.5`。要真验证必须让容器名称短暂不可解析（即上面那次）。

3. **前端里有 3 处与后端实况不符的写死值**，是这次顺手修的：
   上传提示「最大 20MB」（实际 50MB）、`app.js` 里 `file.size > 20 * 1024 * 1024` 的预校验、
   `store.js` 里描述 `memory_backend = inproc` 的注释。
   教训：凡是会随后端配置变化的数字，前端都不该写死，应由 `/health` 下发。

## 6. 回滚

| 目标 | 操作 |
|---|---|
| 回滚 api 镜像 | `docker tag phase1-rag-api:0.1.0-backup-pre-frontend phase1-rag-api:0.1.0 && docker compose up -d --force-recreate api` |
| 回滚代码 | 改动前代码在 `data/_backup_20261007/pre-frontend-deploy-182648/` |
| 回滚前端原位置 | 原始 `backend/app/static/` 在 `data/_backup_20261007/pre-frontend/static-backup/` |
| 停用 nginx 前端 | `docker compose stop frontend`（第五次变更后不再需要 `--profile`） |
| 退回「前端可选」旧行为 | 在 `frontend` 服务下加回一行 `profiles: ["nginx"]`，再 `docker compose up -d`（不推荐，见第五次变更） |
| 完全回到「前端在镜像里」 | 把 `frontend/` 拷回 `backend/app/static/`、删掉 `backend/.dockerignore` 里那行、去掉 compose 的 `./frontend` 挂载，重建镜像 |

## 7. 更新的访问方式

| 入口 | 地址 | 用途 |
|---|---|---|
| api 直连（保留为兜底） | `ssh -L 8000:127.0.0.1:8000 azureuser@20.89.90.58` → `http://localhost:8000/ui/` | 排查用：8080 打不开时用它判断是前端还是后端的锅；`/` 会自动跳到 `/ui/` |
| nginx 前端（**默认入口，随栈常驻**） | `ssh -L 8080:127.0.0.1:8080 azureuser@20.89.90.58` → `http://localhost:8080/` | 带 gzip 的正式入口，`docker compose up -d` 会自动拉起，无需 `--profile` |

两个入口都是只绑 `127.0.0.1`，公网依旧不可达。

---

# 第五次变更：前端改为默认常驻（去掉 profile，2026-10-07 晚）

## 1. 一句话

把 `frontend` 服务上的 `profiles: ["nginx"]` 删掉，其余一律不动。
`docker compose up -d` 从此一条命令拉起全部五个容器。

**改动只有一行**：`docker-compose.yml` 里删掉 `profiles: ["nginx"]`（另加注释修订）。

## 2. 为什么要推翻第四次的决定

第四次把 nginx 前端放进 profile 当「可选服务」，理由是「api 自己也能发 `/ui`，
多一个常驻容器只是多占内存与攻击面」。这个判断错在**把两件事混为一谈**：

| | 事实 |
|---|---|
| 我以为 | 8080 是「锦上添花」的第二个入口，8000 才是主入口 |
| 实际 | 8080 是唯一正式入口；8000 只是排查用的兜底 |

一旦定位成「主入口」，profile 就从「省资源」变成了「藏地雷」：

1. **失败是静默的。** 忘带 `--profile nginx` 时 compose 不报错、不警告，
   只是少起一个容器 —— `docker compose ps` 看着四个容器全绿，
   但站点打不开。这种「一切正常但就是不通」最难排查。
2. **省下的东西微不足道。** 实测 frontend 常驻 **3.08 MiB**。
   为了 3 MiB 让主入口默认不启动，是一笔亏本买卖。
3. **命令记忆负担。** 起全栈、重启、看日志、跑 compose 配置检查……
   每条命令都得额外记着带 `--profile nginx`，漏一次就是一次故障。
4. **它并没有真正「可选」。** 既然每次都得启用，那它就不是可选项，
   只是给默认行为加了一道容易忘的手续。

结论：**「可选」只适用于「不用也无所谓」的东西。主入口不属于这一类。**

## 3. 改动清单

| 项 | 改前 | 改后 |
|---|---|---|
| compose 服务定义 | `frontend` 带 `profiles: ["nginx"]` | 删掉该行 |
| 启动命令 | `docker compose --profile nginx up -d` | `docker compose up -d` |
| `docker compose config --services` | 输出 4 个服务 | 输出 5 个服务 |
| 镜像 / 挂载 / 端口 / 限额 | — | **完全未动** |

镜像仍是 `nginxinc/nginx-unprivileged:1.27-alpine`，端口仍是 `127.0.0.1:8080`，
挂载仍是 `./frontend` 与 `./deploy/nginx-frontend.conf`（均只读），限额仍是 128m。

## 4. 一个有用的副作用

旧命令 `docker compose --profile nginx up -d` **不会报错**，
它退化成与裸命令完全等价（因为没有服务声明该 profile 了，
compose 只是照常按默认集合启动）。所以历史文档、脚本、脑子里的肌肉记忆
都不会突然失效 —— 这次改动是向前兼容的。

## 5. 验收（全部实测）

| # | 项目 | 结果 |
|---|---|---|
| 1 | `docker compose config --quiet` | 通过，无语法错误 |
| 2 | `docker compose config --services`（不带 profile） | `postgres redis chroma api frontend` —— 5 个全在 |
| 3 | **删掉前端容器后跑裸命令** | `docker compose rm -sf frontend` → 8080 变 `HTTP 000` → `docker compose up -d` → `rag-frontend Created/Started` |
| 4 | 前端健康 | 21 秒后 `Up (healthy)` |
| 5 | 首页 | HTTP 200 / text/html / 3682 B |
| 6 | gzip | 响应头带 `Content-Encoding: gzip` |
| 7 | 反代 `/health` | `memory_backend=postgres+redis`、`vector_store=chroma://chroma:8000`、`chroma_status=ok` |
| 8 | 反代 `/api/documents` | HTTP 200 |
| 9 | 5 个静态资源 | app.js / style.css / markdown.js / sse.js / store.js 全部 200 |
| 10 | **SSE 未被缓冲（核心）** | `Transfer-Encoding: chunked`；9 个事件（meta/sources/token×6/done），首事件 0.03s、末事件 1.32s，**跨度 1.30s** |
| 11 | 端到端问答 | "成都东软学院的校训是：精勤博学，学以致用 [来源1]"，命中 4 个来源 |
| 12 | 单独起前端 | `docker compose up -d frontend` 正常 |
| 13 | 单独重启前端 | `docker compose restart frontend` 正常 |
| 14 | **前后端解耦（核心）** | `docker compose stop frontend` 后 api 的 `/health` 仍 200；`start frontend` 后 8080 恢复 200 |
| 15 | 旧命令兼容 | `docker compose --profile nginx up -d` 不报错，行为与裸命令一致 |

内存实测（五容器合计约 **197 MiB**，上限合计 5.125 GiB）：

```
rag-frontend   3.082MiB / 128MiB     2.41%
rag-api        124.6MiB / 2GiB       6.09%
rag-chroma     30.14MiB / 1.465GiB   2.01%
rag-postgres   35.34MiB / 1GiB       3.45%
rag-redis      3.875MiB / 512MiB     0.76%

系统：used 943 MiB / 7932 MiB，swap 占用 0
```

frontend 的 3 MiB 就是这次决策的全部代价 —— 用这点内存换掉一个静默故障点，
很划算。

## 6. 执行中值得记一笔的细节

1. **改完 compose 后 `docker compose up -d` 并没有重建前端容器**（仍显示 `Up 42 minutes`）。
   说明 `profiles` 字段不参与容器的 config-hash 计算，改动对运行中的容器无影响 ——
   本次调整**零停机**。
2. **为了真实验证，我特意把容器删掉重来**（`docker compose rm -sf frontend`），
   而不是只跑一次 `up -d` 看一眼。因为「容器本来就在跑」时，
   `up -d` 成功并不能证明新配置有效 —— 它可能只是复用了旧容器。
   必须先让前端**不存在**，再验证裸命令能把它拉起来。
3. `docker compose config --services` 是这个改动的**最小验证手段**：
   它直接列出「默认会启动哪些服务」，不带任何副作用，比 `up -d` 试错安全得多。

## 7. 回滚

| 目标 | 操作 |
|---|---|
| 回到「可选 profile」 | 在 `frontend` 服务下加回一行 `profiles: ["nginx"]`，`docker compose up -d` |
| 回到改前文件 | 备份在 `data/_backup_20261007/docker-compose.yml.pre-unprofile`（或 `~/docker-compose.yml.pre-unprofile`） |
| 完全不要前端容器 | `docker compose stop frontend`（api 的 `/ui/` 仍可访问，前端不会消失） |

## 8. 日常命令（改后版）

> ⚠️ **本节已被第六次变更部分推翻**：下表「改前端文件」一行已失效
> （前端不再是挂载目录，改成构建产物打进镜像）。保留原表是为了不抹掉当时的判断，
> 请以文末《第六次变更》第 8 节的命令表为准。

| 目的 | 命令 |
|---|---|
| 起/更新全栈 | `docker compose up -d` |
| 只看前端 | `docker compose up -d frontend` / `docker compose restart frontend` / `docker compose stop frontend` |
| 改完 nginx 配置热载 | `docker compose exec frontend nginx -s reload`（★ 2026-10-14 起挂载已改为整个 `deploy/` 目录，热载可靠；此前挂单文件时它会静默失效，见第八次变更 §4） |
| ~~改前端文件~~ | ~~直接改宿主机 `./frontend/*`，无需任何重启~~（第六次变更后已失效） |
| 看前端日志 | `docker compose logs -f --tail=100 frontend` |


---

# 第六次变更：前端迁到 Vue3 + Vite，前后端彻底分家（2026-10-07 深夜）

## 1. 一句话

把 `frontend/` 从「一堆手写 .js 直接挂载」改为**独立的 Vue3 + Vite 工程**；
`frontend` 服务改成多阶段构建（node 构建 → nginx 托管 `dist/`）；
api 侧**彻底摘掉** `StaticFiles` 挂载与 `/ui` 路径，退化为纯接口服务。

## 2. 动机：上一版解决了「发布」，没解决「工程」

第五次变更让前端能独立起服务了，但它仍是我手写的 6 个文件、1818 行：

| 问题 | 具体表现 |
|---|---|
| 没有模块化 | `index.html` 用 4 条 `<script src>` 顺序加载，靠全局作用域共享；加载顺序错了就是 `undefined`，且没有任何工具能发现 |
| 无构建步骤 | 没有压缩、没有内容哈希、没有 tree-shaking；改一行要把整包重下 |
| 无缓存策略可言 | 文件名固定，只能 `no-cache`，等于放弃静态资源缓存 |
| 无类型/编译期检查 | 打错一个属性名要等运行时才发现 |
| 无 HMR | 只能整页刷新 |

用户明确要求走 Vue3 + Vite 的标准形态：
开发 `npm run dev`（5173），生产 `npm run build` → `dist` → nginx。
**这是把「前端」当成一个正经工程来对待，而不是「后端的附属静态目录」。**

## 3. 改成什么

### 3.1 目录结构

```
frontend/
├── index.html              入口 HTML（含防主题闪烁的内联脚本）
├── vite.config.js          开发代理 + 生产构建
├── Dockerfile              多阶段：node 构建 → nginx 托管
├── package.json
├── README.md
└── src/
    ├── main.js
    ├── App.vue
    ├── components/         13 个单文件组件（纯结构）
    ├── composables/        7 个组合式函数（状态与业务逻辑）
    ├── lib/                4 个与框架无关的工具模块
    └── styles/main.css     全局样式（不拆 scoped，理由见文件头）
```

原 6 个文件 → 25 个源文件的映射关系：

| 迁移前 | 迁移后 | 说明 |
|---|---|---|
| `markdown.js` | `src/lib/markdown.js` | **逻辑逐字保留**，只改成 ES module 导出 |
| `sse.js` | `src/lib/sse.js` | 同上，两个坑（分片、汉字截断）的注释一并保留 |
| `store.js` | `src/lib/storage.js` | localStorage 层，另新增 `fmtTime` |
| — | `src/lib/api.js` | 新增：把所有 fetch 收拢到唯一出口 |
| `app.js`（726 行） | `composables/` × 5 + `App.vue` + 组件 | 按职责拆解，见下 |
| `index.html`（结构） | `App.vue` + `components/` × 13 | DOM 操作全部换成模板与响应式 |
| `style.css` | `src/styles/main.css` | 仅 2 处改动（见 3.4） |

`app.js` 的拆解方式：

| 原函数 | 去向 |
|---|---|
| `send` / `stopGenerating` / 流式事件处理 | `composables/useChat.js` |
| `addMessage` / `createStreamingBubble` / `renderTools` / `renderSources` | `components/MessageBubble.vue` + `SourceList.vue` |
| `refreshDocs` / `deleteDoc` / `uploadFile` | `composables/useDocuments.js` |
| `syncSessions` / `openSession` / `deleteSession` / `exportSession` / `renderSessionList` | `composables/useSessions.js` + `useChat.js` + `SessionList.vue` |
| `checkHealth` / `applyServerLimits` | `composables/useHealth.js` |
| `applyTheme` / `toggleTheme` | `composables/useTheme.js` |
| `toast` / `scrollToBottom` / `autoGrow` | `useToast.js` / `MessageList.vue` / `ChatComposer.vue` |
| `fmtTime` | `lib/storage.js` |

### 3.2 部署形态

| 项 | 改前（第五次后） | 改后 |
|---|---|---|
| 前端来源 | 宿主机 `./frontend` 只读挂载 | **镜像内 `dist/`**（多阶段构建产出） |
| 前端镜像 | `nginxinc/nginx-unprivileged:1.27-alpine`（官方镜像） | `phase1-rag-frontend:0.1.0`（自建） |
| 改前端后生效方式 | 改文件即时生效 | `docker compose up -d --build frontend` |
| api 的前端挂载 | `./frontend:/app/frontend:ro` | **已删除** |
| api 的 `/ui` 路由 | 存在（StaticFiles） | **已删除**（现在 404） |
| api 的 `/` | 307 跳 `/ui/` | 返回自描述 JSON |
| nginx 站点根 | 宿主机目录 | 镜像内 `/usr/share/nginx/html` |

### 3.3 开发与生产是两套形态（必须理解）

| | 开发 | 生产 |
|---|---|---|
| 命令 | `cd frontend && npm run dev` | `docker compose up -d --build frontend` |
| 地址 | `http://localhost:5173` | `http://127.0.0.1:8080` |
| 服务方 | Vite dev server（Node） | nginx（alpine） |
| 产物 | 源码即时编译，带 HMR | `dist/` 静态文件，带内容哈希 |
| `/api` 去向 | Vite 代理 → `127.0.0.1:8000` | nginx 反代 → `api:8000` |
| 改代码 | 自动热更新 | **必须重建镜像** |

两者**共用同一份源码**，靠 `vite.config.js` 与 `deploy/nginx-frontend.conf`
保证行为一致。共同点是「浏览器眼里始终同源」，所以**两套都不需要配 CORS**。

### 3.4 只有两处样式/行为改动（其余逐字保留）

1. **新增 `.dot-warn`**：后端 `/health` 的 `status=degraded`（服务在、向量库不可达）
   现在显示黄色「向量库不可用」，而不是和「完全连不上」一样显示红色「异常」。
   这两种故障的处置方式完全不同，混在一起显示会误导排查方向。
2. **流式光标改为伪元素**：原先是真实 `<span class="cursor">`，由 JS 在每次
   重渲染后手动 `appendChild`。迁到 Vue 后内容交给 `v-html`，手动塞节点会和
   虚拟 DOM 的更新打架。改成 `.md-body.streaming::after`，不参与 DOM diff。

另有一处新增：`index.html` 里加了一段**内联主题预设脚本**，在 HTML 解析阶段
就把 `data-theme` 打到 `<html>` 上。Vue 应用有挂载成本，不这样做的话
深色偏好用户会先看到一帧白底再跳变。

### 3.5 其余功能全部保留（逐项核对过）

上传（点击 + 拖拽 + 进度条）、文档删除、会话列表（单击打开 / 双击导出 / 改名 / 删除 / 全清）、
流式问答、停止生成、重新生成、复制（含 execCommand 降级）、引用片段折叠、
性能统计（首字/总耗时/块数/字数）、主题三态、移动端抽屉、
快捷键（Ctrl+K / Ctrl+/ / Esc）、自动滚动只在贴底时触发、
消息工具条 hover 才显示、`prefers-reduced-motion` 支持。

## 4. 执行中踩到的坑（如实记录）

### 4.1 ★ 本机 node 无法创建子进程 —— 本地构建整条路走不通

准备阶段发现 `npm install` 必然失败：

```
npm error Error: spawnSync .../node.exe EBUSY
  at validateBinaryVersion (.../esbuild/install.js:102:28)
```

逐层排查后确认：**这台机器上 node 根本无法 spawn 任何子进程**，连
`spawnSync('C:\\Windows\\System32\\cmd.exe', ['/c','echo ok'])` 都返回 `EBUSY`。
把 node.exe 复制一份到别处同样失败（所以不是文件锁）。后果是：

- `esbuild` 的安装脚本跑不了 → `vite` 装不上可用版本
- `vite build` 跑不了（rollup 要 spawn esbuild）
- `agent-browser` 也用不了（要 spawn Chromium）

**应对**：本地只做「静态校验」，真实构建放到服务器容器里做。静态校验用了三样：

| 手段 | 查出什么 |
|---|---|
| `node --check` 逐个检查 12 个 JS 模块 | 语法错误 |
| `@vue/compiler-sfc` 编译 13 个 SFC | 模板与 `<script setup>` 编译错误 |
| 自写脚本检查相对 import | 路径写错 / 文件不存在 |

> 这三样都是**只读、不依赖子进程**的，所以在受限环境下仍可用。

### 4.2 ★★ `npm ci` 报告成功，但构建时才炸（最阴的一个）

首次容器内构建：

```
#10 RUN npm ci ...   ->  added 32 packages in 2s     ← 看起来完全正常
#12 RUN npm run build
    Error: Cannot find module '@rollup/rollup-linux-x64-musl'
    npm has a bug related to optional dependencies (npm/cli#4828)
```

根因：`rollup 4` / `esbuild` 的**原生加速包是按平台分包的**
（`@rollup/rollup-linux-x64-musl`、`@esbuild/linux-x64`…）。
lock 文件是在 Windows 上生成的，那次安装不会把 Linux 的原生包写进
lock 的包列表，于是容器里 `npm ci` 老老实实按 lock 装完、报告成功，
但 rollup 找不到自己的原生模块 —— **错误被推迟到真正执行 `vite build` 时才出现**。

这类「安装成功、构建才炸」的错最难查：看到 `added 32 packages` 会本能地
认为依赖没问题，然后去怀疑代码。

**处置**：`Dockerfile` 里**故意只 `COPY package.json`，不拷 lock**：

```dockerfile
COPY package.json ./
RUN npm install --no-audit --no-fund
```

不拷 lock 之后，`npm install` 会在容器内按当前平台（linux/musl）重新解析，
装的必然是正确的那一份。代价是失去严格版本锁定 ——
本项目只有 3 个直接依赖，可以接受。
**若将来需要可复现构建，正确做法是在 Linux 容器里生成 lock 再提交，
而不是把 Windows 生成的 lock 拿来复用。**

修完后构建输出：

```
vite v6.4.4 building for production...
✓ 33 modules transformed.
dist/index.html                  1.60 kB │ gzip:  1.06 kB
dist/assets/index-BC2SVm0r.css  11.70 kB │ gzip:  3.12 kB
dist/assets/index-znBUs3lF.js   90.12 kB │ gzip: 36.20 kB
✓ built in 1.31s
```

### 4.3 缓存策略要跟着「有没有内容哈希」一起改

原 nginx 配置对**所有** `.js/.css/.html` 一律 `no-cache`，理由是
「文件名固定，改了就是同一个名字，不能缓存」。引入 Vite 后前提变了：
JS/CSS 被输出到 `/assets/` 且**文件名含内容哈希**，同名文件内容永不改变，
于是可以放心 `max-age=31536000, immutable`。

新的三分法：

| 路径 | 策略 | 原因 |
|---|---|---|
| `/`、`/index.html` | `no-cache, must-revalidate` | 文件名固定，缓存了用户就看不到新版本（而旧版对应的哈希资源可能已被删，直接白屏） |
| `/assets/` | `public, max-age=31536000, immutable` | 内容寻址，永不过期 |
| 图片/字体 | `public, max-age=86400` | 一般不带哈希，给一天 |

同时把 `/assets/` 的 `try_files` 写成 `=404` 而不是回退到 `index.html`：
否则请求一个不存在的 `.js` 会返回 HTML（200 + `text/html`），
浏览器报 `Unexpected token '<'`，排查半天才发现是 404 伪装成了 200。

### 4.4 api 摘掉 `/ui` 后，根路径不能留成 404

`StaticFiles` 挂载和 `Path`/`RedirectResponse` 导入一并删除后，
`GET /` 变成 404。这很糟：直接访问 8000 的人（多半在排查问题）
会以为服务没起来。改成返回一份自描述 JSON：

```json
{"service":"Phase 1 RAG API","notice":"本服务只提供接口，不托管前端页面。",
 "frontend":"页面由独立的 nginx 容器提供（生产 8080，开发 Vite 5173）",
 "docs":"/docs","health":"/health"}
```

### 4.5 配置里删掉「没人读的路径」

`config.py` 里的 `frontend_dir` 一并删除。理由与第二次变更时删 `chroma_dir`
完全一样：留着一个不被任何代码读取的路径，只会让后来者以为
「这里还有一条前端通路」，然后照着它去排查一个不存在的东西。

## 5. 验收（全部实测）

### 5.1 部署层

| # | 项目 | 结果 |
|---|---|---|
| 1 | `docker compose config --quiet` | 通过 |
| 2 | 服务列表 | `chroma postgres redis api frontend` 5 个 |
| 3 | api 的 volumes | 只剩 `./data/uploads`（前端挂载已消失） |
| 4 | frontend 构建 | `exit=0`，33 模块，1.31s |
| 5 | api 构建 | `exit=0` |
| 6 | 五容器状态 | 全部 `Up (healthy)` |
| 7 | **api 容器内 `/app/frontend`** | `No such file or directory` —— 镜像里确实没有前端了 |

### 5.2 静态托管与缓存

| # | 项目 | 结果 |
|---|---|---|
| 8 | `GET /` | HTTP 200 / text/html / 1604 B |
| 9 | 首页内容 | 引用 `/assets/index-znBUs3lF.js` 与 `index-BC2SVm0r.css`（确为 Vite 产物） |
| 10 | 入口页缓存头 | `Cache-Control: no-cache, must-revalidate` |
| 11 | `/assets/*.css` 缓存头 | `public, max-age=31536000, immutable` |
| 12 | gzip | JS 产物响应带 `Content-Encoding: gzip` |
| 13 | 不存在的资源 | `/assets/nope.js` → **HTTP 404**（未伪装成 index.html） |

### 5.3 反向代理与后端

| # | 项目 | 结果 |
|---|---|---|
| 14 | 经 8080 的 `/health` | `status=ok`、`memory_backend=postgres+redis`、`chroma_status=ok` |
| 15 | 经 8080 的 `/api/documents` | HTTP 200 |
| 16 | **api 根路径** | 返回 JSON 指路牌，`application/json`（不再是页面） |
| 17 | **api 的 `/ui/`、`/ui/index.html`** | 均 **404**（前端入口已彻底移除） |

### 5.4 功能链路

| # | 项目 | 结果 |
|---|---|---|
| 18 | **SSE 经 nginx 流式（核心）** | 11 个 token，首 token 1.09s、末 token 1.85s，**跨度 0.76s**，逐条到达未被缓冲 |
| 19 | 端到端问答 | 「校训是：**精勤博学，学以致用**……位于四川省成都市 [来源1]」，命中 4 个来源 |
| 20 | 经 8080 上传 | `{"doc_id":"722092c82f63","filename":"verify.txt","chunks":1,"chars":41}` |
| 21 | 新文档可检索 | 提问「验收口令是什么？」→「ZQ-VERIFY-7788 [来源1]」 |
| 22 | 删除文档 | `{"deleted_chunks":1}`，列表恢复为 1 份 |
| 23 | 原始文件一并清理 | `data/uploads/` 无残留 |

### 5.5 开发服务器（在容器里实跑验证）

宿主机没有 node，于是起了一个 `node:22-alpine` 容器实跑 `npm run dev`：

| # | 项目 | 结果 |
|---|---|---|
| 24 | Vite 启动 | `VITE v6.4.4 ready in 478 ms`，监听 5173 |
| 25 | 首页 | 注入了 `/@vite/client`（确认是 dev server 而非静态文件） |
| 26 | **代理 `/health`** | 经 5173 拿到真实的 `{"status":"ok",...}` |
| 27 | **代理 `/api/documents`** | 经 5173 拿到真实文档列表 |

### 5.6 ★ 真实产物在 jsdom 中执行（31/31 通过）

因为本机开不了浏览器，把容器里构建出的 bundle 下载回来，
在 jsdom 里**真实执行**，并模拟一次完整的流式问答。
这验证了所有「编译器查不出来、只有跑起来才会错」的地方，
尤其是流式那段的 rAF 节流 + 响应式代理写法：

```
PASS  Vue 应用已挂载（#app 非空）  — 2198 字符
PASS  侧栏渲染 / 对话区渲染 / 输入框渲染 / 发送按钮渲染
PASS  品牌标题 / 子标题为「阶段一 · RAG」/ 欢迎语已显示
PASS  文档列表已从 /api/documents 填充（成都东软学院-学校概况.md）
PASS  文档计数为 1
PASS  健康状态显示模型名 / 记忆后端 / 上传上限 50MB / 绿色 dot-ok
PASS  v-model 双向绑定生效
PASS  消息数量 = 欢迎语 + 提问 + 回答  — 实际 3
PASS  用户消息按纯文本渲染
PASS  助手回答已渲染 Markdown（<strong>精勤博学，学以致用</strong>）
PASS  回答正文去掉了 Markdown 记号
PASS  流式光标已移除（.streaming 不再存在）
PASS  引用片段已渲染 <details> / 文件名正确 / 分数 0.7419
PASS  工具条已出现（复制 / 重新生成）
PASS  性能统计已渲染  — 0.04s 首字 · 0.13s 总 · 3 块 · 18 字
PASS  会话 ID 已显示在头部 / 已写入 localStorage
PASS  流式接口被调用过一次
PASS  主题按钮可点击并改变图标（☀ → ☾）/ 主题已落到 <html data-theme>
PASS  提示条已显示

31/31 项通过
```

### 5.7 遗留说明

- 首页 HTML 里保留了我在 `index.html` 写的教学注释（约 1 KB，gzip 后约 1 KB）。
  它随「不缓存的入口页」每次下发。考虑到本项目是教学性质、注释本身是内容的一部分，
  选择保留。若要精简，删掉那段注释即可，不影响功能。
- 旧前端源码已移到 `~/legacy-frontend-20261007`，并打包备份在
  `~/backups/legacy-frontend-20261007-194318.tar.gz`。确认无事可删。

## 6. 回滚

| 目标 | 操作 |
|---|---|
| 退回挂载式前端（第五次后的形态） | `mv ~/legacy-frontend-20261007 frontend`，再 `git revert` 本次提交并 `docker compose up -d --force-recreate api frontend` |
| 只回滚前端镜像 | `docker compose build frontend` 前的镜像层仍在缓存中；或把 compose 的 `image` 改回 `nginxinc/nginx-unprivileged:1.27-alpine` 并恢复挂载 |
| 前端出问题但想先恢复可用 | 旧前端可直接用一个临时容器顶上：`docker run --rm -p 127.0.0.1:8080:80 -v ~/legacy-frontend-20261007:/usr/share/nginx/html:ro nginx:alpine` |
| 数据层 | 完全不受影响（本次未动任何数据卷） |

## 7. 日常命令（第六次变更后）

| 目的 | 命令 |
|---|---|
| 起/更新全栈 | `docker compose up -d` |
| **改完前端后重建** | `docker compose up -d --build frontend` |
| 只重建前端镜像 | `docker compose build frontend` |
| 停/起前端 | `docker compose stop frontend` / `docker compose start frontend` |
| 改完 nginx 配置热载 | `docker compose exec frontend nginx -s reload`（★ 2026-10-14 起挂载已改为整个 `deploy/` 目录，热载可靠；此前挂单文件时它会静默失效，见第八次变更 §4） |
| 看前端日志 | `docker compose logs -f --tail=100 frontend` |
| 本地开发（需 node） | `cd frontend && npm install && npm run dev` → `http://localhost:5173` |
| 开发期连服务器后端 | 先 `ssh -L 8000:127.0.0.1:8000 azureuser@<IP> -N`，再 `npm run dev` |

**已失效的命令**（记下来免得再试）：

| 旧命令 | 现状 |
|---|---|
| 改宿主机 `frontend/*.js` 即时生效 | ❌ 已失效，现在跑的是镜像里的构建产物 |
| 访问 `8000/ui/` 看页面 | ❌ 404，api 不再托管前端 |
| `docker compose exec frontend nginx -s reload` 之外的热更新手段 | 无 |

---

# 第七次变更：取消登录界面，改为单用户本地模式（2026-10-14）

> 目标机：`rag-vm` / `azureuser@20.89.90.58`
> 记录人：WorkBuddy（受用户委托执行）
> 目标：把「个人校园助手」推向「Muse 类个人助手」的第一步 —— 去掉登录，
> 让它成为打开即用的本机应用。整体路线见 `docs/Muse化路线图.md`。

## 1. 一句话

新增 `AUTH_MODE`（`session` | `local`）配置项：置为 `local` 时后端不再要求会话 cookie，
所有请求自动归属到 `LOCAL_OWNER_USERNAME` 指定的身份，前端一并删掉登录表单与退出按钮。

## 2. 动机：为什么不是「把 auth 代码删掉」

「取消登录」最直觉的做法是删掉 `auth_store.py`、摘掉 20 处 `Depends(_current_user)`、
把 `user_id` 从 rag / memory / browser 里抽走。这条路**看起来干净，实际最危险**：

| 做法 | 后果 |
|---|---|
| 删掉 owner 隔离 | 向量片段、会话、凭证都带 `owner_user_id`，外键指向 `app_users`。删掉隔离意味着要么外键插入失败，要么删表 —— 而现存数据是按 owner 存的，删了隔离它们就「全不见」 |
| 删掉 `auth_store` | 加密凭证库（Fernet + 审计表）也在这个模块里。删它等于连同「东软凭证保险箱」一起删 |

所以采用**保留隔离、只换身份来源**：`_current_user` 在 local 模式下返回一个固定的
「本机主人」，下游一行不改。**这是本次能做到零数据迁移的原因。**

## 3. 关键决策

### 3.1 本地身份必须复用 `admin`，不能另起一个用户名

改造前库里已有数据：

| 数据 | 归属 | 换 identity 的后果 |
|---|---|---|
| 知识库文档（4 片段） | `owner_user_id = "shared"` | 无影响（shared 对谁可见） |
| 会话（4 个） | `admin`（`0f9d3a56…`） | 全部看不见 |
| 东软凭证（1 条） | `admin` | **校园自动登录直接失效** |
| 会话（3 个） | `NULL`（owner 隔离上线前的遗留） | 对谁都不可见，本次不动 |

因此 `LOCAL_OWNER_USERNAME=admin`。**这不是可选的**：若写成 `owner` 之类的新名字，
`ensure_local_owner` 会新建一个用户，界面上的表现是「知识库还在、但会话和凭证全空了」，
极容易被误判为数据丢失。

### 3.2 代码默认值仍是 `session`（安全默认）

`config.py` 里 `auth_mode` 默认 `session`，免登录只在 `.env` 里显式写 `AUTH_MODE=local` 才生效。
理由：默认值会在「忘了配 .env」这种最常见的部署事故里生效 —— 默认 local 意味着一次疏忽
就得到一个无鉴权服务；默认 session 最坏只是「要登录」。

### 3.3 local 模式下 `login` / `logout` 返回 404，而不是 200

不能只靠「前端不再调用」。一个仍能签发会话、但会话又被 `_current_user` 完全忽略的接口，
会让调用方（含未来的自己）误以为「登录成功了但界面没变」。返回 404 让这条自相矛盾的
状态无法出现。

### 3.4 前端仍然保留一次 `/api/auth/me` 调用

看起来「都不用登录了为什么还要问身份」。原因：`user_id` 是 localStorage 命名空间的一部分
（`rag.<user_id>.sessions` / `.currentSession`，见 `lib/storage.js`）。跳过它等于
让本地索引换一个命名空间，用户会看到「历史会话全没了」。
所以这次调用保留，但失败语义变了：不再是「去登录」，而是「后端没起来」→ 展示可重试的错误页。

## 4. 改动清单

| 文件 | 改动 |
|---|---|
| `backend/app/config.py` | 新增 `auth_mode`（含取值校验，写错启动即报错）、`local_owner_username`、`is_local_auth` 属性 |
| `backend/app/auth_store.py` | 新增 `ensure_local_owner()`：`INSERT ... ON CONFLICT DO NOTHING` + 回查，幂等；口令写随机值（`password_hash` 非空约束） |
| `backend/app/main.py` | 新增 `_auth_mode()`（运行时读取，可被测试 patch）与 `_local_owner()`（进程内缓存）；`_current_user` 增加 local 分支；`login`/`logout` 在 local 模式返回 404；startup 预热本地身份 |
| `frontend/src/App.vue` | 删除登录表单、用户名/密码状态、`login()`/`logout()`；改为 `bootstrap()` + 可重试错误页；删除已无用的 `watch` 导入与表单 CSS |
| `frontend/src/components/ChatPanel.vue` | 删除「退出」按钮与 `logout` 事件 |
| `frontend/src/lib/api.js` | 删除 `authLogin` / `authLogout`（不再有调用方）；保留 `authMe` |
| `backend/tests/test_auth_store.py` | 原集成测试**显式钉死 `auth_mode="session"`**（否则部署成 local 时它的 401 断言会失去意义）；新增 `LocalSingleUserModeTests` 三个用例 |
| `.env.example` | 新增认证模式段（含「仅本机单人」的前提说明） |
| `docs/Muse化路线图.md` | 新增：Muse 能力对照、现状盘点、阶段 B~E 路线与验收口径 |

## 5. 执行中踩到的坑（如实记录）

### 5.1 ★ 变量名与函数名同名，导致启动直接失败

`main.py` 里我最初写的是：

```python
_local_owner: AuthenticatedUser | None = None      # 缓存变量
def _local_owner() -> AuthenticatedUser: ...       # 同名函数
```

`def` 在模块导入时**把全局名 `_local_owner` 重绑成了函数对象**，于是：

- `if _local_owner is None:` 永假 → `ensure_local_owner()` 从未被调用
- `return _local_owner` 返回的是**函数本身**

现象是容器启动即 `AttributeError: 'function' object has no attribute 'username'`，
`restart: unless-stopped` 让它无限重启。

**这个错误 `python -m py_compile` 查不出来**（语法完全合法），只有真的启动才会暴露。
已改名 `_local_owner_cache`。

**教训**：同一个模块里「缓存变量」与「取缓存的函数」不要用同一个名字。
同类错误的通用判据是：模块级全局名如果同时被 `def`/`class` 绑定过，那它就不再是你赋的那个值。

## 6. 验收（全部实测）

### 6.1 接口行为（经 nginx 正式入口 127.0.0.1:8080）

| # | 项目 | 结果 |
|---|---|---|
| 1 | `GET /api/auth/me`（不带任何 cookie） | **HTTP 200** `{"authenticated":true,"username":"admin","user_id":"0f9d3a56-76c1-4dc7-9695-c8ad01959bfc"}` |
| 2 | `POST /api/auth/login` | HTTP 404 `{"detail":"单用户本地模式未启用账号登录"}` |
| 3 | `POST /api/auth/logout` | HTTP 404 `{"detail":"单用户本地模式没有登录会话"}` |
| 4 | `GET /api/documents` | HTTP 200，含 `成都东软学院-学校概况.md`（shared） |
| 5 | `GET /api/credentials` | **HTTP 200，东软智慧教育平台凭证仍在** ← 零迁移的关键证据 |
| 6 | `GET /api/sessions?assistant_mode=school` | HTTP 200，1 条（`列出资料`，5 轮） |
| 7 | `GET /api/sessions?assistant_mode=student` | HTTP 200，3 条（均 `登录智慧教育平台`） |
| 8 | 启动日志 | `认证模式 local（免登录）：本机主人身份 = admin (user_id=0f9d3a56-…)` |

### 6.2 真实浏览器渲染（Chrome headless，经 SSH 隧道访问真实入口）

| # | 项目 | 结果 |
|---|---|---|
| 9 | 首页 | HTTP 200，1604 B，引用 `/assets/index-CEzvIN46.js` |
| 10 | 渲染后 DOM 中 `input[type=password]` | **0 个** |
| 11 | 渲染后 DOM 中「登录」相关文案 | **0 处** |
| 12 | 主界面已渲染 | `sidebar` / `assistant-tabs` / `了解学校` / `个人助手` / `新会话` / `chat` 全部命中 |
| 13 | 顶栏「退出」按钮 | 已消失（只剩「新会话」） |
| 14 | **真实数据加载（核心）** | DOM 中出现 `成都东软学院-学校概况.md`（文档列表）、`列出资料 · 5 轮`（会话历史）、健康状态 `qwen-plus / postgres+redis` —— 全链路 浏览器→nginx→api→Chroma/PG 无登录打通 |
| 15 | 截图 | 见验证产物（首页直接进入主界面，无任何登录屏） |

### 6.3 构建产物与测试

| # | 项目 | 结果 |
|---|---|---|
| 16 | 前端镜像构建 | 成功；bundle 中 `登录个人校园助手`/`个人助手用户名`/`authLogin`/`authLogout`/`退出个人助手`/`请先登录` 均 **0 次**；`正在启动本机助手`/`无法连接到本机助手` / `api/auth/me` 各 1 次 |
| 17 | 后端测试套件（容器内实跑） | **40 tests, OK**（含原有 session 隔离用例 + 新增 3 个 local 用例） |
| 18 | `docker compose config --quiet` | 通过 |
| 19 | 六容器状态 | 全部 `Up (healthy)` |

### 6.4 数据未被污染

| # | 项目 | 结果 |
|---|---|---|
| 20 | `app_users` | 仅 `admin` / `test` 两行，测试用户已清理 |
| 21 | `encrypted_credentials` | 仍归属 `admin`，`nsu-smart-education` / 东软智慧教育平台 |
| 22 | `sessions` | 7 行（4 行有 owner + 3 行历史遗留 NULL），未被改动 |
| 23 | Chroma 向量片段 | 仍为 **4** 片，全部 `owner_user_id=shared`，文件仍只有成都东软学院那份 |

## 7. 已知问题 / 遗留

| 项 | 说明 | 处置 |
|---|---|---|
| **「恢复上次会话」实际不生效**（既有 bug，非本次引入） | `App.vue` 启动流程里 `setCurrent(null)` 会**删掉** localStorage 里的 currentSession，紧接着 `initializeApplication()` 又去 `getCurrent()` 读它 —— 必然读到 null，于是永远显示欢迎语。实测印证：会话列表里有「列出资料 5 轮」，但对话区显示的是欢迎语。本次**只做等价搬移，未顺手修**（属行为变更，应单独评审） | 待确认后单独立项修复 |
| **local 模式无鉴权** | 免登录 = 服务不再校验任何身份。仅对「端口只绑 127.0.0.1 + 经 SSH 隧道 + 单人使用」成立 | 公网部署 / 多人使用前必须切回 `AUTH_MODE=session` |
| 会话列表仍有 3 条 `owner_user_id IS NULL` 的遗留 | owner 隔离上线前的数据，对任何身份都不可见 | 不清理（无害），若要清理需先确认是否为有价值的历史 |
| `.env` 中的 `AUTH_MODE` 与代码默认值不一致（本机 local / 代码 session） | 这是有意的：代码给安全默认，部署显式选择 | 无 |

## 8. 回滚

```bash
cd ~/phase1-rag

# 方式一（最小、推荐）：只改配置，不动代码
sed -i '/^AUTH_MODE=/d; /^LOCAL_OWNER_USERNAME=/d' .env
docker compose up -d --force-recreate api
# → 立刻回到需要登录的原行为（登录界面前端仍在旧镜像里，见方式二）

# 方式二：连代码与前端一起回退
cp ~/backups/pre-localmode-20261014-211954/changed-files-before.tar.gz /tmp/
tar xzf /tmp/changed-files-before.tar.gz -C ~/phase1-rag
cp ~/phase1-rag/.env.bak-localmode ~/phase1-rag/.env
docker compose up -d --build api frontend
```

改动前快照：`~/backups/pre-localmode-20261014-211954/changed-files-before.tar.gz`
（含 `main.py` / `config.py` / `auth_store.py` / `test_auth_store.py` / `App.vue` /
`ChatPanel.vue` / `api.js` / `.env.example` / `.env`），另有 `.env.bak-localmode`。

## 9. 日常命令差异

| 目的 | 命令 | 说明 |
|---|---|---|
| 改回需要登录 | 删掉 `.env` 里的 `AUTH_MODE`，`docker compose up -d --force-recreate api` | 代码默认即 session，无需改码 |
| 切换本地身份 | 改 `.env` 的 `LOCAL_OWNER_USERNAME` 后重建 api | ⚠️ 换名字等于换 owner，界面会「看起来数据全空」 |
| 跑后端测试 | 见下方一条命令 | api 镜像里不含 `tests/`，需挂载 |

```bash
# 后端测试（在容器内跑，因宿主无 Python 依赖环境）
NET=$(docker inspect rag-postgres --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}
{{end}}' | head -1)
docker run --rm --network "$NET" --env-file .env \
  -v "$PWD/backend/tests:/app/tests:ro" -w /app \
  phase1-rag-api:0.1.0 python -m unittest discover -s tests -p 'test_*.py'
```

---

# 第八次变更：nginx 来源校验（让「误改端口」不再是明文泄露）+ 修掉一个会让配置静默失效的挂载陷阱

> 时间：2026-10-14
> 起因：阶段 E 分发形态定稿后复盘 `AUTH_MODE=local` 的安全边界（详见 `Muse化路线图.md` §5.2）。

## 1. 一句话

`local` 模式下后端不做任何鉴权，访问控制此前**完全依赖**「端口只绑 `127.0.0.1`」这一条；
本次在 nginx 层补上来源校验，使「有人为了手机也能连而把端口改成 `0.0.0.0`」变成 403。
实施过程中还发现并修掉了一个会让 nginx 配置**静默不生效**的挂载陷阱。

## 2. 动机（读码确认的威胁面）

| 事实 | 位置 |
|---|---|
| `_current_user` 对任何请求都直接返回本地主人，无鉴权 | `backend/app/main.py` |
| `GET /api/credentials` 可列出全部凭证 ID | 同上 |
| `GET /api/credentials/{credential_id}` 返回**明文口令** | `auth_store.py:487-516` |

三者叠加：端口一旦被改成 `0.0.0.0`，等于在局域网公开一个「列口令清单 + 读明文」的接口。
本次目标不是改掉该接口的返回行为（它被 `test_auth_store.py:159` 钉死，属产品决策），
而是**让这种误配置不成立**。

## 3. 改动清单

| 文件 | 改动 |
|---|---|
| `deploy/nginx-frontend.conf` | server 块新增：`allow 127.0.0.0/8; allow 172.16.0.0/12; deny all;` |
| `docker-compose.yml` | frontend 挂载由「单个文件」改为「整个 `deploy/` 目录」 |
| `deploy/nginx-frontend.conf` 头注释 | 修正「改完 reload 即可」的说法，并给出「必须确认容器内是新内容」的收工检查 |

## 4. ★ 执行中踩到的坑：挂单个文件时，容器持有的是当时的 inode

第一次实施后，`nginx -t` 通过、`nginx -s reload` 报告成功、`curl` 首页 200 —— 三处都正常，
看起来已经生效。但用一台 `192.168.77.0/24` 的隔离网络做反向验证时，来源 `192.168.77.3`
拿到了 **200**，守卫根本没生效。

| 位置 | inode | 字节数 |
|---|---|---|
| 宿主机 `deploy/nginx-frontend.conf` | 786623 | 8583 |
| 容器内 `/etc/nginx/conf.d/default.conf` | 803397 | 6768 |

根因：`tar` 解包会 unlink 后重建文件 → inode 变了，而**单文件 bind mount 挂的是旧 inode**。
于是 `nginx -t` 校验的是旧配置、`reload` 加载的也是旧配置，全程零报错。

**修法**：改为挂整个目录（`./deploy:/etc/nginx/conf.d:ro`）。目录挂载按路径查找，
目录内文件被替换后立即可见。附带影响：容器内配置文件名变为 `nginx-frontend.conf`
（仍是 `*.conf`，会被加载），镜像自带的 `default.conf` 同时被遮蔽。

**教训（两条，都值得记住）**：
1. **「配置已生效」不能用 `nginx -t` 通过来证明**，必须验证容器内看到的内容。
2. **反向用例比正向用例更能发现「改动没生效」** —— 正向用例在改动完全没生效时同样是绿的。
   本次若只测「合法访问仍 200」，会得到一个完全错误的安全结论。

## 5. 验收（全部实测）

| 项 | 结果 |
|---|---|
| 容器内确认为新配置 | `allow` 2 条、`deny all` 1 条、9168 字节（与宿主机一致） |
| `nginx -t` | configuration file syntax is ok |
| 合法路径（宿主机 → `127.0.0.1:8080`） | `/` 200、`/api/auth/me` 200、`/health` 200 |
| 容器 healthcheck（容器内 `127.0.0.1`） | `rag-frontend` healthy，未受影响 |
| **对照组**：来源在 compose 网段（172.x） | 200 放行 |
| **实验组**：来源 `192.168.77.0/24` | `/` 403、`/api/auth/me` 403、`/api/credentials` 403 |
| nginx 日志 | `access forbidden by rule, client: 192.168.77.3` |
| 真实浏览器渲染（Chrome headless + SSH 隧道） | DOM 与加门禁前**逐字节一致**（4955 字节），截图无变化，无登录表单 |

## 6. 已知缺口（不要当成完备）

| # | 缺口 |
|---|---|
| 1 | `api:8000` 是独立的端口发布路径，不经过 nginx，本次未加守卫 |
| 2 | 局域网若使用 `172.16.0.0/12`（少见；多为 `192.168.x` 或 `10.x`），该来源会被放行 |
| 3 | 本层防的是**误配置**，不防能直接访问宿主机网络栈的定向攻击 |
| 4 | ~~`/api/credentials/{id}` 仍返回明文口令，行为被既有测试钉死，未擅改~~ → **已在第九次变更解决**：默认脱敏 + 显式 `POST /api/credentials/{id}/reveal` |

## 7. 回滚

删掉 `deploy/nginx-frontend.conf` 里的 `allow`/`deny` 三行即可（挂载方式的改动是独立修复，可保留）。
注意：改完必须 `docker compose restart frontend`，或至少确认容器内内容 —— 不要只 `reload`。

## 8. 日常命令差异

| 目的 | 命令 |
|---|---|
| 改 nginx 配置后生效 | `docker compose exec frontend nginx -s reload` |
| ★ 确认容器内就是新内容（收工必做） | `docker exec rag-frontend grep -c <新关键字> /etc/nginx/conf.d/nginx-frontend.conf` |
| 容器内配置文件路径 | `/etc/nginx/conf.d/nginx-frontend.conf`（不再是 `default.conf`） |

---

# 第九次变更：凭证详情接口默认脱敏，取明文改走显式 `POST /reveal`

> 时间：2026-10-14
> 起因：第八次变更「已知缺口 #4」—— `/api/credentials/{id}` 返回明文口令，行为被既有测试钉死，当时判断属产品决策、未擅改。

## 1. 一句话

凭证详情接口不再回传口令明文（返回固定掩码，并带 `password_revealed: false`）；
需要明文时调 `POST /api/credentials/{id}/reveal`，该动作在审计里记为 `reveal`。
**全项目唯一真正需要明文的调用点（浏览器自动登录）已显式声明，并加了防御断言，杜绝掩码被当成口令提交。**

## 2. 动机：这不是「改掉一个产品行为」，而是拆掉一个纯暴露面

第八次变更时我把这条留给你决策，理由是「`test_auth_store.py:159` 钉死了它，说明是有意为之」。
读码复核后可以确认：**这个接口没有任何消费方。**

| 事实 | 位置 |
|---|---|
| 前端全仓没有调用 `/api/credentials/{id}`（只有 fetch 的 `credentials: 'same-origin'` 选项） | `frontend/src` 全仓检索 |
| 真正需要解密口令的是服务端内部路径，不经过 HTTP | `main.py:_execute_login_tool` 直接调 `auth_store.get_credential()` |

所以「被测试钉死」钉住的其实是一个**功能上不必要的暴露面**：它让「端口误暴露」的后果
从「泄露元数据」升级为「泄露可直接使用的口令」。

## 3. 设计：默认脱敏，而不是默认明文 + 遮蔽开关

`get_credential()` 的默认返回值是脱敏的；取明文必须显式传 `reveal_password=True`。
方向是刻意选的：

| 方向 | 忘记传参数的后果 |
|---|---|
| ❌ 默认明文 + `mask=True` | **静默泄露**。新调用点只要忘了加参数，就多一个返回明文的接口，且不会有任何报错 |
| ✅ 默认脱敏 + `reveal_password=True` | **功能失败**。新调用点拿不到口令，自动登录会失败 —— 问题会暴露，属安全的失败方向 |

配套两点：

1. **脱敏路径连密文都不解密** —— 既然不会用到明文，就没必要把它解出来放进内存。
2. **返回 `password_revealed` 标记** —— 让「当前是不是明文」在数据里可见，调用方可以据此断言，
   而不必靠猜那个 `********` 到底是真口令还是掩码。

## 4. 为什么取明文用 POST，而不是 `GET ?reveal=true`

第一条是硬约束，第二条是惯例：

1. 中间件给敏感响应加了 `Cache-Control: no-store`，但**管不住访问日志**。nginx 记的是 `$request`，
   包含 query string。用 GET 会让每次取明文在日志里留下 `.../api/credentials/xxx?reveal=true`，
   等于亲手制造一个新的可观测泄露面。
2. 「把口令取出来」是一次敏感操作，语义上属于动作而非查询；项目对浏览器会话操作的既有约定
   也是 POST（`/api/browser-session/action`）。

## 5. 改动清单

| 文件 | 改动 |
|---|---|
| `backend/app/auth_store.py` | 新增 `MASKED_PASSWORD`；`get_credential()` 增加仅关键字参数 `reveal_password=False`；脱敏时跳过口令解密；审计动作按 `read`/`reveal` 区分；新增 schema 迁移（见 §6） |
| `backend/app/main.py` | `GET /api/credentials/{id}` 改为返回脱敏视图（路径与响应形状不变）；**新增** `POST /api/credentials/{id}/reveal`；`_execute_login_tool` 显式 `reveal_password=True` 并加防御断言 |
| `backend/tests/test_auth_store.py` | 原「详情接口必须返回明文」断言改为「默认脱敏 + reveal 取明文」；新增脱敏彻底性、跨用户 reveal 404、审计动作可区分 |
| `backend/tests/test_browser_login.py` | mock 接受 `reveal_password` 关键字；新增用例钉住「漏传 reveal 时必须安全失败」 |

## 6. ★ 执行中踩到的坑：CHECK 约束会让新审计动作插不进去

`credential_audit.action` 上有 CHECK 约束：

```sql
CHECK (action IN ('create', 'read', 'update', 'delete'))
```

它是**匿名约束**，PostgreSQL 自动命名为 `credential_audit_action_check`（已在库中用 `\d` 确认）。

问题在于 `CREATE TABLE IF NOT EXISTS` 对已存在的表**完全不起作用** —— 只改建表语句，
旧库的约束依然是旧版本。不管它的话，插入 `'reveal'` 会被数据库拒绝，
表现是**取明文接口直接 503**，而不是一个容易看懂的报错。

幂等迁移写法：把两种可能的名字都 DROP 掉，再重建显式命名的约束。

```sql
ALTER TABLE credential_audit DROP CONSTRAINT IF EXISTS credential_audit_action_check;
ALTER TABLE credential_audit DROP CONSTRAINT IF EXISTS ck_credential_audit_action;
ALTER TABLE credential_audit ADD CONSTRAINT ck_credential_audit_action
  CHECK (action IN ('create', 'read', 'update', 'delete', 'reveal'));
```

先删后加：重复启动不会因「约束已存在」而失败。

**顺带一条验证方法上的教训**：光看到「GET 详情返回 200 且没有明文」不足以证明脱敏生效 ——
如果接口被改坏（比如抛异常返回空、或 reveal 路径也一起坏了），观测结果同样是「没有明文」。
必须**成对验证**：脱敏路径拿不到明文 **且** reveal 路径仍拿得到明文。
这与第八次变更的教训是同一回事 —— 只有正向断言时，「改动没生效」和「改动生效」可能给出相同观测。

## 7. 验收（全部实测，2026-10-14）

| 项 | 结果 |
|---|---|
| `GET /api/credentials/{id}` | HTTP 200，`password_revealed=false`，`password` 为掩码，响应体中不含明文 |
| `POST /api/credentials/{id}/reveal` | HTTP 200，`password_revealed=true`，返回真口令（与非掩码值不同、长度 10） |
| `POST /api/credentials/{不存在}/reveal` | HTTP 404 `未找到该校园账号` |
| 缓存头（**两条路径分别验证**） | 直连 `api:8000` 与经 `nginx:8080` 均返回 `cache-control: no-store` + `pragma: no-cache` |
| 审计表 | 取明文后新增一条 `reveal` 记录；动作分布 `create 1 / read 29 / reveal 1` |
| **schema 迁移** | 约束已变为 `ck_credential_audit_action` 且定义含 `reveal`；旧的匿名 `credential_audit_action_check` 已不存在 |
| 后端测试 | **41/41 OK**（新增 1 个防御用例，总数由 40 → 41）；用例运行日志中可见防御断言被真实触发 |
| 前端真实渲染（Chrome headless + SSH 隧道） | DOM **4955 字节，与改动前逐字节一致**；无 `input[type=password]`；侧栏正常 |

## 8. 残余风险（这层防不住什么）

| # | 缺口 |
|---|---|
| 1 | **元数据仍是可读 PII**：`account` / `student_id` / `full_name` / `site_url` 照常返回。本次只掩码了 password |
| 2 | **历史审计无法区分**：迁移前那 26 条 `read` 里混着「脱敏读」与「明文读」，无法回溯重分类。新记录才可区分 |
| 3 | 本层与第八次变更的 nginx 来源校验互补，但都不防能直接访问宿主机网络栈的定向攻击 |

## 9. 回滚

改回「详情接口直接返回 `reveal_password=True` 的结果」即可退回到旧行为（不建议）。
注意：**审计动作的 CHECK 约束迁移不要回滚** —— 表里已经存在 `reveal` 行，
把约束改回 4 个动作会让这些历史行变成非法数据。

## 10. 日常命令差异

| 目的 | 命令 |
|---|---|
| 查看脱敏是否生效 | `curl -s http://127.0.0.1:8080/api/credentials/<id>` → `password` 应为 `********` |
| 显式取明文 | `curl -s -X POST http://127.0.0.1:8080/api/credentials/<id>/reveal` |
| 查取证操作记录 | `docker exec rag-postgres psql -U rag -d ragdb -c "SELECT action, count(*) FROM credential_audit GROUP BY 1;"` |


---

# 第十次变更：供应商凭证解耦（chat 与 embedding 分家）+ 接入智谱 GLM

> 时间：2026-10-15
> 起因：阿里云百炼账户欠费，**chat 与 embedding 全线**返回 400 `Arrearage`
> （实测 `docker exec rag-api python -c "...get_chat_model().invoke(...)"` → `CHAT_FAIL 400`，
> 即不只是向量化挂了，对话也挂了）。
> 由于原实现里两者**共用同一把 key 与 base_url**，想换任何供应商都必须先做解耦 ——
> 否则「换成一家只有 chat 的服务（如 DeepSeek）」这条路根本走不通。

## 1. 一句话

把「一个 key 同时用于 chat 和 embedding」改成**两条独立可配的链路**，
并保留旧变量 `DASHSCOPE_*` 作为回退，使既有 `.env` 不改也能跑。

## 2. 改动面（全量，含一个隐藏引用点）

| 文件 | 改动 |
|---|---|
| `backend/app/config.py` | 新增 `LLM_API_KEY` / `LLM_BASE_URL` / `EMBEDDING_API_KEY` / `EMBEDDING_BASE_URL`；`dashscope_*` 降级为回退；新增 `_resolve_provider_credentials()`（`model_validator(mode="after")`）做回填 |
| `backend/app/llm.py` | 两个工厂各读各自字段；错误信息中性化并同时列出新旧变量名；新增 `_safe_endpoint()` 只回显主机（避免把 URL 内嵌的凭据写进日志） |
| `backend/evaluation/evaluate_ragas.py` | **隐藏引用点**：`settings.dashscope_*` → `settings.llm_*`。漏改即为半迁移（评测判官模型会拿到空 key） |
| `.env.example` | 改为新变量名；旧写法降级为「示例 B」并标注仍可用 |
| `deploy.sh` | 凭证检查按**与 app 完全相同的回退链**解析；打印端点时标注「与 chat 是否同源」 |
| `backend/tests/test_llm_provider_config.py` | **新增 10 条离线单测**（回退链 / 解耦 / 失败方向 / 空白裁剪 / 跨端点告警） |

## 3. 回退链（单一真相源）

实现位置在 `config.Settings._resolve_provider_credentials()`，**刻意不放在 llm.py**。
理由：回退若散落在各调用点，就会出现「`/health` 报的是 A 供应商、请求实际打到 B 供应商」
这类最难查的分裂。

```
llm_api_key        ← LLM_API_KEY        ‖ DASHSCOPE_API_KEY
llm_base_url       ← LLM_BASE_URL       ‖ DASHSCOPE_BASE_URL
embedding_api_key  ← EMBEDDING_API_KEY  ‖ llm_api_key
embedding_base_url ← EMBEDDING_BASE_URL ‖ llm_base_url
```

### ★ 一个刻意的行为改动（以及为什么必须改）

`dashscope_base_url` 的**字段默认值**由 `https://dashscope.aliyuncs.com/compatible-mode/v1`
改为**空串**，旧行为改由常量 `_LEGACY_DASHSCOPE_BASE_URL` 在「检测到 `DASHSCOPE_API_KEY`」时显式重建。

不改的后果：新用户只填 `LLM_API_KEY`（比如智谱的 key）而忘填 `LLM_BASE_URL` 时，
会**静默**拿到百炼的默认地址 → 把智谱的 key 发给百炼 → 401，
而错误信息里不含任何「base_url 不对」的线索。
改成空串后，同一种失误会得到一个指名道姓的 `RuntimeError`（安全的失败方向）。

## 4. 迁移（服务器 `.env`）

**就地手术式改写，不重建文件** —— 重建会丢掉只在服务器上存在的
`CREDENTIAL_ENCRYPTION_KEY`、`POSTGRES_PASSWORD` 等密钥（本地那份 `.env` 是过期副本）。

- 备份：`.env.bak-20261015134818-predcople`（权限 600）
- 旧的 `DASHSCOPE_API_KEY` / `DASHSCOPE_BASE_URL` 改为**注释保留备查**，不再生效
- 新增/替换：`LLM_API_KEY`、`LLM_BASE_URL=https://open.bigmodel.cn/api/paas/v4`、
  `LLM_MODEL=glm-4-flash`、`EMBEDDING_MODEL=embedding-3`、
  `EMBEDDING_BATCH_SIZE=64`（百炼上限 10，智谱为 64）

## 5. 验证（实测）

| 项 | 结果 |
|---|---|
| 全量后端测试 | **72 项通过 / 17 skip / 0 失败**（本次新增 10 条全绿） |
| 未回退到旧变量 | 启动日志**无**「检测到旧变量 DASHSCOPE_*」告警 ⇒ 新变量确实生效 |
| embedding 已独立配置 | `Embeddings = embedding-3 @ https://open.bigmodel.cn (dim=1024, batch=64)` |
| chat 真实链路 | `CHAT_FAIL: 401 {'code':'401','message':'令牌已过期或验证不正确'}` |
| embedding 真实链路 | `EMBED_FAIL: 401 {'code':'401','message':'令牌已过期或验证不正确'}` |
| **★ 解耦的端到端证据** | 错误码由百炼的 `Arrearage` 变为智谱的 `令牌已过期或验证不正确` ⇒ **两条链路都已独立打到智谱**（不再是百炼） |
| 容器 | api / worker 重建后均 `healthy` |

## 6. 未完成（阻塞在密钥）

**当前 key 无效**：`0538398b9f334bafbd5246b53009137d` 是纯 32 位、**无点号**。
智谱 API Key 的标准格式是 **`{id}.{secret}`**（官方 JWT 示例即 `id, secret = apikey.split(".")`），
少一段必然 401。换成完整 key 后需重跑三项验收：
① chat 连通 ② embedding 连通 ③ 上传文档 → 入库 → 提问（真 E2E）。

**另一处待核**：模型名。`glm-4-flash` 在免费档里可能已更名为 `glm-4.7-flash`
（据公开资料，`glm-4.5-flash` 已于 2026-01-30 下线并自动路由）。key 修好后以实测为准。

## 7. 残余风险

| # | 缺口 |
|---|---|
| 1 | 模型名与端点的匹配未经真实调用验证（`LLM_MODEL=glm-4-flash` 目前只是假设） |
| 2 | 向量库里 4 个片段由 `text-embedding-v4` 生成，与 `embedding-3` **向量空间不同** ⇒ key 修好后必须清库重传（源文件 `data/uploads/4271c477f93f.md` 仍在，无需重新上传） |
| 3 | `llm_model` / `embedding_model` 的默认值仍是百炼的 `qwen-plus` / `text-embedding-v4`：换供应商时忘改模型名，报错发生在**调用时**而非启动时（应由 E.3 #1 的首次运行向导接管） |
| 4 | `LLM_PROVIDER=local` 这条路仍是断的（`LOCAL_LLM_BASE_URL` 指向 compose 里不存在的 `llama` 服务）—— 对应 E.3 #7，本次未动 |

## 8. 回滚

```bash
cd ~/phase1-rag
cp .backup-20261015-decouple/config.py      backend/app/config.py
cp .backup-20261015-decouple/llm.py         backend/app/llm.py
cp .backup-20261015-decouple/evaluate_ragas.py backend/evaluation/evaluate_ragas.py
cp .backup-20261015-decouple/.env.example   .env.example
cp .backup-20261015-decouple/deploy.sh      deploy.sh
cp .env.bak-20261015134818-predcople        .env
docker compose up -d --build api && docker compose up -d api worker
```

## 9. 日常命令差异

| 目的 | 命令 |
|---|---|
| 看当前生效的供应商 | `docker logs rag-api \| grep -E 'LLM =|Embeddings ='` |
| 直接读容器内的解析结果 | `docker exec rag-api python -c "from app.config import get_settings as g; s=g(); print(s.llm_base_url, '|', s.embedding_base_url)"` |
| 只测 chat（不走向量库） | `docker exec rag-api python -c "from app.llm import get_chat_model as f; print(f().invoke('ping').content)"` |
| 只测 embedding | `docker exec rag-api python -c "from app.llm import get_embeddings as f; print(len(f().embed_query('测试')))"` |

---

# 第十一次变更：智谱链路端到端验收 + 修掉两个会「删掉源文件」的缺陷（2026-10-15 下午）

> 时间：2026-10-15 下午
> 起因：第十次变更做完供应商解耦后，卡在「智谱 key 不完整」（纯 32 位、无 `.` 分隔符）。
> 本轮拿到完整 key 收尾验收，并在验收过程中**实测踩到两个真实缺陷**，一并修掉。

## 1. 一句话

智谱（GLM）链路已**端到端跑通**：上传 → 队列 → worker 向量化 → 检索问答全部成功。
同时修掉 `delete_document` 的两个缺陷，其中第一个**真的删掉过一份源文件**，已记录在案。

## 2. 智谱 key 形态与配置

完整 key 形如 `{id}.{secret}` —— **32 位 id + `.` + 16 位 secret，总长 49**。
第十次变更时拿到的 `0538398b9f334bafbd5246b53009137d`（纯 32 位、无点号）就是因此 401 的。
官方 JWT 示例写得很直白：`id, secret = apikey.split(".")`。

实测确认（2026-10-15）：

| 项 | 实测结果 |
| --- | --- |
| chat 模型名 | `glm-4-flash` **仍然存在**（无需改成 `glm-4.7-flash`） |
| embedding 模型 | `embedding-3`，`dimensions=1024` 生效 |
| embedding 批量上限 | **64** 条一次成功（百炼是 10） |
| base_url | `https://open.bigmodel.cn/api/paas/v4`（注意不是 `/v1`） |

`.env` 实际改动只有一行：`LLM_API_KEY` 换成完整 key。
`EMBEDDING_API_KEY` / `EMBEDDING_BASE_URL` **刻意不单独设置** —— 它们按
`config.Settings._resolve_provider_credentials()` 的回退链跟随 chat 配置，
正好都落在智谱。少一处可漂移的配置。

## 3. 端到端验收结果

**清库重传的原因**：旧库 4 个片段由 `text-embedding-v4` 生成。
不同模型的向量空间不通用（与维度是否相同无关），换 `embedding-3` 后必须重建。

| 环节 | 证据 |
| --- | --- |
| chat 连通 | 容器内 `get_chat_model().invoke(...)` → `CHAT_OK: 正常` |
| embedding 连通 | `get_embeddings().embed_query(...)` → `EMBED_OK: dim = 1024` |
| 上传契约 | `POST /api/documents/upload` → **202 + `mode:"queued"` + `task_id`** |
| worker 执行 | worker 日志 `POST https://open.bigmodel.cn/api/paas/v4/embeddings "HTTP/1.1 200 OK"` |
| 入库结果 | `status=succeeded`，`chunks=5`，`embedded 5/5 chunks` |
| 检索问答 | 4 个问题全部答对，且 `sources` 指向正确的 `doc_id` / `filename` |

问答抽样（`assistant_mode` 只接受 `school`/`student`，**不是** `knowledge`，传错会 422）：

| 提问 | 回答 |
| --- | --- |
| 校训是什么 | 精勤博学，学以致用 `[来源2]` |
| SOVO 哪年成立 | 2004 年，附完整荣誉列表 `[来源3]` |
| 何时升格本科 | 2011 年 5 月，四川省第一所独立设置的民办本科高校 `[来源3]` |
| 校长与地址 | 张应辉；都江堰市青城山镇东软大道 1 号 `[来源1]` |

## 4. 两个缺陷（都在 `delete_document`）

### 4.1 缺陷一：删除范围与文件范围不一致 ⇒ **真的删掉了源文件**

旧实现：

```python
ids = matched.get("ids") or []      # 按 (doc_id, owner_user_id) 精确匹配
if ids:
    store.delete(ids=ids)
    invalidate_corpus_cache()
for path in Path(s.upload_dir).glob(f"{doc_id}.*"):   # ← 在 if 之外，无条件执行
    path.unlink()
```

向量按「doc_id + 归属」匹配，文件却按 `doc_id` 无条件 glob。
只要归属对不上，就产生**破坏性中间态**：向量一个没删、源文件已经没了。

实测（文档 owner 是 `shared`、请求者不是它）：

```
DELETE /api/documents/4271c477f93f  →  {"detail":"未找到该文档"}  [HTTP=404]
向量数：4 → 4（没删）
uploads/4271c477f93f.md：已消失  ★
```

结果是一份「有向量、没源文件」的文档：既删不掉，也无法重新入库。
**这正是本项目丢掉那份演示文档的直接原因。**

### 4.2 缺陷二：shared 文档谁都删不掉

归属过滤是精确相等，而 `"shared"` 不是任何真实 `user_id`，
于是公共知识库的文档在 UI 上**永远返回 404**，用户没有删除入口。

### 4.3 修复方式

`rag.delete_document(doc_id, owner_user_id, *, allow_shared=False)`

1. **先按 `doc_id` 取全部片段（连元数据）**，分成三种「删不到」：
   - ① `doc_id` 根本不存在 → 顺手清孤儿文件，返回 0
   - ② 存在但一段都不归你 → **什么都不动**（尤其不碰文件），返回 0
   - ③ 存在且有可删片段 → 删片段；**片段清空后**才删文件
2. **只有在删掉的片段数 ≥ 该 doc_id 的全部片段数时，才允许删源文件** ——
   杜绝「有向量、没源文件」的死文档。
3. 归属判断**取回来在本地做**，不写进 Chroma 的 `where`：
   需要的条件形如 `doc_id == X AND (owner == me OR owner == "shared")`，
   而 Chroma 对「`$or` 嵌在 `$and` 里」的支持随版本变化，实测容易行为不一致。
   一个 doc 的片段数很少，本地过滤零风险。
4. `allow_shared` 只在本机单人模式（`AUTH_MODE=local`）由 `main.py` 打开 ——
   那种场景里 `"shared"` 就是主人自己上传的资料，删不掉才是错的。
   多用户（session）模式保持原语义。

新增常量 `rag.SHARED_OWNER_ID = "shared"`，原来这个字面量散落在多处。

## 5. 事故记录：演示文档丢失（不可恢复）

| 项 | 内容 |
| --- | --- |
| 丢失文件 | `data/uploads/4271c477f93f.md`（6,285 字节，成都东软学院-学校概况） |
| 原因 | 缺陷一 —— 删除返回 404，但源文件被无条件删除 |
| 恢复尝试 | 本地副本、服务器 `~`/`/tmp`/`/var/tmp` 全盘查找、Chroma SQLite（`embedding_fulltext_search_data` / `embeddings_queue`）—— **均无**（那些二进制块是 HNSW 索引，不含原文） |
| 处置 | 依据学校官网公开信息**新建**了一份替代文档（`成都东软学院-学校概况.md`，6,177 字节，5 个片段）。**内容非原文**，仅作演示语料 |

## 6. 测试

新增 `backend/tests/test_document_delete.py`，8 条用例。设计要点：
**每条都同时在两层断言** —— ① 返回值（接口语义）② 磁盘副作用（真正闯祸的那层）。

关键用例：

- `test_shared_doc_without_permission_keeps_vectors_and_source` —— 事故本身的正向复现
- `test_partial_ownership_keeps_source` —— 片段没删净时源文件必须保留
- `test_similar_doc_id_file_is_not_touched` —— 反向用例：`doc-abc.*` 不能命中 `doc-abc2`

**有效性验证（重要）**：同一组用例在旧代码上必须失败，否则测试没有价值。
实测旧代码：

```
接口返回删除片段数 = 0
向量剩余         = 2
源文件是否还在   = False      ★ BUG 复现
```

新代码：

```
接口返回删除片段数 = 0
向量剩余         = 2
源文件是否还在   = True       ✔ 删不掉就什么都不动
```

全量回归：**80 项通过 / 17 skip / 0 失败**（第十次变更为 72 项，新增 8 项）。

真实 API 上再验一次（把 owner 手工改成无权限用户 / `shared`）：

| 场景 | 期望 | 实测 |
| --- | --- | --- |
| 无权限删除 | 404 + 文件保留 + 向量不动 | 404，文件在，向量 5 个未动 |
| shared（本机模式） | 200 + 文件同步清除 | `{"deleted_chunks":5}`，文件消失，向量 0 |

## 7. 改动文件

| 文件 | 改动 |
| --- | --- |
| `backend/app/rag.py` | 新增 `SHARED_OWNER_ID`、`_discard_document_files()`；重写 `delete_document()` |
| `backend/app/main.py` | 删除路由传 `allow_shared=_auth_mode() == "local"` |
| `backend/tests/test_document_delete.py` | **新增** 8 条回归测试 |
| `.env`（服务器侧） | 仅 `LLM_API_KEY` 换成完整 key |

## 8. 回滚

```bash
cd ~/phase1-rag
cp .backup-20261015-deletefix/rag.py  backend/app/rag.py
cp .backup-20261015-deletefix/main.py backend/app/main.py
cp .env.bak-20261015135821-prekey .env
docker compose build api && docker compose up -d api worker
```

## 9. 日常命令差异

| 目的 | 命令 |
| --- | --- |
| 看知识库现状 | `curl -s http://127.0.0.1:8000/api/documents` |
| 看向量总数 | `docker exec rag-api python -c "from app.vectorstore import get_vectorstore as g; print(g()._collection.count())"` |
| 看落盘源文件 | `ls -la ~/phase1-rag/data/uploads/` |
| 提问（**注意 mode 取值**） | `curl -s -X POST http://127.0.0.1:8000/api/chat -H 'Content-Type: application/json' -d '{"question":"...","assistant_mode":"school"}'` |
| 删除文档 | `curl -s -X DELETE http://127.0.0.1:8000/api/documents/<doc_id>` |
| 一致性自检（文档数 vs 源文件数） | 见下方脚本 |

一致性自检（三者应互相对应：文档列表 / 向量片段 / uploads 文件）：

```bash
cd ~/phase1-rag
curl -s http://127.0.0.1:8000/api/documents
docker exec rag-api python -c "from app.vectorstore import get_vectorstore as g; print('vectors =', g()._collection.count())"
ls -la data/uploads/
```

---

# 第十二次变更：站点注册表（阶段 C ① —— 「助手能操作哪些站点」从代码搬进数据库）

> 时间：2026-10-15
> 起因：`Muse化路线图.md` 阶段 C ①。改造前同一个站点的信息硬编码在**两个容器的四处**：
> `backend/app/main.py`（`_LOGIN_TARGETS` / `_LOGIN_TARGET_ALIASES`）、
> `backend/browser/mcp_server.py`（`ALLOWED_HOST` / `ALLOWED_LOGIN_URL` /
> `USERNAME_SELECTOR` / `PASSWORD_SELECTOR` / `_BLOCKED_PATH_PARTS`）。
> 加一个站点要同时改两个容器的 Python 并重建镜像。

## 1. 一句话

「助手能操作哪些站点」从代码常量搬进 `sites` 表，由用户在界面上登记；
**新增站点不改任何 Python、不重建镜像、不重启服务**，且下一个句子立刻生效。

## 2. 为什么真相源放 api 侧，而不是让 browser 直连数据库

| | A. browser 直连 PG 读 sites | **B. api 持有真相源，把规格下发给 browser（已选）** |
|---|---|---|
| 改动 | browser 加 psycopg 依赖 + DSN | browser 的 `start_login_session` 多几个入参 |
| 职责 | browser 变成「既读库又开浏览器」 | browser 保持纯执行器 |
| 白名单强度 | 强（browser 自持白名单） | 弱一级，需补两条（见下） |
| 与阶段 E v2 的关系 | v2 合并容器后此改动被推翻 | v2 后自然演化为进程内调用 |

选 B。为一道「api 已被攻破」才成立的威胁，付出「一个容器新增数据库依赖 + 角色混淆」的代价不成比例。
**白名单强度用两条补回来**：

1. `login_url` 的域名必须等于 `host`（自一致性，挡「host 写 A、url 写 B」）；
2. 保留 `_public_target()`（DNS 解析 + 必须是公网 IPv4）—— 它才是真正挡 SSRF 的那道，与白名单来源无关。

第 1 条在 api 与 browser **各实现一遍**（`site_store.validate_site` 与
`mcp_server._validate_site_spec`）。两份刻意独立：跨容器共用同一份代码，等于把
「第二道防线」降级成「同一道防线调用了两次」。

## 3. 改了什么

### 3.1 数据层（新增三个模块，同批落地）

| 文件 | 作用 |
|---|---|
| `backend/app/site_store.py` | `sites` 表 + 规格校验 + 别名匹配 + 幂等播种 |
| `backend/app/approval_store.py` | 审批状态机（W3 用，本次仅落地不接线） |
| `backend/app/audit_timeline.py` | 只读联合视图（W4 用，本次仅落地不接线） |

### 3.2 api 侧 `backend/app/main.py`

- 删掉 `_LOGIN_TARGETS` / `_LOGIN_TARGET_ALIASES` / `_match_login_target` 的常量表版本。
- `_match_login_target(question, owner_user_id)` 改为查 `sites` 表 + `match_site()`。
  **刻意不做进程内缓存**：站点面板刚建完站，用户下一句就会说「登录 XX」，
  带 TTL 的缓存会让刚建的站点在那几秒里「看起来没生效」——排查成本远大于一次对 50 行小表的 SELECT。
- 新增 `GET/POST/PUT/DELETE /api/sites`；`SiteRequest` 的 pydantic 长度上限刻意**放宽**到比
  `site_store` 规则更松，让所有语义错误都从 `validate_site` 出同一种形状的报错（一句中文 `detail`），
  而不是前端要同时处理「列表状 pydantic 错误」和「字符串 detail」两套。
- `_execute_login_tool` 改用站点规格；新增 `enabled=false` 分支（**在查凭证之前**拒绝 ——
  否则一次「不该发生」的登录仍会在凭证审计里留下一条 reveal 记录）。
- 启动期播种内置站点；`/api/sites` 加入 `Cache-Control: no-store` 白名单。

### 3.3 browser 侧 `backend/browser/mcp_server.py`

- 删掉那五个模块常量。`_safe_url(url, host)` / `_safe_path(path, blocked_path_parts)` 改为**带参** ——
  每个会话带自己那份规格，白名单判定逻辑仍只有一处。
- 新增 `_validate_site_spec(...)`（第 2 节说的第二道防线）。
- `start_login_session(owner_user_id, login_url, host, username, password, username_selector, password_selector, blocked_path_parts)`；
  校验顺序是「先验规格 → 再 SSRF 检查 → 最后才开浏览器」，不能颠倒。
- 会话里保存 `site`，供 `_page_summary` / `open_login_session_link` / `go_back_login_session` 使用
  （`urljoin` 的基准从全局常量改为本会话站点的 `login_url`）。

### 3.4 前端

| 文件 | 变化 |
|---|---|
| `lib/api.js` | 新增 `listSites` / `createSite` / `updateSite` / `deleteSite` |
| `composables/useSites.js` | 新增。**不重复实现后端校验规则**，只做「多行文本 → 数组」的输入形式转换 |
| `components/SitePanel.vue` | 新增。侧栏列表 + `Teleport` 浮层编辑器 |
| `components/AppSidebar.vue` | 个人助手侧挂 `SitePanel`（站点只在个人助手模式下参与判断） |
| `App.vue` | 启动时取一次站点列表（不 await，它不影响「显示历史还是欢迎语」） |
| `components/ChatComposer.vue` | 输入提示语从**已登记站点**里取别名，不再写死学校名 |
| `components/BrowserHandoff.vue` | 面板标题从「东软智慧教育平台」改为中性的「校园网站」 |

★ 浮层必须 `Teleport` 到 `body`：窄屏时侧栏带 `transform`（抽屉滑入），
而 `position: fixed` 的元素若位于 transform 祖先内部会以那个祖先为包含块 —— 表现是浮层被裁进侧栏。

### 3.5 接口契约

| 接口 | 成功 | 失败 |
|---|---|---|
| `GET /api/sites` | 200 `{sites:[…]}` | — |
| `POST /api/sites` | 201 | 409 已存在 / 422 校验失败 |
| `PUT /api/sites/{site_key}` | 200 | 404 / 422（含「site_key 不可修改」） |
| `DELETE /api/sites/{site_key}` | 200 | 404 |

删站点**刻意不级联删凭证**：孤儿凭证的表现是「尚未登记该站点的登录凭证」，
这是一个可读的失败，比悄悄删掉用户的账号记录安全得多。

## 4. ★ 不兼容变更：api 与 browser 必须**同时**重建

`start_login_session` 的 MCP 工具签名变了。只重建其中一个的话，api 会传新参数、
browser 还是旧签名 → FastMCP 参数校验失败 → 表现为「自动登录工具暂不可用」。

```bash
docker compose build api browser frontend
docker compose up -d api worker browser frontend   # api/worker 共用镜像，必须一起重启
```

## 5. 行为变化：内置站点只在 `local` 模式播种

启动时若是 `AUTH_MODE=local`，会把改造前的硬编码站点（`nsu-smart-education`）幂等播种给本机主人，
保证升级后「登录智慧教育平台」这句话照旧生效、**不覆盖**用户改过的配置。

**`session` 模式（多用户）从空注册表起步**，不播种。理由：内置站点是「本项目单用户形态」的行为快照，
不该强加给别人的部署；那边由用户在站点面板自行新增 —— 这正是本次改造要买到的东西。
⚠️ 若将来真有 `session` 模式的既有部署，升级后需**手动登记一次站点**
（此时已经不是「改代码」了，改的是界面配置）。

## 6. 已知缺口（写在明面上）

1. ⚠️ **前端没有凭证面板** —— `frontend/src/` 里没有任何地方调 `/api/credentials`。
   今日起用户能在界面上登记站点，**却仍不能用界面登记该站点的账号密码**，只能 curl。
   这与「全程前端页面操作」的目标直接冲突，是紧接着要补的一块（后端接口早就齐了）。
2. `session` 模式升级后站点表为空（见第 5 节）。
3. `BrowserHandoff` 的面板标题改为中性文案「校园网站」，不再显示当前站点的真实名称 ——
   真实名称由聊天消息给出（它由站点规格生成）。要显示在面板标题上需要把站点名透传到
   `/api/browser-session`，本次未做。
4. W2 不验证真实校园平台上的**点击**（那是 W3 审批闸门的范围）；本次验证止于「打开页面 + 自动填表」
   （`verification_pending`），不产生任何提交。

## 7. 验证证据（2026-10-15）

**接口/流程验证脚本 16/16 PASS**（全部走真实 HTTP，不 import 任何 Python 函数 ——
否则恰好绕过了「能不能从界面配」这个命题）：

| # | 断言 | 结果 |
|---|---|---|
| 1 | 启动期播种出内置站点 `nsu-smart-education` | PASS |
| 2 | `POST /api/sites` 建一个全新站点 → 201，返回体字段正确 | PASS |
| 3 | 重复 site_key → 409 | PASS |
| 4 | `login_url` 换域名 → 422 | PASS |
| 5 | `host` 填 IP（169.254.169.254）→ 422 | PASS |
| 6 | `PUT` 改 `site_key` → 422 | PASS |
| 7 | `DELETE` 不存在的站点 → 404，重复删除 → 404 | PASS |
| 8 | 新站点尚无凭证时，回答里带的是**新站点自己的名字**且指出缺凭证 | PASS |
| 9 | ★ 登记探针凭证后 `POST /api/chat`「帮我登录验证用平台」→ `browser_session_active=true` | PASS |
| 10 | 停用站点 → 回答「已停用」，`browser_session_active=false` | PASS |
| 11 | 删站后同一句话不再进登录分支 | PASS |

第 9 条是本次的关键证据：`browser_session_active=true` 只可能在 browser **用注册表下发的选择器**
找到并填好两个输入框**之后**出现（选择器错 → 等元素超时 → `login_unconfirmed`）。
即：站点规格真的从数据库流到了隔离浏览器，全程没有一行代码变化。

**自动化测试**：`Ran 172 tests / OK (skipped=17)`（W1 三个模块 71 条 + W2 新增 32 条在内）。
新文件 `backend/tests/test_sites_api.py` 含 browser 侧纯函数校验、接口契约、owner 隔离，
以及「新建站点立刻可被登录意图命中」这条验收口径。

**真实渲染验证**（Chrome headless + SSH 隧道打开真实页面，非 jsdom）：
站点面板在个人助手侧正确渲染出 `东软智慧教育平台 / study.nsu.edu.cn`（计数 1，数据来自数据库），
输入框提示语为「帮我登录东软智慧教育平台」—— 该别名取自注册表，证明前端也不再持有硬编码站点名。

**验证后清理**：探针站点、探针凭证（编造值）、浏览器会话、临时引导页均已删除。
残留检查：`sites = ["nsu-smart-education"]`、`credentials = ["nsu-smart-education"]`。

## 8. 回滚

### 8.1 ★ 先说一件没做好的事：W2 没有「改动前」的备份

阶段 C 的方案（PLAN-GATE A6）写了「代码备份 `.backup-20261015-stagec/`」，
但实际执行顺序是「改本地副本 → 直接 `scp` 覆盖服务器」，**备份目录一直是空的**。
等收工时想起来，改动前的文件已被覆盖，旧镜像也已 buildkit 回收
（`docker images -f dangling=true` 为空）；`git log` 的基线还停在 Vue3 迁移那次提交，
其后的 56 个文件从未提交，**git 也拿不到**。

⇒ 记为本轮流程失误。教训：**`scp` 之前先 `cp`。备份是动手前的第一步，不是收工前的一步。**
⇒ 已补的是 `.backup-20261015-stagec/w2-snapshot/` —— **W2 完成之后**的状态，
   用途是 W3 出问题时回到 W2 这个已验证过的状态，**不能**用来回滚 W2 本身。

### 8.2 回滚 W2 = 反向操作清单 + 丢表

W2 的改动是可枚举的，反向做一遍即可：

1. `backend/browser/mcp_server.py`：删掉 `_validate_site_spec`，把带参的
   `_safe_url` / `_safe_path` 换回模块常量版本（`ALLOWED_HOST` / `ALLOWED_LOGIN_URL` /
   `USERNAME_SELECTOR` / `PASSWORD_SELECTOR` / `_BLOCKED_PATH_PARTS`），
   `start_login_session` 收回归四参数签名 `(owner_user_id, site_url, username, password)`。
2. `backend/app/main.py`：恢复 `_LOGIN_TARGETS` / `_LOGIN_TARGET_ALIASES`，
   `_requested_login_target(question)` 去掉 user 参数，删掉 `/api/sites` 四个路由与启动播种。
3. 前端：删 `SitePanel.vue` / `useSites.js`，回退 `AppSidebar.vue`、`App.vue`、`lib/api.js`、
   `ChatComposer.vue`、`BrowserHandoff.vue`。
4. 重建：`docker compose build api browser frontend && docker compose up -d api worker browser frontend`
5. 丢表：`docker exec rag-postgres psql -U rag -d ragdb -c "DROP TABLE IF EXISTS sites"`

⚠️ 第 1、2 步必须**同时**做（api 与 browser 的 MCP 签名是一对，见第 4 节），
只回滚一个会得到「自动登录工具暂不可用」。

### 8.3 只回滚数据（不动代码）

```bash
docker exec rag-postgres psql -U rag -d ragdb -c "DROP TABLE IF EXISTS sites"
```

表结构全是 `CREATE TABLE IF NOT EXISTS`，下次启动会自动重建并重新播种内置站点。
用户自己登记的站点会丢，但那些配置能在界面上重建（不像凭证那样需要重新输入口令）。
没有外键指向 `sites`（`encrypted_credentials.service_key` 是弱关联而非外键），
所以丢表不牵连凭证与文档。

## 9. 日常命令

| 目的 | 命令 |
| --- | --- |
| 看已登记站点 | `curl -s http://127.0.0.1:8000/api/sites \| jq` |
| 界面上登记站点的新账号 | `curl -s -X POST http://127.0.0.1:8000/api/sites -H 'Content-Type: application/json' -d '{...}'`（字段见第 3.5 节） |
| 直接查表 | `docker exec rag-postgres psql -U rag -d ragdb -c "select site_key,name,host,enabled from sites order by created_at"` |
| 跑站点相关单测 | 见第七次变更 §9 的容器内命令，额外挂 `-v "$PWD/backend/browser:/app/browser:ro"` |

⚠️ 跑 `test_sites_api.py` 需要额外挂载 `backend/browser`（`BrowserSiteSpecTests` 要 import `mcp_server`）；
没挂会整组跳过，不会让整个模块报错。

---

# 第十三次变更：审批闸门（阶段 C ② —— 「未批准时零外部副作用」）

> 时间：2026-10-15
> 起因：`Muse化路线图.md` 阶段 C ②。改造前，「确认点击」的载体是一段**对话文本**：
> 助手回一句「确认点击：button「Submit order」」，用户把这行字原样打回聊天框，
> `browser_agent` 就调 `confirm_pending_login_session_click` 把控件真的点了。
> 于是审批的唯一门槛是「知道那行字怎么写」—— 而那行字就印在上一句回复里，照抄即可。
> 更麻烦的是那次「确认」完全活在内存里（browser 容器的
> `_sessions[owner]["pending_click"]`）：容器一重启就无从追问「助手到底批准过什么」，
> 也没有任何地方记录「哪一次是超时自动放弃的」。

## 1. 一句话

页面点击从「对话文本确认」改成「PostgreSQL 里一次性、可过期、可对账的审批记录」：
先 `POST /api/approvals/{id}/approve`、再 `POST /api/approvals/{id}/execute`，
两个接口各只能成功一次；执行入口全项目唯一；**只有浏览器明确回报 `clicked` 才会写 `executed`**。

## 2. 状态机与两条不变量

```
pending ──approve──▶ approved ──begin_execution──▶ executing
   │                    │                              │
   │                    │                    ┌─────────┴─────────┐
   │                    │                  clicked            其它回报
   │                    │                    │                   │
   │                    │                 executed      expired / failed
   │                    │
   ├──reject──▶ rejected│
   └──超时────▶ expired ◀┘（approved 也可能在过期后才被点 execute）
```

终态（`rejected` / `expired` / `executed` / `failed` / `unknown`）一经写入不再变更。

两条必须守住的不变量：

1. **`expired` 只能由 `expire_stale()` 写。** `approve()` / `reject()` 一律
   `require_unexpired=True` ⇒ 过期后批准必然 409，不可能「先过期又批准成功」。
2. **`executed` 只能由浏览器回报的 `clicked` 触发。** api 自己「以为点了」不算 ——
   把「我们以为点了」写成「点了」是这张表里最危险的错。

`approve` 与 `begin_execution` 全部写成 `UPDATE ... WHERE status='期望值' RETURNING *`，
拿不到行即视为冲突（回 409）。先 SELECT 再 UPDATE 会留下竞态窗口，双击按钮就会消费两次。

## 3. 四个刻意的取舍

| 情形 | 处理 | 为什么不能反过来 |
|---|---|---|
| 传输层异常（连不上 browser / 超时） | 记 `failed` **终态**，不退回 `approved` | 无法区分「没送达」与「送达了但没等到回复」，后者可能已经点过。允许重试就是允许点两次 |
| 熔断已打开（`browser_breaker` 拒绝） | 回 **502**，但**保留 `approved`** | 这是唯一能确定「请求根本没发出去」的情形，所以允许重试 |
| 浏览器回 `no_pending_click` / `pending_click_expired` | 回 **200**，审批落 `expired`（放行+标记） | 请求被正确处理了，只是没有可点的东西。报错会让用户以为系统坏了 |
| 停在 `executing` 超过 `STUCK_EXECUTION_SECONDS=300` | 标记 `unknown`，**不自动重放** | 与阶段 B 同源：宁可要一次人工介入，也不要静默做两次 |

前两条方向相反、理由一致：**能不能确定「什么都没发生」决定了能不能重试。**

## 4. 改了什么

### 4.1 browser 侧 `backend/browser/mcp_server.py`

- `start_login_session` 多收一个 `site_key: str = ""`（**纯信息性**，只被记住并在控件清单里回传；
  截断到 64 字符而不是校验，因为它不参与任何安全判定）。
- `click_login_session_element` 的返回值里 `confirmation_text` 保留，但**新增**
  `fingerprint` 与 `site_key` —— 前者让「批准时看到的东西」与「执行时点的东西」可对账，
  后者让审计时间线说得出「在哪个站点上」。
- `confirm_pending_login_session_click` 补上文档：它是**全项目唯一**会真的点击网页的工具，
  也是闸门的落点。它自己还有第二道关（`pending_click` 90 秒 TTL + 目标文本精确匹配），
  与 api 侧那道**刻意独立** —— 纵深防御的意义在于两处不会同时错。
- `PENDING_CLICK_TTL_SECONDS = 90` 必须与 `approval_store.APPROVAL_TTL_SECONDS = 90` 一致：
  两处不一致会出现「界面显示还能点，实际浏览器已判超时」这种自相矛盾的表现。

### 4.2 api 侧 `backend/app/main.py`

- 新增 4 个路由：`GET /api/approvals`（先惰性 `expire_stale()` 再列）、
  `POST /api/approvals/{id}/approve`、`/reject`、`/execute`。
- `/api/approvals` 进 `no-store` 白名单（90 秒 TTL，缓存下来就是给出过期按钮）。
- `_answer_browser_task` 新增：把浏览器回报的 `pending_click` **登记成一条审批记录**，
  并把记录塞进返回值（`task["approval"]`）。
  ★ 闸门不可用时**刻意不退化成「直接点」** —— 只记 error 日志并告诉用户「本次没有登记可批准的操作」。
  「审批组件挂了」不等于「可以在没有批准的情况下点击」。
- `chat()` 返回 `click_approval`；`chat_stream()` 推 `{"type": "click_approval", ...}`（SSE 协议已更新）。
- 启动时若审批闸门初始化失败，打 **error** 日志说明「不会下发一键批准按钮（不会退化成直接点击）」。
- HTTP 状态映射：404 记录不存在 / 409 未批准或已被处理 / 410 已过期（仅 execute）/ 502 浏览器不可用。

### 4.3 `backend/app/browser_agent.py`

- **删掉**「手打确认文本即执行」的分支（见第 6 节）。
- `confirmation_text` 返回键 → `pending_click` 字典（含 `target` / `fingerprint` / `site_key`）。
- `_TOOLS` 里 `click_page_element` 的说明与 `_SYSTEM_PROMPT` 的相关条目改写：
  不再让模型转达一段确认文本，而是说明批准只能发生在界面卡片上。

### 4.4 前端

| 文件 | 改动 |
|---|---|
| `components/ClickApprovalCard.vue` | **新增**。「批准并执行」= approve + execute 两次请求（刻意分开，审计才能区分「用户确认了什么」与「系统执行了什么」）；1 秒倒计时；终态就地显示结果 |
| `composables/useChat.js` | 消息形状 `pendingClick` → `pendingApproval`；SSE 事件 `click_confirmation` → `click_approval`；新增 `settleApproval()`；开新会话时**只清未落终态的卡片** |
| `lib/api.js` | 四个审批方法 |
| `components/MessageBubble.vue` / `MessageList.vue` | 挂载卡片、转发 `settle-approval` |
| `styles/main.css` | `.msg-approval` 按状态着色左边框（pending 警示 / executed 成功 / rejected·expired 灰 / failed 红） |

倒计时**只是提示而不是判据**：`expires_at` 来自服务器，浏览器时钟可能不准（实测本机与 VM
有约 1 秒偏差，界面会显示「91 秒」）。真正说了算的是服务端，过期后点按钮会收到 409/410，
卡片把那句话如实显示出来。

### 4.5 测试

新增 `backend/tests/test_approvals_api.py`（约 400 行）。核心断言是**调用次数为零**，
而不是状态码 —— 接口回 409 只说明它认为自己拒绝了，说明不了没人点过。
另含放行+标记、终态不可重放、他人不可见、每步只留一条审计事件等。

## 5. ★ 不兼容变更：api 与 browser 必须**同时**重建

`start_login_session` 的 MCP 签名从 8 参变成 9 参（新增 `site_key`）。
只重建一个不会崩，而是静默表现为「自动登录工具暂不可用」——
很容易被误判成「浏览器没起来」。

```bash
docker compose up -d --build api browser worker   # worker 与 api 共用镜像，一起重启
```

## 6. ★ 故意封死的一条路径（用旧用法的人会以为功能坏了）

`browser_agent.py` 里，凡是匹配 `^\s*确认点击：.+?\s*$` 的输入**不再触发任何点击**，
改为回一句「页面点击已改为『先批准、再执行』：请在上一条消息的卡片上点『批准并执行』」。

这是有意的行为回退：旧用法（照抄助手给的那行字）不再生效，而这正是闸门能成立的前提 ——
**审批的载体一旦是用户可自由伪造的自然语言，闸门就等于不存在**。
`test_typed_confirmation_no_longer_clicks_anything` 断言 `calls == []` 且 `get_model.assert_not_called()`。

## 7. 验收证据

| 项目 | 结果 |
|---|---|
| `.verify-w3.sh`（**在宿主机上**跑，需要 `docker` 与 `curl`） | **PASS=22 FAIL=0** |
| `.verify-w3-e2e.py`（真实 chat 路径产出审批） | **FAIL=0 全部通过** |
| 后端单测 | `Ran 192 tests / OK (skipped=17)` |
| 真实渲染（真 Chrome + CDP + SSH 隧道） | `.shots/W3-0/1/2/4-*.png` 四张 |

断言尽量落在**副作用层**，而不是接口返回值：

| 断言 | 取证位置 |
|---|---|
| 未批准直接 execute → 409，且审批仍是 `pending` | `operation_approvals` |
| 浏览器工具**零调用**（对照：已批准路径调用 1 次） | `docker logs rag-browser` 里 `CallToolRequest` 的条数 |
| 全程没有 `approval_executed` 事件 | `operation_audit` |
| 该审批的 `executed_at` 仍为空 | `operation_approvals` |

一次真实界面批准+执行留下的审计（逐条）：

```
approval_requested  {target: "button「Submit order」", site_key: "w3r1792049840", approval_id: "8f9f9b03…"}
approval_granted    {status: "approved",   approval_id: "8f9f9b03…"}
approval_executed   {status: "executed",   page_title: "", approval_id: "8f9f9b03…"}
```

**点击真的发生了的独立证据**：卡片显示「已按你的批准点击该控件。**已跳转到：/post**」——
httpbin 的表单真的被提交了，页面从 `/forms/post` 走到了 `/post`。
这不是接口自述，是目标站点真的变了状态。

惰性过期也验证过：一条 `pending` 记录在 `GET /api/approvals` 之后被写成 `expired`，
并补上一条 `approval_expired` 审计。**在有人读列表之前，数据库里那条记录一直是 `pending`**——
这是惰性过期的固有代价，界面靠自己的倒计时先一步放弃。

## 8. 本轮踩到的坑

1. **`site_key` 漏传**（被 `.verify-w3-e2e.py` 抓出来）。给 browser 的
   `start_login_session` 加了参数，却忘了在 `main._execute_login_tool` 里传。
   后果**不是功能坏掉**，而是审计里所有点击都归到空站点 —— 这种「功能正常但取证失真」
   的缺陷只有端到端脚本能发现，接口返回值和单测都看不出来。
2. **空串 vs NULL**。修 `site_key` 之前创建的审批存的是 `''`，不是 NULL。
   清理脚本写成 `site_key IS NULL` 会**静默漏行**：脚本报成功，残留检查却还是 1。
   ⇒ 一律用 `coalesce(site_key,'') = ''`。`.verify-w3-cleanup.sql` 里已标注。
3. **测试写死了与环境相关的终态**。C 段原本断言终态是 `failed`（依赖「此刻没有活的浏览器会话」
   ⇒ browser 回 `session_expired`）。跑完渲染取证后有了活会话，browser 改回
   `no_pending_click` ⇒ 终态变 `expired`，脚本无故变红 3 项。
   ⇒ 改为接受 `failed|expired`（两者都等于「什么都没点」），并把真正的不变量单独断言：
   不是 `executed`、`executed_at` 为空、重放被拒。
   **一个会无故变红的测试最终会被忽略**，所以宁可少断言也不写死环境。
4. **e2e 依赖外部站点会抖动**。httpbin.org 偶发首次失败（`login_unconfirmed`），重试即过。
   但清理在 `finally` 里，所以失败那次残留也是 0。
   （对照：第十二次变更期间那份 e2e 的清理在顺路上，一次失败就留下了一条孤儿审批 ——
   就是本轮清理时那条 `site_key` 为空的 `8f4839da`。）

## 9. 已知遗留（诚实标注）

- ⚠️ **刷新页面后审批卡片会消失**。`approval` 只活在消息对象上，而 `/api/chat/{id}/history`
  返回的历史里没有它（且已消费的操作走的是另外的接口，不在会话记忆里）。
  待办的「待批准操作」侧栏面板（用 `api.listApprovals`）能关掉这个口子 —— **本轮未做**，
  它不在 W3 的方案范围内。
- 前端仍无**凭证面板** —— 但这条已**由用户裁定「不做」**（2026-10-15，范围决策而非缺陷）：
  页面里能登记站点，不能登记该站点的账号密码；凭证登记继续走 API（`POST/PUT /api/credentials`
  + `/{id}/reveal`）。**不再作为阻塞点或待办跟踪。**
- `page_title` 常为空（点击伴随跳转，读 title 时新页面还没就绪）。前端已改为退到路径显示
  （「已跳转到：/post」），不影响闸门判定。
- 不覆盖真实校园平台上的端到端点击：那需要真实账号且会产生不可逆副作用，刻意不做。

## 10. 回滚

改动前的文件都在 `.backup-20261015-stagec/w3-pre/`（含目录结构）：

```
backend/app/browser_agent.py  backend/app/main.py  backend/browser/mcp_server.py
backend/tests/test_browser_agent.py  backend/tests/test_browser_login.py
frontend/src/components/MessageBubble.vue  frontend/src/components/MessageList.vue
frontend/src/composables/useChat.js  frontend/src/lib/api.js  frontend/src/styles/main.css
```

```bash
cd ~/phase1-rag && cp -r .backup-20261015-stagec/w3-pre/backend/. backend/
cp -r .backup-20261015-stagec/w3-pre/frontend/. frontend/
docker compose up -d --build api browser worker frontend
```

⚠️ api 与 browser 必须一起重建（见第 5 节）。只回滚一个会得到「自动登录工具暂不可用」。

**数据不用回滚。** `operation_approvals` / `operation_audit` 两张表留着无害：
旧代码根本不读它们，只是从「审批的真相源」退回成「一堆没人看的记录」。
**不要为了回滚去 DROP 它们** —— 那些审批记录本身就是审计价值，
而且 `DROP` 会连带丢掉「哪些操作被批准过」的历史。

回滚后必须重新验证：`click_approval` 会重新变成 `click_confirmation`，
`ClickApprovalCard.vue` 不再被引用（旧版 `MessageBubble.vue` 里是 `.msg-confirm` 按钮块）。

# 第十四次变更：审计时间线（阶段 C ④ —— 「助手到底碰过什么」有了可核对的页面）

日期：2026-10-15 晚。**阶段 C 至此全部完成**（①站点注册表 ②审批闸门 ③审计时间线，
编号对应路线图交付栏 ①②③④ 中的后三项）。

## 1. 这次解决什么问题

前两次变更给了「助手能碰哪些站点」（W2）和「碰之前要你点头」（W3），但没有
「它到底碰过什么」。`credential_audit` 表从第十次变更前就在记，`operation_audit`
从第十三次变更起在记，可它们只存在于 PG 里 —— 信任面最后一块拼图是把这两份
记录变成**用户能核对的东西**，否则前两块只能靠信任，不能靠验证。

## 2. 读模型（`audit_timeline.py` 改造）

W1 就位的模块原本只有 `for_owner(limit)`，本次补齐接线所需的三块：

### 2.1 游标翻页（不是 OFFSET）

原实现只有 `LIMIT`，没有翻页。翻页必须做在 SQL 里（UNION 之后 ORDER BY +
WHERE 游标），不能在应用层「各查各的再合并」—— 后者「第 2 页」可能比
「第 1 页」还新。游标 = `created_at|source|seq` 三段：

- **为什么带 `source` 和 `seq`**：`created_at` 撞在同一微秒时（批量登记 +
  取明文就发生在同一事务毫秒里），只按时间翻页会在边界处**跳行或重复**。
  `source`（credential/operation/site）+ 自增 `seq`（各表 audit_id/serial）
  组成全序 tiebreaker，严格 `<(cursor)` 排除游标自身。
- **游标格式刻意用 `Z` 结尾而不是 `isoformat()` 的 `+00:00`** —— 这是本次
  踩到的**真 bug**：`+` 在 query string 里会被解码成空格，服务端自己吐出的
  游标原样回传反而被判 422「格式不正确」。时间戳永远 UTC 且以 `Z` 结尾，
  解析时两种格式都接受（旧数据兼容）。

### 2.2 站点变更也进时间线（W2 → W4 的接缝）

`site_store.py` 的 create/update/delete 现在在**同一事务**里写
`operation_audit`（`site_registered` / `site_updated` / `site_removed`）。
理由：站点注册表是「你授权了助手能碰什么」，正是信任面最该留痕的事；
而它能放进同一事务是因为 `_record_site_event()` 复用调用方传入的 connection，
成功/失败一起回滚 —— 测试 `test_audit_and_site_write_succeed_or_fail_together`
钉住这条。内置站点播种（`ensure_builtin_sites`）**刻意不记审计**：那不是
用户做的动作，记进去只会把「你做过什么」和「升级脚本做过什么」混在一起。

### 2.3 摘要仍在后端生成

join 凭证 label、站点名这些前端拿不到的数据；action → 中文的映射一处维护。
不认识的动作**原样透传**（`archive` 就输出 `archive`），绝不猜 —— 时间线是
核对用的界面，一句编出来的中文比空白更有害。

## 3. API（`main.py`）

`GET /api/audit/timeline?limit=&cursor=`：

- `limit: ge=1, le=_AUDIT_PAGE_MAX`（=100，比模块 `MAX_LIMIT=200` 小 1 ——
  内部多取一条判断 `has_more`，正好取满会截断多取的那条）。
- 返回 `{events, has_more, next_cursor}`；`next_cursor` 只在 `has_more` 时给出。
- 非法游标 → **422**（带原因），不是 500 —— 旧链接/手滑不该变成服务器错误。
- 加入 no-store 列表：审计是事实记录，被缓存就会「删了还在」。
- 启动/关闭接线 `get_audit_timeline_or_none()` / `close_audit_timeline()`，
  初始化失败只报 error 不拖垮启动（与 site/approval store 同一策略）。

## 4. 前端

- `composables/useAuditTimeline.js`：状态 + 游标累积加载。
- `components/AuditTimelinePanel.vue`：**侧栏最近几条 + 「查看全部」浮层**
  （Teleport 到 body —— SitePanel 踩过的 transform 裁剪坑，注释里留了案底）。
  侧栏预览回答「刚刚那次批准真的执行了吗」，浮层负责翻旧账。
- **刷新由用户触发，不自动轮询**：审计是事实记录不是进度条，每 5 秒变的
  数字只会让人怀疑自己看的是哪一版。
- 时间戳**保留秒**：同一分钟内「批准 → 执行」是常态，只到分钟分不出先后。
- level 颜色由后端定（ok/warn/info/muted），前端只映射不猜语义。
- 浮层里的行**允许换行**：核对时被省略号截断的句子毫无用处。

## 5. 测试

- `test_audit_timeline.py`：纯函数（摘要不猜、level 兜底、游标编解码）+
  需 PG 的（跨表全局倒序、翻页不重不漏、**取页之间插入新事件不出错**——
  游标语义的核心证明、站点审计与站点写入同生共死、失败的事不留痕）。
- `test_audit_api.py`：走 HTTP 的 owner 隔离（★ 先证明别人的事件**真的写进
  库了**，再断言接口读不到 —— 否则一次静默失败的 INSERT 会让测试假通过）、
  非法游标 422、三页游标不重不漏、`has_more` 最后一页为 False。
- 全量回归：**212 tests OK (skipped=17)**（新增 20 条）。

## 6. 验收（宿主机 `.verify-w4.sh`，PASS=20 FAIL=0）

判据全部落在数据层而非返回值：翻页取到的总条数**等于库里该 owner 的总数**
（48 = 48，少一条就红）；跨页 id 去重后等于总数；越权用例先 INSERT 别人的
事件、确认写库成功、再断言接口里读不到；站点用例同时看时间线事件与站点列表。

**渲染取证**（CDP 驱动真 Chrome，`.verify-w4-render.mjs`）：侧栏面板真实
渲染 30 条预览与计数；浮层「加载更多」点击后 30 → 48 条（与库里总数吻合），
按钮消失、「没有更多了」出现。level 色条（warn 橙 / info 蓝 / muted 灰）肉眼可辨。

## 7. 踩到的坑

1. **验收脚本自己的 `limit=200` 越界**：路由上限是 100（`_AUDIT_PAGE_MAX`），
   脚本里五处 `limit=200` 全部 422，jq 报 `Cannot iterate over null`，
   5 项 FAIL 其实是同一个原因。修脚本而不是放宽接口 —— 上限是刻意设计的。
2. **游标里的 `+00:00`**：见 §2.1。教训是「服务端生成的、要经 query string
   回传的值，必须自己先过一遍 URL 编码」。
3. **清理条件与写入格式不一致**：探针站点名为 `W4探针站点$TS`，清理脚本写
   `%探针站点$TS%` 看似能匹配，实际 `LIKE` 是能匹配的 —— 真正漏掉的是
   审计事件 `detail` 里的 `target` 带了 `W4` 前缀之外，清理语句只删了
   `owner_user_id='$OTHER_ID'` 的行，而探针站点事件属于**本 owner**。
   清理要以 `site_key`/`w4probe` 这种精确令牌为准。

## 8. 回滚

```bash
cd ~/phase1-rag/.backup-20261015-stagec/w4-pre
cp backend/app/audit_timeline.py ~/phase1-rag/backend/app/
cp backend/app/site_store.py    ~/phase1-rag/backend/app/
cp backend/app/main.py          ~/phase1-rag/backend/app/
cp backend/tests/test_audit_timeline.py ~/phase1-rag/backend/tests/
cp frontend/src/lib/api.js      ~/phase1-rag/frontend/src/lib/
cp frontend/src/composables/useChat.js ~/phase1-rag/frontend/src/composables/
cp frontend/src/App.vue         ~/phase1-rag/frontend/src/
cp frontend/src/components/AppSidebar.vue ~/phase1-rag/frontend/src/components/
cd ~/phase1-rag
rm -f backend/tests/test_audit_api.py frontend/src/composables/useAuditTimeline.js \
      frontend/src/components/AuditTimelinePanel.vue
docker compose up -d --build api frontend && docker compose up -d api worker
```

回滚后时间线面板消失（AppSidebar 不再引用），站点变更不再写审计。
**已写入的审计行不用删** —— 旧代码不读它们，留着就是历史。

# 第十五次变更：终端执行 run_shell（受限沙箱 + 审批闸门 + 审计，2026-10-15 晚）

> 详细设计见 `docs/终端执行-方案.md`（12 条决策已固化）。这里只留要点与坑。

- 独立 shell 容器（非特权 + no-new-privileges + 无 docker.sock + 独立 shell_internal 网络），
  命令只在该容器内执行，不碰宿主机。
- 三档分类器 `app/shell_exec.py`（双镜像共用源码）：readonly（直接执行）/
  dangerous（走 W3 审批）/ forbidden（永久拒绝：rm/docker/ssh/拼接符/重定向/路径越界）。
- shell 容器双 MCP 工具：run_shell（只读第二道防线）vs run_approved_shell（已批准写命令）。
  注意：不能用一个工具 + approved 参数 —— 会把「是否批准」变成可伪造入参。
- 意图路由靠 LLM function-calling（shell_agent.intends_shell），非关键词。
- 路径校验必须用 posixpath.normpath（注意不是 os.path.normpath，Windows 本地会把
  /work/x 转成反斜杠路径误判越界）。
- 端口：shell MCP 8767、health uvicorn 8768（都 bind 8767 会让 /mcp 404）。
- 前端 ClickApprovalCard 按 action 分流；SSE 事件名 shell_approval（不等于 click_approval）
  前端必须各有一个 case，漏了就静默丢卡片。
- 提交 f068fec（后端）+ 44934dc（前端收尾）。

# 第十六次变更：阶段 D 长期记忆（一）—— user_facts 跨会话抽取 + pgvector 检索 + 遗忘（2026-10-15 深夜）

## 1. 做了什么

路线图 §阶段 D 拆两块，本次只做 ①跨会话记忆，②主动建议留待下一轮。

- 新增 backend/app/fact_store.py：
  - user_facts 表：fact / fact_type（CHECK 约束 preference|habit|topic）/
    confidence / source_session_id+source_message_id（出处）/ embedding vector(1024) /
    forgotten（软删除）。
  - LLM 抽取：每轮对话后调一次轻量 LLM，抽成结构化 facts。
  - 语义检索：pgvector 的 <=> 余弦距离，search() 返回 top-k 相关未遗忘 facts。
  - forget()：软删除 + owner 隔离。
- worker 新增 fact_extraction 任务类型：抽取放后台异步，不阻塞用户眼前这轮回答。
- rag.answer / rag.answer_stream 在拼 Prompt 前注入【用户长期记忆】段（检索与当前问题
  top-k 相关的 facts），rag_prompt.py 的 SYSTEM_PROMPT 加 user_facts 占位。
- 新增 GET /api/facts（列出）与 DELETE /api/facts/{id}（遗忘，软删除）。
- postgres 镜像 postgres:16-alpine 换成 pgvector/pgvector:pg16（官方 alpine 版不带 vector
  扩展；数据目录兼容，换镜像零迁移）。

## 2. 关键设计决定

1. 抽取放 worker，不是请求线程：抽取是一次额外 LLM 调用，没理由拖慢眼前这轮回答；
   facts 只服务「以后」的对话。
2. 记忆是增强，不是硬依赖：队列不可用 / fact_store 不可用 / embedding 失败，全都
   静默降级（返回空串/空列表），绝不影响主链路回答。
3. 「忘掉」是软删除：审计 + 可恢复；owner 隔离在 SQL 的 WHERE 里。
4. 检索用 pgvector，不新引向量库：user_facts 就几十上百条，PG 的 vector 类型够用，
   且 embedding 与知识库共用同一 EMBEDDING_* 配置（同一向量空间）。

## 3. 踩过的坑（真·排查成本）

1. .format() 把 JSON 示例当占位符：抽取 prompt 用 .format(user_msg=..., ai_msg=...)
   填充，而 prompt 里的 JSON 示例 {"fact": ...} 会被 .format() 误当 {fact} 占位符，
   抛 KeyError: 'fact'。修法：prompt 里所有字面量花括号写成 {{ }} 转义。
   教训：用 .format() 填充的模板串，先扫一遍有没有「本想当字面量」的花括号。
2. 新会话不能传 session_id：/api/chat 对不存在的 session_id 返回 404
   （session_exists_for_user 检查）。验收脚本第一轮必须传空 session_id，让后端
   _new_session_id 生成，再从返回里取回真实 id。这是既有行为，不是本次引入。

## 4. 验收口径（7 项断言全 PASS）

- LLM 抽中「用户偏好简洁直接的答案」（preference 类，精准抓到「简洁直接」）。
- 每轮对话后 worker 异步抽取，user_facts 表出现新行。
- embedding 全生成（无空向量）；<=> 余弦检索排序正确（sim=1 是自身）。
- 软删除 forgotten=t；重复遗忘 404；越权遗忘 404（不影响他人）；忘掉后检索不到。

## 5. 回滚

```bash
cd ~/phase1-rag
git revert 4a17d7b   # 代码层面回滚
# postgres 镜像回退：compose 里 image 改回 postgres:16-alpine 后
docker compose up -d postgres   # volume 数据兼容，无需迁移
# user_facts 表与已写入 facts：代码回滚后旧代码不读它们，留着无害；
# 要彻底清掉则 DROP TABLE user_facts;（DROP EXTENSION vector 需先删表）
```

# 第十七次变更：阶段 D 长期记忆② —— 主动建议（入口触发 + 采纳转任务 + 忽略去重，2026-10-16）

## 1. 做了什么

路线图 §阶段 D 的第二块，把「记住的东西」变成「主动说出口的建议」。

- 新增 `backend/app/suggestion_store.py`：
  - `suggestions` 表：`suggestion` / `reason`（依据）/ `kind`（study|job|health|life）/
    `status`（CHECK 约束 pending|accepted|dismissed|expired）/ `created_at`。
  - LLM 推断：把 user_facts 全量喂给轻量 LLM，推断 1~3 条「现在该做」的建议。
  - 去重：`_recently_dismissed` 用 bigram 共词比例（≥0.5 判同类）跳过冷却期内 dismiss 过的建议。
- 新增三个路由（均在 `tags=["记忆"]`）：
  - `GET /api/suggestions` —— 入口触发：返回 pending；若无 pending 则现场推断一批再返回。
  - `POST /api/suggestions/{id}/accept` —— 采纳：pending → accepted + 复用阶段 B 队列转 `suggestion_action` 任务。
  - `POST /api/suggestions/{id}/dismiss` —— 忽略：pending → dismissed。
- worker 新增 `suggestion_action` 执行器：把采纳的建议展开成可执行计划（LLM 生成），
  作为任务结果返回，前端任务面板可读。
- 前端新增 `SuggestionPanel.vue` + `useSuggestions.js`，挂在个人助手侧栏最上（比审批、审计更优先）。

## 2. 关键设计决定

1. **触发是「入口触发」，不是后台定时**。本机是「网页界面 + 单人 + 打开即用」，
   没有手机推送通道。「后台生成建议」若不解决「怎么送到眼前」，就是一堆没人读的
   笔记。所以触发点放在用户下一次打开页面时（前端 onMounted 拉 GET /api/suggestions，
   后端无 pending 时现场推断）。保证建议一定在用户「在」的时候送达。
2. **生成是「LLM 推断」，不引入独立待办登记**。user_facts 已有的偏好/习惯/话题
   足够 LLM 推断「该复习了」「该投实习了」。显式带截止日期的事项登记是另一个功能
   （偏待办/提醒），不在本模块范围，避免阶段 D ② 失控。
3. **建议是候选，确认才转任务；忽略要记住不再重复**。建议不能自动执行。采纳 →
   转 `agent_tasks`（复用阶段 B）；忽略 → dismissed，冷却期（7 天）内同类不再推送，
   否则每次打开都弹一遍变成骚扰。
4. **采纳与「转任务」解耦**：采纳本身是 `suggestions` 表的状态迁移，转任务依赖
   队列可用；队列不可用时采纳仍生效，只跳过入队（记 info 日志），不让增强动作拖垮主链路。

## 3. 踩过的坑

1. **`.format()` 花括号转义**（与第十六次 fact_store 同源）：生成 prompt 里的 JSON 示例
   `{"suggestion": ...}` 会被 `.format(facts_text=...)` 误当占位符，必须写成 `{{ }}`。
   教训沿用：用 `.format()` 填充的模板，先扫一遍字面量花括号。
2. **后台任务读不到 `.ssh` 密钥**：`docker compose build` 放后台（run_in_background）时，
   沙箱拦掉 `.ssh/config` 与 key 的读取，导致 ssh 别名无法解析、公钥认证失败。而前台
   执行会被 escalation 放行、正常读 config。结论：**需要 ssh 的长命令（构建/部署）必须前台跑**，
   不能 run_in_background。这是环境约束，不是代码问题。

## 4. 验收口径（11 项断言全 PASS）

- 有 user_facts 时入口触发生成建议（LLM 从「在准备考研」推断出「复习计划拆解」等 study 建议 + 依据）。
- 再次调用不重复生成（已有 pending 不再推断，防骚扰）。
- 采纳 → accepted + 生成 `suggestion_action` 任务，worker 执行 succeeded（有 plan 产出）。
- 忽略 → dismissed，同类建议冷却期内可被 `_recently_dismissed` 命中。
- 重复采纳/忽略已终结建议返回 404（状态机终态不可变更）。
- 全量回归 252 tests OK（新增 13 条 suggestion_store 测试，零回归）。

## 5. 回滚

```bash
cd ~/phase1-rag
git revert 513111a   # 代码层面回滚
# suggestions 表与已写入建议：代码回滚后旧代码不读它们，留着无害；
# 要彻底清掉则 DROP TABLE suggestions;
```
