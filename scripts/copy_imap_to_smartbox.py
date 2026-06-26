#!/usr/bin/env python3
"""One-off: copy mail from another IMAP server into a SmartBox folder.

Fetches messages from a source IMAP account and APPENDs them into a folder on
SmartBox (Stalwart). The destination folder is created if it does not exist.

SmartBox defaults come from .env (MAIL_ADDRESS, MAIL_PASSWORD, MAIL_SERVER_IP,
IMAP_HOST_PORT). Source server credentials are passed on the command line.

Examples:

  # Copy source INBOX → SmartBox folder "Imported"
  python3 scripts/copy_imap_to_smartbox.py \\
    --source-host imap.example.com --source-user me@example.com \\
    --source-password secret --dest-folder Imported

  # Copy every selectable folder (maps Junk → "Junk Mail" on SmartBox)
  python3 scripts/copy_imap_to_smartbox.py \\
    --source-host imap.example.com --source-user me@example.com \\
    --source-password secret --all-folders --dest-folder Imported

  # Source on port 1144 with STARTTLS (self-signed certs skipped by default)
  python3 scripts/copy_imap_to_smartbox.py \\
    --source-host imap.example.com --source-port 1144 \\
    --no-source-ssl --source-starttls \\
    --source-user me@example.com --source-password secret \\
    --dest-folder Imported

  # Dry run (list what would be copied)
  python3 scripts/copy_imap_to_smartbox.py ... --dry-run

  # Move: delete from source after a successful append
  python3 scripts/copy_imap_to_smartbox.py ... --move

  # Re-run safely: messages already in the destination folder are skipped automatically
  python3 scripts/copy_imap_to_smartbox.py ... --dest-folder Imported

  # Force a full re-index (ignore local skip cache)
  python3 scripts/copy_imap_to_smartbox.py ... --rebuild-index
"""

from __future__ import annotations

import argparse
import email
import hashlib
import imaplib
import json
import re
import ssl
import sys
import time
from datetime import datetime, timezone
from email.header import decode_header
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple, Union


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


DEFAULT_LOG_EVERY = 500
INDEX_BATCH_SIZE = 250
CACHE_VERSION = 1
FETCH_INDEX = "(RFC822.SIZE BODY.PEEK[HEADER.FIELDS (MESSAGE-ID FROM SUBJECT DATE)])"


def log_progress(
    prefix: str,
    current: int,
    total: int,
    *,
    log_every: int,
    extra: str = "",
) -> None:
    if total <= 0:
        return
    if current != 1 and current != total and current % log_every != 0:
        return
    pct = 100.0 * current / total
    suffix = f" {extra}" if extra else ""
    print(f"  [{current}/{total} ({pct:.1f}%)] {prefix}{suffix}", flush=True)


def dedup_key_kind(key: str) -> str:
    return "message-id" if key.startswith("mid:") else "fingerprint"


def count_key_kinds(keys: Set[str]) -> Tuple[int, int]:
    mid = sum(1 for key in keys if key.startswith("mid:"))
    return mid, len(keys) - mid


def quote_mailbox(name: str) -> str:
    if name.upper() == "INBOX":
        return "INBOX"
    escaped = name.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def insecure_ssl_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def tls_ssl_context(insecure: bool) -> ssl.SSLContext:
    if insecure:
        return insecure_ssl_context()
    return ssl.create_default_context()


def connect(
    host: str,
    port: int,
    *,
    use_ssl: bool = False,
    use_starttls: bool = False,
    insecure: bool = False,
) -> imaplib.IMAP4:
    if use_ssl and use_starttls:
        raise ValueError("use_ssl and use_starttls are mutually exclusive")

    context = None
    if use_ssl or use_starttls:
        context = tls_ssl_context(insecure)

    if use_ssl:
        return imaplib.IMAP4_SSL(host, port, ssl_context=context)

    imap = imaplib.IMAP4(host, port, timeout=120)
    if use_starttls:
        imap.starttls(ssl_context=context)
    return imap


def source_skip_cert_verify(args) -> bool:
    if args.verify_source_cert:
        return False
    if args.insecure or args.source_insecure:
        return True
    # STARTTLS on internal servers often uses self-signed certificates.
    return args.source_starttls


