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
2. **工坊 chip**：仅作缸位筛选，无独立工坊 CRUD 页；chip 上带「还原中 n」占用摘要
3. **点缸展开**：同页内登记浸染批次、改状态、看近几笔；无平行「染缸表 / 批次表」
4. **并发上限专页**（顶栏「并发上限」）：上限卡的列表、新建、启停与调整

**业务规则**：状态改为 `ready`（可染色）时，最新批次 `redoxMv` 须已填且 ≤ -500（见 `vat_rules.py`）。

## 并发上限（按染种）

上限卡字段：**工坊、染种名、最大还原中缸数、生效日起、是否启用**。卡是长期有效的
standing 配置（不按自然日/班次建卡），自「生效日起」生效；同坊同染种同一时刻只许
一张启用卡（应用层校验 + `(workshop_id, dyeType) WHERE enabled` 部分唯一索引兜底）。

- **按染种计数**：闲置改「还原中」前，统计目标缸**同坊且同染种**、当前已是还原中的
  缸数；只数 `reducing`，**可染色（ready）不计入**。无生效中的启用卡、或占用已达
  最大数，均中文拒绝，状态不变。
- **立即生效**：改「是否启用」或「最大还原中缸数」后，下一笔改状态判定立即按新值放行/拒绝。
- **计数同源**：占用计数唯一来源是 `vat_rules.reducing_occupancy()`（一条按
  `(workshop_id, dyeType)` 分组统计 `reducing` 的查询）。改状态校验、缸位 chip 的
  「还原中 n」摘要、上限卡列表的「当前占用」列，全部从它取数，因此界面占用数与库内
  还原中行数始终一致（差为 0）。
- **双改态互斥**：改状态与占用计数在同一事务内完成；判定前对生效上限卡行
  `SELECT ... FOR UPDATE`，同坊同染种的并发「改还原中」在卡行上串行。两人几乎同时
  把两口同坊同染种闲置缸改入还原中、只剩 1 个名额时，至多一笔成功，另一笔收到中文
  拒绝；被拒后还原台照常打开，占用数与库内一致。

种子数据：蓝靛湾一号坊·土靛 上限卡 `最大还原中缸数 = 1`（启用、即日生效），
V-01（土靛）已占这 1 口；另备 V-03 / V-04 两口同染种闲置缸——默认改任一口还原中
都会被拒；在专页把上限调到 2 后，两口同时改还原中可验证互斥（至多一笔成功）。

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
4. **ReductionCap**：并发上限卡——归属工坊、`dyeType`、`maxReducing`、`effectiveFrom`、`enabled`

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
    templates/   # base / bay / caps / login
```
