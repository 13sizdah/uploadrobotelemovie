#!/usr/bin/env python3
from __future__ import annotations

import base64
import getpass
import hashlib
import os

password = getpass.getpass("رمز جدید پنل وب (حداقل ۱۲ کاراکتر): ")
confirmation = getpass.getpass("تکرار رمز: ")
if password != confirmation:
    raise SystemExit("رمزها یکسان نیستند.")
if len(password) < 12:
    raise SystemExit("رمز باید حداقل ۱۲ کاراکتر باشد.")
salt = os.urandom(16)
iterations = 600_000
digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
encoded = "pbkdf2_sha256${}${}${}".format(
    iterations,
    base64.urlsafe_b64encode(salt).decode(),
    base64.urlsafe_b64encode(digest).decode(),
)
print("\nاین خط را در .env قرار دهید:\n")
print(f"ADMIN_WEB_PASSWORD_HASH='{encoded}'")
