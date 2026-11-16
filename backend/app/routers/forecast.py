"""经营预测模型接口：维护预测任务，覆盖生成预测、验证结果、归档任务等动作。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.forecast import ForecastService

router = APIRouter(prefix="/api/forecast", tags=["经营预测模型"])

service = ForecastService()

LIST_FIELDS = ["任务编号", "预测对象", "预测周期", "预测算法", "预测结果", "置信区间", "实际对比", "任务状态"]
STATUSES = ["待预测", "预测中", "已生成", "已验证"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按任务编号检索"),
    status: str | None = Query(default=None, description="待预测、预测中、已生成、已验证"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按任务编号与状态过滤经营预测模型列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条预测任务明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"预测任务 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条预测任务，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="预测任务已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条预测任务执行生成预测、验证结果、归档任务；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出经营预测模型清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "forecast", "total": total, "items": items}


@router.post("/funnel-dataset/consume", response_model=ActionResult)
def consume_funnel_dataset(payload: EntryPayload) -> ActionResult:
    """跨模块消费漏斗转化分析数据集。

    同时保存入组周期、观测截止时间与有效样本量；观察期不足（右删失）的商机
    不会被当成最终流失。
    """
    record, error = service.consume_funnel_dataset(payload.values)
    if record is None:
        return ActionResult(ok=False, message=error)
    return ActionResult(ok=True, message="漏斗转化分析数据集已接入预测任务", entry=record)
