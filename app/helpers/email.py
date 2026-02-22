import sys
import os
from app.config import RESEND_API_KEY, RESEND_FROM_EMAIL
import resend

ADMIN_EMAILS_RAW = os.getenv("ADMIN_EMAILS", "")
# Comma-separated list of admin emails, e.g. "host@urbanclimb.com,other@uc.com"
ADMIN_EMAILS = {
    e.strip().lower()
    for e in ADMIN_EMAILS_RAW.split(",")
    if e.strip()
}

def normalize_email(email: str) -> str:
    return (email or "").strip().lower()

def send_login_code_via_email(email: str, code: str):
    """
    Send the 6-digit login code via Resend in production.

    - If RESEND_API_KEY is not set, just log to stderr (local dev).
    """
    # Dev / fallback path
    if not RESEND_API_KEY:
        print(f"[LOGIN CODE - DEV ONLY] {email} -> {code}", file=sys.stderr)
        return

    html = f"""
      <div style="font-family: system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; padding: 16px;">
        <p>Hey climber 👋</p>
        <p>Your Urban Climb Comp login code is:</p>
        <p style="font-size: 24px; font-weight: 700; letter-spacing: 4px; margin: 12px 0;">{code}</p>
        <p>This code will expire in 10 minutes. If you didn’t request this, you can ignore this email.</p>
      </div>
    """

    try:
        params = {
            "from": RESEND_FROM_EMAIL,
            "to": [email],
            "subject": "Your Urban Climb Comp login code",
            "html": html,
        }
        resend.Emails.send(params)
        print(f"[LOGIN CODE] Sent login code to {email}", file=sys.stderr)
    except Exception as e:
        # Don't crash the app if email fails; just log it.
        print(f"[LOGIN CODE] Failed to send via Resend: {e}", file=sys.stderr)

def send_scoring_link_via_email(email: str, comp_name: str, scoring_url: str):
    """
    Email the user a direct link to their scoring page for a comp.
    """
    # Dev / fallback path
    if not RESEND_API_KEY:
        print(f"[SCORING LINK - DEV ONLY] {email} -> {scoring_url}", file=sys.stderr)
        return

    html = f"""
      <div style="font-family: system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; padding: 16px;">
        <p>Hey climber 👋</p>
        <p>Your scoring link for <strong>{comp_name}</strong> is ready:</p>
        <p style="margin: 12px 0;">
          <a href="{scoring_url}" style="display: inline-block; padding: 10px 14px; border-radius: 10px; background: #1a2942; color: #fff; text-decoration: none;">
            Open scoring
          </a>
        </p>
        <p style="color:#667; font-size: 13px;">If the button doesn’t work, copy/paste this link:</p>
        <p style="font-size: 12px; word-break: break-all;">{scoring_url}</p>
      </div>
    """

    try:
        params = {
            "from": RESEND_FROM_EMAIL,
            "to": [email],
            "subject": f"Your scoring link — {comp_name}",
            "html": html,
        }
        resend.Emails.send(params)
        print(f"[SCORING LINK] Sent scoring link to {email}", file=sys.stderr)
    except Exception as e:
        print(f"[SCORING LINK] Failed to send via Resend: {e}", file=sys.stderr)

def is_admin_email(email: str) -> bool:
    """Return True if this email is configured as an admin."""
    if not email:
        return False
    return email.strip().lower() in ADMIN_EMAILS
