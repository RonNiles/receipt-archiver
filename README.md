# receipt_archiver.py

Scans Thunderbird mbox archive files (e.g. `Archive.sbd/2025.sbd/2025-02`),
detects POS-system receipt emails (Square, Toast, Clover, Shopify, Stripe,
PayPal, etc.), and renders each one to a PDF with all hyperlinks removed.
The PDF's CreationDate/ModDate metadata and the output file's own mtime are
set to match the email's `Date` header.

`.msf` files are Thunderbird's proprietary Mork index format and are never
read -- the tool only opens files it verifies start with a `From ` mbox
envelope line, so `.msf`/other junk is skipped automatically.

## Install

```bash
pip3 install --break-system-packages beautifulsoup4 lxml pikepdf playwright
playwright install chromium
```

Email HTML is now rendered through headless Chromium (Playwright), the same
engine used for the live-render feature below -- this matches Thunderbird's
own rendering fidelity (full CSS: clip-path/mask, flexbox, web fonts, proper
nested-table width resolution) far better than a pure-Python CSS engine can.
`weasyprint` is kept as an optional lower-fidelity fallback (`pip3 install
--break-system-packages weasyprint`) used automatically only if playwright
isn't installed at all -- so the tool still works without the browser
dependency, just with less accurate output on visually elaborate templates
(e.g. star-rating icons drawn with clip-path will render as plain filled
shapes instead of stars).

## Usage

```bash
# Dry run first: see what would be matched, write nothing
python3 receipt_archiver.py -i ~/Mail/Local\ Folders/Archive.sbd -o ~/receipts --dry-run -v

# Real run
python3 receipt_archiver.py -i ~/Mail/Local\ Folders/Archive.sbd -o ~/receipts
```

PDFs land in `~/receipts/YYYY/MM/YYYY-MM-DD_HHMMSS_sender-domain_subject-slug_hash.pdf`.

A state file (`~/receipts/.receipt_archiver_state.json`) records processed
Message-IDs so re-running the tool later (e.g. after a new month's archive
lands) only picks up new mail. Use `--force` to reprocess everything.

## Flags

- `--strict` -- only match by known sender domain (`senders.json`), ignore the
  subject-keyword fallback. Use this if you're getting false positives.
- `--config path.json` -- point at a customized copy of `senders.json` to add
  more POS vendors or subject keywords.
- `--block-remote-images` -- strip ALL remote (http/https) `<img>` tags,
  including real content images like logos, for maximum privacy / no network
  dependence at render time. By default, only images that look like actual
  tracking pixels/beacons (0/1px, tracker-ish class/id/filename) are dropped;
  real content images (logos, decorative art) are kept and fetched so the
  PDF actually looks like the receipt. Note this does mean rendering an
  email will contact the sender's server for those images each time.
- `--live-render` / `--no-live-render` -- see below. On by default.
- `--live-render-timeout MS` -- how long to wait for the live receipt page to
  load (default 20000ms).
- `--test-live-url URL` -- debug helper, see below.
- `--dry-run` -- list matches without writing PDFs or touching the state file.
- `--force` -- reprocess messages even if already recorded in the state file.
- `-v` -- verbose logging.

## How detection works

A message matches if its `From` header contains one of the known POS/payment
domains in `senders.json` (Square, Toast, Clover, Shopify, Stripe, PayPal,
SumUp, Lightspeed, Heartland, Cash App, Vend, TouchBistro, Loyverse,
ChowNow, DoorDash, Uber Eats, Grubhub, Olo, etc.), OR its subject contains a
receipt-like keyword ("receipt", "order confirmation", ...) *and* the sender
address itself looks automated (no-reply/receipts/orders/billing). That
second condition exists so a random newsletter that happens to say "receipt"
in its subject doesn't get swept in. Edit `senders.json` freely to tune this
for your own inbox -- it's a plain JSON file, no code changes needed.

## Live-rendering a linked hosted receipt page (currently unused)

Some POS emails are a thin wrapper around a "view your full receipt" link to
a hosted page. There's a mechanism for loading that page in a real headless
browser and printing it instead of the email's own HTML -- see
`render_live_receipt_pdf()` and the `--live-render` / `--test-live-url`
flags -- but it's currently **inactive**: `live_receipt_link_patterns` in
`senders.json` is empty.

It was originally used for Square's `squareup.com/r/...` links, since their
hosted receipt page looked better than the raw email. That's no longer
needed now that email HTML renders through Chromium (see above) -- the
email's own HTML looks right on its own, and fetching a separate hosted page
for just one vendor made those receipts look visibly different from every
other receipt in the archive (different layout/branding chrome from
Square's site rather than the restaurant's own email template).

If you run into a specific vendor whose email HTML still renders worse than
its hosted receipt page, you can turn this back on by adding a regex to
`live_receipt_link_patterns`, e.g.:
```json
"live_receipt_link_patterns": [
  "https?://squareup\\.com/r/[A-Za-z0-9]+"
]
```
Everything described above (link stripped from the live DOM before
printing, automatic fallback to email HTML on any failure, `--test-live-url`
for testing a single link) still works exactly as before if you do.

## Page sizing (single page, no near-empty overflow page)

Output PDFs use Legal-size pages (8.5 x 14in) sized to fit each receipt on
exactly one page, instead of a fixed Letter page that could split a receipt
across two pages (with the second page holding just a line or two -- the
original motivation for this).

For each receipt, the tool measures the actual rendered content height and:
- If it fits within one Legal page, the page is sized exactly to the
  content (no wasted trailing blank space, no fixed 14in page for a 2in
  receipt).
- If it's taller than one Legal page, the content is scaled down (a uniform
  print zoom, not a re-layout) just enough to fit within 14in.
- In the rare case a receipt is so long that even a substantial scale-down
  (down to 40%) wouldn't make it fit legibly, the page is allowed to grow
  taller than 14in instead of shrinking further -- so output is always a
  single page, even if that means it's technically not Legal-sized in that
  edge case.

## Notes / edge cases handled

- Inline images referenced via `cid:` (common for receipt logos) are resolved
  to embedded data URIs so they render correctly in the PDF.
- Malformed/unreadable individual messages, and any single message that
  throws an unexpected error partway through processing, are logged and
  skipped rather than aborting the whole scan.
- Headers that come back as `email.header.Header` objects instead of plain
  strings (a stdlib quirk on some encoded/folded headers), and encoded-word
  headers with corrupted base64/quoted-printable payloads, are both handled
  without crashing.
- If a matched message has no HTML part and no live-render link, it's logged
  but not rendered (nothing to convert).
- Timezone in the original `Date` header is preserved in the PDF's
  CreationDate/ModDate (same instant, just expressed with the render
  machine's local UTC offset -- this doesn't change *when* the receipt was
  sent, only how the offset is written).
