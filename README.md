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

## Live-rendering a linked hosted receipt page

Some POS emails are a thin wrapper around a "view your full receipt" link
to a hosted page -- sometimes because the email itself just looks worse
than the hosted page, sometimes (like Clover) because the email genuinely
has no line-item detail at all and only the linked page does. When a
matched receipt's body contains a link matching one of the
`live_receipt_link_patterns` regexes in `senders.json`, the tool loads that
page in a real headless browser and prints *that* to PDF instead of the
email's own HTML.

**Currently configured:** two patterns, since Clover has sent both formats
over time --
- A SendGrid click-tracking redirect link
  (`u<digits>.ct.sendgrid.net/ls/click?...`), used by newer receipts and any
  other vendor that routes its receipt link through SendGrid the same way.
  These are redirect links -- SendGrid logs the click, then 302s to the
  vendor's actual hosted page -- and Playwright follows redirects
  automatically, so no special handling was needed for that part. Worth
  knowing: clicking through does mean SendGrid/the vendor logs that you
  opened and clicked the link, same tradeoff as fetching
  remote images (see above).
- A direct `clover.com/p/<id>` link, used by older receipts -- no redirect
  hop, Clover's own hosted receipt page directly.

One wrinkle specific to plain-text email bodies: long URLs often get
hard-wrapped (or soft-wrapped per RFC 3676 format=flowed, with a trailing
space before the line break) across two lines, which would otherwise break
matching mid-URL. The link finder handles this by also searching a
whitespace-collapsed copy of the body -- safe to do since URLs never
legitimately contain whitespace, so this can only help reassemble a
wrapped link, never cause a false match elsewhere in the email.

Square's `squareup.com/r/...` links used to be handled this way too, but
were removed once email HTML started rendering through Chromium (see
above) -- the email's own HTML looked right on its own at that point, and
fetching Square's separate hosted page made those receipts look visibly
different (different branding/chrome) from everything else in the archive.
If you run into another vendor whose email HTML is genuinely missing
detail like Clover's, add a regex for its link to
`live_receipt_link_patterns` the same way.

**How it behaves:**
- Hyperlinks are stripped directly in the live page's DOM before printing,
  so the PDF has no clickable links, same as the email-HTML path.
- The same single-page Legal-size fitting (see below) applies to
  live-rendered pages too.
- If the page fails to load, times out, or renders suspiciously little text
  (blocked, expired link, or slow to hydrate), it logs a warning and
  automatically falls back to rendering the email's own HTML. A live-render
  failure never causes a receipt to be skipped as long as the email itself
  had an HTML body.
- If `playwright` isn't installed at all, you get one clear warning at
  startup and every match uses the email-HTML path -- nothing breaks.

**Testing before a big run:** sanity-check the pipeline against one real
link first, rather than running the whole archive:
```bash
python3 receipt_archiver.py --test-live-url "PASTE_THE_FULL_LINK_HERE" -o ./test.pdf
```
This loads that one URL, strips links, prints to PDF, and exits.

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

## Missing-date footer

A couple of receipt templates (seen so far: some older Clover receipts)
print no date anywhere in the visible body -- the only place it survives is
the email's `Date` header, which gets stamped into the PDF's
CreationDate/ModDate metadata but isn't something you'd notice while
reading a printed/exported receipt.

Before rendering, the tool scans the receipt's text for anything
date-shaped (`2026-06-08`, `6/8/26`, `June 8, 2026`, `8 June 2026`, etc.).
If nothing matches, it appends a small footer line at the bottom of the
page:
```
Email date: June 08, 2026 07:33 PM UTC-07:00
```
using the same date that's in the PDF metadata. This is a text-based check
(it can't see a date baked into an image, if a template ever does that) and
a heuristic rather than exhaustive, so an unusual format it doesn't
recognize just means a harmless redundant footer gets added -- it never
removes or second-guesses a date that's already there.

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

## review_receipts.py -- pruning receipts after the fact

A separate companion script for walking back through generated PDFs
(usually somewhere under `~/receipts`) and deciding, one at a time, whether
to keep or delete each one.

```bash
python3 review_receipts.py ~/receipts
```

For each PDF it prints a quick preview right in the terminal (title,
creation date, a snippet of extracted text) and opens the file in your
system's default PDF viewer (`xdg-open` on Linux), then prompts:

```
[Enter=keep] [d]elete [o]pen [s]kip [q]uit >
```

- **Enter / k** -- keep it, move to the next
- **d** -- delete it. Goes to the system trash (`gio trash`, recoverable
  the normal way you'd recover anything from Trash) by default; pass
  `--permanent-delete` to actually remove the file instead.
- **o** -- (re)open it in the viewer, if you closed the window or want
  another look
- **s** -- skip for now without recording a decision
- **q** -- quit; progress is saved automatically

**Resuming a long review:** decisions are recorded in
`<root>/.review_state.json` as you go. Run with `--resume` to skip anything
already decided on in a prior run and pick up where you left off:
```bash
python3 review_receipts.py ~/receipts --resume
```
Without `--resume`, every run reviews everything found, from scratch
(harmless for files you already decided to keep -- you'll just see them
again).

**Other flags:**
- `--no-auto-open` -- skip launching the viewer automatically; just read the
  terminal preview, and press `o` if you want to actually open one
- `--viewer CMD` -- use a specific viewer instead of the system default,
  e.g. `--viewer evince` or `--viewer okular`
- `--sort mtime` -- review oldest-created-file-first instead of the default
  path order (which follows the `YYYY/MM` layout `receipt_archiver.py`
  produces)
