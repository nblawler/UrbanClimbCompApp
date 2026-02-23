from flask import session

def set_pending_join(comp_slug: str, email: str, name: str, gender: str):
    session["pending_join"] = True
    session["pending_join_slug"] = (comp_slug or "").strip()
    session["pending_join_email"] = (email or "").strip().lower()
    session["pending_join_name"] = (name or "").strip()
    session["pending_join_gender"] = (gender or "").strip()

def clear_pending_join():
    session.pop("pending_join", None)
    session.pop("pending_join_slug", None)
    session.pop("pending_join_email", None)
    session.pop("pending_join_name", None)
    session.pop("pending_join_gender", None)

def has_pending_join() -> bool:
    return bool(session.get("pending_join"))
