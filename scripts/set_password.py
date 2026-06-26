#!/usr/bin/env python3
"""Change the SmartBox mailbox password without resetting the data volume.

The mailbox password is stored inside Stalwart's data volume (set once during
bootstrap). Because the bootstrap marker persists in that volume, editing
MAIL_PASSWORD in .env alone does NOT change it. This script updates the
credential live through the admin JMAP API instead.

It also keeps .env in sync (the healthcheck authenticates with
MAIL_PASSWORD) and recreates the containers so they pick up the new value. The
named volume and bootstrap marker are preserved, so no data is lost.
"""

import argparse
import base64
import getpass
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict


def load_dotenv(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def jmap(url: str, admin: str, method_calls: list) -> dict:
    auth = base64.b64encode(admin.encode()).decode()
    body = json.dumps(
        {"using": ["urn:ietf:params:jmap:core", "urn:stalwart:jmap"], "methodCalls": method_calls}
    ).encode()
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Basic {auth}"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def set_credential(url: str, admin: str, account: str, new_password: str) -> str:
    queried = jmap(url, admin, [["x:Account/query", {"filter": {"name": account}}, "q"]])
    ids = queried["methodResponses"][0][1].get("ids") or []
    if not ids:
        raise RuntimeError(f"Account '{account}' not found")
    account_id = ids[0]

    result = jmap(
        url,
        admin,
        [["x:Account/set", {"update": {account_id: {"credentials/0/secret": new_password}}}, "u"]],
    )["methodResponses"][0][1]
    if account_id not in (result.get("updated") or {}):
        raise RuntimeError(f"Failed to update password: {result}")
    return account_id


def update_env_password(env_path: Path, new_password: str) -> bool:
    if not env_path.is_file():
        return False
    lines = env_path.read_text().splitlines()
    changed = False
    for index, line in enumerate(lines):
        if line.startswith("MAIL_PASSWORD="):
            lines[index] = f"MAIL_PASSWORD={new_password}"
            changed = True
            break
    if not changed:
        lines.append(f"MAIL_PASSWORD={new_password}")
        changed = True
    env_path.write_text("\n".join(lines) + "\n")
    return changed


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    env = load_dotenv(root / ".env")

    admin_user = env.get("STALWART_ADMIN_USER", "admin")
    admin_password = env.get("STALWART_ADMIN_PASSWORD", "smartbox-admin")
    admin_port = env.get("STALWART_ADMIN_HOST_PORT", "8087")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("new_password", nargs="?", help="New password (prompted if omitted)")
    parser.add_argument(
        "--url",
        default=f"http://127.0.0.1:{admin_port}/jmap",
        help="Stalwart admin JMAP URL (default: local admin port from .env)",
    )
    parser.add_argument(
        "--admin",
        default=f"{admin_user}:{admin_password}",
        help="Admin credentials as user:password (default: from .env)",
    )
    parser.add_argument(
        "--account",
        default=env.get("MAIL_USER", "default"),
        help="Mailbox account name (default: MAIL_USER from .env)",
    )
    parser.add_argument(
        "--no-env",
        action="store_true",
        help="Do not update MAIL_PASSWORD in .env",
    )
    parser.add_argument(
        "--no-recreate",
        action="store_true",
        help="Do not run 'docker compose up -d' afterwards",
    )
    args = parser.parse_args()

    new_password = args.new_password
    if not new_password:
        new_password = getpass.getpass(f"New password for '{args.account}': ")
        confirm = getpass.getpass("Confirm new password: ")
        if new_password != confirm:
            print("Passwords do not match.", file=sys.stderr)
            return 1
    if not new_password:
        print("Empty password.", file=sys.stderr)
        return 1

    try:
        account_id = set_credential(args.url, args.admin, args.account, new_password)
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:300].decode(errors="replace")
        print(f"JMAP request failed ({exc.code}): {detail}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Failed to update password: {exc}", file=sys.stderr)
        return 1
    print(f"Password updated for '{args.account}' (account {account_id}).")

    if not args.no_env:
        if update_env_password(root / ".env", new_password):
            print("Updated MAIL_PASSWORD in .env.")

    if not args.no_recreate:
        print("Recreating containers (volume preserved)...")
        result = subprocess.run(
            ["docker", "compose", "up", "-d"], cwd=root
        )
        if result.returncode != 0:
            print("docker compose up -d failed; recreate manually.", file=sys.stderr)
            return result.returncode

    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
