"""Set the dashboard login password: stores only its PBKDF2 hash in .env (DASHBOARD_PASSWORD_HASH).

Run in PowerShell or cmd (hidden input works there; Git Bash/mintty often cannot read it):
    uv run python scripts/set_dashboard_password.py
Then restart the API (`uv run deskmind api`).
"""

from __future__ import annotations

import getpass
import sys
from pathlib import Path

sys.path.insert(0, "src")

from deskmind.api.auth import hash_password, verify_password

ENV = Path(".env")
KEY = "DASHBOARD_PASSWORD_HASH"
MIN_LEN = 8


def ask(prompt: str) -> str:
    value = getpass.getpass(prompt)
    if not value and not sys.stdin.isatty():
        print("This terminal cannot read hidden input. Run the script in PowerShell or cmd.")
        sys.exit(1)
    return value


def main() -> int:
    if not ENV.exists():
        print(".env not found: run this from the Deskmind project folder.")
        return 1
    pw = ask("New dashboard password: ")
    if len(pw) < MIN_LEN:
        print(f"Password must be at least {MIN_LEN} characters (got {len(pw)}). Nothing changed.")
        print("If you did type one, this terminal did not pass it through: use PowerShell or cmd.")
        return 1
    if ask("Repeat password: ") != pw:
        print("The two passwords do not match. Nothing changed.")
        return 1
    hashed = hash_password(pw)
    assert verify_password(pw, hashed)
    lines = ENV.read_text(encoding="utf-8").splitlines()
    lines = [ln for ln in lines if not ln.startswith(f"{KEY}=")]
    lines.append(f"{KEY}={hashed}")
    ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(
        f"Saved (length {len(pw)} characters). Restart the API, then log in as the dashboard user."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
