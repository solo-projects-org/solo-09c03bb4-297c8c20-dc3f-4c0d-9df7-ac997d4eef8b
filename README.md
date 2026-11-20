# 口述史馆 · 禁播消音标记管理

口述史馆在一版短片里标出需要消音的内容后，剪辑师需要找到同一段原声在其他版本里的位置。
本系统导入各版剪辑表（CSV），审核员在任一版登记禁播时段（可跨剪接点），系统自动追溯到
原录音时段，并在任一版列出每次使用所需遮盖的成片时段；重叠处显示全部标记号。

## 运行

```bash
docker compose up --build
# 打开 http://localhost:8000
```

SQLite 数据库写入命名卷 `mute-data`（容器内 `/data/app.db`），重启后数据保留，
仍可按标记号核对剩余生效时段。

本地开发（不用 Docker）：

```bash
pip install -r requirements-dev.txt
DB_PATH=data/app.db uvicorn app.main:app --reload
python -m pytest tests/ -v     # 自测
```

## 概念与规则

- **剪辑版（version）**：一次导入的剪辑 CSV，导入后固定，不可修改、不可删除。
  每行 4 列：`原录音号, 源起点ms, 源终点ms, 成片起点ms`
  （表头 `recording_id,src_start_ms,src_end_ms,timeline_start_ms` 可带可不带）。
  片段按原速使用（成片时长 = 源时长），在成片时间线上互不重叠，导入时校验。
- **按交接编号收表（handover）**：页面「交接收表」按交接编号收表，交接编号唯一。
  - **原录音剪辑表**直接取用原录音，沿用上面的原 CSV 格式，不填上游编号；
  - **二剪表**填一个直接上游的交接编号，每行 3 列
    `上游起点ms, 上游终点ms, 本版起点ms`（表头
    `upstream_start_ms,upstream_end_ms,timeline_start_ms` 可带可不带），
    表示上游成片时段 `[上游起点, 上游终点)` 放在本版 `[本版起点, …)`。
  - 二剪表可先暂存。补交或改交上游后自动逐级换算到原录音坐标，跨剪接点自动拆段。
  - 页面标出缺少的上游交接编号、以及未被上游完整覆盖的行（附未覆盖区间）；
    依赖该表的版本继续等待，其他能完整追溯的版本照常入库。
  - 只有所有片段都追溯成功、上游链全部入库后才**整版入库**，此后供禁播登记和
    遮盖查询使用，该编号不可再改交。尚未入库的表可改交；形成循环引用时拒收并保留原表。
  - 重启后仍可接着处理原来的待办（启动时自动放行已齐备的暂存表，也可点「处理待办」）。
  - 并发补表或重送时，交接编号唯一约束 + 单写事务保证同一编号至多入库一次；
    同编号同内容重送幂等返回已有表。
- **区间语义**：一律半开区间 `[起点, 终点)` —— 含起点、不含终点。
- **禁播标记（marker）**：登记在某一版的成片时间线上，可跨剪接点。登记时按该版
  剪辑表追溯到涉及的原录音时段并整笔存储（同一事务，要么全部写入要么不写）。
- **遮盖计算**：任一版中，标记剩余生效的原声时段经该版剪辑表映射回成片时间线，
  列出每次使用所需遮盖的时段；多个标记的遮盖重叠时，重叠段显示全部标记号。
- **整笔撤回**：撤回后该标记在所有版本立即不再产生遮盖（记录保留，可核对）。
- **局部解除（lift）**：在任一版按成片时段解除。解除时段换算到原声坐标、与该标记
  的原声时段求交后存储，因此作用于所有版本；其他标记照常生效。
  解除只追加新记录、不改写旧记录，且写事务串行化（`BEGIN IMMEDIATE`），
  两次并发解除不会互相覆盖。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/handovers` | 按交接编号收表 `{handover_no, name, kind: original\|recut, upstream_no, csv_text}`；可暂存，同编号同内容重送幂等 |
| GET | `/api/handovers` | 交接表列表（含状态、行数、缺上游编号、未完整覆盖行数） |
| GET | `/api/handovers/{no}` | 单据明细（每行追溯状态、未覆盖区间、换算片段、等待链） |
| PUT | `/api/handovers/{no}` | 改交尚未入库的表（循环引用拒收并保留原表） |
| POST | `/api/handovers/process` | 处理待办：放行已齐备的暂存表 |
| POST | `/api/versions` | 导入剪辑版 `{name, csv_text}` |
| GET | `/api/versions` / `/api/versions/{id}` | 版本列表 / 片段明细 |
| GET | `/api/versions/{id}/muted` | 该版当前需遮盖的成片时段（含标记号） |
| POST | `/api/markers` | 登记禁播标记 `{version_id, start_ms, end_ms, note}` |
| GET | `/api/markers` / `/api/markers/{id}` | 标记列表 / 详情（含剩余生效原声时段） |
| POST | `/api/markers/{id}/withdraw` | 整笔撤回 |
| POST | `/api/markers/{id}/lifts` | 局部解除 `{version_id, start_ms, end_ms}` |
| GET | `/api/markers/{id}/impact` | 该标记在每一版每次使用所需遮盖的时段 |

交互式文档见 `/docs`。示例 CSV 见 `examples/`
（`version_a.csv` / `version_b.csv` 为原录音表，`recut_*.csv` 为二剪表示例）。

## 目录结构

```
app/
  main.py       FastAPI 入口与路由
  handover.py   交接收表：暂存、逐级换算、缺上游/未覆盖诊断、整版入库
  logic.py      领域逻辑（导入校验、追溯、解除、遮盖计算）
  intervals.py  半开区间运算（并/减/扫描线合并）
  db.py         SQLite 连接与建表
  static/index.html  网页
tests/test_app.py       既有功能自测
tests/test_handover.py  交接收表自测
```
