# 部署到 Linux 服务器

面向生产形态的落地手册。**开发机上的 `scripts/dev-up.ps1` / `dev-down.ps1` 在 Linux 上不适用**——
那套是 Windows 本地开发栈（隐藏窗口起 uvicorn + vite + celery），服务器上由本文第 9 节的
systemd 取代。

形态见 TECH_SPEC §11：

| 层 | 跑在哪 | 由谁管 |
|----|--------|--------|
| PostgreSQL / Redis / Milvus / 业务 MinIO | Docker 容器 | 仓库根 `docker-compose.yml` |
| API（FastAPI / uvicorn） | 宿主进程 | `deploy/systemd/kb-api.service` |
| Celery worker（导入解析、切块入库、FAQ 挖掘） | 宿主进程 | `deploy/systemd/kb-worker.service` |
| Celery beat（每日 03:00 触发挖掘） | 宿主进程 | `deploy/systemd/kb-beat.service` |
| 前端静态产物 + `/api` 反代 | nginx | `deploy/nginx/knowledge-base.conf` |

> 三份 unit 与 nginx 配置**没有在真机验证过**（编写环境是 Windows，无 systemd / nginx 可跑）。
> 落盘前按各文件头部的 TODO 改路径与账号，并逐条跑文中的自检命令。
> 最后对照仓库代码核对：2026-09-16。

---

## 1. 前置条件

| 项 | 要求 | 说明 |
|----|------|------|
| 系统 | Ubuntu 22.04 / 24.04 或等价（RHEL 系见第 12 节的 SELinux 一条） | |
| Docker | Docker CE + compose v2 插件 | 只跑依赖，不跑应用 |
| Python | **不用装**，由 uv 代管 3.12 | 避免与系统 python 打架 |
| Node | 20.x，**仅构建前端时用** | 也可以本机构建好再传 `dist/` |
| GPU | 可选。有 NVIDIA 卡则驱动要支持所选 CUDA 版本 | 无卡改 `EMBED_DEVICE=cpu`，会慢一个量级 |
| 内存 / 磁盘 | 建议 ≥16G 内存；模型权重数 GB，`var/mineru` 会持续增长 | 数据卷与权重放同一块大盘更省事 |
| 对外端口 | 只放行 80 / 443 | 其余端口一律绑回环，见第 4 节 |

```bash
# 一次装齐运行时依赖。docker-compose-v2 提供 `docker compose`（v2 子命令），
# 不是老的 `docker-compose` 独立二进制。
sudo apt update && sudo apt install -y docker.io docker-compose-v2 nginx curl rsync

# 开机自启并立刻拉起 docker：下面起 compose 依赖它。
sudo systemctl enable --now docker
```

---

## 2. 建服务账号与代码目录

```bash
# 建专用系统账号，与 deploy/systemd/*.service 里的 User=kb / Group=kb 对齐。
#   --system          不建普通用户模板，不占 UID 段
#   --create-home     让 uv / HuggingFace 缓存有落脚处（HF_HOME 没显式指定时用它）
#   --shell nologin   禁止交互登录：这个账号只用来跑服务
sudo useradd --system --create-home --shell /usr/sbin/nologin kb

# 代码目录，必须与三份 unit 里的 /opt/kb-platform 一致（不一致就改 unit）。
# chown 给 kb：unit 里指定了 User=kb，而应用要写 backend/var 与模型缓存。
sudo mkdir -p /opt/kb-platform && sudo chown kb:kb /opt/kb-platform
```

**接下来哪些步骤用哪个身份**，混用会出权限问题：

| 步骤 | 身份 | 原因 |
|------|------|------|
| 第 3 节 收尾的 chown、第 4、9、10、11、12 节 | 你的管理员账号（带 `sudo`） | 涉及 docker、`/etc`、systemd |
| 第 5~8 节（装依赖、建表、构建前端） | `kb` 账号 | 产物要落在 `backend/.venv`，root 装出来服务会读不了 |

```bash
# 以 kb 身份进入登录 shell 做第 5~8 节。注意：kb 不是 sudoer，
# 这个 shell 里执行 sudo 会被拒——需要 sudo 的命令回到自己的账号跑。
sudo -u kb -i bash

# 装 uv（会落到 ~/.local/bin，登录 shell 自动进 PATH）。
curl -LsSf https://astral.sh/uv/install.sh | sh
```

