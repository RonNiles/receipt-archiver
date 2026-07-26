# receipt_archiver.py

Scans Thunderbird mbox archive files (e.g. `Archive.sbd/2025.sbd/2025-02`),
detects POS-system receipt emails (Square, Toast, Clover, Shopify, Stripe,
PayPal, etc.), and renders each one's HTML body to a PDF with all hyperlinks
removed. The PDF's CreationDate/ModDate metadata and the output file's own
mtime are set to match the email's `Date` header.

`.msf` files are Thunderbird's proprietary Mork index format and are never
read — the tool only opens files it verifies start with a `From ` mbox
envelope line, so `.msf`/other junk is skipped automatically.

## Install

```bash
pip3 install --break-system-packages beautifulsoup4 lxml weasyprint pikepdf
```

(weasyprint here is the newer pure-Python build — no system Cairo/Pango/GTK
libraries required. If your `pip3 install` pulls an older weasyprint that
needs those, `sudo apt install libpango-1.0-0 libpangoft2-1.0-0` will cover it.)

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

- `--strict` — only match by known sender domain (`senders.json`), ignore the
  subject-keyword fallback. Use this if you're getting false positives.
- `--config path.json` — point at a customized copy of `senders.json` to add
  more POS vendors or subject keywords.
- `--keep-remote-images` — by default, remote (http/https) `<img>` tags are
  stripped since they're almost always tracking pixels or hotlinked logos;
  pass this to keep them (note: doing so makes the PDF depend on network
  access at render time and may load a tracking pixel).
- `--dry-run` — list matches without writing PDFs or touching the state file.
- `-v` — verbose logging.

## How detection works

A message matches if its `From` header contains one of the known POS/payment
domains in `senders.json` (Square, Toast, Clover, Shopify, Stripe, PayPal,
SumUp, Lightspeed, Heartland, Cash App, Vend, TouchBistro, Loyverse,
ChowNow, DoorDash, Uber Eats, Grubhub, Olo, etc.), OR its subject contains a
receipt-like keyword ("receipt", "order confirmation", ...) *and* the sender
address itself looks automated (no-reply/receipts/orders/billing). That
second condition exists so a random newsletter that happens to say "receipt"
in its subject doesn't get swept in. Edit `senders.json` freely to tune this
for your own inbox — it's a plain JSON file, no code changes needed.

## Notes / edge cases handled

- Inline images referenced via `cid:` (common for receipt logos) are resolved
  to embedded data URIs so they render correctly in the PDF.
- Malformed/unreadable individual messages are logged and skipped rather than
  aborting the whole scan.
- If a matched message has no HTML part at all, it's logged but not rendered
  (nothing to convert).
- Timezone in the original `Date` header is preserved in the PDF's
  CreationDate/ModDate (same instant, just expressed with the render
  machine's local UTC offset — this doesn't change *when* the receipt was
  sent, only how the offset is written).
