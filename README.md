# 一二矿山 生产协同系统

湖南一二矿山科技有限公司生产部门的配套系统，覆盖从销售订单到成品出库的整个生产闭环：

```
销售订单 → BOM 多级展开 → 齐套计算与冲填预占（只分空闲量，已分配的冻结；动他单的料走调料申请）
        → 缺料：外购生成请购 → 采购订单（行级锁定）→ 供应商门户回复交期
                自制生成加工任务 → 报工入库
                外协生成外协单 → 发出（图纸确认）→ 回厂
        → 到货 → 检验 → 合格入库（批次/先进先出）  ┐
                       └ 不合格 → 来料异常审批闭环  ┘→ 入库即触发重算、冲填给急单
        → 图纸版本确认（准入）→ 按 BOM 领料 → 开工装配 → 完工入库
        → 发货计划审批 → 发货出库
  贯穿：预警中心 / 车间大屏（车间模式、展示模式）/ 部门周计划 / 开放接口 / AI 辅助（可选）
```

需求与功能的逐条对照见 [`docs/需求对照与设计说明.md`](docs/需求对照与设计说明.md)。

---

## 快速体验（演示数据）

需要 **Python 3.12**（已在 3.12 上完整安装并通过全部测试；3.10 已结束支持）。前端已经打包好，放在 `frontend/dist`，不需要装 Node。
启动脚本会设置 `APP_ENV=demo`（演示模式，带演示账号和数据），**不要用于正式环境**。

- **Windows**：双击 `start-demo.bat`
- **Linux / macOS**：运行 `./start-demo.sh`

启动后浏览器打开 <http://127.0.0.1:8000>。

| 账号 | 密码 | 角色 |
|---|---|---|
| admin | admin123 | 管理员（全部权限） |
| zhuguan | 123456 | 生产主管 + 计划员 |
| jihua | 123456 | 计划员 |
| caigou | 123456 | 采购员 |
| cangku | 123456 | 仓库 |
| zhijian | 123456 | 质检 |
| chejian | 123456 | 车间 |
| jishu | 123456 | 技术 |
| shouhou | 123456 | 售后 |
| xiaoshou | 123456 | 销售 |

- 车间大屏：`/board/workshop?key=board-demo`
- 周计划小屏：`/board/weekly?key=board-demo`
- 供应商门户：`/portal/demo-supplier-s001`
- 接口文档（Swagger）：`/docs`

演示数据里的客户名、供应商名都是虚构的。想重置演示库，删除 `backend/prodsync.db` 后重新启动即可。

> 默认的 `APP_ENV` 是 **production**：不设置就会按正式环境启动，此时系统拒绝弱/默认密钥、默认账号密码、演示看板密钥、通配跨域和自动建表，并且不能灌入演示数据。

## 正式部署

### 方式一：Docker（推荐）

```bash
export MYSQL_ROOT_PASSWORD=... MYSQL_PASSWORD=... SECRET_KEY=$(openssl rand -hex 32) CORS_ORIGINS=https://你的访问域名
docker compose up -d
```

- 启动时会自动执行 `alembic upgrade head` 建表或迁移。
- 数据库数据和上传的图纸分别保存在 `dbdata`、`uploads` 两个卷里。
- 首次启动后，创建管理员账号（密码至少 10 位，含字母和数字；不要用演示账号）：

  ```bash
  docker compose exec app python -m app.cli create-admin --username 你的账号
  ```

- 在【仓库库位】里建好三类仓库：生产物料、售后备件、成品。
- 然后在【系统设置 → 数据导入】里按顺序导入：物料 → BOM → 期初库存。

### 方式二：Windows 服务器直接运行

1. 安装 Python 3.12 和 MySQL 8，创建库：

   ```sql
   CREATE DATABASE prodsync CHARACTER SET utf8mb4;
   ```

2. 参考 `backend/.env.example` 设置环境变量：`APP_ENV=production`、强随机 `SECRET_KEY`、实际访问域名 `CORS_ORIGINS`、`AUTO_CREATE_TABLES=0`。
3. 在 `backend` 目录下依次执行：

   ```
   pip install -r requirements.txt
   alembic upgrade head
   python -m app.cli create-admin --username 你的账号
   uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 2
   ```

4. 建议用 NSSM 把上面的命令注册成 Windows 服务，开机自启。

### 备份

- 每天备份 MySQL：`mysqldump prodsync`
- 同时备份 `UPLOAD_DIR` 目录（里面是图纸文件）

### 升级

覆盖代码后执行 `alembic upgrade head`，再重启服务。表结构的变更只走 Alembic 增量迁移；生产环境不要用 `AUTO_CREATE_TABLES`。

## 开发

```bash
# 后端
cd backend
pip install -r requirements.txt
python -m app.seed
uvicorn app.main:app --reload

# 前端（另开一个终端）
cd frontend
npm install
npm run dev   # http://localhost:5173，/api 会代理到 8000 端口

# 测试（默认 SQLite；指定 MySQL 8 则用 DATABASE_URL，两种库都应通过）
cd backend && pip install -r requirements-dev.txt
APP_ENV=test python -m pytest -q
DATABASE_URL='mysql+pymysql://user:pwd@127.0.0.1:3306/prodsync_test?charset=utf8mb4' python -m pytest -q
```

改了数据模型之后，要生成迁移：

```
alembic revision --autogenerate -m "说明"
```

## 技术栈

| 层 | 选型 | 说明 |
|---|---|---|
| 后端 | Python 3.12、FastAPI、SQLAlchemy 2.0、Alembic、Pydantic 2 | 同步 ORM，事务清晰，便于排查 |
| 数据库 | MySQL 8（生产）/ SQLite（演示、测试） | 枚举值都存成字符串，新增状态不需要改表 |
| 前端 | Vue 3、Element Plus、Pinia、Vite | 多页签工作区，切换模块不丢失原页面 |
| 字体 | Barlow / Barlow Condensed（打包在本地） | 工厂内网无外网也能正常显示 |
| 认证 | JWT + bcrypt；开放接口用 API Key（只存哈希） | 一个账号可以兼多个角色 |

## 目录

```
backend/
  app/models/        数据模型（39 张表）
  app/services/      业务规则：kitting 齐套引擎、purchase 采购（行级锁定/冲销）、
                     receiving 到货检验与异常、inventory 库存（先进先出）、
                     orders 订单/领料/退料/发货、reallocation 调料申请、drawings 图纸（订单绑定批准版本）、board 看板、
                     alerts 预警、ai 辅助、importer 导入
  app/api/           HTTP 接口
  alembic/           数据库迁移
  tests/             自动化测试（齐套引擎单测 + 端到端业务流程 + 7 个关键场景的并发/回滚测试）
frontend/src/views/  页面
docs/                需求对照与设计说明
```
