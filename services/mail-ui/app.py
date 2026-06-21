import base64
import email
import imaplib
import json
import os
import smtplib
from datetime import datetime, timezone
from email.header import decode_header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import parsedate_to_datetime
from typing import Dict, List, Optional, Tuple
from urllib.parse import unquote

from flask import Flask, flash, redirect, render_template, request, url_for

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "smartbox-local-mail-dev")

INBOX_LIMIT = 100


def mail_config():
    address = os.environ.get("MAIL_ADDRESS", "default@mail.thm64.com")
    use_ssl = os.environ.get("IMAP_USE_SSL", "false").lower() == "true"
    return {
        "address": address,
        "user": os.environ.get("MAIL_USER", address.split("@", 1)[0]),
        "password": os.environ.get("MAIL_PASSWORD", "default"),
        "domain": os.environ.get("MAIL_DOMAIN", address.split("@", 1)[1]),
        "imap_host": os.environ.get("IMAP_HOST", "stalwart"),
        "imap_port": int(os.environ.get("IMAP_PORT", "143")),
        "imap_use_ssl": use_ssl,
        "smtp_host": os.environ.get("SMTP_HOST", "stalwart"),
        "smtp_port": int(os.environ.get("SMTP_PORT", "25")),
        "smtp_use_tls": os.environ.get("SMTP_USE_TLS", "false").lower() == "true",
        "smtp_auth": os.environ.get("SMTP_AUTH", "false").lower() == "true",
    }


def _mailbox_entry(
    address: str,
    password: str,
    label: Optional[str] = None,
    imap_host: Optional[str] = None,
    imap_port: Optional[int] = None,
    imap_use_ssl: Optional[bool] = None,
    defaults: Optional[Dict] = None,
) -> Dict:
    defaults = defaults or mail_config()
    return {
        "label": label or address,
        "address": address,
        "password": password,
        "imap_host": imap_host or defaults["imap_host"],
        "imap_port": int(imap_port if imap_port is not None else defaults["imap_port"]),
        "imap_use_ssl": (
            imap_use_ssl if imap_use_ssl is not None else defaults["imap_use_ssl"]
        ),
    }


def parse_extra_mailboxes(defaults: Dict) -> List[Dict]:
    raw = os.environ.get("EXTRA_MAILBOXES", "").strip()
    if not raw:
        return []

    entries: List[Dict] = []
    if raw.startswith("["):
        items = json.loads(raw)
        if not isinstance(items, list):
            raise ValueError("EXTRA_MAILBOXES JSON must be an array")
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("Each EXTRA_MAILBOXES entry must be an object")
            address = (item.get("address") or "").strip()
            password = item.get("password") or ""
            if not address or not password:
                raise ValueError("Each mailbox needs address and password")
            port = item.get("imap_port")
            use_ssl = item.get("imap_use_ssl")
            if use_ssl is not None and not isinstance(use_ssl, bool):
                use_ssl = str(use_ssl).lower() == "true"
            entries.append(
                _mailbox_entry(
                    address=address,
                    password=password,
                    label=(item.get("label") or address).strip(),
                    imap_host=item.get("imap_host"),
                    imap_port=int(port) if port is not None else None,
                    imap_use_ssl=use_ssl,
                    defaults=defaults,
                )
            )
        return entries

    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        segments = part.split(":")
        if len(segments) == 2:
            address, password = segments[0].strip(), segments[1]
            label = address
        elif len(segments) >= 3:
            label = segments[0].strip()
            address = segments[1].strip()
            password = ":".join(segments[2:])
        else:
            raise ValueError(
                "EXTRA_MAILBOXES entries must be address:password or label:address:password"
            )
        if not address or not password:
            raise ValueError("Mailbox address and password cannot be empty")
        entries.append(
            _mailbox_entry(address, password, label=label, defaults=defaults)
        )
    return entries