def dest_skip_cert_verify(args) -> bool:
    if args.verify_dest_cert:
        return False
    if args.insecure or args.dest_insecure:
        return True
    return args.dest_starttls


def connection_label(use_ssl: bool, use_starttls: bool, skip_verify: bool) -> str:
    if use_ssl:
        mode = "IMAPS"
    elif use_starttls:
        mode = "STARTTLS"
    else:
        mode = "plaintext"
    if skip_verify and mode != "plaintext":
        mode += ", cert verify off"
    return mode


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


def list_selectable_folders(imap: imaplib.IMAP4) -> List[str]:
    status, data = imap.list()
    if status != "OK" or not data:
        return ["INBOX"]
    folders: List[str] = []
    for line in data:
        name = parse_mailbox_name(line)
        if name:
            folders.append(name)
    return folders or ["INBOX"]


def decode_bytes(chunk: bytes, charset: Optional[str]) -> str:
    """Decode header bytes, falling back when the charset name is invalid."""
    candidates = []
    if charset:
        candidates.append(charset.strip().strip('"').strip("'"))
    candidates.extend(["utf-8", "latin-1"])
    seen = set()
    for encoding in candidates:
        key = encoding.lower()
        if not encoding or key in seen:
            continue
        seen.add(key)
        try:
            return chunk.decode(encoding, errors="replace")
        except LookupError:
            continue
    return chunk.decode("utf-8", errors="replace")


def decode_mime(value: Optional[str]) -> str:
    if not value:
        return ""
    parts = decode_header(value)
    chunks: List[str] = []
    for chunk, charset in parts:
        if isinstance(chunk, bytes):
            chunks.append(decode_bytes(chunk, charset))
        else:
            chunks.append(chunk)
    return "".join(chunks)


def message_internal_date(raw: bytes) -> Optional[bytes]:
    msg = email.message_from_bytes(raw)
    date_raw = msg.get("Date")
    if not date_raw:
        return None
    try:
        dt = parsedate_to_datetime(date_raw)
    except (TypeError, ValueError):
        return None
    return imaplib.Time2Internaldate(dt.timestamp())


def parse_fetch_flags(fetch_line) -> Tuple[bytes, List[str]]:
    """Return (raw_rfc822, imap_flag_list) from a FETCH response item."""
    raw = b""
    flags: List[str] = []
    if not fetch_line or not isinstance(fetch_line, tuple):
        return raw, flags
    for part in fetch_line:
        if isinstance(part, bytes) and len(part) > 100:
            raw = part
        elif isinstance(part, bytes):
            text = part.decode(errors="replace")
            match = re.search(r"FLAGS \(([^)]*)\)", text)
            if match:
                flags = match.group(1).split()
    return raw, flags


def ensure_folder(imap: imaplib.IMAP4, folder: str, dry_run: bool) -> None:
    quoted = quote_mailbox(folder)
    status, _ = imap.select(quoted, readonly=False)
    if status == "OK":
        return
    if dry_run:
        print(f"  [dry-run] would create folder {folder!r}")
        return
    status, response = imap.create(quoted)
    if status != "OK":
        raise RuntimeError(f"Could not create folder {folder!r}: {response}")


def message_dedup_key(msg: email.message.Message, raw_or_size: Union[bytes, int]) -> str:
    """Stable key for skip-if-exists checks (Message-ID, or header + size fingerprint)."""
    message_id = msg.get("Message-ID")
    if message_id:
        return "mid:" + message_id.strip().lower()
    sender = decode_mime(msg.get("From"))
    subject = decode_mime(msg.get("Subject"))
    date = msg.get("Date") or ""
    size = len(raw_or_size) if isinstance(raw_or_size, bytes) else raw_or_size
    return f"fp:{sender}|{subject}|{date}|{size}"


def parse_header_fetch(fetch_line) -> Tuple[email.message.Message, int]:
    header_bytes = b""
    size = 0
    if not fetch_line or not isinstance(fetch_line, tuple):
        return email.message_from_bytes(b""), 0
    for part in fetch_line:
        if not isinstance(part, bytes):
            continue
        text = part.decode(errors="replace")
        size_match = re.search(r"RFC822\.SIZE (\d+)", text)
        if size_match:
            size = int(size_match.group(1))
        if len(part) > 40 and (b"From:" in part or b"Message-ID:" in part or b"Subject:" in part):
            header_bytes = part
    return email.message_from_bytes(header_bytes or b""), size


