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
import urllib.parse
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
    cfg.setdefault("live_receipt_link_patterns", [])
    cfg["sender_patterns"] = [s.lower() for s in cfg["sender_patterns"]]
    cfg["subject_keywords"] = [s.lower() for s in cfg["subject_keywords"]]
    cfg["live_receipt_link_res"] = [re.compile(p) for p in cfg["live_receipt_link_patterns"]]
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


def get_text_content_by_type(msg: Message) -> dict:
    """Return {'text/plain': '...', 'text/html': '...'} of decoded content
    (parts of the same type are concatenated together). Used only for
    scanning for a 'full receipt' link -- not for rendering."""
    chunks = {"text/plain": [], "text/html": []}
    parts = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
        ctype = part.get_content_type()
        if ctype not in chunks:
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            chunks[ctype].append(payload.decode(charset, errors="replace"))
        except (LookupError, TypeError):
            chunks[ctype].append(payload.decode("utf-8", errors="replace"))
    return {k: "\n".join(v) for k, v in chunks.items()}


_URL_TRAILING_JUNK = re.compile(r'''[\s'")\]>.,;]+$''')


def _clean_matched_url(url: str) -> str:
    """Defensively trim characters that are clearly not part of a URL but
    can get swept up by a regex match -- e.g. the closing quote of an HTML
    href="...">, a trailing '>' from an angle-bracket-wrapped plain-text
    link, or trailing prose punctuation. This is a safety net independent
    of how tight any individual live_receipt_link_patterns regex is."""
    return _URL_TRAILING_JUNK.sub("", url)


def find_live_receipt_url(msg: Message, live_link_res):
    """If the message body contains a link matching one of the configured
    'live_receipt_link_patterns', return the first match. Otherwise None.

    Plain-text and HTML bodies are searched separately (plain-text first)
    rather than as one concatenated blob: concatenating them let a failed
    match against a wrapped plain-text URL fall through into an unrelated,
    unwrapped occurrence of the same link inside the HTML part's
    href="...">, which could swallow the closing quote as if it were part
    of the URL. HTML entities (e.g. &amp; in a multi-parameter query
    string) are unescaped before matching against the HTML body, and any
    match is run through _clean_matched_url() as a final safety net.

    Plain-text bodies commonly hard-wrap long lines (and format=flowed
    bodies soft-wrap with a trailing space before the newline), which can
    split a long URL across two lines. Matching against the raw text would
    miss that, so each body is also tried with whitespace collapsed --
    safe to do because URLs never legitimately contain whitespace, so
    collapsing it can only help reassemble a wrapped URL, and can't turn
    unrelated prose into a false positive (the patterns all require a
    specific https?://... prefix).
    """
    if not live_link_res:
        return None

    import html as html_module

    by_type = get_text_content_by_type(msg)
    candidates = [
        by_type.get("text/plain", ""),
        html_module.unescape(by_type.get("text/html", "")),
    ]

    for body in candidates:
        if not body:
            continue
        for variant in (body, re.sub(r"\s+", "", body)):
            for pattern in live_link_res:
                m = pattern.search(variant)
                if m:
                    return _clean_matched_url(m.group(0))
    return None


# --------------------------------------------------------------------------
# HTML cleanup: inline cid: images, strip hyperlinks, drop remote trackers
# --------------------------------------------------------------------------

def _looks_like_tracking_pixel(img) -> bool:
    """Heuristic for actual tracking pixels (as opposed to real content
    images like logos): explicit 0/1px dimensions, or common
    tracker/beacon naming in class/id/src."""
    def _px(val):
        if val is None:
            return None
        try:
            return int(re.sub(r"[^\d]", "", str(val)) or -1)
        except ValueError:
            return None

    w, h = _px(img.get("width")), _px(img.get("height"))
    if (w is not None and w <= 1) or (h is not None and h <= 1):
        return True

    style = (img.get("style") or "").lower()
    if re.search(r"width\s*:\s*[01]px|height\s*:\s*[01]px", style):
        return True

    haystack = " ".join([
        img.get("class") and " ".join(img.get("class")) or "",
        img.get("id") or "",
        img.get("src") or "",
        img.get("alt") or "",
    ]).lower()
    return any(m in haystack for m in ("track", "beacon", "pixel.gif", "open.gif", "spacer.gif"))


