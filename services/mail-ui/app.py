import base64
import email
import imaplib
import os
import re
import smtplib
from email.header import decode_header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import parsedate_to_datetime
from typing import List, Optional, Tuple
from urllib.parse import unquote

from flask import Flask, flash, redirect, render_template, request, url_for

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "smartbox-local-mail-dev")

INBOX_LIMIT = 100
DEFAULT_FOLDER = "INBOX"


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


def connect_imap(cfg):
    if cfg["imap_use_ssl"]:
        import ssl

        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return imaplib.IMAP4_SSL(cfg["imap_host"], cfg["imap_port"], ssl_context=context)
    return imaplib.IMAP4(cfg["imap_host"], cfg["imap_port"])


def quote_mailbox(name: str) -> str:
    """Quote mailbox names for IMAP SELECT (required for names containing spaces)."""
    if name.upper() == "INBOX":
        return "INBOX"
    escaped = name.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def parse_mailbox_name(line: bytes) -> Optional[str]:
    text = line.decode(errors="replace")
    if ")" not in text:
        return None
    flags = text[1 : text.index(")")]
    if "Noselect" in flags or "\\Noselect" in flags:
        return None
    match = re.search(r'"([^"]*)"\s*$', text)
    if match:
        name = match.group(1)
        return name or None
    return text.split()[-1].strip('"')


def sort_folders(folders: List[str]) -> List[str]:
    def sort_key(name: str):
        upper = name.upper()
        if upper == "INBOX":
            return (0, name.lower())
        if upper in {"JUNK", "SPAM"}:
            return (1, name.lower())
        if upper in {"TRASH", "DELETED"}:
            return (2, name.lower())
        if upper in {"SENT", "SENT ITEMS", "SENT MESSAGES"}:
            return (3, name.lower())
        if upper in {"DRAFTS", "DRAFT"}:
            return (4, name.lower())
        return (5, name.lower())

    return sorted(dict.fromkeys(folders), key=sort_key)


def with_imap(cfg):
    imap = connect_imap(cfg)
    imap.login(cfg["address"], cfg["password"])
    return imap


def list_folders(cfg=None) -> List[str]:
    cfg = cfg or mail_config()
    imap = with_imap(cfg)
    try:
        status, data = imap.list()
        if status != "OK" or not data:
            return [DEFAULT_FOLDER]
        folders = []
        for line in data:
            name = parse_mailbox_name(line)
            if name:
                folders.append(name)
        return sort_folders(folders) or [DEFAULT_FOLDER]
    finally:
        try:
            imap.logout()
        except imaplib.IMAP4.error:
            pass


def resolve_folder(requested: Optional[str], folders: List[str]) -> str:
    if not folders:
        return DEFAULT_FOLDER
    if not requested:
        return DEFAULT_FOLDER if DEFAULT_FOLDER in folders else folders[0]
    for folder in folders:
        if folder == requested:
            return folder
    requested_upper = requested.upper()
    for folder in folders:
        if folder.upper() == requested_upper:
            return folder
    return DEFAULT_FOLDER if DEFAULT_FOLDER in folders else folders[0]


def encode_message_ref(folder: str, imap_id: str) -> str:
    raw = f"{folder}\x00{imap_id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_message_ref(ref: str) -> Tuple[str, str]:
    padded = ref + "=" * (-len(ref) % 4)
    folder, imap_id = base64.urlsafe_b64decode(padded).decode().split("\x00", 1)
    return folder, imap_id


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


def fetch_messages(folder: str, limit: int = INBOX_LIMIT):
    cfg = mail_config()
    folders = list_folders(cfg)
    folder = resolve_folder(folder, folders)
    imap = with_imap(cfg)
    try:
        status, _ = imap.select(quote_mailbox(folder), readonly=True)
        if status != "OK":
            raise RuntimeError(f"Could not open folder {folder}")

        status, data = imap.search(None, "ALL")
        if status != "OK":
            return [], folders, folder

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
            parsed["id"] = encode_message_ref(folder, imap_id)
            parsed["folder"] = folder
            messages.append(parsed)

        return messages, folders, folder
    finally:
        try:
            imap.logout()
        except imaplib.IMAP4.error:
            pass


def fetch_message(ref: str):
    folder, imap_id = decode_message_ref(unquote(ref))
    cfg = mail_config()
    folders = list_folders(cfg)
    folder = resolve_folder(folder, folders)

    imap = with_imap(cfg)
    try:
        status, _ = imap.select(quote_mailbox(folder), readonly=True)
        if status != "OK":
            return None
        status, fetched = imap.fetch(imap_id.encode(), "(RFC822)")
        if status != "OK" or not fetched or not fetched[0]:
            return None
        parsed = parse_message(fetched[0][1])
        parsed["id"] = ref
        parsed["folder"] = folder
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
    try:
        folders = list_folders(cfg)
    except Exception:
        folders = [DEFAULT_FOLDER]

    current_folder = resolve_folder(request.args.get("folder"), folders)
    if request.endpoint == "message_view" and request.view_args:
        try:
            folder, _ = decode_message_ref(unquote(request.view_args.get("msg_id", "")))
            current_folder = resolve_folder(folder, folders)
        except Exception:
            pass

    return {
        "mail_address": cfg["address"],
        "folders": folders,
        "current_folder": current_folder,
        "imap_host_port": os.environ.get("IMAP_HOST_PORT", "1143"),
        "smtp_host_port": os.environ.get("SMTP_HOST_PORT", "25"),
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/")
def inbox():
    requested_folder = request.args.get("folder", DEFAULT_FOLDER)
    try:
        messages, folders, folder = fetch_messages(requested_folder)
    except Exception as exc:
        flash(f"Could not load folder: {exc}", "error")
        messages = []
        try:
            folders = list_folders()
        except Exception:
            folders = [DEFAULT_FOLDER]
        folder = resolve_folder(requested_folder, folders)
    return render_template(
        "inbox.html",
        messages=messages,
        folders=folders,
        current_folder=folder,
    )


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
