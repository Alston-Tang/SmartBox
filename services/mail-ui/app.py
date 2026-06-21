import email
import imaplib
import os
import smtplib
from datetime import datetime
from email.header import decode_header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import parsedate_to_datetime

from flask import Flask, flash, redirect, render_template, request, url_for

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "smartbox-local-mail-dev")


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


def fetch_inbox():
    cfg = mail_config()
    imap = connect_imap(cfg)
    try:
        imap.login(cfg["address"], cfg["password"])
        imap.select("INBOX")
        status, data = imap.search(None, "ALL")
        if status != "OK":
            return []

        ids = data[0].split()
        ids.reverse()
        messages = []

        for msg_id in ids[:100]:
            status, fetched = imap.fetch(msg_id, "(RFC822)")
            if status != "OK" or not fetched or not fetched[0]:
                continue
            raw = fetched[0][1]
            parsed = parse_message(raw)
            parsed["id"] = msg_id.decode()
            messages.append(parsed)

        return messages
    finally:
        try:
            imap.logout()
        except imaplib.IMAP4.error:
            pass


def fetch_message(msg_id):
    cfg = mail_config()
    imap = connect_imap(cfg)
    try:
        imap.login(cfg["address"], cfg["password"])
        imap.select("INBOX")
        status, fetched = imap.fetch(msg_id.encode(), "(RFC822)")
        if status != "OK" or not fetched or not fetched[0]:
            return None
        parsed = parse_message(fetched[0][1])
        parsed["id"] = msg_id
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
    return {
        "mail_address": cfg["address"],
        "imap_host_port": os.environ.get("IMAP_HOST_PORT", "1143"),
        "smtp_host_port": os.environ.get("SMTP_HOST_PORT", "25"),
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/")
def inbox():
    try:
        messages = fetch_inbox()
    except Exception as exc:
        flash(f"Could not load inbox: {exc}", "error")
        messages = []
    return render_template("inbox.html", messages=messages)


@app.get("/message/<msg_id>")
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