def default_cache_dir() -> Path:
    return Path(__file__).resolve().parents[1] / ".cache" / "copy_imap"


def cache_file_path(
    cache_dir: Path,
    host: str,
    port: int,
    user: str,
    folder: str,
) -> Path:
    digest = hashlib.sha256(f"{host}\0{port}\0{user}\0{folder}".encode()).hexdigest()[:24]
    safe_user = re.sub(r"[^\w.-]+", "_", user)
    safe_folder = re.sub(r"[^\w.-]+", "_", folder)
    name = f"{safe_user}__{safe_folder}__{digest}.json"
    return cache_dir / name


def read_dedup_cache(path: Path) -> Optional[dict]:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if payload.get("version") != CACHE_VERSION:
        return None
    return payload


def write_dedup_cache(
    path: Path,
    *,
    host: str,
    port: int,
    user: str,
    folder: str,
    message_count: int,
    keys: Set[str],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": CACHE_VERSION,
        "dest": {
            "host": host,
            "port": port,
            "user": user,
            "folder": folder,
        },
        "message_count": message_count,
        "keys": sorted(keys),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)


def folder_message_count(imap: imaplib.IMAP4, folder: str) -> int:
    status, data = imap.select(quote_mailbox(folder), readonly=True)
    if status != "OK" or not data or not data[0]:
        return 0
    return int(data[0])


def index_fetch_items(fetched) -> List[Tuple[email.message.Message, int]]:
    items: List[Tuple[email.message.Message, int]] = []
    if not fetched:
        return items
    for entry in fetched:
        if isinstance(entry, tuple):
            items.append(parse_header_fetch(entry))
    return items


def index_sequence_range(
    dest: imaplib.IMAP4,
    keys: Set[str],
    *,
    seq_start: int,
    seq_end: int,
    total: int,
    log_every: int,
    batch_size: int = INDEX_BATCH_SIZE,
) -> Tuple[int, int]:
    """Index dedup keys for message sequence numbers [seq_start, seq_end]."""
    if seq_start > seq_end:
        return 0, 0

    fetch_failed = 0
    duplicate_keys = 0
    indexed = 0

    for batch_start in range(seq_start, seq_end + 1, batch_size):
        batch_end = min(batch_start + batch_size - 1, seq_end)
        status, fetched = dest.fetch(f"{batch_start}:{batch_end}", FETCH_INDEX)
        if status != "OK" or not fetched:
            for seq in range(batch_start, batch_end + 1):
                status_one, fetched_one = dest.fetch(str(seq), FETCH_INDEX)
                if status_one != "OK" or not fetched_one:
                    fetch_failed += 1
                    continue
                for header_msg, size in index_fetch_items(fetched_one):
                    indexed += 1
                    key = message_dedup_key(header_msg, size)
                    if key in keys:
                        duplicate_keys += 1
                    else:
                        keys.add(key)
            log_progress(
                "indexing destination",
                batch_end,
                total,
                log_every=log_every,
                extra=f"unique={len(keys)} fetch_failed={fetch_failed}",
            )
            continue

        for header_msg, size in index_fetch_items(fetched):
            indexed += 1
            if size == 0 and not header_msg.get("Message-ID") and not header_msg.get("From"):
                fetch_failed += 1
                continue
            key = message_dedup_key(header_msg, size)
            if key in keys:
                duplicate_keys += 1
            else:
                keys.add(key)

        log_progress(
            "indexing destination",
            batch_end,
            total,
            log_every=log_every,
            extra=(
                f"unique={len(keys)} fetch_failed={fetch_failed} "
                f"duplicates={duplicate_keys}"
            ),
        )

    return fetch_failed, duplicate_keys