# --------------------------------------------------------------------------
# Some receipts (seen so far: a couple of older Clover-style templates)
# print no date anywhere in the visible body -- the only place it survives
# is the email's Date header, which we already stamp into the PDF's
# CreationDate/ModDate metadata, but that's not something anyone glances at
# while reading a printed/exported receipt. If a heuristic scan of the
# rendered text finds nothing date-shaped, we append a small footer line
# with the email's date so it's visible on the page itself too.
# --------------------------------------------------------------------------

_MONTH_NAMES = (r"Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
                r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t|tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?")

_DATE_PATTERNS = [re.compile(p, re.IGNORECASE) for p in (
    r"\b\d{4}-\d{1,2}-\d{1,2}\b",                          # 2026-06-08
    r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b",                  # 6/8/26, 06-08-2026
    rf"\b(?:{_MONTH_NAMES})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{2,4}}\b",   # June 8, 2026
    rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{_MONTH_NAMES})\.?,?\s+\d{{2,4}}\b",   # 8 June 2026
)]


def text_contains_date(text: str) -> bool:
    """Best-effort heuristic: does this text have anything date-shaped in
    it? False negatives (an unusual format we don't recognize) just mean a
    redundant footer gets added, which is harmless; false positives (some
    unrelated number sequence matching the shape of a date) mean we skip
    adding a footer that would've been useful -- rare given the patterns
    require date-like separators and groupings that ordinary receipt data
    (phone numbers, prices, order numbers) doesn't happen to take."""
    return any(p.search(text) for p in _DATE_PATTERNS)


def format_date_footer_text(dt: datetime) -> str:
    dt = dt.astimezone()  # local tz, same conversion pdf_date() uses for CreationDate/ModDate
    offset = dt.strftime("%z")  # e.g. -0700
    offset = f"UTC{offset[:3]}:{offset[3:]}" if offset else ""
    return f"Email date: {dt.strftime('%B %d, %Y %I:%M %p')} {offset}".strip()


def clean_html(html: str, cid_map: dict, strip_remote_images: bool, dt: datetime) -> str:
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
        elif src.startswith(("http://", "https://")):
            if strip_remote_images:
                # Opted into stripping ALL remote images (max privacy /
                # no network dependence), at the cost of dropping real
                # content images like logos too.
                img.decompose()
            elif _looks_like_tracking_pixel(img):
                # Default: only drop images that actually look like
                # tracking pixels/beacons; keep real content images (logos,
                # decorative art) so the rendered receipt looks right. Note
                # this does mean the renderer will contact the sender's
                # server for these images each time you render.
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

    if not text_contains_date(soup.get_text()):
        footer = soup.new_tag("div")
        footer["style"] = (
            "margin-top:12px; padding-top:8px; border-top:1px solid #ccc; "
            "font-family:sans-serif; font-size:11px; color:#666; text-align:center;"
        )
        footer.string = format_date_footer_text(dt)
        target = soup.body if soup.body else soup
        target.append(footer)

    return str(soup)


# --------------------------------------------------------------------------
# Live rendering: load the linked hosted receipt page in a real headless
# browser (needed because pages like squareup.com/r/... are client-side
# rendered -- a plain HTTP fetch returns an empty shell) and print that page
# to PDF instead of the email's own HTML.
# --------------------------------------------------------------------------

class LiveRenderError(Exception):
    """Raised for any failure in the live-render path; callers should catch
    this (and only this) to decide whether to fall back to the email's own
    HTML, since it means 'this didn't work', not 'something is broken'."""


# --------------------------------------------------------------------------
# Fit-to-one-page PDF output.
#
# Chromium's page.pdf() paginates like a real print job: content taller than
# one page spills onto a second (often nearly-empty) page. Receipts are
# usually shorter than a Legal-size sheet, so using Legal instead of Letter
# already avoids most of that; for the rare receipt still too long, we
# measure its actual rendered height and shrink it (Chromium's print
# "scale" factor -- a uniform zoom, not a re-layout) just enough to fit
# within one Legal page, rather than letting it spill onto a second page.
# --------------------------------------------------------------------------