---

## 3. 把代码放上去

本仓库不是 git 仓库，没有 `git pull` 这条路；从开发机推一份过去：

```bash
# 在开发机执行。排除的都是「本机特异性产物」：
#   .venv / node_modules  → 服务器上要重建，体系结构与路径都不同
#   backend/hub           → 模型权重，按第 5 节重新拉或单独传
#   backend/var           → 日志、PID、MinerU 原始 ZIP，属于运行态
rsync -av \
  --exclude .venv --exclude node_modules \
  --exclude backend/hub --exclude backend/var \
  ./ kb@<服务器>:/opt/kb-platform/

# 回服务器执行：rsync 以推送账号身份落盘，统一改回 kb。
sudo chown -R kb:kb /opt/kb-platform
```

开发机的 `backend/.env` 里既有开发值也有真实网关 Key，可以顺手传过去当起点，
但**第 6 节那张表的每一项都要在服务器上重过一遍**。

---

## 4. 起依赖，并立刻收口端口 ⚠️

```bash
# 起 PostgreSQL / Redis / Milvus（含内部 etcd + minio）/ 业务 MinIO。
# -d 后台；首次会拉镜像，Milvus 的 healthcheck 有 90s start_period，别急着判断失败。
cd /opt/kb-platform && docker compose up -d

# 看健康状态：五个容器都要 healthy（minio/etcd/milvus 是 Milvus 的内部依赖）。
# milvus 起不来时先看它的日志：docker compose logs -f milvus
docker compose ps
```

**这一步之后必须收口端口**，否则等于把数据库敞在公网上：

`docker-compose.yml` 里这几个端口是发布到 `0.0.0.0` 的，而且口令是开发值：

| 服务 | 当前写法 | 收口后 |
|------|----------|--------|
| postgres | `"15432:5432"` | `"127.0.0.1:15432:5432"` |
| redis | `"16379:6379"` | `"127.0.0.1:16379:6379"` |
| objectstorage | `"19000:9000"` / `"19001:9001"` | `"127.0.0.1:19000:9000"` / `"127.0.0.1:19001:9001"` |
| milvus | `"19530:19530"` | `"127.0.0.1:19530:19530"` |

```bash
# 改端口绑定后重建容器使映射生效（数据在具名卷里，不会丢）。
# 应用侧 .env 里写的是 127.0.0.1:15432 这种地址，所以宿主机回环访问完全不受影响。
docker compose up -d

# 确认端口已经不在 0.0.0.0 上监听：期望每行都是 127.0.0.1:xxx，而不是 *:xxx 或 0.0.0.0:xxx。
sudo ss -ltnp | grep -E '15432|16379|19000|19001|19530'

# 同时把口令换掉（compose 与 .env 要成对改）：
#   POSTGRES_PASSWORD        → 新口令，并同步改 .env 的 DATABASE_URL
#   MINIO_ROOT_PASSWORD      → 新口令，并同步改 .env 的 MINIO_SECRET_KEY
# 不要只改一处：改一半会让 API 启动后连不上库/对象存储。
```

> 别指望用 `ufw` 兜住这些端口：Docker 的端口发布走 `DOCKER-USER` 链，绕过 `ufw` 的 INPUT 规则。
> 绑回环是最省事也最不容易出错的做法。

---

## 5. 后端虚拟环境与模型权重

```bash
# 都在 kb 账号 + backend/ 目录下操作：
# uv 会自动发现当前目录的 .venv，所以先 cd 进来再装包。
cd /opt/kb-platform/backend

# uv 自带 Python 管理，不用 apt 装 python3.12：
# 缺 3.12 时 uv 会自己下载一份独立的解释器，不污染系统。
uv venv --python 3.12

# 业务依赖（FastAPI / SQLAlchemy / Celery / pymilvus / minio 等），秒级装完。
uv pip install -r requirements.txt

# 从样例生成 .env，然后按第 6 节改。
cp ../.env.example .env
```

重模型（BGE-M3 与 reranker）。**torch 必须单独先装，且与服务器驱动匹配**：

