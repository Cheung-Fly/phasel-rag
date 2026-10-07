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
