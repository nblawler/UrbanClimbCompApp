from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Optional

MELB_TZ = ZoneInfo("Australia/Melbourne")

def utc_to_melbourne(dt: Optional[datetime]) -> Optional[datetime]:
    if not dt:
        return None

    # Treat naive DB values as UTC
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt.astimezone(MELB_TZ)

def utc_naive_to_aware_utc(dt_utc_naive: Optional[datetime]) -> Optional[datetime]:
    """DB -> aware UTC. DB stores UTC-naive."""
    if dt_utc_naive is None:
        return None
    return dt_utc_naive.replace(tzinfo=timezone.utc)


def aware_utc_to_naive_utc(dt_aware_utc: Optional[datetime]) -> Optional[datetime]:
    """aware UTC -> DB UTC-naive."""
    if dt_aware_utc is None:
        return None
    return dt_aware_utc.astimezone(timezone.utc).replace(tzinfo=None)


def melb_now() -> datetime:
    """Current time in Melbourne (aware)."""
    return datetime.now(MELB_TZ)


def utc_naive_to_melb(dt_utc_naive: Optional[datetime]) -> Optional[datetime]:
    """DB UTC-naive -> Melbourne aware."""
    dt_aware_utc = utc_naive_to_aware_utc(dt_utc_naive)
    return dt_aware_utc.astimezone(MELB_TZ) if dt_aware_utc else None


def melb_naive_to_utc_naive(dt_melb_naive: Optional[datetime]) -> Optional[datetime]:
    """
    Admin input (naive, intended Melbourne local) -> DB UTC-naive.
    IMPORTANT: dt_melb_naive is interpreted as Melbourne local time.
    """
    if dt_melb_naive is None:
        return None
    dt_melb_aware = dt_melb_naive.replace(tzinfo=MELB_TZ)
    dt_utc_aware = dt_melb_aware.astimezone(timezone.utc)
    return dt_utc_aware.replace(tzinfo=None)