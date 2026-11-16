# 企业经营分析决策平台

面向企业高层管理的经营分析决策平台。覆盖经营指标看板、财务多维分析、销售漏斗洞察、客户画像构建、供应链效率监控、人力效能评估、经营预测模型、异常预警与决策建议推送的完整企业分析链路。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 经营指标看板 | `kpi` | 经营指标 | 指标编号、指标名称、计算公式 |
| 财务多维分析 | `finance` | 财务科目 | 科目编号、科目名称、科目类型 |
| 销售漏斗洞察 | `sales` | 漏斗阶段 | 阶段编号、阶段名称、客户数量 |
| 客户画像构建 | `customer` | 客户画像 | 画像编号、客户名称、行业分类 |
| 供应链效率监控 | `supply` | 效率指标 | 指标编号、环节名称、当前耗时 |
| 人力效能评估 | `hr` | 效能数据 | 数据编号、部门名称、人效指标 |
| 经营预测模型 | `forecast` | 预测任务 | 任务编号、预测对象、预测周期 |
| 异常预警中心 | `alert` | 预警记录 | 记录编号、预警类型、预警级别 |
| 决策建议推送 | `decision` | 决策建议 | 建议编号、分析主题、决策选项 |
| 多维下钻分析 | `drill` | 下钻路径 | 路径编号、起点指标、下钻维度 |
| 经营分析报告 | `report` | 分析报告 | 报告编号、报告主题、分析周期 |
| 同期对比分析 | `compare` | 对比方案 | 方案编号、对比指标、基准周期 |
| 分析数据接入 | `datasource` | 接入配置 | 配置编号、数据源名称、连接字符串 |
| 分析缓存管理 | `cache` | 缓存策略 | 策略编号、缓存对象、缓存时间 |
| 分析模板库 | `template` | 分析模板 | 模板编号、模板名称、适用场景 |
| 数据刷新调度 | `refresh` | 刷新任务 | 任务编号、数据对象、刷新类型 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 事件驱动漏斗分析（`app/funnel`）

销售漏斗洞察除了维护阶段客户数量，还提供从商机阶段事件重建的漏斗转化分析数据集，
位于 `backend/app/funnel/`（与按模块平铺的 router/service 并存的独立子域）：

- `domain.py`：纯函数领域口径。事件标准化、稳定排序（发生时间 + 事件编号，不依赖
  接入数组次序）、同刻业务去重、同刻矛盾整组排除、单商机多轮路径重建、队列分阶段统计。
- `repository.py`：事件库（事件编号去重）、按「入组周期 + 观测截止点」整体覆盖的快照、
  事件水位驱动的迟到待重建标记、预测消费留痕。
- `service.py`：多选周期批量重建（每条队列独立固定截止点、单条失败隔离、可重试）、
  数据集读取、预测模型跨模块消费。
- `seed_events.py`：覆盖流失重开、逆序接收、重复成交、同刻矛盾、孤儿事件、右删失等
  全部边界的示例事件。

关键口径：

- 队列成员资格 = 首条有效「进入」事件所在自然月；重开不产生新客户，上一轮流失经历保留。
- 同一商机多次进入同一阶段按多个「阶段周期」分别计入停留与转化，去重商机数仍为 1。
- 未结束阶段为右删失，停留时长输出 `null` 绝不补零，也不计入最终流失。
- 同刻互斥事件（成交/流失、终止/重开等）整组排除并在「被排除事件」逐条说明原因。
- 晚于观测截止点的事件不计入本期但保留在事件库；迟到事件按事件水位标记快照待重建。
- 预测模型消费时同时固化入组周期、观测截止时间、有效样本量与右删失样本量。

主要接口（前缀 `/api/funnel`）：事件接入/查询 `events`、批量重建 `cohorts/rebuild`、
数据集与路径 `cohorts/{period}/datasets/{cutoff}`、预测消费 `forecast/consume`；
预测模块侧另有 `POST /api/forecast/funnel-dataset/consume` 留痕。
后端测试：`cd backend && .venv/bin/python -m unittest discover -s tests`。

领域：数据工程
子方向：数据分析与数据应用
