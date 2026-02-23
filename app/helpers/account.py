from flask import session
from typing import Optional

from app.extensions import db
from app.models import Account
from app.helpers.email import normalize_email

def get_or_create_account_for_email(email: str) -> Account:
    email = normalize_email(email)
    if not email:
        raise ValueError("email required")

    acct = Account.query.filter_by(email=email).first()
    if acct:
        return acct

    acct = Account(email=email)
    db.session.add(acct)
    db.session.commit()
    return acct

def get_account_for_session() -> Optional[Account]:
    # Prefer explicit session account_id if present
    account_id = session.get("account_id")
    if account_id:
        acct = Account.query.get(account_id)
        if acct:
            return acct

    # Fallback: derive from competitor_email
    email = normalize_email(session.get("competitor_email"))
    if not email:
        return None
    return Account.query.filter_by(email=email).first()
