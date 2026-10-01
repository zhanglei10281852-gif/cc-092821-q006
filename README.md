# 种质资源入库与活力复检服务

本项目是面向种质资源库的 Python 后端服务，用于登记采集或引进材料、建立种子批次、管理低温库位和容器移动、执行发芽活力检测、生成复检日程并处理环境与质量告警。档案、库存、检测和发放审批都保存在本地 SQLite 中，关键写入带版本或幂等键，适合在单个 Linux 应用容器内运行。

## 运行环境

- Python 3.11
- FastAPI 与 Uvicorn
- SQLite 3，由 Python 标准库提供

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

默认数据库位于 `data/germplasm.db`，也可以通过 `GERMPLASM_DATABASE_PATH` 指向其他 `.db`、`.sqlite` 或 `.sqlite3` 文件。

## 初始化与启动

```bash
python -m app.cli init-db
python -m app.cli check-db
uvicorn app.main:app --host 0.0.0.0 --port 8432
```

健康检查为 `GET /api/system/health`。首次使用可调用 `POST /api/auth/bootstrap` 创建管理员，再通过 `POST /api/auth/login` 取得 Bearer 会话令牌。种质业务接口统一位于 `/api/germplasm`。

## 测试与构建检查

```bash
python -m pytest
python -m compileall -q app tests
```

下面两条命令分别检查 HTTP 入口和完整的入库演示链路：

```bash
python -m app.cli smoke
python -m app.cli demo
```

## 业务边界

- `app/germplasm/accessions.py` 管理来源、资源档案、护照信息与接收状态。
- `app/germplasm/inventory.py` 管理批次、库位容量、容器摆放、移动、领用和冻结。
- `app/germplasm/viability.py` 管理检测规程、取样、重复计数、活力结果与复检日程。
- `app/germplasm/results.py` 定义活力结果的合并计算规则（按总粒数加权，带规则版本）。
- `app/germplasm/quality.py` 管理温湿度读数、偏离告警和种质发放审批。
- `app/api`、`app/services` 和 `app/repositories` 提供身份、权限、审计、后台作业及维护能力。

## 一致性约定

SQLite 连接启用外键、WAL、忙等待和即时写事务。资源档案、库位、容器摆放、检测任务和发放申请采用版本号防止旧请求覆盖新状态；入库、移库、取样和传感读数使用业务键去重。活力检测保留采用的规程版本和每个重复的观察计数，完成后可依据作物及风险策略生成下一次复检日期。会话令牌只保存摘要，审计记录不保存明文密码或令牌。

### 活力结果计算（pooled-weighted-v1）

- 发芽率 = 各有效重复最终观察日正常苗总数 ÷ 各有效重复检测总粒数 ×100；活力指数分母相同，分子为正常苗总数加半数新鲜未发芽种子。重复样本量不同时按粒数自然加权，不再对各重复百分比做算术平均（例如 20 粒中 10 粒、100 粒中 90 粒，结果为 100/120=83.33% 而非 70.00%）。
- 每个重复只采用最大观察日的有效计数；作废计数（`status='voided'`，记录作废人、时间、原因）不参与分子分母，作废后可重新录入。全部计数作废或有效总粒数为零时拒绝完成检测。
- 完成时向不可变的 `viability_results` 写入一份结果：分子、分母、活力分子、采用/排除的计数 ID、分类合计、规则版本 `pooled-weighted-v1` 与计算人/时间；`test_id` 唯一约束配合检测版本号，保证并发完成只有一份结果生效。
- 风险等级（<70 high、<85 medium、其余 low）、低活力告警（<50）和复检日程均读取同一份已落库结果；日程保存 `risk_level` 与 `source_result_id`。检测完成后计数与结果不可修改；检测作废只会置失效状态并作废旧日程，历史结果行原样保留，纠错须新建检测。
