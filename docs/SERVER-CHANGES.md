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
| 改完 nginx 配置热载 | `docker compose exec frontend nginx -s reload` |
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
| 改完 nginx 配置热载 | `docker compose exec frontend nginx -s reload` |
| 看前端日志 | `docker compose logs -f --tail=100 frontend` |
| 本地开发（需 node） | `cd frontend && npm install && npm run dev` → `http://localhost:5173` |
| 开发期连服务器后端 | 先 `ssh -L 8000:127.0.0.1:8000 azureuser@<IP> -N`，再 `npm run dev` |

**已失效的命令**（记下来免得再试）：

| 旧命令 | 现状 |
|---|---|
| 改宿主机 `frontend/*.js` 即时生效 | ❌ 已失效，现在跑的是镜像里的构建产物 |
| 访问 `8000/ui/` 看页面 | ❌ 404，api 不再托管前端 |
| `docker compose exec frontend nginx -s reload` 之外的热更新手段 | 无 |

