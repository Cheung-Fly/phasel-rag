# Git 使用指南（阶段一项目）

> 仓库位置：**服务器** `~/phase1-rag`（分支 `main`，1 个初始提交）
> 连接：`ssh -i "C:\Users\30651\.ssh\rag-vm_key.pem" azureuser@20.89.90.58`

---

## 0. 先理解一个前提：仓库在服务器上，不在本地

**为什么不在本地建仓库：** 你的 Windows 上**没有安装 git**，
也没有 winget / choco / scoop 任何一个包管理器可以装它。

所以我选择在**服务器上**建仓库 —— 那里有 git 2.43.0，立即可用。

**这个选择的后果，你需要知道：**

| | 位置 | 有版本控制吗 |
|---|---|---|
| 项目源文件 | 本地 `default-workspace/phase1-rag` | ❌ **没有** |
| 项目运行副本 | 服务器 `~/phase1-rag` | ✅ 有 |

**风险：** 本地那份"真正的源"仍然没有任何历史保护。
如果本地文件被误删，你**只能从服务器拉回来**，而拉回来的是最近一次提交的状态，
**未提交的改动会丢**。

**建议：** 想让本地也有版本控制，去 https://git-scm.com/download/win 下载安装
Git for Windows（约 60MB），装完重开终端，然后告诉我，我帮你把本地也初始化，
并把服务器设为远程仓库 —— 那样两边就同步了。

---

## 1. 仓库现状

```
0533a50  初始提交：阶段一 RAG 知识库问答系统
分支:     main
跟踪文件: 20 个
仓库体积: 408K
工作区:   干净
```

**20 个被跟踪的文件：**

```
.env.example              ← 模板（无密钥，应该提交）
.gitignore
backend/Dockerfile
backend/app/{__init__,config,llm,main,memory,rag,vectorstore}.py
backend/app/static/{index.html,style.css,app.js,markdown.js,sse.js,store.js}
backend/requirements.txt
data/.gitkeep             ← 占位，让 data/ 目录结构进仓库
deploy.sh
docker-compose.yml
```

**3 个被忽略的（关键）：**

```
!! .env              ← 你的 API Key
!! data/chroma/      ← 向量库
!! data/uploads/     ← 上传的文档
```

---

## 2. 首次要做的事：改提交身份

我配置的是**占位值**，因为它不该由我替你决定：

```
user.name  = azureuser
user.email = azureuser@rag-vm.local
```

改成你自己的（`--local` 只影响这个仓库，不动全局配置）：

```bash
ssh -i "C:\Users\30651\.ssh\rag-vm_key.pem" azureuser@20.89.90.58
cd ~/phase1-rag

git config --local user.name  "你的名字"
git config --local user.email "你的邮箱"

# 只有 1 个提交，直接改它的作者最省事
git commit --amend --reset-author --no-edit

git log -1 --format='%an <%ae>'    # 确认改好了
```

> **为什么提交身份重要：** 它是版本历史里的"谁改的"。
> 以后协作或推到 GitHub 时，邮箱要和账号匹配，否则提交不会被算到你头上。

---

## 3. 日常提交流程（核心）

```bash
cd ~/phase1-rag

# ---------- 1. 看当前状态 ----------
git status

# ---------- 2. 看具体改了什么 ----------
git diff                       # 还没暂存的改动
git diff --staged              # 已暂存的改动

# ---------- 3. 暂存 ----------
git add 文件名                  # 暂存指定文件（推荐，更可控）
git add -A                     # 暂存全部改动

# ---------- 4. ⚠️ 提交前一定要检查这个 ----------
git diff --cached --name-only  # 即将提交的文件清单

# ---------- 5. 提交 ----------
git commit -m "说明这次改了什么"

# ---------- 6. 确认 ----------
git log --oneline -5
```

### ⚠️ 第 4 步不能省

**每次提交前都看一眼 `git diff --cached --name-only`。**

因为 `.gitignore` 只挡"已知的"敏感文件。如果你新建了一个
`config-secret.yaml`、`notes-with-key.md` 之类的文件，规则挡不住它，
而 `git add -A` 会把它一起提交进去。

**养成习惯：提交前扫一眼文件清单，比事后清理历史省事一百倍。**

---

## 4. 常用命令速查

### 查看历史

```bash
git log --oneline                 # 简洁历史
git log --stat -3                 # 最近 3 次，带改动统计
git log --oneline --graph         # 带分支图
git show 0533a50                  # 看某次提交的完整内容
git log -p -- backend/app/rag.py  # 只看某个文件的历史
git blame backend/app/rag.py      # 每行是谁什么时候改的
```

### 撤销改动

