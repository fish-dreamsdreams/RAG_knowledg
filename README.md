# 知识库管理平台

企业内部知识库演示项目：文档导入 → 父子块切片 → 本地 BGE-M3 向量化 → 三路召回 + 重排 →
**四维数据权限（默认拒绝）** → AI 问答与运营闭环。

- 需求基线：`.monkeycode/specs/knowledge-base-platform/prd.md`
- 技术规范：`.monkeycode/docs/TECH_SPEC.md`
- 详细设计：`.monkeycode/specs/knowledge-base-platform/design.md`
- 实施计划：`.monkeycode/specs/knowledge-base-platform/tasklist.md`

## 技术栈

| 层 | 选型 |
|----|------|
| 后端 | Python 3.12 + FastAPI + SQLAlchemy 2.0（异步）+ Alembic |
| 前端 | React 18 + Vite + Ant Design 5（JavaScript） |
| 存储 | PostgreSQL 16、Milvus 2.4（standalone）、Redis 7 |
| 模型 | 本地 BGE-M3（dense + sparse）、本地 bge-reranker；Chat 走 OpenAI 兼容网关 |
| 文档解析 | MinerU **在线 API**（不本地部署） |
| 任务队列 | Celery（Windows 用 `--pool=threads`）+ Redis |

## 端口

| 服务 | 地址 | 说明 |
|------|------|------|
| 前端 | http://127.0.0.1:5173 | Vite dev，`/api` 代理到后端 |
| 后端 | http://127.0.0.1:8000 | `GET /health`、`GET /api/v1/health` |
| PostgreSQL | 127.0.0.1:15432 | 避开其他项目占用的 5432 |
| Redis | 127.0.0.1:16379 | 避开其他项目占用的 6379 |
| Milvus | 127.0.0.1:19530 | standalone，含内部 etcd/minio |

## 快速开始

### 1. 启动依赖

```bat
docker compose up -d
docker compose ps
```

### 2. 后端

```bat
cd backend
uv venv --python 3.12
uv pip install -r requirements.txt
copy ..\.env.example .env

.venv\Scripts\python.exe -m alembic upgrade head
.venv\Scripts\python.exe -m app.services.seed
```

重模型依赖（BGE-M3、bge-reranker）体积大，按需单独安装。**本机显卡是 RTX 5070 Laptop（Blackwell / sm_120），必须用 CUDA 12.8 源**：

```bat
:: cu121 / cu124 轮子不含 sm_120 内核，装上会报 no kernel image is available
uv pip install torch --index-url https://download.pytorch.org/whl/cu128
uv pip install -r requirements-ml.txt

:: 校验 GPU
.venv\Scripts\python.exe -c "import torch; print(torch.cuda.is_available(), torch.cuda.get_device_name(0))"
```

`.env` 里 `EMBED_DEVICE=cuda`（RTX 5070 Laptop 8GB 显存跑 BGE-M3 + bge-reranker-base 够用）；无显卡的机器改回 `cpu`。

### 3. 前后端开发服务器

```bat
cd frontend && npm install
powershell -ExecutionPolicy Bypass -File scripts\dev-up.ps1     :: 启动（隐藏窗口）
powershell -ExecutionPolicy Bypass -File scripts\dev-down.ps1   :: 停止
```

日志与 PID：`backend\var\uvicorn*.log`、`backend\var\vite*.log`、`backend\var\celery*.log`。

只想起后端（不开 vite / celery、要热重载或换端口）时用 `main.py`，默认值与上面那条 uvicorn 命令一致：

```bat
cd backend
.venv\Scripts\python.exe main.py                 :: 127.0.0.1:8000
.venv\Scripts\python.exe main.py --reload          :: 改后端代码自动重启（会断一次 WebSocket）
.venv\Scripts\python.exe main.py --port 8010
.venv\Scripts\python.exe main.py --host 0.0.0.0    :: 局域网可访问
```

