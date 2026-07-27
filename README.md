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
pip3 install --break-system-packages beautifulsoup4 lxml weasyprint pikepdf
```

(weasyprint here is the newer pure-Python build -- no system Cairo/Pango/GTK
libraries required. If your `pip3 install` pulls an older weasyprint that
needs those, `sudo apt install libpango-1.0-0 libpangoft2-1.0-0` will cover it.)

Optional, for live-rendering hosted receipt pages (see below):
```bash
pip3 install --break-system-packages playwright
playwright install chromium
```

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
- `--keep-remote-images` -- by default, remote (http/https) `<img>` tags are
  stripped since they're almost always tracking pixels or hotlinked logos;
  pass this to keep them (note: doing so makes the PDF depend on network
  access at render time and may load a tracking pixel).
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

## Live-rendering the hosted receipt page (e.g. squareup.com/r/...)

Many Square receipt emails are a thin wrapper around a link like:

```
View your full receipt:
https://squareup.com/r/arvZNgpgkI6hJLltqJ9vVT6wp1JJZZY
```

...and that hosted page is a much cleaner, better-formatted version of the
receipt than the email itself. By default, when a matched receipt's body
contains a link matching one of the `live_receipt_link_patterns` in
`senders.json`, the tool loads that page in a real headless browser and
prints *that* to PDF instead of the email's HTML -- including small images
like logos (`print_background=True` keeps CSS background images too).

This needs a real browser (not just an HTTP fetch) because these hosted
receipt pages are client-side rendered -- Square's own developer forum
confirms that a plain `curl`/HTTP client on the `receipt_url` gets back an
empty shell; a browser is required to run the JS that fills it in. That's
why this uses Playwright (https://playwright.dev/) + headless Chromium
rather than a simple HTTP GET.

**Setup:**
```bash
pip3 install --break-system-packages playwright
playwright install chromium
# if that complains about missing system libraries:
playwright install --with-deps chromium
```

**How it behaves:**
- Hyperlinks are stripped directly in the live page's DOM before printing
  (every `<a>` is replaced with a plain `<span>` of its text), so the PDF has
  no clickable links, same as the email-HTML path.
- As an extra safety net, *every* PDF this tool writes -- whether from the
  live page or the email HTML -- has any `/Link` annotations stripped after
  the fact, in case a renderer ever adds one some other way.
- If the page fails to load, times out, or renders suspiciously little text
  (a decent signal the site blocked the headless browser, the link expired,
  or the page needed more time to hydrate -- the tool already waits a few
  extra seconds past initial network-idle for this), it logs a warning and
  automatically falls back to rendering the email's own HTML, same as before
  this feature existed. A live-render failure never causes a receipt to be
  skipped as long as the email itself had an HTML body.
- If `playwright` isn't installed at all, you'll get one clear warning at
  startup and every match will use the email-HTML path -- nothing breaks.

**Testing before a big run:** rather than running the whole archive, sanity
check the pipeline against one real link first:
```bash
python3 receipt_archiver.py --test-live-url "https://squareup.com/r/XXXXXXXX" -o ./test.pdf
```
This loads that one URL, strips links, prints to PDF, and exits -- tells you
immediately whether Square is letting the headless browser through, and lets
you eyeball the output before committing to hundreds of files.

Since these hosted pages are third-party and can change their layout, block
headless browsers, or expire links over time, treat this as best-effort: the
guaranteed fallback is what makes it safe to leave on by default.

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
