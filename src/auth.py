"""
Single-admin TOTP (Google Authenticator-style) gate for the webapp.

"Registered" is defined purely by whether a row exists in auth_totp — a
CHECK(id = 1) makes a second row impossible, so there is never a separate
flag that could drift out of sync with reality. To allow a new
registration, delete that row directly in the database
(sqlite3 data/muni.db "DELETE FROM auth_totp") and the webapp falls back to
showing the setup page again on the next visit — exactly as specified:
no in-app way to deregister, only direct DB access.
"""

import base64
import io

import pyotp
import qrcode

ISSUER_NAME = "Muni Packet Crawler"
ACCOUNT_NAME = "admin"


def is_registered(conn) -> bool:
    row = conn.execute("SELECT 1 FROM auth_totp WHERE id = 1").fetchone()
    return row is not None


def get_secret(conn) -> str | None:
    row = conn.execute("SELECT secret FROM auth_totp WHERE id = 1").fetchone()
    return row[0] if row else None


def register_secret(conn, secret: str) -> bool:
    """Returns False (registration already exists) rather than raising, so
    a race between two browser tabs both finishing setup fails soft."""
    try:
        conn.execute("INSERT INTO auth_totp (id, secret) VALUES (1, ?)", (secret,))
        conn.commit()
        return True
    except Exception:
        return False


def generate_secret() -> str:
    return pyotp.random_base32()


def verify_code(secret: str, code: str) -> bool:
    if not code or not code.strip().isdigit():
        return False
    # valid_window=1 tolerates ~30s of clock drift between server and phone
    return pyotp.TOTP(secret).verify(code.strip(), valid_window=1)


def qr_code_data_uri(secret: str) -> str:
    uri = pyotp.TOTP(secret).provisioning_uri(name=ACCOUNT_NAME, issuer_name=ISSUER_NAME)
    img = qrcode.make(uri)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    encoded = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"