def load_existing_dedup_keys(
    dest: imaplib.IMAP4,
    dest_folder: str,
    *,
    dest_host: str,
    dest_port: int,
    dest_user: str,
    cache_dir: Path,
    use_cache: bool = True,
    force_rebuild: bool = False,
    log_every: int = DEFAULT_LOG_EVERY,
    batch_size: int = INDEX_BATCH_SIZE,
) -> Set[str]:
    """Collect dedup keys for destination messages, using a local cache when possible."""
    cache_path = cache_file_path(cache_dir, dest_host, dest_port, dest_user, dest_folder)
    print(f"  indexing destination {dest_folder!r} for existing messages...", flush=True)
    started = time.monotonic()

    message_count = folder_message_count(dest, dest_folder)
    if message_count == 0:
        print(f"  destination {dest_folder!r} is empty (nothing to skip)", flush=True)
        if use_cache:
            write_dedup_cache(
                cache_path,
                host=dest_host,
                port=dest_port,
                user=dest_user,
                folder=dest_folder,
                message_count=0,
                keys=set(),
            )
        return set()

    keys: Set[str] = set()
    seq_start = 1
    mode = "full"

    if use_cache and not force_rebuild:
        cached = read_dedup_cache(cache_path)
        if cached and cached.get("dest") == {
            "host": dest_host,
            "port": dest_port,
            "user": dest_user,
            "folder": dest_folder,
        }:
            cached_count = int(cached.get("message_count") or 0)
            cached_keys = set(cached.get("keys") or [])
            if cached_count == message_count:
                mid_count, fp_count = count_key_kinds(cached_keys)
                elapsed = time.monotonic() - started
                print(
                    f"  loaded skip cache {cache_path.name}: "
                    f"{len(cached_keys)} key(s) for {message_count} message(s) "
                    f"({mid_count} message-id, {fp_count} fingerprint) in {elapsed:.2f}s",
                    flush=True,
                )
                return cached_keys
            if cached_count < message_count:
                keys = cached_keys
                seq_start = cached_count + 1
                mode = "incremental"
                print(
                    f"  cache hit ({len(keys)} key(s) for {cached_count} message(s)); "
                    f"indexing {message_count - cached_count} new message(s)...",
                    flush=True,
                )
            else:
                print(
                    f"  cache stale (had {cached_count}, folder now {message_count}); "
                    f"rebuilding index...",
                    flush=True,
                )

    if mode == "full":
        print(
            f"  scanning {message_count} message(s) in {dest_folder!r} "
            f"(batch size {batch_size})...",
            flush=True,
        )

    fetch_failed, duplicate_keys = index_sequence_range(
        dest,
        keys,
        seq_start=seq_start,
        seq_end=message_count,
        total=message_count,
        log_every=log_every,
        batch_size=batch_size,
    )

    mid_count, fp_count = count_key_kinds(keys)
    elapsed = time.monotonic() - started
    print(
        f"  index complete ({mode}) for {dest_folder!r}: "
        f"{len(keys)} unique key(s) for {message_count} message(s) "
        f"({mid_count} message-id, {fp_count} fingerprint, "
        f"{fetch_failed} fetch failed, {duplicate_keys} duplicate keys) "
        f"in {elapsed:.1f}s",
        flush=True,
    )

    if use_cache:
        write_dedup_cache(
            cache_path,
            host=dest_host,
            port=dest_port,
            user=dest_user,
            folder=dest_folder,
            message_count=message_count,
            keys=keys,
        )
        print(f"  saved skip cache to {cache_path}", flush=True)

    return keys


def save_dedup_cache_from_dest(
    dest: imaplib.IMAP4,
    dest_folder: str,
    keys: Set[str],
    *,
    dest_host: str,
    dest_port: int,
    dest_user: str,
    cache_dir: Path,
) -> None:
    message_count = folder_message_count(dest, dest_folder)
    cache_path = cache_file_path(cache_dir, dest_host, dest_port, dest_user, dest_folder)
    write_dedup_cache(
        cache_path,
        host=dest_host,
        port=dest_port,
        user=dest_user,
        folder=dest_folder,
        message_count=message_count,
        keys=keys,
    )


def reconnect_dest(dest_factory: Callable[[], imaplib.IMAP4], dest_folder: str) -> imaplib.IMAP4:
    imap = dest_factory()
    ensure_folder(imap, dest_folder, dry_run=False)
    status, _ = imap.select(quote_mailbox(dest_folder), readonly=False)
    if status != "OK":
        raise RuntimeError(f"Could not reopen destination folder {dest_folder!r}")
    return imap


def append_message(
    dest: imaplib.IMAP4,
    dest_quoted: str,
    raw: bytes,
    flags: List[str],
    internal_date: Optional[bytes],
) -> Tuple[str, object]:
    flag_str = " ".join(flags) if flags else None
    if flag_str:
        return dest.append(dest_quoted, flag_str, internal_date, raw)
    return dest.append(dest_quoted, None, internal_date, raw)


