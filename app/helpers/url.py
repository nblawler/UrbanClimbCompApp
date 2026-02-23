import re
import secrets
import hashlib

def slugify(name: str) -> str:
    """Create URL friendly string ("The Slab" -> "the-slab")"""
    s = re.sub(r"[^a-zA-Z0-9]+", "-", name.strip().lower()).strip("-")
    return s or "section"

def make_token() -> str:
    return secrets.token_urlsafe(32)

def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
