#!/usr/bin/env python3
"""
receipt_archiver.py

Scan Thunderbird mbox-format archive files (e.g. Archive.sbd/2025.sbd/2025-02),
detect emails that look like POS-system receipts (Square, Toast, Clover,
Shopify, Stripe, etc.), and render the HTML body of each match to a PDF with
hyperlinks stripped. The PDF's CreationDate/ModDate metadata (and the file's
own mtime) are set to the email's Date header.

Thunderbird's ".msf" files are Mork-format folder indexes -- they are not
needed here because the mbox files themselves already contain the full
message data. This tool only ever reads the extensionless mbox files
(it verifies each file starts with a "From " mbox marker before touching it),
so .msf files are naturally skipped.

Usage:
    python3 receipt_archiver.py --input ~/Mail/Archive.sbd --output ~/receipts
    python3 receipt_archiver.py --input ~/Mail/Archive.sbd --output ~/receipts --dry-run -v
    python3 receipt_archiver.py --input ~/Mail/Archive.sbd --output ~/receipts --strict

Requires: beautifulsoup4, lxml, weasyprint, pikepdf
    pip install --break-system-packages beautifulsoup4 lxml weasyprint pikepdf
"""

import argparse
import binascii
import hashlib
import json
import logging
import mailbox
import os
import re
import sys
from datetime import datetime, timezone
from email.errors import HeaderParseError
from email.header import decode_header
from email.message import Message
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path

log = logging.getLogger("receipt_archiver")

DEFAULT_CONFIG = Path(__file__).with_name("senders.json")


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

def load_config(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)
    cfg.setdefault("sender_patterns", [])
    cfg.setdefault("subject_keywords", [])
    cfg["sender_patterns"] = [s.lower() for s in cfg["sender_patterns"]]
    cfg["subject_keywords"] = [s.lower() for s in cfg["subject_keywords"]]
    return cfg


# --------------------------------------------------------------------------
# Discovering mbox files under a Thunderbird archive root
# --------------------------------------------------------------------------

def looks_like_mbox(path: Path) -> bool:
    """Thunderbird mbox archive files have no extension and start with a
    'From ' envelope line. .msf index files are Mork format and will fail
    this check, so they're skipped automatically."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(5)
        return head == b"From "
    except OSError:
        return False


def find_mbox_files(root: Path):
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if name.endswith(".msf") or name.endswith(".dat") or name.startswith("."):
                continue
            p = Path(dirpath) / name
            if looks_like_mbox(p):
                yield p


# --------------------------------------------------------------------------
# Message inspection helpers
# --------------------------------------------------------------------------

def hstr(value, default: str = "") -> str:
    """Coerce a header value to a plain str.

    email.message.Message.get() is documented to return a str, but on
    certain malformed/folded encoded-word headers the compat32 policy
    hands back an email.header.Header instance instead, which doesn't
    support str methods like .lower(). Route everything through this.
    """
    if value is None:
        return default
    return str(value)


def decode_mime_header(raw) -> str:
    raw = hstr(raw)
    if not raw:
        return ""
    try:
        parts = decode_header(raw)
    except (HeaderParseError, ValueError, binascii.Error) as e:
        # Real-world mail sometimes has malformed encoded-words (bad base64
        # padding, truncated quoted-printable, etc). decode_header() raises
        # on these instead of degrading gracefully -- fall back to the raw
        # header text rather than aborting the whole scan over one bad
        # header.
        log.debug("Malformed MIME header %r (%s); using raw text", raw[:80], e)
        return raw
    out = []
    for text, enc in parts:
        if isinstance(text, bytes):
            try:
                out.append(text.decode(enc or "utf-8", errors="replace"))
            except (LookupError, TypeError):
                out.append(text.decode("utf-8", errors="replace"))
        else:
            out.append(text)
    return "".join(out)


def get_message_datetime(msg: Message, fallback_mtime: float) -> datetime:
    raw_date = hstr(msg.get("Date")) or None
    if raw_date:
        try:
            dt = parsedate_to_datetime(raw_date)
            if dt is not None:
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
        except (TypeError, ValueError):
            pass
    return datetime.fromtimestamp(fallback_mtime, tz=timezone.utc)


def is_receipt(msg: Message, cfg: dict, strict: bool) -> bool:
    from_header = hstr(msg.get("From")).lower()
    subject = decode_mime_header(msg.get("Subject")).lower()

    sender_hit = any(pat in from_header for pat in cfg["sender_patterns"])
    subject_hit = any(kw in subject for kw in cfg["subject_keywords"])

    if strict:
        return sender_hit
    return sender_hit or (sender_hit is False and subject_hit and looks_transactional(from_header))


def looks_transactional(from_header: str) -> bool:
    """Cheap heuristic to avoid matching every newsletter that happens to
    contain the word 'receipt' in its subject: require the sender address
    itself to look automated (no-reply / receipts / orders / billing)."""
    markers = ("noreply", "no-reply", "receipt", "receipts", "orders",
               "order", "billing", "payments", "payment", "notifications")
    return any(m in from_header for m in markers)


def get_html_and_inline_images(msg: Message):
    """Return (html_string, cid_to_data_uri_map) for the first text/html part
    found in the message, or (None, {}) if there isn't one."""
    html = None
    html_charset = None
    cid_map = {}

    if msg.is_multipart():
        parts = msg.walk()
    else:
        parts = [msg]

    for part in parts:
        content_type = part.get_content_type()
        disposition = hstr(part.get("Content-Disposition"))

        if content_type == "text/html" and html is None and "attachment" not in disposition:
            payload = part.get_payload(decode=True)
            if payload is not None:
                html_charset = part.get_content_charset() or "utf-8"
                try:
                    html = payload.decode(html_charset, errors="replace")
                except (LookupError, TypeError):
                    html = payload.decode("utf-8", errors="replace")

        cid = hstr(part.get("Content-ID"))
        if cid and content_type.startswith("image/"):
            payload = part.get_payload(decode=True)
            if payload:
                import base64
                b64 = base64.b64encode(payload).decode("ascii")
                cid_clean = cid.strip("<>")
                cid_map[cid_clean] = f"data:{content_type};base64,{b64}"

    return html, cid_map