def copy_folder(
    source: imaplib.IMAP4,
    dest: imaplib.IMAP4,
    source_folder: str,
    dest_folder: str,
    *,
    dry_run: bool,
    move: bool,
    limit: Optional[int],
    existing_keys: Optional[Set[str]] = None,
    dest_factory: Optional[Callable[[], imaplib.IMAP4]] = None,
    skip_existing: bool = True,
    log_every: int = DEFAULT_LOG_EVERY,
    dest_host: str = "",
    dest_port: int = 143,
    dest_user: str = "",
    cache_dir: Optional[Path] = None,
    use_cache: bool = True,
    force_rebuild: bool = False,
) -> Tuple[int, int, int, imaplib.IMAP4]:
    """Copy messages from source_folder to dest_folder.

    Returns (copied, skipped, failed, dest_imap). dest_imap may be reconnected.
    """
    status, _ = source.select(quote_mailbox(source_folder), readonly=not move)
    if status != "OK":
        raise RuntimeError(f"Could not open source folder {source_folder!r}")

    status, data = source.search(None, "ALL")
    if status != "OK" or not data or not data[0]:
        print(f"  {source_folder!r}: no messages")
        return 0, 0, 0, dest

    msg_ids = data[0].split()
    total = len(msg_ids)
    if limit is not None:
        msg_ids = msg_ids[:limit]
    print(f"  {source_folder!r}: {len(msg_ids)} of {total} message(s) → {dest_folder!r}")

    if dry_run:
        for msg_id in msg_ids:
            status, fetched = source.fetch(msg_id, "(RFC822.HEADER)")
            if status != "OK" or not fetched or not fetched[0]:
                continue
            header = fetched[0][1]
            msg = email.message_from_bytes(header)
            subject = decode_mime(msg.get("Subject")) or "(no subject)"
            sender = decode_mime(msg.get("From")) or "(unknown)"
            print(f"    would copy uid={msg_id.decode()} from={sender} subject={subject}")
        return len(msg_ids), 0, 0, dest

    keys: Set[str] = existing_keys if existing_keys is not None else set()
    if skip_existing:
        if len(keys) == 0:
            print(f"  building skip index from destination {dest_folder!r}...", flush=True)
            keys.update(
                load_existing_dedup_keys(
                    dest,
                    dest_folder,
                    dest_host=dest_host,
                    dest_port=dest_port,
                    dest_user=dest_user,
                    cache_dir=cache_dir or default_cache_dir(),
                    use_cache=use_cache,
                    force_rebuild=force_rebuild,
                    log_every=log_every,
                )
            )
        else:
            mid_count, fp_count = count_key_kinds(keys)
            print(
                f"  reusing skip index: {len(keys)} key(s) "
                f"({mid_count} message-id, {fp_count} fingerprint)",
                flush=True,
            )
    else:
        print("  skip-existing disabled; all source messages will be appended", flush=True)

    ensure_folder(dest, dest_folder, dry_run=False)
    status, _ = dest.select(quote_mailbox(dest_folder), readonly=False)
    if status != "OK":
        raise RuntimeError(f"Could not open destination folder {dest_folder!r}")

    copied = skipped = failed = 0
    dest_quoted = quote_mailbox(dest_folder)
    skip_detail_limit = 5
    loop_started = time.monotonic()

    print(
        f"  processing {len(msg_ids)} source message(s) "
        f"(skip index size={len(keys)}, log every {log_every})...",
        flush=True,
    )

    for index, msg_id in enumerate(msg_ids, start=1):
        uid = msg_id.decode()
        status, fetched = source.fetch(msg_id, "(RFC822 FLAGS)")
        if status != "OK" or not fetched or not fetched[0]:
            failed += 1
            print(f"    fetch failed uid={uid}", file=sys.stderr)
            log_progress(
                f"uid={uid}",
                index,
                len(msg_ids),
                log_every=log_every,
                extra=f"copied={copied} skipped={skipped} failed={failed}",
            )
            continue

        raw, flags = parse_fetch_flags(fetched[0])
        if not raw:
            failed += 1
            print(f"    empty body uid={uid}", file=sys.stderr)
            log_progress(
                f"uid={uid}",
                index,
                len(msg_ids),
                log_every=log_every,
                extra=f"copied={copied} skipped={skipped} failed={failed}",
            )
            continue

        msg = email.message_from_bytes(raw)
        subject = decode_mime(msg.get("Subject")) or "(no subject)"
        dedup_key = message_dedup_key(msg, raw)

        if skip_existing and dedup_key in keys:
            skipped += 1
            if skipped <= skip_detail_limit:
                print(
                    f"    skip uid={uid} ({dedup_key_kind(dedup_key)}) "
                    f"already in {dest_folder!r}: {subject!r}",
                    flush=True,
                )
            log_progress(
                "scanning source",
                index,
                len(msg_ids),
                log_every=log_every,
                extra=f"copied={copied} skipped={skipped} failed={failed}",
            )
            continue

        internal_date = message_internal_date(raw)

        def do_append(active_dest: imaplib.IMAP4):
            return append_message(active_dest, dest_quoted, raw, flags, internal_date)

        try:
            append_status, append_response = do_append(dest)
        except (imaplib.IMAP4.error, ConnectionResetError, BrokenPipeError, OSError) as exc:
            if dest_factory is None:
                failed += 1
                print(
                    f"    append failed uid={msg_id.decode()} subject={subject!r}: {exc}",
                    file=sys.stderr,
                )
                continue
            print(f"    destination connection lost, reconnecting...", file=sys.stderr)
            try:
                try:
                    dest.logout()
                except Exception:
                    pass
                dest = reconnect_dest(dest_factory, dest_folder)
                append_status, append_response = do_append(dest)
            except Exception as retry_exc:
                failed += 1
                print(
                    f"    append failed uid={msg_id.decode()} subject={subject!r}: {retry_exc}",
                    file=sys.stderr,
                )
                continue

        if append_status != "OK":
            failed += 1
            print(
                f"    append failed uid={msg_id.decode()} subject={subject!r}: {append_response}",
                file=sys.stderr,
            )
            continue

        keys.add(dedup_key)
        copied += 1
        print(
            f"    copy uid={uid} ({dedup_key_kind(dedup_key)}) subject={subject!r}",
            flush=True,
        )
        log_progress(
            "scanning source",
            index,
            len(msg_ids),
            log_every=log_every,
            extra=f"copied={copied} skipped={skipped} failed={failed}",
        )

        if move:
            source.store(msg_id, "+FLAGS", "\\Deleted")

    if move and copied:
        source.expunge()

    elapsed = time.monotonic() - loop_started
    print(
        f"  folder summary {source_folder!r} → {dest_folder!r}: "
        f"copied={copied} skipped={skipped} failed={failed} "
        f"processed={len(msg_ids)} skip_index={len(keys)} "
        f"in {elapsed:.1f}s",
        flush=True,
    )

    return copied, skipped, failed, dest


