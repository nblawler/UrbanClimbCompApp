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