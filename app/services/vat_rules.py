"""染缸状态业务规则。"""

from datetime import date
from decimal import Decimal
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import DipLot, ReductionCap, Vat


class VatRuleError(Exception):
    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


def assert_can_mark_ready(latest: Optional[DipLot]) -> None:
    """不能将染缸标为 ready，除非最新浸染批次 redoxMv 已填且 <= -500。"""
    if latest is None or latest.redoxMv is None or Decimal(latest.redoxMv) > Decimal("-500"):
        raise VatRuleError(
            "无法设为可染色：最新浸染批次的氧化还原电位为空或高于 -500 mV。"
        )


def reducing_occupancy(db: Session) -> dict[tuple[int, str], int]:
    """占用计数唯一来源：按 (工坊, 染种) 统计当前「还原中」缸数。

    只数 status = reducing 的行；可染色(ready)与闲置不计入。
    改状态校验、缸位 chip 占用摘要、上限卡专页占用列都从这里取数。
    """
    rows = (
        db.query(Vat.workshop_id, Vat.dyeType, func.count(Vat.id))
        .filter(Vat.status == Vat.STATUS_REDUCING)
        .group_by(Vat.workshop_id, Vat.dyeType)
        .all()
    )
    return {(ws_id, dye): n for ws_id, dye, n in rows}


def get_effective_cap(
    db: Session, workshop_id: int, dye_type: str, for_update: bool = False
) -> Optional[ReductionCap]:
    """同坊同染种当前生效的启用卡（启用且已到生效日）；至多一张。"""
    q = db.query(ReductionCap).filter(
        ReductionCap.workshop_id == workshop_id,
        ReductionCap.dyeType == dye_type,
        ReductionCap.enabled.is_(True),
        ReductionCap.effectiveFrom <= date.today(),
    )
    if for_update:
        # 并发互斥的关键：锁住上限卡行，让同坊同染种的「改还原中」串行判定
        q = q.with_for_update()
    return q.first()


def assert_can_start_reducing(db: Session, vat: Vat) -> None:
    """闲置改还原中的占用校验：无生效启用卡或占用已达上限则拒绝。

    须在改状态的同一事务内调用：先 SELECT ... FOR UPDATE 锁上限卡行，
    再用 reducing_occupancy 同源计数，保证两人同时改时至多一笔成功。
    """
    ws_name = vat.workshop.name if vat.workshop else f"工坊#{vat.workshop_id}"
    cap = get_effective_cap(db, vat.workshop_id, vat.dyeType, for_update=True)
    if cap is None:
        raise VatRuleError(
            f"无法设为还原中：{ws_name} · {vat.dyeType} 没有生效中的并发上限卡，"
            "请先在「并发上限」页新建并启用。"
        )
    used = reducing_occupancy(db).get((vat.workshop_id, vat.dyeType), 0)
    if used >= cap.maxReducing:
        raise VatRuleError(
            f"无法设为还原中：{ws_name} · {vat.dyeType} 当前已有 {used} 口还原中，"
            f"已达并发上限 {cap.maxReducing} 口。"
        )


def validate_vat_status_change(
    db: Session, vat: Vat, new_status: str, latest: Optional[DipLot]
) -> None:
    if new_status not in (Vat.STATUS_IDLE, Vat.STATUS_REDUCING, Vat.STATUS_READY):
        raise VatRuleError(f"未知目标状态：{new_status}")
    if new_status == Vat.STATUS_READY:
        assert_can_mark_ready(latest)
    if new_status == Vat.STATUS_REDUCING and vat.status != Vat.STATUS_REDUCING:
        # 只在「进入还原中」时占用名额；还原中改还原中不重复占位
        assert_can_start_reducing(db, vat)
