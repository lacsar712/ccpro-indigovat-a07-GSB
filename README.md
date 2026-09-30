# IndigoVat-01 · 染缸还原台

FastAPI + PostgreSQL + Jinja2：主界面是横向**缸位条**（Alpine 反应式），不是工坊/染缸/批次三表导航。Session Cookie 登录；规则在 `app/services/vat_rules.py`。

## 技术栈

- FastAPI、SQLAlchemy 2、PostgreSQL
- 启动时 `create_all` + 幂等种子（蓝靛湾一号坊 / 清水江二号坊）
- Session Cookie 认证（Starlette SessionMiddleware）
- Jinja2 + Alpine.js + Pico（叠靛蓝水墨自定义样式）
- Docker Compose：`web` + `db`

## 端口与数据库

| 服务 | 端口 |
|------|------|
| Web  | **4720** |
| Postgres | **6120**（容器内 5432） |

数据库账号：`indigovat` / `indigovat` / 库名 `indigovat`

## 快速启动

```bash
cd IndigoVat/IndigoVat-01
docker compose up --build -d
```

浏览器打开：http://localhost:4720

演示账号（登录页已预填）：

- `admin` / `123456`
- `worker` / `123456`

## 交互（信息架构）

1. **染缸还原台**：横滑缸位条，每缸显示状态、最近电位与 redox sparkline
2. **工坊 chip**：仅作缸位筛选，无独立工坊 CRUD 页
3. **点缸展开**：同页内登记浸染批次、改状态、看近几笔；无平行「染缸表 / 批次表」
4. **并发上限专页**（顶栏「并发上限」）：上限卡的列表、新建、启停与改最大数

**业务规则**：

- 状态改为 `ready`（可染色）时，最新批次 `redoxMv` 须已填且 ≤ -500（见 `vat_rules.py`）。
- **还原并发上限（按染种计数）**：每张上限卡绑定「工坊 + 染种名」，字段为最大还原中缸数、生效日起、是否启用；同坊同染种同时只许一张启用卡。闲置改还原中前，按目标缸同坊同染种统计当前 `reducing` 缸数（`ready` 可染色**不计入**）；无生效中的启用卡或已达上限即中文拒绝。改启用或改最大数提交后，下一笔改状态立即按新值判定。
- **双改态互斥**：判定与占用计数、还原台 chip 占用摘要共用同一计数函数 `count_reducing`；闲置改还原中会 `SELECT ... FOR UPDATE` 锁住 生效中的上限卡行，两人同时改两口同坊同染种闲置缸时在同一卡行上排队，后到者重新计数，名额不足即拒——至多一笔成功，且占用数始终等于库内 `reducing` 行数。

## 种子数据

- 账号：`admin` / `worker`（密码均 `123456`）
- 工坊：蓝靛湾一号坊、清水江二号坊，各带样例缸位与电位序列
- **上限演示**：蓝靛湾一号坊 · 土靛 上限卡 `max=1` 且启用；`V-01` 已是还原中（占去唯一名额），另备 `V-03` 同染种闲置缸——直接改 `V-03` 为还原中会被拒，先把 `V-01` 改出还原中后即可入。

## 本地开发（可选）

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
pip install -r requirements.txt
set POSTGRES_HOST=localhost
set POSTGRES_PORT=6120
uvicorn app.main:app --host 0.0.0.0 --port 4720 --reload
```

## 业务模型

1. **Workshop**：`name`、`region`、`notes`（UI 上仅为筛选片）
2. **Vat**：归属工坊、`code`、`dyeType`、`volumeL`、状态 `idle|reducing|ready`
3. **DipLot**：归属染缸、`dippedAt`、`clothMeters`、`redoxMv`（可空）
4. **ReducingLimitCard**：归属工坊、`dyeType`、`maxReducing`、`effectiveFrom`（生效日起）、`enabled`；同坊同染种启用卡唯一（部分唯一索引兜底）

## 目录结构

```
IndigoVat-01/
  Dockerfile
  entrypoint.sh
  docker-compose.yml
  requirements.txt
  app/
    main.py
    db.py
    models.py
    schemas.py
    auth.py
    seed.py
    routers/
    services/vat_rules.py
    templates/   # base / bay / limits / login
```
