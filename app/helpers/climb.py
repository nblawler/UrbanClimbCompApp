import json

def parse_boundary_points(raw) -> list[dict]:
    """
    Accepts:
      - list of dicts [{"x":..,"y":..}, ...]
      - JSON string of that list
    Returns cleaned list with floats clamped 0..100.
    """
    if raw is None:
        return []

    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return []
        try:
            raw = json.loads(raw)
        except Exception:
            return []

    if not isinstance(raw, list):
        return []

    cleaned = []
    for p in raw:
        if not isinstance(p, dict):
            continue
        try:
            x = float(p.get("x"))
            y = float(p.get("y"))
        except Exception:
            continue

        # clamp to 0..100 (since we're storing % coords)
        x = max(0.0, min(100.0, x))
        y = max(0.0, min(100.0, y))
        cleaned.append({"x": x, "y": y})

    return cleaned

def boundary_to_json(points: list[dict]) -> str:
    return json.dumps(points, separators=(",", ":"))
