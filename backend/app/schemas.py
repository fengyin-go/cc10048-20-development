"""通用接口契约；模块字段由领域服务持有，不再为每个模块复制一份模型类。"""
from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class PageResult(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int = 1
    size: int = 20


class ActionResult(BaseModel):
    ok: bool
    message: str
    entry: dict[str, Any] | None = None


class EntryPayload(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)
    remark: str | None = None


class ActionPayload(BaseModel):
    action: str
    remark: str | None = None


class SnapshotCreatePayload(BaseModel):
    """创建下钻数据快照：可显式登记父子组事件明细，缺省则执行下钻生成。"""
    groups: list[dict[str, Any]] | None = None


class SupplementPayload(BaseModel):
    """补录子项记录：无来源凭据只能记为待证记录。"""
    parent: str
    child: str
    value: float
    source: str | None = None
    batch: str | None = None
    dimension: str = ""


class AttachSourcePayload(BaseModel):
    """给待证记录补齐来源凭据。"""
    record_id: str
    source: str


class RenderSnapshotPayload(BaseModel):
    """指定图表生成所依据的已确认下钻数据快照。"""
    snapshot_id: int