### 4. Celery worker 与 beat

`dev-up.ps1` 已经带上 worker（`--pool=threads --concurrency=4`）：导入解析、切块入库、图注生成与 FAQ 挖掘都跑在它里面，
worker 不在时导入接口照常返回 `task_id`，但进度会永远停在 `queued`，所以它跟 uvicorn / vite 一样属于开发栈的一部分。
需要手工单独起（或调试 worker）时用：

```bat
cd backend
.venv\Scripts\python.exe -m celery -A app.engines.celery.celery_app worker --pool=threads --concurrency=4 --loglevel=info
.venv\Scripts\python.exe -m celery -A app.engines.celery.celery_app beat
```

## 测试

后端测试只在 `backend/test/`，结构镜像 `app/`；前端测试在 `frontend/test/`，结构镜像 `src/`。

```bat
cd backend
.venv\Scripts\python.exe -m pytest                  :: 单元测试（默认排除 integration）
.venv\Scripts\python.exe -m pytest -m integration    :: 需要 docker compose 依赖

cd frontend
npm test                                            :: 关键交互测试（vitest + jsdom，无需后端）
```

前端测试只跑在 jsdom 里、不连后端：覆盖 `ConsoleLayout` 的菜单权限边界与 `ChatBubble` 的权限缺失提示条（14.8）。

端到端验收（需 compose + 后端 + worker 在跑）：

```bat
cd backend
.venv\Scripts\python.exe scripts\e2e_acceptance.py   :: 登录 → 导入现状 → 问答 → 审计 → 看板 → 图片权限
```

退出码 `0` 全通过、`1` 有断言失败、`2` 前置没满足（stack 没起 / 演示数据没导）。它走真实 HTTP 与
WebSocket，覆盖 P3、P4、P12、P14、P19 的端到端形态；更细的逐条断言在 `pytest` 与
`pytest -m integration` 里（完整口径是三者的并集，脚本自身也这么打印）。

它提的是真问题，所以会往库里留东西：每跑一次多 5 条 `qa_audit_logs`（看板 PV 会涨，反复跑同一
个问句也可能被挖掘成候选）。要回到「初值」的演示态，清掉非 `demo-` 前缀的审计行即可（`demo_logs`
只清自己写的那批）。

## 演示账号

口令统一 `Demo@123456`（见 `data/seeds/users.json`，仅演示用，入库为 bcrypt 哈希）。

| 账号 | 姓名 | 部门 | 角色 |
|------|------|------|------|
| admin | 系统管理员 | 总公司 | 系统管理员 |
| kbadm | 知识管理员 | 总公司 | 知识管理员 |
| finance01 | 财务专员 | 财务部 | 普通员工 |
| sales01 | 销售专员 | 销售部 | 普通员工 |
| sales02 | 华东销售 | 销售部-华东大区 | 普通员工（用于验证不继承父部门） |
| cs01 | 客服专员 | 客服部 | 普通员工 |
| hr01 | 人事专员 | 人力资源部 | 普通员工 |

## 目录

```
backend/
  app/
    api/            HTTP 路由（v1、deps 鉴权与权限码）
    websocket/      问答流式通道
    common/         配置、日志、异常、数据库、安全、Redis
    models/         ORM 模型（17 张表）
    schemas/        出入参
    repositories/   唯一写 SQL 的地方
    services/       用例编排、种子加载
    graphs/         LangGraph 状态机（state/edges/nodes）
    engines/        acl / retrieve / rerank / faq_cache / chunking / embed / mineru / celery
  test/             全部测试
  alembic/          迁移
frontend/           Vite + React 18 + Ant Design 5
  src/              页面、组件、请求层
  test/             关键交互测试（vitest）
data/
  seeds/            部门、权限码、角色、演示账号
  knowledge/        场景文档
scripts/            启停脚本
```

## 演示数据