| 场景 | 命令 | 说明 |
|---|---|---|
| 改了文件想还原 | `git restore 文件名` | 丢弃工作区改动（**不可恢复**） |
| 暂存了想取消 | `git restore --staged 文件名` | 只是取消暂存，改动还在 |
| 提交了想撤回（未推送） | `git reset --soft HEAD~1` | 撤销提交但保留改动 |
| 提交了想撤销（已推送） | `git revert HEAD` | **新增一个反向提交**，不改历史 |

> **`reset` 和 `revert` 的区别很重要：**
> - `reset` **改写历史**。如果已经推送出去，协作者的仓库会和你不一致，很麻烦。
> - `revert` **不改历史**，而是加一个"反向操作"的新提交。
>   所有人在历史里都能看到"加过又撤了"，审计上更清晰。
>
> **判断标准：推没推送过。** 没推送 → 可以 `reset`；推送了 → 用 `revert`。

### 临时保存改动

```bash
git stash            # 把当前改动收起来（工作区变干净）
git stash list       # 看有哪些
git stash pop        # 取回来
```

**什么时候用：** 改到一半，突然要切分支或拉新代码，但不想提交半成品。

---

## 5. 如果以后要推到 GitHub

```bash
# ---------- 在服务器上生成 SSH key ----------
ssh-keygen -t ed25519 -C "你的邮箱"
cat ~/.ssh/id_ed25519.pub          # 复制输出，粘到 GitHub 的 SSH Keys 设置里

# ---------- 关联远程仓库 ----------
git remote add origin git@github.com:你的用户名/仓库名.git
git branch -M main
git push -u origin main
```

### ⚠️ 推送前必须再检查一次

因为**一旦推送到 GitHub，密钥就真的泄露了**（即使删掉，也可能已被缓存/爬取）。

```bash
# 推之前跑这两条
git log --all --full-history -- .env
git log --all -p | grep -E 'sk-[A-Za-z0-9_.-]{20,}'
```

**两条都没有输出**，才推。

**如果发现密钥已经在历史里：**

1. **立刻去百炼控制台吊销那个 key 并重建** —— 这是第一步，也是最有效的
2. 然后才谈清理历史（`git filter-repo`，复杂且需强制推送）

**顺序不能反。** 吊销密钥是止损，清理历史是补救。

---

## 6. 这个仓库的 `.gitignore` 踩过的两个坑

我写 `.gitignore` 时用真实 git 验证，发现并修掉了两个 bug。**都值得记住**：

### 坑 1：`.gitignore` 不支持行尾注释

我原来写了：

```gitignore
!.env.example          # ← 例外：模板文件应该提交
```

**结果这条规则完全失效。** 因为 `.gitignore` 和 `.ini`/`.conf` 不同，
**它没有行尾注释语法** —— `#` 后面的内容会被当成**模式名的一部分**。

真实模式变成了 `!.env.example          # ← 例外：模板文件应该提交`，
永远匹配不到任何文件。于是 `.env.example` 被上面的 `.env.*` 挡住了。

> **结论：`.gitignore` 里的注释必须单独占一行。**
> 这个坑特别隐蔽 —— 不报错，只是**静默失效**。

### 坑 2：父目录被排除后，无法重新包含其中的文件

我原来写了 `data/`，想让 `data/` 整体不进仓库，但保留一个占位文件：

```gitignore
data/
!data/.gitkeep        # ← 这行不会生效
```

**Git 的规则：父目录被排除后，Git 根本不会进入该目录**，
所以 `!data/.gitkeep` 永远不生效。后果是 clone 下来没有 `data/` 目录，
而 `docker-compose.yml` 要挂载它 → 挂载出问题。

**修法：改成 `data/*`**

```gitignore
data/*                # 排除内容，但不排除目录本身
!data/.gitkeep        # 这样 negation 才能生效
```

### 验证方式（建议你以后照做）

**写完 `.gitignore` 不要凭感觉，用真实 git 验一遍：**

```bash
cd ~/phase1-rag

# 造诱饵文件
echo 'DASHSCOPE_API_KEY=sk-fake-secret' > .env.test
git check-ignore -v .env.test      # 应该输出命中的规则
rm .env.test

# 直接看将来会提交什么
git add -A --dry-run
git status --ignored --short
```

**`git status --ignored --short` 是最好用的一个** ——
它以 `!!` 标出被忽略的文件，一眼就能看出"哪些被挡了"。

---

## 7. 一句话总结

| 要点 | 内容 |
|---|---|
| 仓库在哪 | 服务器 `~/phase1-rag`，分支 `main` |
| 本地有版本控制吗 | ❌ 没有（本地没装 git） |
| 密钥安全吗 | ✅ `.env` 被忽略，且**从未进入过任何提交**（已验证） |
| 每次提交前 | **看一眼 `git diff --cached --name-only`** |
| 撤销提交 | 没推送用 `reset`，推送了用 `revert` |
| 推送前 | 再搜一遍密钥 |
| `.gitignore` 注释 | **必须单独占一行** |