def smartbox_defaults(env: Dict[str, str]) -> Dict[str, object]:
    address = env.get("MAIL_ADDRESS", "default@mail.thm64.com")
    server_ip = env.get("MAIL_SERVER_IP", "127.0.0.1")
    use_ssl = env.get("IMAP_USE_SSL", "false").lower() == "true"
    return {
        "host": env.get("DEST_IMAP_HOST", env.get("IMAP_HOST", server_ip)),
        "port": int(env.get("DEST_IMAP_PORT", env.get("IMAP_HOST_PORT", "1143"))),
        "use_ssl": use_ssl,
        "username": address,
        "password": env.get("MAIL_PASSWORD", ""),
    }


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    env = load_dotenv(root / ".env")
    dest_defaults = smartbox_defaults(env)

    parser = argparse.ArgumentParser(
        description="Copy mail from another IMAP server into a SmartBox folder."
    )
    parser.add_argument("--source-host", required=True, help="Source IMAP host")
    parser.add_argument("--source-port", type=int, default=993, help="Source IMAP port (default: 993)")
    parser.add_argument("--source-user", required=True, help="Source IMAP username")
    parser.add_argument("--source-password", required=True, help="Source IMAP password")
    parser.add_argument(
        "--source-folder",
        default="INBOX",
        help="Source folder to copy (default: INBOX). Ignored if --all-folders is set.",
    )
    parser.add_argument(
        "--all-folders",
        action="store_true",
        help="Copy every selectable folder on the source account",
    )
    parser.add_argument(
        "--dest-folder",
        default="Imported",
        help='SmartBox destination folder (default: "Imported"; created if missing)',
    )
    parser.add_argument("--dest-host", default=dest_defaults["host"], help="SmartBox IMAP host")
    parser.add_argument("--dest-port", type=int, default=dest_defaults["port"], help="SmartBox IMAP port")
    parser.add_argument("--dest-user", default=dest_defaults["username"], help="SmartBox login")
    parser.add_argument("--dest-password", default=dest_defaults["password"], help="SmartBox password")
    parser.add_argument(
        "--source-ssl",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Use IMAPS for source (default: on)",
    )
    parser.add_argument(
        "--source-starttls",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Upgrade source connection with STARTTLS (use with --no-source-ssl; typical port 1144)",
    )
    parser.add_argument(
        "--dest-ssl",
        action=argparse.BooleanOptionalAction,
        default=dest_defaults["use_ssl"],
        help="Use IMAPS for SmartBox (default: from .env, usually off)",
    )
    parser.add_argument(
        "--dest-starttls",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Upgrade SmartBox connection with STARTTLS (use with --no-dest-ssl)",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Skip TLS certificate verification on both source and destination",
    )
    parser.add_argument(
        "--source-insecure",
        action="store_true",
        help="Skip TLS certificate verification on the source (IMAPS/STARTTLS)",
    )
    parser.add_argument(
        "--dest-insecure",
        action="store_true",
        help="Skip TLS certificate verification on SmartBox (IMAPS/STARTTLS)",
    )
    parser.add_argument(
        "--verify-source-cert",
        action="store_true",
        help="Verify source TLS certificate (default: skip when using --source-starttls)",
    )
    parser.add_argument(
        "--verify-dest-cert",
        action="store_true",
        help="Verify SmartBox TLS certificate (default: skip when using --dest-starttls)",
    )
    parser.add_argument(
        "--no-skip-existing",
        action="store_true",
        help="Copy even when a matching Message-ID (or fingerprint) is already in the destination",
    )
    parser.add_argument(
        "--move",
        action="store_true",
        help="Delete from source after each successful append (IMAP move)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List messages that would be copied without writing to SmartBox",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum messages per folder (for testing)",
    )
    parser.add_argument(
        "--log-every",
        type=int,
        default=DEFAULT_LOG_EVERY,
        metavar="N",
        help=f"Progress log interval while indexing/skipping/copying (default: {DEFAULT_LOG_EVERY})",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=default_cache_dir(),
        help="Directory for local skip-index cache files (default: .cache/copy_imap)",
    )
    parser.add_argument(
        "--rebuild-index",
        action="store_true",
        help="Ignore local skip cache and re-index the destination folder",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Do not read or write the local skip-index cache",
    )
    args = parser.parse_args()

    if args.source_ssl and args.source_starttls:
        print("Use either --source-ssl or --source-starttls, not both.", file=sys.stderr)
        return 1
    if args.dest_ssl and args.dest_starttls:
        print("Use either --dest-ssl or --dest-starttls, not both.", file=sys.stderr)
        return 1

    if not args.dest_password:
        print("SmartBox password missing: set MAIL_PASSWORD in .env or pass --dest-password", file=sys.stderr)
        return 1

    source_insecure = source_skip_cert_verify(args)
    dest_insecure = dest_skip_cert_verify(args)

    print(
        f"Source: {args.source_user}@{args.source_host}:{args.source_port} "
        f"({connection_label(args.source_ssl, args.source_starttls, source_insecure)})"
    )
    print(
        f"Dest:   {args.dest_user}@{args.dest_host}:{args.dest_port} → {args.dest_folder!r} "
        f"({connection_label(args.dest_ssl, args.dest_starttls, dest_insecure)})"
    )
    if args.dry_run:
        print("Mode:   dry-run")
    if args.move:
        print("Mode:   move (delete from source after copy)")
    skip_existing = not args.no_skip_existing
    if skip_existing:
        print(f"Mode:   skip existing (log every {args.log_every} messages)")
        if args.no_cache:
            print("Mode:   skip cache disabled")
        elif args.rebuild_index:
            print("Mode:   rebuilding skip index (ignoring cache)")
        else:
            print(f"Mode:   skip cache in {args.cache_dir}")
    else:
        print("Mode:   no skip (all messages appended)")

    try:
        source = connect(
            args.source_host,
            args.source_port,
            use_ssl=args.source_ssl,
            use_starttls=args.source_starttls,
            insecure=source_insecure,
        )
        source.login(args.source_user, args.source_password)
    except ssl.SSLCertVerificationError as exc:
        print(f"Source login failed: {exc}", file=sys.stderr)
        print(
            "Tip: pass --source-insecure or use --source-starttls (skips verify by default).",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:
        err = str(exc)
        print(f"Source login failed: {exc}", file=sys.stderr)
        if "CERTIFICATE_VERIFY_FAILED" in err or "certificate verify failed" in err.lower():
            print(
                "Tip: pass --source-insecure or use --source-starttls (skips verify by default).",
                file=sys.stderr,
            )
        return 1

    dest = None
    existing_keys: Set[str] = set()
    cache_dir = args.cache_dir
    use_cache = not args.no_cache
    force_rebuild = args.rebuild_index

    def open_dest() -> imaplib.IMAP4:
        imap = connect(
            args.dest_host,
            args.dest_port,
            use_ssl=args.dest_ssl,
            use_starttls=args.dest_starttls,
            insecure=dest_insecure,
        )
        imap.login(args.dest_user, args.dest_password)
        return imap

    if not args.dry_run:
        try:
            dest = open_dest()
        except Exception as exc:
            print(f"SmartBox login failed: {exc}", file=sys.stderr)
            try:
                source.logout()
            except imaplib.IMAP4.error:
                pass
            return 1

    total_copied = total_skipped = total_failed = 0
    try:
        if args.all_folders:
            folders = list_selectable_folders(source)
            print(f"Source folders: {', '.join(folders)}")
            for folder in folders:
                if args.dry_run:
                    c, s, f, _ = copy_folder(
                        source,
                        source,
                        folder,
                        args.dest_folder,
                        dry_run=True,
                        move=False,
                        limit=args.limit,
                    )
                else:
                    c, s, f, dest = copy_folder(
                        source,
                        dest,
                        folder,
                        args.dest_folder,
                        dry_run=False,
                        move=args.move,
                        limit=args.limit,
                        existing_keys=existing_keys,
                        dest_factory=open_dest,
                        skip_existing=skip_existing,
                        log_every=args.log_every,
                        dest_host=args.dest_host,
                        dest_port=args.dest_port,
                        dest_user=args.dest_user,
                        cache_dir=cache_dir,
                        use_cache=use_cache,
                        force_rebuild=force_rebuild,
                    )
                total_copied += c
                total_skipped += s
                total_failed += f
        else:
            if args.dry_run:
                c, s, f, _ = copy_folder(
                    source,
                    source,
                    args.source_folder,
                    args.dest_folder,
                    dry_run=True,
                    move=False,
                    limit=args.limit,
                )
            else:
                c, s, f, dest = copy_folder(
                    source,
                    dest,
                    args.source_folder,
                    args.dest_folder,
                    dry_run=False,
                    move=args.move,
                    limit=args.limit,
                    existing_keys=existing_keys,
                    dest_factory=open_dest,
                    skip_existing=skip_existing,
                    log_every=args.log_every,
                    dest_host=args.dest_host,
                    dest_port=args.dest_port,
                    dest_user=args.dest_user,
                    cache_dir=cache_dir,
                    use_cache=use_cache,
                    force_rebuild=force_rebuild,
                )
            total_copied += c
            total_skipped += s
            total_failed += f
    finally:
        try:
            source.logout()
        except imaplib.IMAP4.error:
            pass
        if dest is not None:
            if skip_existing and use_cache and not args.dry_run and existing_keys:
                try:
                    save_dedup_cache_from_dest(
                        dest,
                        args.dest_folder,
                        existing_keys,
                        dest_host=args.dest_host,
                        dest_port=args.dest_port,
                        dest_user=args.dest_user,
                        cache_dir=cache_dir,
                    )
                except Exception as exc:
                    print(f"Warning: could not update skip cache: {exc}", file=sys.stderr)
            try:
                dest.logout()
            except imaplib.IMAP4.error:
                pass

    print(f"\nDone: {total_copied} copied, {total_skipped} skipped (already present), {total_failed} failed.")
    return 0 if total_failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
