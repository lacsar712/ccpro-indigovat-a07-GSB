import hashlib
import hmac
import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models import DipLot, ReducingLimitCard, User, Vat, Workshop

_PWD_SALT = os.environ.get("PWD_SALT", "indigovat-dev-salt").encode("utf-8")


def hash_password(password: str) -> str:
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), _PWD_SALT, 120000
    )
    return digest.hex()


def verify_password(plain: str, hashed: str) -> bool:
    return hmac.compare_digest(hash_password(plain), hashed)


def ensure_seed_data(db: Session) -> None:
    """幂等种子：账号 + 蓝靛湾/清水江样例缸位与电位序列。"""
    if not db.query(User).filter_by(username="admin").first():
        db.add(
            User(
                username="admin",
                password_hash=hash_password("123456"),
                is_superuser=True,
            )
        )
    if not db.query(User).filter_by(username="worker").first():
        db.add(
            User(
                username="worker",
                password_hash=hash_password("123456"),
                is_superuser=False,
            )
        )
    db.commit()

    if db.query(Workshop).first():
        _ensure_reducing_limit_seed(db)
        return

    w1 = Workshop(name="蓝靛湾一号坊", region="黔东南", notes="晨露还原较快")
    w2 = Workshop(name="清水江二号坊", region="黔南", notes="缸体较深，保温好")
    db.add_all([w1, w2])
    db.flush()

    v1 = Vat(
        workshop_id=w1.id,
        code="V-01",
        dyeType="土靛",
        volumeL=Decimal("800.00"),
        status=Vat.STATUS_REDUCING,
    )
    v2 = Vat(
        workshop_id=w1.id,
        code="V-02",
        dyeType="合成靛",
        volumeL=Decimal("600.00"),
        status=Vat.STATUS_IDLE,
    )
    v3 = Vat(
        workshop_id=w2.id,
        code="V-11",
        dyeType="土靛",
        volumeL=Decimal("900.00"),
        status=Vat.STATUS_REDUCING,
    )
    v4 = Vat(
        workshop_id=w2.id,
        code="V-12",
        dyeType="板蓝根靛",
        volumeL=Decimal("750.00"),
        status=Vat.STATUS_READY,
    )
    db.add_all([v1, v2, v3, v4])
    db.flush()

    now = datetime.now(timezone.utc)

    def lots(vat_id: int, series):
        """series: (hours_ago, meters, redox or None)"""
        rows = []
        for hours, meters, redox in series:
            rows.append(
                DipLot(
                    vat_id=vat_id,
                    dippedAt=now - timedelta(hours=hours),
                    clothMeters=Decimal(meters),
                    redoxMv=Decimal(redox) if redox is not None else None,
                )
            )
        return rows

    db.add_all(
        lots(
            v1.id,
            [
                (36, "18.00", "-410.00"),
                (28, "22.50", "-455.00"),
                (20, "30.00", "-490.00"),
                (12, "40.00", "-510.00"),
                (8, "45.00", "-520.00"),
            ],
        )
    )
    db.add_all(
        lots(
            v2.id,
            [
                (6, "8.00", None),
                (1, "12.00", None),
            ],
        )
    )
    db.add_all(
        lots(
            v3.id,
            [
                (40, "25.00", "-390.00"),
                (30, "35.00", "-430.00"),
                (22, "48.00", "-460.00"),
                (14, "60.00", "-480.00"),
            ],
        )
    )
    db.add_all(
        lots(
            v4.id,
            [
                (48, "20.00", "-420.00"),
                (32, "28.00", "-470.00"),
                (20, "33.00", "-505.00"),
                (10, "38.50", "-530.00"),
            ],
        )
    )
    db.commit()
    _ensure_reducing_limit_seed(db)


def _ensure_reducing_limit_seed(db: Session) -> None:
    """幂等补齐并发上限演示数据（新老库都走这里）。

    蓝靛湾一号坊 · 土靛：上限卡 max=1 且启用；V-01 已是还原中（占了一名额），
    另备 V-03 同染种闲置缸，用于演示「名额已满拒绝 / 腾出后可入」。
    """
    w1 = db.query(Workshop).filter_by(name="蓝靛湾一号坊").first()
    if w1 is None:
        return

    standby = db.query(Vat).filter_by(workshop_id=w1.id, code="V-03").first()
    if standby is None:
        standby = Vat(
            workshop_id=w1.id,
            code="V-03",
            dyeType="土靛",
            volumeL=Decimal("700.00"),
            status=Vat.STATUS_IDLE,
        )
        db.add(standby)
        db.flush()
        now = datetime.now(timezone.utc)
        db.add_all(
            [
                DipLot(
                    vat_id=standby.id,
                    dippedAt=now - timedelta(hours=5),
                    clothMeters=Decimal("9.00"),
                    redoxMv=None,
                ),
                DipLot(
                    vat_id=standby.id,
                    dippedAt=now - timedelta(hours=1),
                    clothMeters=Decimal("11.50"),
                    redoxMv=None,
                ),
            ]
        )

    card = (
        db.query(ReducingLimitCard)
        .filter_by(workshop_id=w1.id, dyeType="土靛")
        .first()
    )
    if card is None:
        db.add(
            ReducingLimitCard(
                workshop_id=w1.id,
                dyeType="土靛",
                maxReducing=1,
                effectiveFrom=date.today(),
                enabled=True,
            )
        )
    db.commit()