# --------------------------------------------------------------------------
# HTML cleanup: inline cid: images, strip hyperlinks, drop remote trackers
# --------------------------------------------------------------------------

def clean_html(html: str, cid_map: dict, strip_remote_images: bool) -> str:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")

    # Resolve cid: image references to embedded data URIs.
    for img in soup.find_all("img"):
        src = img.get("src", "")
        if src.startswith("cid:"):
            cid = src[4:]
            if cid in cid_map:
                img["src"] = cid_map[cid]
            else:
                img.decompose()
        elif strip_remote_images and src.startswith(("http://", "https://")):
            # Most remote images in receipt emails are tracking pixels or
            # logos hotlinked from the sender's CDN; drop them so the PDF
            # doesn't depend on network access and doesn't leak a read
            # receipt when later opened.
            img.decompose()

    # Strip all hyperlinks, keeping the visible text.
    for a in soup.find_all("a"):
        a.unwrap()

    # Belt-and-suspenders: remove any other clickable/tracking constructs.
    for tag in soup.find_all(["base"]):
        tag.decompose()
    for tag in soup.find_all(True):
        for attr in ("onclick",):
            if tag.has_attr(attr):
                del tag[attr]

    return str(soup)


# --------------------------------------------------------------------------
# PDF rendering + metadata
# --------------------------------------------------------------------------

def render_pdf(html: str, out_path: Path):
    from weasyprint import HTML
    HTML(string=html, base_url=None).write_pdf(str(out_path))


def pdf_date(dt: datetime) -> str:
    dt = dt.astimezone()  # local tz for a human-readable offset
    offset = dt.strftime("%z")  # e.g. -0700
    if offset:
        offset = f"{offset[:3]}'{offset[3:]}'"
    else:
        offset = "Z"
    return f"D:{dt.strftime('%Y%m%d%H%M%S')}{offset}"


def stamp_pdf_metadata(pdf_path: Path, dt: datetime, subject: str, sender: str):
    import pikepdf
    with pikepdf.open(str(pdf_path), allow_overwriting_input=True) as pdf:
        info = pdf.docinfo
        pdfdate = pdf_date(dt)
        info["/CreationDate"] = pikepdf.String(pdfdate)
        info["/ModDate"] = pikepdf.String(pdfdate)
        info["/Title"] = pikepdf.String(subject or "Receipt")
        info["/Author"] = pikepdf.String(sender or "")
        info["/Producer"] = pikepdf.String("receipt_archiver.py")
        pdf.save(str(pdf_path))

    # Also set the filesystem timestamps to match, since that's often what
    # people actually browse/sort by.
    ts = dt.timestamp()
    os.utime(pdf_path, (ts, ts))


# --------------------------------------------------------------------------
# Naming / filesystem
# --------------------------------------------------------------------------

def slugify(text: str, max_len: int = 60) -> str:
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE).strip().lower()
    text = re.sub(r"[\s_-]+", "-", text)
    return text[:max_len].strip("-") or "message"


def sender_domain(from_header: str) -> str:
    _, addr = parseaddr(from_header or "")
    if "@" in addr:
        return addr.split("@", 1)[1].lower()
    return "unknown"


def build_output_path(out_root: Path, dt: datetime, from_header: str, subject: str, msg_id: str) -> Path:
    year_dir = out_root / f"{dt:%Y}" / f"{dt:%m}"
    domain = slugify(sender_domain(from_header), 30)
    subj = slugify(decode_mime_header(subject), 50)
    short_hash = hashlib.sha1(msg_id.encode("utf-8", errors="replace")).hexdigest()[:8]
    filename = f"{dt:%Y-%m-%d_%H%M%S}_{domain}_{subj}_{short_hash}.pdf"
    return year_dir / filename