`data/knowledge/` 下按 PRD 双场景造好 5 份制度文档（MD ×3、TXT ×1、PDF ×1）。实际 ACL 与
导入脚本一致（`backend/scripts/import_scenario_a.py` / `prepare_scenario_b.py`）：

| 场景 | 文件 | ACL | 为什么 |
|------|------|-----|--------|
| A | `scenario-a/差旅费用标准.md` | 全局 | 销售部问差旅要拿得到答案与溯源 |
| A | `scenario-a/薪酬管理制度.md` | 部门=人力资源部 | 销售部问薪酬必须回权限缺失，且不得泄露标题正文 |
| A | `scenario-a/财务报销.pdf`（走 MinerU，含票据图）| 部门=财务部 | 部门内部单据规范 |
| B | `scenario-b/客服退款流程.md` | 全局 | 客服高频退款问法供 FAQ 沉淀 |
| B | `scenario-b/清关问题说明.txt` | 全局 | **刻意留到「缺口 → 转建导入」那一步再导**，早导就没有缺口可看了 |

`scenario-a/财务报销制度.pdf` 是 5.5KB 占位件，正片是 `财务报销.pdf`，导入脚本会提示而不导入它。
映射与关键事实点见 `data/knowledge/README.md`；重新生成 PDF：

```bat
uv run --with reportlab python scripts\make_demo_pdf.py
```

### 从零跑到可演示

```bat
scripts\dev-up.ps1                                   :: compose + 后端 + worker + 前端
cd backend
.venv\Scripts\python.exe -m app.services.seed        :: 部门/权限码/角色/账号/模型配置
.venv\Scripts\python.exe -m app.services.demo_logs   :: 场景 B 日志 67 行 + 清关缺口
.venv\Scripts\python.exe scripts\import_scenario_a.py      :: 场景 A 三份文档 + ACL + 启用
.venv\Scripts\python.exe scripts\prepare_scenario_b.py     :: 场景 B 退款文档 + FAQ 挖掘 + 发布
```

两个导入脚本都幂等（已有单元跳过、`failed` 的重投）：`import_scenario_a.py --verify` 只核对
现状；`prepare_scenario_b.py --import-customs` 是「转建导入」之后补导清关文档的那一步。

`demo_logs` 也是幂等的：重跑会先清掉 `trace_id` 以 `demo-` 开头的旧行与同名缺口再重建。
退款四大簇（生鲜时限 / 到账时间 / 审核时长 / 七天无理由）供 FAQ 挖掘聚成候选；清关三簇的
相似度刻意压在 `gap_sim_threshold` 以下，供缺口列表与「转建导入」演示。

### 演示走法

**场景 A（财务薪酬隔离）**——用销售部账号 `sales01`：

| 问法 | 预期 |
|------|------|
| 出差住宿费标准是多少？ | 给出三档城市限额 + 「差旅费用标准」溯源卡片；若同批召回里有无权单元，另出「部分资料无权查阅」提示 |
| 薪酬标准和报销补贴有哪些？ | 只回权限缺失文案，**不提**薪酬制度标题与正文（P3） |

财务部账号 `finance01` 问「财务报销需要哪些票据和审批材料？」应给出引用卡片与票据附图。

**场景 B（客服 FAQ 与缺口）**——用客服账号 `cs01`：

| 问法 | 预期 |
|------|------|
| 已发布 FAQ 的问句原样（如「退款审核要多久」） | FAQ 命中：`faq_hit=true`，token 为 0、无引用（不检索、不生成） |
| 公司年会在哪里举办 / 海外直邮清关延误怎么处理 | 缺口文案「知识库里暂时没有能回答这个问题的内容」+ `knowledge_gaps` 新增一行 |