LEGAL_WIDTH_IN = 8.5
LEGAL_MAX_HEIGHT_IN = 14.0
PDF_MARGIN_IN = 0.25
CSS_PX_PER_IN = 96
# Chromium clamps print scale to [0.1, 2]. We use a higher floor than that
# so content doesn't shrink into illegibility -- past this point we let the
# page grow taller than Legal size instead of shrinking further, so output
# is still always a single page, just not always Legal-sized in the most
# extreme cases (a receipt with an enormous number of line items, say).
MIN_PRINT_SCALE = 0.4


def _legal_page_viewport() -> dict:
    """Viewport sized to the printable width of a Legal page (width minus
    margins), so what we measure for content height matches what will
    actually be laid out when printed -- and so responsive/media-query CSS
    in the email sees the same width in both steps. Height is kept small on
    purpose: browsers generally report document.documentElement.scrollHeight
    as at least the viewport height, so a tall initial viewport would make
    every short receipt measure as artificially tall."""
    printable_width_in = LEGAL_WIDTH_IN - 2 * PDF_MARGIN_IN
    return {"width": round(printable_width_in * CSS_PX_PER_IN), "height": 100}


def _print_fit_to_one_legal_page(page, out_path: Path):
    """Print `page`'s already-loaded content to a single Legal-size PDF
    page, shrinking it (never enlarging) just enough that it doesn't spill
    onto a second page."""
    # Chromium's page.pdf() defaults to "print" CSS media. Real hosted web
    # pages (unlike raw email HTML, which essentially never defines
    # print-specific overrides) commonly ship an actual @media print
    # stylesheet that strips decorative styling to save ink when someone
    # prints the page on paper -- colored backgrounds, icons, etc. removed.
    # We want the rich on-screen look, not the bare-bones print version, so
    # explicitly force "screen" media instead.
    page.emulate_media(media="screen")
    content_height_px = page.evaluate("document.documentElement.scrollHeight")
    content_height_in = content_height_px / CSS_PX_PER_IN
    printable_height_in = LEGAL_MAX_HEIGHT_IN - 2 * PDF_MARGIN_IN

    scale = 1.0
    if content_height_in > printable_height_in:
        scale = printable_height_in / content_height_in
        if scale < MIN_PRINT_SCALE:
            log.debug(
                "content is %.1fin tall -- even at the minimum print scale "
                "(%.2f) it won't fit a %.1fin Legal page; letting the page "
                "grow taller instead so it still comes out as one page",
                content_height_in, MIN_PRINT_SCALE, printable_height_in,
            )
            scale = MIN_PRINT_SCALE

    page_height_in = (content_height_in * scale) + 2 * PDF_MARGIN_IN
    log.debug("fit-to-page: content=%.2fin scale=%.2f page_height=%.2fin",
              content_height_in, scale, page_height_in)

    page.pdf(
        path=str(out_path),
        print_background=True,
        prefer_css_page_size=False,
        width=f"{LEGAL_WIDTH_IN}in",
        height=f"{page_height_in}in",
        scale=scale,
        margin={
            "top": f"{PDF_MARGIN_IN}in", "bottom": f"{PDF_MARGIN_IN}in",
            "left": f"{PDF_MARGIN_IN}in", "right": f"{PDF_MARGIN_IN}in",
        },
    )


_LIVE_RENDER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def extract_date_from_text(text: str):
    """Best-effort: find an actual datetime.datetime in receipt page text,
    for standalone URL rendering where there's no email Date header to draw
    from. Only lines that already look date-shaped (per the same patterns
    text_contains_date() uses) are handed to dateutil's fuzzy parser, so an
    unrelated number sequence elsewhere on the page can't get misread as a
    date -- the regex pre-filter is the safety gate. Returns None if
    dateutil isn't installed or nothing on the page parses cleanly."""
    try:
        from dateutil import parser as dateutil_parser
    except ImportError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line or not any(p.search(line) for p in _DATE_PATTERNS):
            continue
        try:
            parsed = dateutil_parser.parse(line, fuzzy=True)
        except (ValueError, OverflowError, TypeError):
            continue
        if 2000 <= parsed.year <= 2100:  # reject obviously-wrong fuzzy parses
            return parsed
    return None