```bash
# 有 NVIDIA 卡：index-url 里的 cuXXX 要选与驱动匹配的版本。
# cu121 / cu124 的轮子不含 Blackwell（sm_120）内核，装错会报
# "no kernel image is available for execution on the device"。
# 非 Blackwell 卡先用 nvidia-smi 看驱动支持的 CUDA 版本再定。
uv pip install torch --index-url https://download.pytorch.org/whl/cu128

# 再装其余模型依赖。--constraint 不能省：FlagEmbedding 依赖 torch，
# 不加约束时 uv 会顺手把它换成 PyPI 的轮子，模型可能静默跑在 CPU 上。
uv pip install -r requirements-ml.txt --constraint requirements-ml.constraints.txt

# 校验真能用到 GPU：期望输出 True 和显卡型号（无卡机器跳过）。
# 这里报 False 而 .env 又写着 cuda，模型会回落 CPU，导入会慢到不可用。
.venv/bin/python -c "import torch; print(torch.cuda.is_available(), torch.cuda.get_device_name(0))"

# 无显卡的机器走这条：默认源的 torch 即可，但要在 .env 里设 EMBED_DEVICE=cpu。
# uv pip install -r requirements-ml.txt --constraint requirements-ml.constraints.txt
```

权重（BGE-M3 ≈2.2G，bge-reranker-v2-m3 ≈2.2G）：

```bash
# 方式一：交给脚本/首次运行自动拉。先确认 .env 里 HF_HOME 指向大盘，
# 否则会落在 /home/kb/.cache/huggingface——系统盘容易被撑满。
.venv/bin/python scripts/fetch_models.py

# 方式二：内网机器。从开发机拷缓存目录，再设 HF_HUB_OFFLINE=1 防止联网校验：
#   rsync -av ~/.cache/huggingface/hub/ kb@<服务器>:$HF_HOME/hub/
# （Windows 开发机上就是 C:\Users\<你>\.cache\huggingface\hub）
```

> systemd 那三份 unit 里都写了 `EnvironmentFile=.../backend/.env`，作用就是让 `HF_HOME`
> 这类被 huggingface_hub 直接读 `os.environ` 的变量也生效——它们不走 pydantic，
> 只写在 `.env` 里对模型加载没作用。

---

## 6. 生产 `.env`：逐项过一遍

```bash
# 编辑，逐项对照下表。字段含义的权威来源是仓库根 .env.example 的注释。
nano /opt/kb-platform/backend/.env
```

| 变量 | 改成 | 不改的后果 |
|------|------|-----------|
| `SECRET_KEY` | 随机长串 | 开发值 `dev-only-change-me` 是公开的，JWT 可被伪造 |
| `CONFIG_ENCRYPTION_KEY` | 显式生成（`.env.example` 第 20 行有现成命令） | 留空会从 `SECRET_KEY` 派生，之后轮换 `SECRET_KEY` 会让已存模型 Key 密文全部解不开 |
| `DATABASE_URL` / `MINIO_SECRET_KEY` | 与第 4 节改后的口令一致 | 连不上库 / 对象存储，API 起不来 |
| `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_CHAT_MODEL` | 真实 OpenAI 兼容网关 | 三个都空着，问答链路直接不可用 |
| `MINERU_API_KEY` | 控制台给的 Access Key | PDF / DOCX 解析必失败（MD / TXT 直读不受影响） |
| `EMBED_DEVICE` | `cuda`（无卡改 `cpu`） | 有卡却写 cpu 会白慢一个量级 |
| `HF_HOME` | 大盘路径 | 权重数 GB 落到系统盘 |
| `MINERU_OUTPUT_DIR` | 有空间的盘 | 每次解析的原始 ZIP 都在这儿留一份，只涨不降 |

```bash
# 生成 CONFIG_ENCRYPTION_KEY（在 backend/ 下跑，用虚拟环境的 python：
# cryptography 是 python-jose[cryptography] 带进来的依赖）。
.venv/bin/python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# 生成 SECRET_KEY 的现成做法（48 字节够长）。
.venv/bin/python -c "import secrets; print(secrets.token_urlsafe(48))"
```

---

## 7. 建表与种子数据

```bash
# 仍在 /opt/kb-platform/backend 下。
# 建全部 17 张表：干净库一次通过，重复执行是空操作。
.venv/bin/python -m alembic upgrade head

# 写入权限码、角色、部门、模型配置默认值与演示账号（密码入库为 bcrypt 哈希）。
.venv/bin/python -m app.services.seed

# 校验：应能看到表数量与演示账号。
docker exec kb_postgres psql -U kb -d knowledge_base -c "\dt" | head -20
```

