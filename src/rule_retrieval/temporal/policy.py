from datetime import datetime, date
import re


def parse_date(value) -> str | None:
    if not value or not isinstance(value, str):
        return None
    value = value.strip()
    for fmt in ["%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y"]:
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def date_in_evidence(value: str, evidence: str) -> bool:
    parsed = parse_date(value)
    if parsed is None:
        return False
    dt = date.fromisoformat(parsed)
    for m in re.finditer(r"\b(\d{1,2})\s*[/.-]\s*(\d{1,2})\s*[/.-]\s*(\d{4})\b|ngày\s+(\d{1,2})\s+tháng\s+(\d{1,2})\s+năm\s+(\d{4})", evidence, re.I):
        values = m.groups()[:3] if m.group(1) else m.groups()[3:]
        try:
            if date(int(values[2]), int(values[1]), int(values[0])) == dt:
                return True
        except ValueError:
            continue
    return False


def temporal_check(item: dict, event_date: str | None) -> str:
    if not event_date or not parse_date(event_date):
        return "TEMPORAL_UNCERTAIN"
    raw_start,raw_end=item.get("effective_from"),item.get("effective_to")
    start,end=parse_date(raw_start),parse_date(raw_end)
    if (raw_start and not start) or (raw_end and not end) or (start and end and start>=end):
        return "TEMPORAL_UNCERTAIN"
    if start and event_date < start:
        return "OUTSIDE_RECORDED_INTERVAL"
    if end and event_date >= end:  # half-open interval [from, to)
        return "OUTSIDE_RECORDED_INTERVAL"
    if item.get("temporal_status") != "VERIFIED_INTERVAL" or not start:
        return "TEMPORAL_UNCERTAIN"
    if item.get("verification_status") != "VERIFIED":
        return "TEMPORAL_UNCERTAIN"
    return "VERIFIED_IN_INTERVAL"
