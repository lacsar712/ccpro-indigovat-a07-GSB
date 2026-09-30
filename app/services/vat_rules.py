"""染缸状态业务规则。"""

from datetime import date
from decimal import Decimal
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import DipLot, ReducingLimitCard, Vat


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


def count_reducing(db: Session, workshop_id: int, dye_type: str) -> int:
    """同坊同染种当前处于还原中的缸数。

    唯一计数来源：闲置改还原中的判定与还原台 chip 占用摘要都走这里；
    只数 status=reducing，可染色(ready)不计入。
    """
    return (
        db.query(func.count(Vat.id))
        .filter(
            Vat.workshop_id == workshop_id,
            Vat.dyeType == dye_type,
            Vat.status == Vat.STATUS_REDUCING,
        )
        .scalar()
        or 0
    )


def lock_active_limit_card(
    db: Session, workshop_id: int, dye_type: str, today: Optional[date] = None
) -> Optional[ReducingLimitCard]:
    """取出并锁定生效中的启用上限卡（无启用卡则 None）。

    SELECT ... FOR UPDATE 锁住卡行：两人同时闲置改还原中会在同一张卡上排队，
    后到者在前者提交后重新计数，名额不足即拒；卡的启停/改上限同样写卡行，
    与改状态互斥，下一笔立即按新值判定。
    """
    day = today or date.today()
    return (
        db.query(ReducingLimitCard)
        .filter(
            ReducingLimitCard.workshop_id == workshop_id,
            ReducingLimitCard.dyeType == dye_type,
            ReducingLimitCard.enabled.is_(True),
            ReducingLimitCard.effectiveFrom <= day,
        )
        .with_for_update()
        .first()
    )


def assert_within_reducing_limit(db: Session, vat: Vat) -> None:
    """闲置改还原中：须存在生效中的启用上限卡，且同坊同染种还原中缸数未达上限。"""
    card = lock_active_limit_card(db, vat.workshop_id, vat.dyeType)
    workshop_name = vat.workshop.name if vat.workshop else str(vat.workshop_id)
    if card is None:
        raise VatRuleError(
            f"未找到生效中的还原并发上限卡（{workshop_name} · {vat.dyeType}），不能改为还原中。"
        )
    used = count_reducing(db, vat.workshop_id, vat.dyeType)
    if used >= card.maxReducing:
        raise VatRuleError(
            f"{workshop_name}「{vat.dyeType}」还原中缸数已达上限（{used}/{card.maxReducing}），不能改为还原中。"
        )


def validate_vat_status_change(
    db: Session, vat: Vat, new_status: str, latest: Optional[DipLot]
) -> None:
    """改状态同一判定：可染色电位门槛 + 闲置改还原中的并发上限占用计数。"""
    if new_status == Vat.STATUS_READY:
        assert_can_mark_ready(latest)
    if vat.status == Vat.STATUS_IDLE and new_status == Vat.STATUS_REDUCING:
        assert_within_reducing_limit(db, vat)