> FAQ 命中的向量阈值 `faq_sim_threshold` 默认 `0.88`，只认近乎同句的问法（挖掘时合并同义问法的
> `faq_cluster_sim` 是 `0.8`，比它宽）。所以「退款审核一般几天」这类同义问法会落到正常检索链——
> 照样答得出、带引用，只是不走 FAQ 快捷出口。要放宽就在「系统配置 → 模型配置」调它，调完按
> §缺口阈值 的口径重新标定。`scripts/e2e_acceptance.py` 因此拿**已发布 FAQ 的问句原样**去问
> （它自己从接口取），不把阈值标定混进「命中即不检索」这条不变量的验收里。

两条链路的审计与结论可在控制台核对，也可用 `GET /api/v1/audit-logs`（或直接查库）：`answer_status`
分别是 `answered` / `denied` / `gap`，FAQ 命中时 token 恒为 0。控制台对应两个页面：

- **运营看板**（`/dashboard`，`dash:view`）：「概览」页签给 PV/UV、FAQ 命中率、知识覆盖率、响应时长
  （P50/P90）、Token 总量与按天趋势、高频问题与热门知识 TOP10，切「今天 / 近 7 天」两档；
  「审计流水」页签是逐条明细（按日期区间/回答状态/FAQ 命中筛选，行可展开看 trace_id、
  改写问句、放行单元、引用切片与 token 耗时）——不选日期就是近 7 天。
- **沉淀运营**（`/operations`）：FAQ 候选审核（发布 `faq:publish` / 驳回 `faq:review`）、已发布 FAQ 管理
  （改答案、缓存开关、上下线）、知识缺口列表与「转建知识文档」（`gap:convert`，打开导入抽屉并带
  `from_gap_id`，索引完成后缺口自动置 `filled`）。

审计明细也可直接走接口 `GET /api/v1/audit-logs`（参数与页签筛选一一对应）。

### 缺口阈值

`gap_sim_threshold`（代码默认 0.62）决定「低于多少相似度就按没有知识处理」，存在 `model_configs`
单行表里，可在控制台的「系统配置 → 模型配置」（`sys:model`）里改，也可直接
`PUT /api/v1/system/model-config`。标定依据：实测 83 条真实问答中相关问法 `max_similarity` ≥ 0.634、
无关问法 ≤ 0.596。
**换嵌入模型或语料规模后必须重新标定**：偏低会让缺口收不到（`knowledge_gaps` 不增长），
偏高会把「弱相关但答得出」的问题说成没有知识。旧库升级时这一行不会自动跟随代码默认值，
需显式改一次。


## 环境注意事项

1. **端口冲突**：宿主机已有其他项目占用 5432/6379/9000，本项目依赖改用 15432/16379，且 Milvus 的 minio 不对外发布端口。
2. **PowerShell 脚本必须是纯 ASCII**：Windows PowerShell 5.1 按系统 ANSI（GBK）读取 `.ps1`，中文串会破坏解析。
3. **`backend\alembic.ini` 保持纯 ASCII**：Alembic 同样按 locale 编码读取。
4. **Celery `threads` pool 下不要每个任务各自 `asyncio.run()`**：需按线程复用事件循环，否则 asyncpg 连接池跨事件循环复用会报 `Event loop is closed`。
5. **MinerU 在线 API 会把原件上传到外部服务**：如需文档不出内网，把 `MINERU_API_BASE_URL` 指向内网自建 MinerU，业务代码无需改动。
6. **torch 必须用 CUDA 12.8 源**：本机 Blackwell 架构（RTX 50 系）不被 cu121/cu124 支持，见 `backend\requirements-ml.txt`。
7. **导入整个目录请用「拖拽」而不是「选择文件夹」**：Windows 会按应用记住文件类型过滤，某些机器上
   系统目录选择框会一个文件都不列（点确定也拿不到内容，`webkitdirectory` 与 File System Access 两条路都会中招）。
   把文件夹直接拖进导入抽屉的虚线框，或用「选择文件」在对话框里 Ctrl+A 全选——这两条都不经过那个对话框。
   抽屉在读到 0 个文件时会直接把这两条路写在界面上。
