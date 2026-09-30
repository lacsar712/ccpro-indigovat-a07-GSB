from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Optional
import json

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from jinja2.utils import markupsafe
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.auth import get_current_user
from app.db import get_db
from app.models import DipLot, ReducingLimitCard, Vat, Workshop
from app.services.vat_rules import (
    VatRuleError,
    count_reducing,
    validate_vat_status_change,
)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _tojson(value):
    return markupsafe.Markup(json.dumps(value, ensure_ascii=False))


templates.env.filters["tojson"] = _tojson

STATUS_LABELS = {
    Vat.STATUS_IDLE: "闲置",
    Vat.STATUS_REDUCING: "还原中",
    Vat.STATUS_READY: "可染色",
}


def render(request: Request, name: str, context: dict, status_code: int = 200):
    ctx = {k: v for k, v in context.items() if k != "request"}
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def _need_login(request: Request, db: Session):
    return get_current_user(request, db)


def _spark_points(lots: list[DipLot], width: int = 72, height: int = 28) -> list[dict]:
    """把 redox 序列压成 sparkline 坐标（无有效读数则空）。"""
    vals = [float(l.redoxMv) for l in lots if l.redoxMv is not None]
    if not vals:
        return []
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    n = len(vals)
    pts = []
    for i, v in enumerate(vals):
        x = 0 if n == 1 else round(i * (width - 1) / (n - 1), 2)
        y = round(height - 1 - ((v - lo) / span) * (height - 1), 2)
        pts.append({"x": x, "y": y})
    return pts


def _vat_payload(vat: Vat) -> dict:
    lots = sorted(vat.lots, key=lambda x: (x.dippedAt, x.id))
    chronological = lots
    latest = lots[-1] if lots else None
    recent = list(reversed(lots[-8:]))  # 展开区展示近几笔
    return {
        "id": vat.id,
        "code": vat.code,
        "dyeType": vat.dyeType,
        "volumeL": float(vat.volumeL),
        "status": vat.status,
        "statusLabel": STATUS_LABELS.get(vat.status, vat.status),
        "workshopId": vat.workshop_id,
        "workshopName": vat.workshop.name if vat.workshop else "",
        "lastRedox": float(latest.redoxMv) if latest and latest.redoxMv is not None else None,
        "lastMeters": float(latest.clothMeters) if latest else None,
        "lastDippedAt": latest.dippedAt.strftime("%Y-%m-%d %H:%M") if latest else None,
        "spark": _spark_points(chronological),
        "recentLots": [
            {
                "id": l.id,
                "dippedAt": l.dippedAt.strftime("%Y-%m-%d %H:%M"),
                "clothMeters": float(l.clothMeters),
                "redoxMv": float(l.redoxMv) if l.redoxMv is not None else None,
            }
            for l in recent
        ],
    }


def _limit_summaries(db: Session) -> list[dict]:
    """生效中启用卡的占用摘要，与改状态判定同一计数来源（count_reducing）。"""
    cards = (
        db.query(ReducingLimitCard)
        .options(joinedload(ReducingLimitCard.workshop))
        .filter(
            ReducingLimitCard.enabled.is_(True),
            ReducingLimitCard.effectiveFrom <= date.today(),
        )
        .order_by(ReducingLimitCard.workshop_id, ReducingLimitCard.dyeType)
        .all()
    )
    return [
        {
            "workshopName": c.workshop.name if c.workshop else "",
            "dyeType": c.dyeType,
            "used": count_reducing(db, c.workshop_id, c.dyeType),
            "max": c.maxReducing,
        }
        for c in cards
    ]


def _bay_context(
    request: Request,
    db: Session,
    user,
    workshop_id: Optional[int] = None,
    selected_vat: Optional[int] = None,
    error: Optional[str] = None,
):
    # 始终下发全部缸位；工坊仅作前端 chip 筛选，避免切回「全部」时缺数据
    workshops = db.query(Workshop).order_by(Workshop.name).all()
    vats = (
        db.query(Vat)
        .options(joinedload(Vat.workshop), joinedload(Vat.lots))
        .order_by(Vat.code)
        .all()
    )
    return {
        "request": request,
        "user": user,
        "workshops": [{"id": w.id, "name": w.name, "region": w.region} for w in workshops],
        "vats": [_vat_payload(v) for v in vats],
        "limit_summaries": _limit_summaries(db),
        "filter_workshop": workshop_id,
        "selected_vat": selected_vat,
        "error": error,
        "status_labels": STATUS_LABELS,
        "active": "bay",
    }