def list_mailboxes() -> List[Dict]:
    defaults = mail_config()
    primary = _mailbox_entry(
        defaults["address"],
        defaults["password"],
        label=defaults["address"],
        defaults=defaults,
    )
    extras = parse_extra_mailboxes(defaults)
    seen = {primary["address"].lower()}
    mailboxes = [primary]
    for entry in extras:
        key = entry["address"].lower()
        if key in seen:
            continue
        seen.add(key)
        mailboxes.append(entry)
    return mailboxes


def encode_message_ref(mailbox_address: str, imap_id: str) -> str:
    raw = f"{mailbox_address}\x00{imap_id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_message_ref(ref: str) -> Tuple[str, str]:
    padded = ref + "=" * (-len(ref) % 4)
    mailbox_address, imap_id = base64.urlsafe_b64decode(padded).decode().split("\x00", 1)
    return mailbox_address, imap_id


def find_mailbox(address: str) -> Optional[Dict]:
    key = address.lower()
    for mailbox in list_mailboxes():
        if mailbox["address"].lower() == key:
            return mailbox
    return None


def connect_imap(cfg):
    if cfg["imap_use_ssl"]:
        import ssl

        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return imaplib.IMAP4_SSL(cfg["imap_host"], cfg["imap_port"], ssl_context=context)
    return imaplib.IMAP4(cfg["imap_host"], cfg["imap_port"])