def dump_rendered_dom(url: str, timeout_ms: int = 20000) -> str:
    """Load `url` exactly the way render_live_receipt_pdf does (same
    viewport, user agent, wait strategy) and return the fully-hydrated
    page.content() -- i.e. what our headless Chromium actually sees after
    JS runs, not the pre-JS source `curl` would return. Meant for debugging
    what a heuristic like the chrome-bar stripper is (or isn't) matching
    against; see --dump-dom."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--disable-blink-features=AutomationControlled"])
        try:
            context = browser.new_context(user_agent=_LIVE_RENDER_UA, viewport=_legal_page_viewport())
            page = context.new_page()
            page.goto(url, wait_until="networkidle", timeout=timeout_ms)
            # Match the extra hydration-tolerance buffer used elsewhere.
            page.wait_for_timeout(1500)
            return page.content()
        finally:
            browser.close()


def render_live_receipt_pdf(url: str, out_path: Path, dt=None, timeout_ms: int = 20000,
                             min_text_chars: int = 40):
    """Load `url` in headless Chromium, strip hyperlinks directly in the
    live DOM (so the exported PDF has none, regardless of whether the site's
    printed output would otherwise carry link annotations), then print the
    page to `out_path`.

    `dt`: the date to use for the missing-date footer (and, via the
    caller, PDF metadata). Pass a known datetime (e.g. from an email's Date
    header) to use it as-is. Pass None to auto-detect a date from the
    page's own visible text instead (dateutil fuzzy-parses lines that look
    date-shaped) -- meant for standalone URL rendering (see --test-live-url)
    where there's no email to draw a date from. If auto-detection can't
    find anything, falls back to the current time and always adds a footer
    noting the real date is unknown, rather than presenting a guess as fact.

    Returns (resolved_dt, page_title) so a caller in auto-detect mode can
    use them for its own PDF metadata stamping.

    Raises LiveRenderError on any failure (missing playwright/browser,
    navigation failure, or a page that rendered suspiciously little text --
    which usually means the site blocked the headless browser or the link
    had expired). Callers should catch LiveRenderError and fall back to
    rendering the email's own HTML instead.
    """
    try:
        from playwright.sync_api import sync_playwright, Error as PWError, TimeoutError as PWTimeout
    except ImportError as e:
        raise LiveRenderError(
            "playwright is not installed; run "
            "'pip install --break-system-packages playwright && playwright install chromium' "
            "or use --no-live-render"
        ) from e

    page_title = ""
    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(args=["--disable-blink-features=AutomationControlled"])
            except PWError as e:
                raise LiveRenderError(
                    f"could not launch chromium ({e}); run 'playwright install chromium'"
                ) from e

            try:
                context = browser.new_context(user_agent=_LIVE_RENDER_UA, viewport=_legal_page_viewport())
                page = context.new_page()
                try:
                    page.goto(url, wait_until="networkidle", timeout=timeout_ms)
                except PWTimeout as e:
                    raise LiveRenderError(f"timed out loading {url}: {e}") from e
                except PWError as e:
                    raise LiveRenderError(f"navigation failed for {url}: {e}") from e

                body_text = page.inner_text("body") if page.query_selector("body") else ""
                if len(body_text.strip()) < min_text_chars:
                    # networkidle only waits on network activity; some pages
                    # finish hydrating slightly after that (a JS timer, a
                    # requestIdleCallback, etc. with no further network
                    # calls). Give it a little more time before giving up.
                    for _ in range(10):
                        page.wait_for_timeout(300)
                        body_text = page.inner_text("body") if page.query_selector("body") else ""
                        if len(body_text.strip()) >= min_text_chars:
                            break

                if len(body_text.strip()) < min_text_chars:
                    raise LiveRenderError(
                        f"page rendered almost no text ({len(body_text.strip())} chars) -- "
                        "likely blocked, expired, or requires JS this browser didn't run"
                    )

                try:
                    page_title = page.title() or ""
                except PWError:
                    page_title = ""

                date_was_auto_detected = dt is None
                if dt is None:
                    dt = extract_date_from_text(body_text)
                    if dt is not None:
                        log.info("  auto-detected receipt date from page content: %s", dt.isoformat())
                    else:
                        dt = datetime.now(timezone.utc)
                        log.warning(
                            "  could not determine a reliable date from the page content; using "
                            "render time (%s) as a placeholder for PDF metadata -- pass --date "
                            "if you know the real one",
                            dt.isoformat(),
                        )

                # Forcing "screen" media (see _print_fit_to_one_legal_page)
                # restores real page styling, but can also un-hide site-level
                # chrome -- a nav bar, promo banner, etc. -- that the site's
                # own @media print rules would normally suppress on a real
                # printout.
                #
                # Primary check: elements matching known nav/banner markers
                # -- <nav>, role="navigation"/"banner", or a class
                # containing "navbar" (e.g. Toast's own receipt pages use
                # <body><div class="wrapper"><div class="navbar
                # navbar-fixed-top">...containing their logo<img>...). This
                # is unconditional on content (even if it contains a real
                # logo image) because an actual site nav bar is never
                # legitimate receipt content regardless of what's in it --
                # real receipt content (line items, totals, business info)
                # lives in the page's own receipt/card container, not in
                # navigation markup. Deliberately NOT bare <header> --
                # that's ambiguous enough (a business could legitimately use
                # it for its own name/address block) that stripping it
                # caused real receipt content to disappear in testing.
                # This also doesn't depend on
                # the element actually being CSS position:fixed at render
                # time, which isn't reliable -- responsive breakpoints,
                # stylesheet load timing, etc. can all mean a real nav bar
                # computes to position:static in our headless render even
                # though it's visually a fixed top bar in a normal browser.
                #
                # Secondary check: any position:fixed/sticky element
                # anchored near the top of the page, for chrome that isn't
                # caught by the selector match above (unknown vendors,
                # different markup conventions).
                #
                # Tertiary check: among the first few top-level children of
                # <body> specifically, anything with a solid background but
                # no visible text and no image/logo -- catches decorative
                # empty bars that aren't fixed-positioned and don't match
                # known nav selectors either.
                page.evaluate(
                    """() => {
                        document.querySelectorAll(
                            'nav, [role="navigation"], [role="banner"], [class*="navbar"]'
                        ).forEach(el => {
                            const rect = el.getBoundingClientRect();
                            if (rect.top <= 300) el.style.display = 'none';
                        });

                        document.querySelectorAll('*').forEach(el => {
                            const style = getComputedStyle(el);
                            if (style.position !== 'fixed' && style.position !== 'sticky') return;
                            const rect = el.getBoundingClientRect();
                            if (rect.height <= 0) return;
                            if (rect.top > 150) return;  // not anchored near the top
                            el.style.display = 'none';
                        });

                        const candidates = Array.from(document.body.children).slice(0, 4);
                        for (const el of candidates) {
                            const rect = el.getBoundingClientRect();
                            if (rect.height < 8) continue;
                            const text = (el.innerText || '').replace(/\\s+/g, '');
                            if (text.length > 0) continue;
                            if (el.querySelector('img, svg, picture')) continue;
                            const bg = getComputedStyle(el).backgroundColor;
                            if (bg && bg !== 'rgba(0, 0, 0, 0)' && bg !== 'transparent') {
                                el.style.display = 'none';
                            }
                        }
                    }"""
                )

                # Unwrap every <a> in the live DOM before printing, so the
                # exported PDF has no clickable link constructs at all.
                page.evaluate(
                    """() => {
                        document.querySelectorAll('a').forEach(a => {
                            const span = document.createElement('span');
                            span.textContent = a.textContent;
                            a.replaceWith(span);
                        });
                    }"""
                )

                # If the hosted page has no date visible anywhere (seen on
                # a couple of older receipt templates), append one so it's
                # not only recoverable from PDF metadata. If we had to fall
                # back to "now" above, always add it (with honest wording),
                # since in that case we know for certain the real date
                # isn't shown anywhere on the page.
                current_text = page.inner_text("body") if page.query_selector("body") else ""
                needs_footer = not text_contains_date(current_text)
                footer_text = format_date_footer_text(dt)
                if date_was_auto_detected and needs_footer:
                    footer_text = f"PDF rendered: {footer_text.replace('Email date: ', '')} (actual receipt date not found on page)"
                if needs_footer:
                    page.evaluate(
                        """(footerText) => {
                            const footer = document.createElement('div');
                            footer.textContent = footerText;
                            footer.style.marginTop = '12px';
                            footer.style.paddingTop = '8px';
                            footer.style.borderTop = '1px solid #ccc';
                            footer.style.fontFamily = 'sans-serif';
                            footer.style.fontSize = '11px';
                            footer.style.color = '#666';
                            footer.style.textAlign = 'center';
                            document.body.appendChild(footer);
                        }""",
                        footer_text,
                    )

                _print_fit_to_one_legal_page(page, out_path)
            finally:
                browser.close()
    except LiveRenderError:
        raise
    except Exception as e:  # noqa: BLE001 - any other playwright/OS surprise
        raise LiveRenderError(f"unexpected error rendering {url}: {e}") from e

    if not out_path.exists() or out_path.stat().st_size < 500:
        raise LiveRenderError(f"output PDF for {url} is missing or suspiciously small")

    return dt, page_title


def strip_pdf_link_annotations(pdf_path: Path):
    """Remove any clickable link annotations from an already-written PDF.
    Belt-and-suspenders: the email-HTML path never has <a> tags left by the
    time it reaches the renderer, and the live-render path already unwraps
    them in the DOM, but this guarantees no PDF this tool writes can carry a
    clickable /Link annotation regardless of how it was produced."""
    import pikepdf
    with pikepdf.open(str(pdf_path), allow_overwriting_input=True) as pdf:
        changed = False
        for page in pdf.pages:
            if "/Annots" not in page:
                continue
            kept = [a for a in page["/Annots"] if a.get("/Subtype") != "/Link"]
            if len(kept) != len(page["/Annots"]):
                changed = True
                if kept:
                    page["/Annots"] = kept
                else:
                    del page["/Annots"]
        if changed:
            pdf.save(str(pdf_path))


# --------------------------------------------------------------------------
# PDF rendering + metadata
# --------------------------------------------------------------------------

_warned_no_playwright_for_email_render = False


def render_pdf(html: str, out_path: Path, timeout_ms: int = 20000):
    """Render an email's HTML body to PDF.

    Prefers headless Chromium (via Playwright) because it has full CSS
    support -- clip-path/mask, flexbox, web fonts, proper nested-table width
    resolution -- which is what real POS receipt templates rely on to get
    their layout (rounded/starred rating icons, a centered narrow "receipt
    strip" on a colored background, etc). weasyprint's CSS engine doesn't
    support all of that, so its output can diverge visually from what
    Thunderbird (a real browser engine) shows for the same email.

    Falls back to weasyprint if playwright isn't installed, so the tool
    still works without the extra dependency -- just with lower fidelity
    on visually elaborate templates.
    """
    global _warned_no_playwright_for_email_render
    try:
        _render_html_via_chromium(html, out_path, timeout_ms=timeout_ms)
        return
    except LiveRenderError as e:
        if not _warned_no_playwright_for_email_render:
            log.warning(
                "Falling back to weasyprint for email-HTML rendering (%s). "
                "For higher-fidelity output matching what Thunderbird shows, run "
                "'pip install --break-system-packages playwright && playwright install chromium'.",
                e,
            )
            _warned_no_playwright_for_email_render = True

    from weasyprint import HTML
    HTML(string=html, base_url=None).write_pdf(str(out_path))


def _render_html_via_chromium(html: str, out_path: Path, timeout_ms: int = 20000):
    """Render a raw HTML string to PDF via headless Chromium. Raises
    LiveRenderError (reusing the same type as the live-page renderer, since
    the failure modes and the "just fall back" handling are identical) if
    playwright isn't installed or the render fails outright."""
    try:
        from playwright.sync_api import sync_playwright, Error as PWError, TimeoutError as PWTimeout
    except ImportError as e:
        raise LiveRenderError("playwright is not installed") from e

    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch()
            except PWError as e:
                raise LiveRenderError(f"could not launch chromium ({e})") from e
            try:
                page = browser.new_page(viewport=_legal_page_viewport())
                try:
                    page.set_content(html, wait_until="networkidle", timeout=timeout_ms)
                except PWTimeout as e:
                    raise LiveRenderError(f"timed out rendering HTML: {e}") from e
                except PWError as e:
                    raise LiveRenderError(f"failed to render HTML: {e}") from e
                _print_fit_to_one_legal_page(page, out_path)
            finally:
                browser.close()
    except LiveRenderError:
        raise
    except Exception as e:  # noqa: BLE001
        raise LiveRenderError(f"unexpected error rendering HTML: {e}") from e

    if not out_path.exists() or out_path.stat().st_size < 500:
        raise LiveRenderError("output PDF is missing or suspiciously small")


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
                       force: bool, live_render: bool, live_render_timeout_ms: int) -> int:
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

                live_url = find_live_receipt_url(msg, cfg["live_receipt_link_res"]) if live_render else None

                if html is None and live_url is None:
                    log.info("MATCH (no HTML body, skipped) | %s | %s | %s", dt.isoformat(), from_header, subject)
                    continue

                log.info("MATCH | %s | %s | %s", dt.isoformat(), from_header, subject)
                count += 1

                if dry_run:
                    continue

                out_path = build_output_path(out_root, dt, from_header, subject, msg_id)
                out_path.parent.mkdir(parents=True, exist_ok=True)

                rendered = False
                if live_url:
                    try:
                        log.info("  live-rendering %s", live_url)
                        render_live_receipt_pdf(live_url, out_path, dt, timeout_ms=live_render_timeout_ms)
                        rendered = True
                    except LiveRenderError as e:
                        log.warning("  live render failed (%s), falling back to email HTML", e)

                if not rendered:
                    if html is None:
                        log.error("  no email HTML to fall back to for %s, skipping", msg_id)
                        continue
                    try:
                        cleaned = clean_html(html, cid_map, strip_remote_images, dt)
                        render_pdf(cleaned, out_path, timeout_ms=live_render_timeout_ms)
                        rendered = True
                    except Exception as e:  # noqa: BLE001
                        log.error("Failed to render %s: %s", out_path, e)
                        continue

                try:
                    strip_pdf_link_annotations(out_path)
                    stamp_pdf_metadata(out_path, dt, subject, from_header)
                except Exception as e:  # noqa: BLE001
                    log.error("Failed to finalize %s: %s", out_path, e)
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
    ap.add_argument("--input", "-i", type=Path,
                     help="Root directory to scan for Thunderbird mbox archive files "
                          "(e.g. .../Archive.sbd). Required unless --test-live-url is used.")
    ap.add_argument("--output", "-o", type=Path,
                     help="Directory to write PDFs into (organized as YYYY/MM/). Required "
                          "unless --test-live-url is used (in which case it's an optional "
                          "output file/dir for the single test PDF).")
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
    ap.add_argument("--block-remote-images", dest="strip_remote_images", action="store_true",
                     default=False,
                     help="Strip ALL remote (http/https) <img> tags, including real content "
                          "images like logos, for maximum privacy/no network dependence at "
                          "render time. By default only images that look like actual tracking "
                          "pixels/beacons (0/1px, tracker-ish names) are dropped, and real "
                          "content images are kept and fetched so the PDF looks like the "
                          "actual receipt.")
    ap.add_argument("--keep-remote-images", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--live-render", dest="live_render", action="store_true", default=True,
                     help="For matched receipts containing a link matching "
                          "live_receipt_link_patterns (e.g. squareup.com/r/...), load that "
                          "page in a real headless browser and render IT to PDF instead of "
                          "the email's own HTML. Falls back to the email HTML automatically "
                          "if the live render fails for any reason. Requires playwright "
                          "(pip install playwright && playwright install chromium). On by "
                          "default.")
    ap.add_argument("--no-live-render", dest="live_render", action="store_false",
                     help="Disable live rendering; always use the email's own HTML.")
    ap.add_argument("--live-render-timeout", type=int, default=20000,
                     help="Milliseconds to wait for the live receipt page to load (default: 20000)")
    ap.add_argument("--receipt-url", "--test-live-url", dest="receipt_url", metavar="URL",
                     help="Render a single receipt URL (e.g. one texted to you directly, not "
                          "from an email) to a finished PDF and exit -- same pipeline as the "
                          "main archive run: single Legal-size page fit to content, no "
                          "clickable links, a date footer added if none is visible on the page. "
                          "Since there's no email to pull a date from, the date is "
                          "auto-detected from the page's own text; use --date to override or "
                          "provide one if auto-detection can't find it. Combine with --output "
                          "to choose where the PDF is written (defaults to the current "
                          "directory).")
    ap.add_argument("--date", metavar="DATE",
                     help="Date to use for --receipt-url's PDF metadata/footer (e.g. "
                          "'2026-03-13 17:44' or 'March 13, 2026 5:44pm'). Only used with "
                          "--receipt-url; ignored otherwise. If omitted, the date is "
                          "auto-detected from the page content.")
    ap.add_argument("--dump-dom", metavar="URL",
                     help="Debug helper: load URL exactly the way --receipt-url does (same "
                          "viewport/user-agent/wait strategy) and write the fully-hydrated "
                          "page HTML to disk, then exit -- no PDF, no rendering. Use this to "
                          "see what our headless Chromium actually renders (post-JS), which "
                          "is different from what plain curl would show for JS-heavy pages. "
                          "Combine with --output to choose the file (default: dom_dump.html).")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    for noisy in ("fontTools", "weasyprint"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if args.dump_dom:
        out_path = (args.output if args.output else Path("dom_dump.html"))
        if out_path.is_dir() or not out_path.suffix:
            out_path = out_path / "dom_dump.html"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            html = dump_rendered_dom(args.dump_dom, timeout_ms=args.live_render_timeout)
        except Exception as e:  # noqa: BLE001
            log.error("Could not load %s: %s", args.dump_dom, e)
            sys.exit(1)
        out_path.write_text(html, encoding="utf-8")
        log.info("Dumped rendered DOM -> %s (%d bytes)", out_path, len(html))
        return

    if args.receipt_url:
        out_path = (args.output if args.output else Path("receipt.pdf"))
        if out_path.is_dir() or not out_path.suffix:
            out_path = out_path / "receipt.pdf"
        out_path.parent.mkdir(parents=True, exist_ok=True)

        explicit_dt = None
        if args.date:
            try:
                from dateutil import parser as dateutil_parser
                explicit_dt = dateutil_parser.parse(args.date)
            except ImportError:
                log.error("--date requires python-dateutil: pip install --break-system-packages python-dateutil")
                sys.exit(1)
            except (ValueError, OverflowError) as e:
                log.error("Could not parse --date %r: %s", args.date, e)
                sys.exit(1)

        try:
            resolved_dt, page_title = render_live_receipt_pdf(
                args.receipt_url, out_path, explicit_dt, timeout_ms=args.live_render_timeout
            )
        except LiveRenderError as e:
            log.error("Rendering %s FAILED: %s", args.receipt_url, e)
            sys.exit(1)

        strip_pdf_link_annotations(out_path)
        domain = urllib.parse.urlparse(args.receipt_url).netloc
        stamp_pdf_metadata(out_path, resolved_dt, page_title or domain, domain)
        log.info("Rendered -> %s (%s)", out_path, resolved_dt.isoformat())
        return

    if not args.input or not args.output:
        ap.error("--input and --output are required (unless using --receipt-url)")

    if args.keep_remote_images:
        log.warning(
            "--keep-remote-images is deprecated and has no effect: keeping real content "
            "images (like logos) is now the default. Use --block-remote-images if you "
            "want the old strip-everything-remote behavior."
        )

    if args.live_render:
        try:
            import playwright.sync_api  # noqa: F401
        except ImportError:
            log.warning(
                "--live-render is on but playwright isn't installed; every matched receipt "
                "will fall back to email-HTML rendering. Run 'pip install --break-system-packages "
                "playwright && playwright install chromium', or pass --no-live-render to silence this."
            )

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
            strip_remote_images=args.strip_remote_images,
            force=args.force,
            live_render=args.live_render,
            live_render_timeout_ms=args.live_render_timeout,
        )

    if not args.dry_run:
        save_state(state_path, state)

    log.info("Done. %d receipt(s) matched%s.", total, "" if args.dry_run else " and processed")


if __name__ == "__main__":
    main()
