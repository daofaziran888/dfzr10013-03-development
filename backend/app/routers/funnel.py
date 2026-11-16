"""事件驱动漏斗分析接口。

路由层只做参数校验与 HTTP 适配，所有业务口径都在 app.funnel 内实现。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.funnel.domain import FUNNEL_STAGES
from app.funnel.service import funnel_service

router = APIRouter(prefix="/api/funnel", tags=["事件驱动漏斗分析"])


class IngestPayload(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)
    remark: str | None = None


class CohortSpec(BaseModel):
    入组周期: str
    观测截止时间: str


class RebuildPayload(BaseModel):
    cohorts: list[CohortSpec]


class ConsumePayload(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)


@router.get("/stages")
def list_stages() -> dict[str, Any]:
    """漏斗阶段目录与事件类型口径，供前端渲染与提交校验。"""
    return {
        "漏斗阶段": FUNNEL_STAGES,
        "事件类型": ["进入", "退出", "成交", "流失", "重新开启"],
    }


@router.get("/events")
def list_events(
    opportunity_id: str | None = Query(default=None, description="按商机编号过滤"),
    event_type: str | None = Query(default=None, description="进入、退出、成交、流失、重新开启"),
) -> dict[str, Any]:
    """事件库查询：按发生时间 + 事件编号稳定排序，重复编号在接入环节已被拦截。"""
    items = funnel_service.list_events(opportunity_id=opportunity_id, event_type=event_type)
    return {"items": items, "total": len(items)}


@router.post("/events")
def ingest_event(payload: IngestPayload) -> dict[str, Any]:
    """接入单条阶段事件；重复事件只计一次，字段缺失会明确说明。"""
    result, missing = funnel_service.ingest_event(payload.values)
    if missing:
        return {"ok": False, "message": f"缺少必填字段：{'、'.join(missing)}"}
    assert result is not None
    result["message"] = "事件已接入" if result["ok"] else f"事件未接入：{result['原因']}"
    return result


@router.get("/events/ingest-log")
def ingest_log() -> dict[str, Any]:
    """接入审计：接入、重复、拒绝逐条留痕，便于核对去重口径。"""
    items = funnel_service.ingest_log()
    return {"items": items, "total": len(items)}


@router.post("/cohorts/rebuild")
def rebuild_cohorts(payload: RebuildPayload) -> dict[str, Any]:
    """多选入组周期批量重建；每条队列独立固定观测截止点，逐条返回成员与排除事件。"""
    cohorts = [item.model_dump() for item in payload.cohorts]
    return funnel_service.rebuild(cohorts)


@router.get("/cohorts/datasets")
def list_datasets() -> dict[str, Any]:
    """已生成的漏斗转化分析数据集清单（含版本与迟到待重建标记）。"""
    items = funnel_service.list_datasets()
    return {"items": items, "total": len(items)}


@router.get("/cohorts/{period}/datasets/{cutoff}")
def get_dataset(period: str, cutoff: str) -> dict[str, Any]:
    """读取单个队列的完整转化分析数据集；不存在时提示先重建，不返回别的周期数据。"""
    snapshot = funnel_service.get_dataset(period, cutoff)
    if snapshot is None:
        raise HTTPException(
            status_code=404,
            detail=f"入组周期 {period}、截止点 {cutoff} 的漏斗数据集不存在，请先执行队列重建",
        )
    return snapshot


@router.get("/cohorts/{period}/datasets/{cutoff}/paths/{opportunity_id}")
def get_path(period: str, cutoff: str, opportunity_id: str) -> dict[str, Any]:
    """单商机路径明细：与队列总人数、分阶段指标相互核验。"""
    path = funnel_service.get_path(period, cutoff, opportunity_id)
    if path is None:
        raise HTTPException(
            status_code=404,
            detail=f"商机 {opportunity_id} 不在周期 {period} 的有效成员中，或数据集尚未重建",
        )
    return path


@router.post("/forecast/consume")
def consume_for_forecast(payload: ConsumePayload) -> dict[str, Any]:
    """经营预测模型跨模块消费漏斗数据集，固化周期、截止点与有效样本量。"""
    record, error = funnel_service.consume_for_forecast(payload.values)
    if record is None:
        raise HTTPException(status_code=400, detail=error)
    return {"ok": True, "message": "漏斗转化分析数据集已被预测模型消费并留痕", "record": record}


@router.get("/forecast/consumptions")
def list_consumptions() -> dict[str, Any]:
    """预测模型消费记录：核对每次消费使用的周期、截止点与样本量。"""
    items = funnel_service.list_consumptions()
    return {"items": items, "total": len(items)}