def decode_mime(value):
    if not value:
        return ""
    parts = decode_header(value)
    decoded = []
    for chunk, charset in parts:
        if isinstance(chunk, bytes):
            decoded.append(chunk.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(chunk)
    return "".join(decoded)


def parse_message(raw_bytes):
    msg = email.message_from_bytes(raw_bytes)
    body_text = ""
    body_html = None

    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            if content_type == "text/plain" and not body_text:
                payload = part.get_payload(decode=True)
                if payload:
                    body_text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
            elif content_type == "text/html" and body_html is None:
                payload = part.get_payload(decode=True)
                if payload:
                    body_html = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            text = payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
            if msg.get_content_type() == "text/html":
                body_html = text
            else:
                body_text = text

    date_raw = msg.get("Date")
    try:
        received_at = parsedate_to_datetime(date_raw) if date_raw else None
    except (TypeError, ValueError):
        received_at = None

    return {
        "subject": decode_mime(msg.get("Subject")) or "(no subject)",
        "from": decode_mime(msg.get("From")) or "(unknown)",
        "to": decode_mime(msg.get("To")) or "(unknown)",
        "received_at": received_at,
        "body_text": body_text,
        "body_html": body_html,
    }


def _sort_key(message: Dict) -> datetime:
    received_at = message.get("received_at")
    if received_at is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    if received_at.tzinfo is None:
        return received_at.replace(tzinfo=timezone.utc)
    return received_at


def fetch_inbox_for_mailbox(mailbox: Dict, limit: int = INBOX_LIMIT) -> List[Dict]:
    imap = connect_imap(mailbox)
    try:
        imap.login(mailbox["address"], mailbox["password"])
        imap.select("INBOX")
        status, data = imap.search(None, "ALL")
        if status != "OK":
            return []

        ids = data[0].split()
        ids.reverse()
        messages = []

        for msg_id in ids[:limit]:
            status, fetched = imap.fetch(msg_id, "(RFC822)")
            if status != "OK" or not fetched or not fetched[0]:
                continue
            raw = fetched[0][1]
            parsed = parse_message(raw)
            imap_id = msg_id.decode()
            parsed["id"] = encode_message_ref(mailbox["address"], imap_id)
            parsed["mailbox"] = mailbox["label"]
            parsed["mailbox_address"] = mailbox["address"]
            messages.append(parsed)

        return messages
    finally:
        try:
            imap.logout()
        except imaplib.IMAP4.error:
            pass


def fetch_inbox() -> Tuple[List[Dict], List[str]]:
    messages: List[Dict] = []
    errors: List[str] = []
    per_mailbox = max(1, INBOX_LIMIT // max(len(list_mailboxes()), 1))

    for mailbox in list_mailboxes():
        try:
            messages.extend(fetch_inbox_for_mailbox(mailbox, limit=per_mailbox))
        except Exception as exc:
            errors.append(f"{mailbox['label']}: {exc}")

    messages.sort(key=_sort_key, reverse=True)
    return messages[:INBOX_LIMIT], errors


def fetch_message(ref: str) -> Optional[Dict]:
    mailbox_address, imap_id = decode_message_ref(unquote(ref))
    mailbox = find_mailbox(mailbox_address)
    if mailbox is None:
        return None

    imap = connect_imap(mailbox)
    try:
        imap.login(mailbox["address"], mailbox["password"])
        imap.select("INBOX")
        status, fetched = imap.fetch(imap_id.encode(), "(RFC822)")
        if status != "OK" or not fetched or not fetched[0]:
            return None
        parsed = parse_message(fetched[0][1])
        parsed["id"] = ref
        parsed["mailbox"] = mailbox["label"]
        parsed["mailbox_address"] = mailbox["address"]
        return parsed
    finally:
        try:
            imap.logout()
        except imaplib.IMAP4.error:
            pass


def send_mail(from_addr, to_addr, subject, body):
    cfg = mail_config()
    message = MIMEMultipart("alternative")
    message["From"] = from_addr
    message["To"] = to_addr
    message["Subject"] = subject
    message.attach(MIMEText(body, "plain", "utf-8"))

    if cfg["smtp_use_tls"]:
        import ssl

        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        smtp = smtplib.SMTP_SSL(cfg["smtp_host"], cfg["smtp_port"], context=context)
    else:
        smtp = smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"])

    with smtp:
        if cfg["smtp_auth"]:
            smtp.login(cfg["address"], cfg["password"])
        smtp.send_message(message)


@app.context_processor
def inject_config():
    cfg = mail_config()
    mailboxes = list_mailboxes()
    return {
        "mail_address": cfg["address"],
        "mailboxes": mailboxes,
        "imap_host_port": os.environ.get("IMAP_HOST_PORT", "1143"),
        "smtp_host_port": os.environ.get("SMTP_HOST_PORT", "25"),
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/")
def inbox():
    try:
        messages, errors = fetch_inbox()
        for error in errors:
            flash(f"Could not load mailbox: {error}", "error")
    except Exception as exc:
        flash(f"Could not load inbox: {exc}", "error")
        messages = []
    return render_template("inbox.html", messages=messages)


@app.get("/message/<path:msg_id>")
def message_view(msg_id):
    try:
        message = fetch_message(msg_id)
    except Exception as exc:
        flash(f"Could not load message: {exc}", "error")
        return redirect(url_for("inbox"))
    if message is None:
        flash("Message not found.", "error")
        return redirect(url_for("inbox"))
    return render_template("message.html", message=message)


@app.route("/send", methods=["GET", "POST"])
def send():
    cfg = mail_config()
    defaults = {
        "from_addr": cfg["address"],
        "to_addr": cfg["address"],
        "subject": "SmartBox test message",
        "body": "Hello from the SmartBox local mail service.",
    }

    if request.method == "POST":
        from_addr = request.form.get("from_addr", "").strip()
        to_addr = request.form.get("to_addr", "").strip()
        subject = request.form.get("subject", "").strip()
        body = request.form.get("body", "")

        if not from_addr or not to_addr:
            flash("From and To addresses are required.", "error")
            return render_template("send.html", **defaults)

        try:
            send_mail(from_addr, to_addr, subject, body)
            flash(f"Sent message to {to_addr} via SMTP.", "success")
            return redirect(url_for("inbox"))
        except Exception as exc:
            flash(f"SMTP send failed: {exc}", "error")
            defaults.update(
                {
                    "from_addr": from_addr,
                    "to_addr": to_addr,
                    "subject": subject,
                    "body": body,
                }
            )

    return render_template("send.html", **defaults)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=os.environ.get("FLASK_DEBUG") == "1")