⚠️ **演示账号口令统一是 `Demo@123456`**（`README.md` 的「演示账号」一节与
`data/seeds/users.json` 里都写着）。上线前必须改口令或删掉这批账号，否则等于留了后门。

---

## 8. 构建前端

```bash
# Node 20 环境。有 package-lock.json，用 npm ci 保证与开发机逐字一致。
cd /opt/kb-platform/frontend
npm ci

# 产出 frontend/dist。前端所有请求都走同源相对路径 /api，
# 因此产物不绑定构建机器——也可以在本机构建后只把 dist/ 传过去，
# 服务器上就不必装 Node。
npm run build
```

---

## 9. 装 systemd 三件套

先改三份 unit 里的 TODO，再启用：

```bash
# 三份 unit 里各有两处必须改：
#   User=kb / Group=kb               → 实际跑服务的账号（第 2 节建的那个）
#   /opt/kb-platform/...             → 代码实际路径（每份文件里出现多次）
# 其它已按本项目写死，不用动：
#   WorkingDirectory 必须是 backend/（.env、alembic.ini、var/ 都相对它）
#   worker 必须 --pool=threads（prefork 会让每个子进程各加载一份 BGE-M3，显存翻 N 倍）
#   worker 并发数要与 .env 的 INGEST_CONCURRENCY 一致（systemd 的 ExecStart 不做变量展开）
sudo cp /opt/kb-platform/deploy/systemd/*.service /etc/systemd/system/
sudo nano /etc/systemd/system/kb-api.service      # 同样处理 kb-worker / kb-beat

# beat 的调度状态文件要写进 backend/var，先确保目录存在且归属正确。
sudo mkdir -p /opt/kb-platform/backend/var
sudo chown -R kb:kb /opt/kb-platform/backend/var

# 让 systemd 重新读 unit，再设置开机自启并立刻启动。
sudo systemctl daemon-reload
sudo systemctl enable --now kb-api kb-worker kb-beat

# 看三个服务的状态：active (running)。API 首次启动要加载模型权重，
# TimeoutStartSec=300 就是留给它的，别在 30 秒内判死刑。
systemctl status kb-api kb-worker kb-beat --no-pager

# 跟日志。API 是 journalctl -u kb-api -f；worker 里能看到 MinerU 轮询与切块进度。
journalctl -u kb-api -f
```

必须全部启用：**导入接口在 worker 不在时照常受理，但进度会永远停在 `queued`**，
而 beat 负责每日 FAQ 挖掘。worker 与 beat 都只能各起一个实例。

---

## 10. 装 nginx

```bash
# conf.d 里的文件是被 include 进 http 块的，本配置开头的 map 写在同级正好合法。
sudo cp /opt/kb-platform/deploy/nginx/knowledge-base.conf /etc/nginx/conf.d/

# 要改两处：server_name（域名，没有就填 _）与 root（前端 dist 的实际路径）。
sudo nano /etc/nginx/conf.d/knowledge-base.conf

# 语法自检 + 平滑重载。nginx -t 报错就别 reload，直接读报错行。
sudo nginx -t && sudo systemctl reload nginx
```

配置里两处**不能省**的东西（都写着注释）：

- `/api/` 反代的 `Upgrade` / `Connection` 头：AI 工作台的问答走 `WS /api/v1/ws/chat`，
  少了这两行握手建不起来，前端一直转圈、**后端日志里什么都不会有**
- `client_max_body_size 256m`：nginx 默认 1m，导入接口会直接 413

要 HTTPS 就上 certbot；WebSocket 会跟着页面协议自动用 wss，前端不用改。

---

## 11. 上线自检

```bash
# 1) 后端活着：期望 {"code":0,...,"status":"ok"} 这类统一响应。
curl -s http://127.0.0.1:8000/api/v1/health

# 2) 经过 nginx 也通：期望 200（顺带确认静态托管的入口页可访问）。
curl -s -o /dev/null -w '%{http_code}\n' http://<域名>/

# 3) 全链路验收脚本：真实 HTTP + WebSocket，覆盖权限隔离、FAQ 命中、审计与看板，
#    需要整套栈（compose + api + worker）都在跑。
cd /opt/kb-platform/backend && .venv/bin/python scripts/e2e_acceptance.py
```