# --------------------------------------------------------------------------
# State (dedup across runs)
# --------------------------------------------------------------------------

def load_state(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log.warning("State file %s is corrupt, starting fresh", path)
    return {}


def save_state(path: Path, state: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


# --------------------------------------------------------------------------
# Main processing
# --------------------------------------------------------------------------

def process_mbox_file(mbox_path: Path, cfg: dict, out_root: Path, state: dict,
                       strict: bool, dry_run: bool, strip_remote_images: bool,
                       force: bool) -> int:
    count = 0
    box = mailbox.mbox(str(mbox_path), factory=None, create=False)
    try:
        mtime = mbox_path.stat().st_mtime
        for key in box.keys():
            try:
                msg = box.get_message(key)
            except Exception as e:  # noqa: BLE001 - keep scanning on malformed msgs
                log.warning("Skipping unreadable message in %s (key %s): %s", mbox_path, key, e)
                continue

            try:
                if not is_receipt(msg, cfg, strict):
                    continue

                msg_id = hstr(msg.get("Message-ID")) or f"{mbox_path}:{key}"
                if not force and msg_id in state:
                    log.debug("Already processed %s, skipping", msg_id)
                    continue

                subject = decode_mime_header(msg.get("Subject")) or "(no subject)"
                from_header = hstr(msg.get("From")) or "(unknown sender)"
                dt = get_message_datetime(msg, mtime)

                html, cid_map = get_html_and_inline_images(msg)
                if html is None:
                    log.info("MATCH (no HTML body, skipped) | %s | %s | %s", dt.isoformat(), from_header, subject)
                    continue

                log.info("MATCH | %s | %s | %s", dt.isoformat(), from_header, subject)
                count += 1

                if dry_run:
                    continue

                cleaned = clean_html(html, cid_map, strip_remote_images)
                out_path = build_output_path(out_root, dt, from_header, subject, msg_id)
                out_path.parent.mkdir(parents=True, exist_ok=True)

                try:
                    render_pdf(cleaned, out_path)
                    stamp_pdf_metadata(out_path, dt, subject, from_header)
                except Exception as e:  # noqa: BLE001
                    log.error("Failed to render/stamp %s: %s", out_path, e)
                    continue

                state[msg_id] = str(out_path)
            except Exception as e:  # noqa: BLE001 - one weird message must never abort the whole scan
                log.error("Unexpected error processing message in %s (key %s): %s", mbox_path, key, e)
                continue
    finally:
        box.close()
    return count


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", "-i", required=True, type=Path,
                     help="Root directory to scan for Thunderbird mbox archive files "
                          "(e.g. .../Archive.sbd)")
    ap.add_argument("--output", "-o", required=True, type=Path,
                     help="Directory to write PDFs into (organized as YYYY/MM/)")
    ap.add_argument("--config", "-c", type=Path, default=DEFAULT_CONFIG,
                     help=f"Path to sender/subject config JSON (default: {DEFAULT_CONFIG})")
    ap.add_argument("--state-file", type=Path, default=None,
                     help="JSON file tracking already-processed Message-IDs "
                          "(default: <output>/.receipt_archiver_state.json)")
    ap.add_argument("--strict", action="store_true",
                     help="Only match by known sender domain (ignore subject-keyword heuristic)")
    ap.add_argument("--dry-run", action="store_true",
                     help="List matches without writing any PDFs or updating state")
    ap.add_argument("--force", action="store_true",
                     help="Reprocess messages even if already recorded in the state file")
    ap.add_argument("--keep-remote-images", action="store_true",
                     help="Do not strip remote (http/https) <img> tags. By default these "
                          "are removed to avoid tracking pixels and network dependence.")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    for noisy in ("fontTools", "weasyprint"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if not args.input.exists():
        log.error("Input path does not exist: %s", args.input)
        sys.exit(1)

    cfg = load_config(args.config)
    state_path = args.state_file or (args.output / ".receipt_archiver_state.json")
    state = {} if args.dry_run else load_state(state_path)

    mbox_files = list(find_mbox_files(args.input))
    log.info("Found %d mbox archive file(s) under %s", len(mbox_files), args.input)

    total = 0
    for mbox_path in mbox_files:
        log.debug("Scanning %s", mbox_path)
        total += process_mbox_file(
            mbox_path, cfg, args.output, state,
            strict=args.strict,
            dry_run=args.dry_run,
            strip_remote_images=not args.keep_remote_images,
            force=args.force,
        )

    if not args.dry_run:
        save_state(state_path, state)

    log.info("Done. %d receipt(s) matched%s.", total, "" if args.dry_run else " and processed")


if __name__ == "__main__":
    main()
