"""经营预测模型业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

from typing import Any

from app.store import store

MODULE = "forecast"
REQUIRED_FIELDS = ["任务编号", "预测对象", "预测周期"]
STATUS_ORDER = ["待预测", "预测中", "已生成", "已验证"]
ACTION_RULES = {"生成预测": "待预测", "验证结果": "已生成", "归档任务": "已验证"}
NEGATIVE_ACTIONS = []


class ForecastService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("任务编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"预测任务 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于经营预测模型可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"预测任务已{action}"

    def consume_funnel_dataset(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
        """跨模块读取漏斗转化分析数据集，并在预测模块侧留存消费痕迹。

        漏斗侧负责口径与样本量计算；这里把入组周期、观测截止时间、有效样本量
        与右删失样本量固化到预测任务记录，避免观察期不足的商机被当成最终流失。
        """
        # 延迟导入，避免两个业务模块在包加载阶段产生循环依赖。
        from app.funnel.service import funnel_service

        record, error = funnel_service.consume_for_forecast(values)
        if record is None:
            return None, error
        rows = store.rows(MODULE)
        trace = {
            "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
            "status": "数据已接入",
            "pending": False,
            "abnormal": record["数据集待重建"],
            "任务编号": record["任务编号"] or record["记录编号"],
            "预测对象": "漏斗队列转化",
            "预测周期": record["入组周期"],
            "观测截止时间": record["观测截止时间"],
            "有效样本量": record["有效样本量"],
            "右删失样本量": record["右删失样本量"],
            "来源": "销售漏斗洞察-转化分析数据集",
        }
        rows.append(trace)
        return record, None