再手工过一遍界面：登录 → 导入一份文档（看进度走到 `indexed`）→ 提问（看引用卡片与流式输出）
→ 停用该单元（确认检索不再命中）。

---

## 12. 日常运维

### 更新代码

```bash
# 1) 传新代码（同第 3 节的排除项；.env 与 var/ 会被保留，除非你刻意覆盖）
# 2) 依赖有变动才需要重装
cd /opt/kb-platform/backend && uv pip install -r requirements.txt
# 3) 有迁移就执行（没有新迁移时是空操作，可无脑跑）
.venv/bin/python -m alembic upgrade head
# 4) 重启应用三件套。前端产物更新不需要动 nginx。
sudo systemctl restart kb-api kb-worker kb-beat
# 5) 前端有改动则重新构建
cd /opt/kb-platform/frontend && npm ci && npm run build
```

### 备份

```bash
# 先看清卷的真实名字：compose 会自动加项目名前缀（一般是以代码目录名派生）。
docker volume ls | grep -i kb

# PostgreSQL 逻辑备份：服务运行时也能做，恢复时用 psql 灌回即可。
docker exec kb_postgres pg_dump -U kb -d knowledge_base | gzip > /backup/kb-$(date +%F).sql.gz

# 业务对象存储：原件与答案附图都在这里，是「知识」的本体，必须备。
docker run --rm -v <objectdata卷名>:/data -v /backup:/backup \
  alpine tar czf /backup/objectdata-$(date +%F).tar.gz -C /data .

# Milvus 卷可以不单独备：向量都能由原件重投重建（重新导入该单元即可），
# 只是费时且要重跑 MinerU。若导入量很大，建议一起备。
```

`redisdata` 丢了不影响正确性：里面是权限/ACL/FAQ 缓存与 Celery 的 broker 结果，
丢的是缓存与未消费的任务，重投即可。

### 排障表

| 现象 | 先看这里 |
|------|----------|
| 导入进度永远 `queued` | worker 没起或崩了：`systemctl status kb-worker`、`journalctl -u kb-worker -n 100` |
| 前端发请求 413 | `client_max_body_size`（导入一次可带多个文件，单文件上限 20MB） |
| 问答一直转圈、没有输出 | nginx `/api/` 的 `Upgrade` / `Connection` 头 |
| 问答中途断 | `proxy_read_timeout`：后端空闲 10 分钟才断，配置里给的是 900s |
| 解析慢到不可用 | GPU 没用上：第 5 节的 torch 校验命令 + `.env` 的 `EMBED_DEVICE` |
| API 起不来、连库失败 | 第 4 节口令改了一半（compose 与 `.env` 不一致） |
| Milvus 起不来 | `docker compose logs -f milvus`，以及它依赖的 etcd / minio 是否 healthy |
| 显存不足 | `nvidia-smi`；API 是单 worker + worker 各一份 BGE-M3 与 reranker |

---

## 13. 与开发机的差异（容易踩的认知偏差）

1. **`--pool=threads` 在 Linux 上同样必须保留**。README 的技术栈表把它写成「Windows 用」，
   那是按平台归因；真正原因在 `app/engines/celery/celery_app.py` 顶部——prefork 进程池会让
   每个子进程各加载一份 BGE-M3，显存翻 N 倍。除非服务器显存很宽裕，不要换成 prefork。
2. **`UPLOAD_DIR` 已废弃**（`.env.example` 里注明了）：原件现在存 MinIO，
   所以持久化重点是 `objectdata` 卷，不是本地上传目录。
3. `.venv` 的解释器路径是 `.venv/bin/python`，不是 Windows 的 `.venv\Scripts\python.exe`；
   本文与 unit 里都用前者。
4. `backend/main.py` 那个本地启动脚本在 Linux 上也能跑（`python main.py --host 0.0.0.0`），
   但它不带进程守护，生产一律走 systemd。
5. RHEL / CentOS 系默认开 SELinux，nginx 读 `/opt/...` 会被拒：
   ```bash
   # 给前端产物打上 httpd 可读标签（或改用 /usr/share/nginx/html 作为 root）。
   sudo chcon -Rt httpd_sys_content_t /opt/kb-platform/frontend/dist
   ```
