#!/usr/bin/env python3
"""List messages from the SmartBox IMAP mailbox."""

import argparse
import email
import imaplib
import os
import ssl
import sys
from email.header import decode_header
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Dict, List, Optional


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


def decode_mime(value: Optional[str]) -> str:
    if not value:
        return ""
    parts = decode_header(value)
    chunks: list[str] = []
    for chunk, charset in parts:
        if isinstance(chunk, bytes):
            chunks.append(chunk.decode(charset or "utf-8", errors="replace"))
        else:
            chunks.append(chunk)
    return "".join(chunks)


def parse_env() -> Dict[str, object]:
    root = Path(__file__).resolve().parents[1]
    env = load_dotenv(root / ".env")

    mail_address = env.get("MAIL_ADDRESS", "default@mail.thm64.com")
    server_ip = env.get("MAIL_SERVER_IP", "192.168.1.34")
    use_ssl = env.get("IMAP_USE_SSL", "false").lower() == "true"
    default_port = env.get("IMAP_HOST_PORT", "1143")

    return {
        "host": env.get("IMAP_HOST", server_ip),
        "port": int(env.get("IMAP_PORT", default_port)),
        "use_ssl": use_ssl,
        "username": mail_address,
        "password": env.get("MAIL_PASSWORD", "default"),
    }


def connect(host: str, port: int, use_ssl: bool, insecure: bool) -> imaplib.IMAP4:
    if use_ssl:
        context = ssl.create_default_context()
        if insecure:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        return imaplib.IMAP4_SSL(host, port, ssl_context=context)
    return imaplib.IMAP4(host, port)


def list_messages(imap: imaplib.IMAP4, limit: int) -> List[Dict[str, str]]:
    imap.select("INBOX")
    status, data = imap.search(None, "ALL")
    if status != "OK" or not data or not data[0]:
        return []

    ids = data[0].split()
    ids.reverse()

    messages: List[Dict[str, str]] = []
    for msg_id in ids[:limit]:
        status, fetched = imap.fetch(msg_id, "(RFC822.HEADER)")
        if status != "OK" or not fetched or not fetched[0]:
            continue

        raw = fetched[0][1]
        msg = email.message_from_bytes(raw)
        date_raw = msg.get("Date")
        try:
            received_at = parsedate_to_datetime(date_raw).strftime("%Y-%m-%d %H:%M") if date_raw else "—"
        except (TypeError, ValueError):
            received_at = date_raw or "—"

        messages.append(
            {
                "id": msg_id.decode(),
                "from": decode_mime(msg.get("From")) or "(unknown)",
                "subject": decode_mime(msg.get("Subject")) or "(no subject)",
                "date": received_at,
            }
        )

    return messages


def main() -> int:
    defaults = parse_env()

    parser = argparse.ArgumentParser(description="List emails from the SmartBox IMAP server.")
    parser.add_argument("--host", default=defaults["host"], help="IMAP host")
    parser.add_argument("--port", type=int, default=defaults["port"], help="IMAP port")
    parser.add_argument("--username", default=defaults["username"], help="IMAP username")
    parser.add_argument("--password", default=defaults["password"], help="IMAP password")
    parser.add_argument("--limit", type=int, default=20, help="Maximum messages to show")
    parser.add_argument(
        "--ssl",
        action=argparse.BooleanOptionalAction,
        default=defaults["use_ssl"],
        help="Use IMAPS (default: off; plaintext IMAP on port 1143)",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Skip TLS certificate verification (self-signed certs)",
    )
    args = parser.parse_args()

    try:
        imap = connect(args.host, args.port, args.ssl, args.insecure)
        imap.login(args.username, args.password)
        messages = list_messages(imap, args.limit)
        imap.logout()
    except Exception as exc:
        print(f"Failed to connect: {exc}", file=sys.stderr)
        return 1

    if not messages:
        print("No messages in INBOX.")
        return 0

    print(f"{'ID':<6} {'Date':<16} {'From':<30} Subject")
    print("-" * 90)
    for message in messages:
        sender = message["from"]
        if len(sender) > 28:
            sender = sender[:25] + "..."
        print(f"{message['id']:<6} {message['date']:<16} {sender:<30} {message['subject']}")

    print(f"\n{len(messages)} message(s) shown.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