@router.get("/", response_class=HTMLResponse)
async def bay(
    request: Request,
    workshop: Optional[int] = None,
    vat: Optional[int] = None,
    db: Session = Depends(get_db),
):
    user = _need_login(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    return render(request, "bay.html", _bay_context(request, db, user, workshop, vat))


@router.post("/bay/vats/{pk}/status", response_class=HTMLResponse)
async def bay_vat_status(
    pk: int,
    request: Request,
    status: str = Form(...),
    workshop: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need_login(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    item = (
        db.query(Vat)
        .options(joinedload(Vat.workshop), joinedload(Vat.lots))
        .filter(Vat.id == pk)
        .first()
    )
    ws = int(workshop) if workshop.strip() else None
    if not item:
        return RedirectResponse("/", status_code=303)
    error = None
    try:
        latest = item.latest_lot()
        validate_vat_status_change(db, item, status, latest)
        item.status = status
        db.commit()
        return RedirectResponse(f"/?vat={pk}" + (f"&workshop={ws}" if ws else ""), status_code=303)
    except VatRuleError as exc:
        error = exc.message
        db.rollback()
    return render(
        request,
        "bay.html",
        _bay_context(request, db, user, ws, pk, error),
        status_code=400,
    )


@router.post("/bay/vats/{pk}/lots", response_class=HTMLResponse)
async def bay_log_lot(
    pk: int,
    request: Request,
    dippedAt: str = Form(...),
    clothMeters: str = Form(...),
    redoxMv: str = Form(""),
    workshop: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need_login(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    item = db.get(Vat, pk)
    ws = int(workshop) if workshop.strip() else None
    if not item:
        return RedirectResponse("/", status_code=303)
    error = None
    try:
        lot = DipLot(
            vat_id=pk,
            dippedAt=datetime.fromisoformat(dippedAt),
            clothMeters=Decimal(clothMeters),
            redoxMv=Decimal(redoxMv) if redoxMv.strip() else None,
        )
        db.add(lot)
        db.commit()
        return RedirectResponse(f"/?vat={pk}" + (f"&workshop={ws}" if ws else ""), status_code=303)
    except (ValueError, InvalidOperation) as exc:
        error = f"浸染记录无效：{exc}"
        db.rollback()
    return render(
        request,
        "bay.html",
        _bay_context(request, db, user, ws, pk, error),
        status_code=400,
    )


def _limits_context(
    request: Request,
    db: Session,
    user,
    error: Optional[str] = None,
):
    cards = (
        db.query(ReducingLimitCard)
        .options(joinedload(ReducingLimitCard.workshop))
        .order_by(ReducingLimitCard.workshop_id, ReducingLimitCard.dyeType, ReducingLimitCard.id)
        .all()
    )
    workshops = db.query(Workshop).order_by(Workshop.name).all()
    return {
        "request": request,
        "user": user,
        "cards": [
            {
                "id": c.id,
                "workshopId": c.workshop_id,
                "workshopName": c.workshop.name if c.workshop else "",
                "dyeType": c.dyeType,
                "maxReducing": c.maxReducing,
                "effectiveFrom": c.effectiveFrom.isoformat(),
                "enabled": c.enabled,
                # 与改状态判定同一计数来源
                "used": count_reducing(db, c.workshop_id, c.dyeType),
            }
            for c in cards
        ],
        "workshops": [{"id": w.id, "name": w.name} for w in workshops],
        "today": date.today().isoformat(),
        "error": error,
        "active": "limits",
    }


def _enabled_card_clash(db: Session, workshop_id: int, dye_type: str, exclude_id: Optional[int] = None):
    """同坊同染种是否已有（另一张）启用卡。"""
    q = db.query(ReducingLimitCard).filter(
        ReducingLimitCard.workshop_id == workshop_id,
        ReducingLimitCard.dyeType == dye_type,
        ReducingLimitCard.enabled.is_(True),
    )
    if exclude_id is not None:
        q = q.filter(ReducingLimitCard.id != exclude_id)
    return q.first()


def _parse_card_fields(dye_type: str, max_reducing: str, effective_from: str):
    dye = dye_type.strip()
    if not dye:
        raise VatRuleError("染种名不能为空。")
    try:
        max_n = int(max_reducing)
    except ValueError:
        raise VatRuleError("最大还原中缸数须为整数。")
    if max_n < 0:
        raise VatRuleError("最大还原中缸数不能为负数。")
    try:
        eff = date.fromisoformat(effective_from)
    except ValueError:
        raise VatRuleError("生效日起格式应为 YYYY-MM-DD。")
    return dye, max_n, eff


@router.get("/limits", response_class=HTMLResponse)
async def limits_page(request: Request, db: Session = Depends(get_db)):
    user = _need_login(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    return render(request, "limits.html", _limits_context(request, db, user))


@router.post("/limits", response_class=HTMLResponse)
async def limits_create(
    request: Request,
    workshop_id: int = Form(...),
    dyeType: str = Form(...),
    maxReducing: str = Form(...),
    effectiveFrom: str = Form(...),
    enabled: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need_login(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    enabled_flag = enabled.lower() in ("on", "1", "true", "yes")
    error = None
    dye = dyeType.strip()
    try:
        if not db.get(Workshop, workshop_id):
            raise VatRuleError("所选工坊不存在。")
        dye, max_n, eff = _parse_card_fields(dyeType, maxReducing, effectiveFrom)
        if enabled_flag and _enabled_card_clash(db, workshop_id, dye):
            raise VatRuleError(
                f"该工坊「{dye}」已存在启用中的上限卡，同坊同染种同时只许一张启用卡。"
            )
        db.add(
            ReducingLimitCard(
                workshop_id=workshop_id,
                dyeType=dye,
                maxReducing=max_n,
                effectiveFrom=eff,
                enabled=enabled_flag,
            )
        )
        db.commit()
        return RedirectResponse("/limits", status_code=303)
    except VatRuleError as exc:
        error = exc.message
        db.rollback()
    except IntegrityError:
        db.rollback()
        error = f"该工坊「{dye}」已存在启用中的上限卡，同坊同染种同时只许一张启用卡。"
    return render(
        request,
        "limits.html",
        _limits_context(request, db, user, error=error),
        status_code=400,
    )


@router.post("/limits/{card_id}/toggle", response_class=HTMLResponse)
async def limits_toggle(card_id: int, request: Request, db: Session = Depends(get_db)):
    user = _need_login(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    card = db.get(ReducingLimitCard, card_id)
    if not card:
        return RedirectResponse("/limits", status_code=303)
    error = None
    try:
        if card.enabled:
            card.enabled = False
        else:
            if _enabled_card_clash(db, card.workshop_id, card.dyeType, exclude_id=card.id):
                raise VatRuleError(
                    f"该工坊「{card.dyeType}」已存在启用中的上限卡，同坊同染种同时只许一张启用卡。"
                )
            card.enabled = True
        db.commit()
        return RedirectResponse("/limits", status_code=303)
    except VatRuleError as exc:
        error = exc.message
        db.rollback()
    except IntegrityError:
        db.rollback()
        error = f"该工坊「{card.dyeType}」已存在启用中的上限卡，同坊同染种同时只许一张启用卡。"
    return render(
        request,
        "limits.html",
        _limits_context(request, db, user, error=error),
        status_code=400,
    )


@router.post("/limits/{card_id}", response_class=HTMLResponse)
async def limits_update(
    card_id: int,
    request: Request,
    maxReducing: str = Form(...),
    effectiveFrom: str = Form(...),
    db: Session = Depends(get_db),
):
    user = _need_login(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    card = db.get(ReducingLimitCard, card_id)
    if not card:
        return RedirectResponse("/limits", status_code=303)
    error = None
    try:
        _, max_n, eff = _parse_card_fields(card.dyeType, maxReducing, effectiveFrom)
        card.maxReducing = max_n
        card.effectiveFrom = eff
        db.commit()
        return RedirectResponse("/limits", status_code=303)
    except VatRuleError as exc:
        error = exc.message
        db.rollback()
    return render(
        request,
        "limits.html",
        _limits_context(request, db, user, error=error),
        status_code=400,
    )


# 旧顶栏 CRUD 路径一律回到还原台，避免「换皮表页」残留入口
@router.get("/workshops")
@router.get("/vats")
@router.get("/lots")
@router.get("/home")
async def legacy_redirect():
    return RedirectResponse("/", status_code=303)
