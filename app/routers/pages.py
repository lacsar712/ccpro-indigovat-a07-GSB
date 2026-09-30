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
from app.models import DipLot, ReductionCap, Vat, Workshop
from app.services.vat_rules import (
    VatRuleError,
    reducing_occupancy,
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


def _vat_payload(vat: Vat, occupancy: dict, cap_map: dict) -> dict:
    lots = sorted(vat.lots, key=lambda x: (x.dippedAt, x.id))
    chronological = lots
    latest = lots[-1] if lots else None
    recent = list(reversed(lots[-8:]))  # 展开区展示近几笔
    key = (vat.workshop_id, vat.dyeType)
    return {
        "id": vat.id,
        "code": vat.code,
        "dyeType": vat.dyeType,
        "volumeL": float(vat.volumeL),
        "status": vat.status,
        "statusLabel": STATUS_LABELS.get(vat.status, vat.status),
        "workshopId": vat.workshop_id,
        "workshopName": vat.workshop.name if vat.workshop else "",
        "capUsed": occupancy.get(key, 0),
        "capMax": cap_map.get(key),
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


def _effective_cap_map(db: Session) -> dict:
    """(工坊, 染种) -> 生效中启用卡的最大还原中缸数。"""
    caps = (
        db.query(ReductionCap)
        .filter(
            ReductionCap.enabled.is_(True),
            ReductionCap.effectiveFrom <= date.today(),
        )
        .all()
    )
    return {(c.workshop_id, c.dyeType): c.maxReducing for c in caps}


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
    # 占用计数与改状态校验同源：同一条 reducing_occupancy 统计
    occupancy = reducing_occupancy(db)
    cap_map = _effective_cap_map(db)
    ws_reducing = {}
    for (ws_id, _dye), n in occupancy.items():
        ws_reducing[ws_id] = ws_reducing.get(ws_id, 0) + n
    return {
        "request": request,
        "user": user,
        "workshops": [
            {
                "id": w.id,
                "name": w.name,
                "region": w.region,
                "reducing": ws_reducing.get(w.id, 0),
            }
            for w in workshops
        ],
        "vats": [_vat_payload(v, occupancy, cap_map) for v in vats],
        "reducing_total": sum(occupancy.values()),
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
    # 锁目标缸行，与上限卡行锁配合，保证并发改态在同一事务内串行判定
    item = (
        db.query(Vat)
        .options(joinedload(Vat.workshop), joinedload(Vat.lots))
        .filter(Vat.id == pk)
        .with_for_update(of=Vat)
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


def _caps_context(
    request: Request,
    db: Session,
    user,
    error: Optional[str] = None,
):
    caps = (
        db.query(ReductionCap)
        .options(joinedload(ReductionCap.workshop))
        .order_by(ReductionCap.workshop_id, ReductionCap.dyeType, ReductionCap.id)
        .all()
    )
    # 占用列与改状态校验同源
    occupancy = reducing_occupancy(db)
    workshops = db.query(Workshop).order_by(Workshop.name).all()
    dye_types = [d for (d,) in db.query(Vat.dyeType).distinct().order_by(Vat.dyeType).all()]
    today = date.today()
    return {
        "request": request,
        "user": user,
        "caps": [
            {
                "id": c.id,
                "workshop_id": c.workshop_id,
                "workshop_name": c.workshop.name if c.workshop else "",
                "dyeType": c.dyeType,
                "maxReducing": c.maxReducing,
                "effectiveFrom": c.effectiveFrom.isoformat(),
                "enabled": c.enabled,
                "effective": c.enabled and c.effectiveFrom <= today,
                "used": occupancy.get((c.workshop_id, c.dyeType), 0),
            }
            for c in caps
        ],
        "workshops": [{"id": w.id, "name": w.name} for w in workshops],
        "dye_types": dye_types,
        "today": today.isoformat(),
        "error": error,
        "active": "caps",
    }


def _caps_error(request: Request, db: Session, user, message: str):
    return render(request, "caps.html", _caps_context(request, db, user, message), status_code=400)


def _check_single_enabled(db: Session, workshop_id: int, dye_type: str, exclude_id: Optional[int] = None) -> None:
    """同坊同染种只许一张启用卡（应用层校验，部分唯一索引兜底）。"""
    q = db.query(ReductionCap).filter(
        ReductionCap.workshop_id == workshop_id,
        ReductionCap.dyeType == dye_type,
        ReductionCap.enabled.is_(True),
    )
    if exclude_id is not None:
        q = q.filter(ReductionCap.id != exclude_id)
    if q.first() is not None:
        raise VatRuleError("该工坊此染种已存在启用中的上限卡，请先停用旧卡。")


@router.get("/caps", response_class=HTMLResponse)
async def caps_page(request: Request, db: Session = Depends(get_db)):
    user = _need_login(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    return render(request, "caps.html", _caps_context(request, db, user))


@router.post("/caps", response_class=HTMLResponse)
async def caps_create(
    request: Request,
    workshop_id: int = Form(...),
    dyeType: str = Form(...),
    maxReducing: int = Form(...),
    effectiveFrom: str = Form(...),
    enabled: bool = Form(False),
    db: Session = Depends(get_db),
):
    user = _need_login(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    dye = dyeType.strip()
    try:
        if not db.get(Workshop, workshop_id):
            raise VatRuleError("工坊不存在。")
        if not dye:
            raise VatRuleError("染种名不能为空。")
        if maxReducing < 1:
            raise VatRuleError("最大还原中缸数至少为 1。")
        try:
            eff = date.fromisoformat(effectiveFrom)
        except ValueError:
            raise VatRuleError("生效日起格式无效，应为 YYYY-MM-DD。")
        if enabled:
            _check_single_enabled(db, workshop_id, dye)
        db.add(
            ReductionCap(
                workshop_id=workshop_id,
                dyeType=dye,
                maxReducing=maxReducing,
                effectiveFrom=eff,
                enabled=enabled,
            )
        )
        db.commit()
        return RedirectResponse("/caps", status_code=303)
    except VatRuleError as exc:
        db.rollback()
        return _caps_error(request, db, user, exc.message)
    except IntegrityError:
        db.rollback()
        return _caps_error(request, db, user, "该工坊此染种已存在启用中的上限卡，请先停用旧卡。")


@router.post("/caps/{pk}/toggle", response_class=HTMLResponse)
async def caps_toggle(pk: int, request: Request, db: Session = Depends(get_db)):
    user = _need_login(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    cap = db.query(ReductionCap).filter(ReductionCap.id == pk).with_for_update().first()
    if not cap:
        return RedirectResponse("/caps", status_code=303)
    try:
        if not cap.enabled:
            _check_single_enabled(db, cap.workshop_id, cap.dyeType, exclude_id=cap.id)
        cap.enabled = not cap.enabled
        db.commit()
        return RedirectResponse("/caps", status_code=303)
    except VatRuleError as exc:
        db.rollback()
        return _caps_error(request, db, user, exc.message)
    except IntegrityError:
        db.rollback()
        return _caps_error(request, db, user, "该工坊此染种已存在启用中的上限卡，请先停用旧卡。")


@router.post("/caps/{pk}/update", response_class=HTMLResponse)
async def caps_update(
    pk: int,
    request: Request,
    maxReducing: int = Form(...),
    effectiveFrom: str = Form(...),
    db: Session = Depends(get_db),
):
    user = _need_login(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    cap = db.query(ReductionCap).filter(ReductionCap.id == pk).with_for_update().first()
    if not cap:
        return RedirectResponse("/caps", status_code=303)
    try:
        if maxReducing < 1:
            raise VatRuleError("最大还原中缸数至少为 1。")
        try:
            eff = date.fromisoformat(effectiveFrom)
        except ValueError:
            raise VatRuleError("生效日起格式无效，应为 YYYY-MM-DD。")
        cap.maxReducing = maxReducing
        cap.effectiveFrom = eff
        db.commit()
        return RedirectResponse("/caps", status_code=303)
    except VatRuleError as exc:
        db.rollback()
        return _caps_error(request, db, user, exc.message)


# 旧顶栏 CRUD 路径一律回到还原台，避免「换皮表页」残留入口
@router.get("/workshops")
@router.get("/vats")
@router.get("/lots")
@router.get("/home")
async def legacy_redirect():
    return RedirectResponse("/", status_code=303)
