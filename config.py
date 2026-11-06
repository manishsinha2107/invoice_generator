# --- AFTER (PATCHED CODE | File: config.py | Lines 1 to 15) ---
# config.py
import os
import base64
import hashlib
from dotenv import load_dotenv
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

load_dotenv()

# # [MODULE/CLASS SSOT ROLE]: Global Environment Configuration & AES-256-GCM Credential Decryption SSOT (`config.py`).
# # [FUNCTION CONTRACT & MATH]: Derives a 256-bit key `SHA-256(UTF8(AUTH_SALT))` and decrypts `ENCv1:<base64_iv>:<base64_ct_and_tag>` strings back to UTF-8 plaintext while passing unencrypted legacy/dev strings through untouched.

# GLOBAL TOGGLE: Change to True for Production, False for Testing
IS_LIVE = True

# TABLE MAPPING: Automatically switches based on IS_LIVE status
VAULT_TABLE = "subscriber_vault" if IS_LIVE else "subscriber_vault_dev"
REGISTRY_TABLE = "broker_registry" if IS_LIVE else "broker_registry_dev"

# GLOBAL WHITELIST: Accounts used when IS_LIVE is False
TEST_EMAILS = [
    "support@ensuringsuccess.in",
    "seemadrishti@gmail.com"
]

SUPPORTED_BROKERS = ["KOTAK", "RMONEY"]

# # [TECHNICAL]: Decrypts a single `ENCv1:<iv_b64>:<ct_b64>` string using `AESGCM` and `SHA-256(AUTH_SALT)`.
# # [BUSINESS / DOMAIN LOGIC]: Restores encrypted broker passwords and TOTP secret keys in memory during the 8:40 AM automated login run.
def decrypt_vault_value(val):
    if not val or not isinstance(val, str) or not val.startswith("ENCv1:"):
        return val
    auth_salt = os.environ.get("AUTH_SALT", "")
    if not auth_salt:
        raise ValueError("CRITICAL: AUTH_SALT environment variable is missing. Add AUTH_SALT to GitHub Secrets / .env.")
    parts = val.split(":")
    if len(parts) != 3:
        raise ValueError("Malformed ENCv1 ciphertext envelope in vault.")
    iv = base64.b64decode(parts[1])
    ct_and_tag = base64.b64decode(parts[2])
    aes_key = hashlib.sha256(auth_salt.encode("utf-8")).digest()
    plaintext_bytes = AESGCM(aes_key).decrypt(iv, ct_and_tag, None)
    return plaintext_bytes.decode("utf-8")

# # [TECHNICAL]: Iterates over sensitive credential columns in a Supabase row dict and decrypts any `ENCv1:` fields in-place on a copy.
# # [BUSINESS / DOMAIN LOGIC]: Allows all Python broker automation scripts (`finvasia_auth.py`, `flattrade_async.py`, `kotak_finvasia_auth.py`) to work seamlessly with both encrypted Production rows and unencrypted Dev rows.
def decrypt_account_credentials(acc: dict) -> dict:
    if not isinstance(acc, dict):
        return acc
    decrypted = dict(acc)
    sensitive_keys = ["password", "totp_key", "tradetron_password", "shared_broker_code"]
    for key in sensitive_keys:
        if key in decrypted and isinstance(decrypted[key], str) and decrypted[key].startswith("ENCv1:"):
            decrypted[key] = decrypt_vault_value(decrypted[key])
    return decrypted