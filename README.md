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
pip3 install --break-system-packages beautifulsoup4 lxml pikepdf playwright python-dateutil
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
- `--receipt-url URL` -- render a single receipt link directly (e.g. one
  texted to you), see below.
- `--date DATE` -- date override for --receipt-url, see below.
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

**Excluding noisy senders:** `exclude_sender_patterns` in `senders.json` is
checked first, before anything else -- if the `From` header matches one of
these, the message is never treated as a receipt, even if it would
otherwise match a broader `sender_patterns` entry. This is for cases like
PayPal's marketing sending from `news.paypal.com`: `paypal.com` needs to
stay in `sender_patterns` to catch real PayPal receipts, but that same
broad match would otherwise sweep in PayPal's newsletter too. Add more
subdomains/addresses here as you spot noisy senders slipping through.

**Preferring a PDF attachment over the HTML body:** some senders attach the
actual receipt as a PDF but the HTML body is just marketing (Staples does
this -- the email body is a "thanks for shopping, here's a coupon" wrapper
around the real receipt attachment). By default, when a message has both,
the HTML body is used and the attachment is ignored (this matches the
common case where HTML *is* the receipt and any PDF attached alongside it
is a supplementary copy). Add a sender to `prefer_attachment_senders` in
`senders.json` to flip that priority for just that sender -- the PDF
attachment is archived directly instead. This is separate from (and works
alongside) the existing PDF-attachment fallback for senders with no HTML
body at all, like GitHub's payment receipts -- see below.

**Processing receipts forwarded by family members:** if someone forwards
you a receipt, the outer `From` is their address, not the vendor's, so it
won't match directly no matter what's in `sender_patterns`. Add their
address to `forwarder_senders` in `senders.json` (e.g. `"mom@gmail.com"`)
to handle this: for messages from a listed address that don't already
match directly, the body is scanned for an embedded forwarded-header block
-- the "-------- Forwarded Message --------" / "Begin forwarded message:"
style block every mail client (Thunderbird, Gmail, Outlook, Apple Mail)
produces for an inline forward -- whose own `From:` line matches a
`sender_patterns` entry. When found, the archived receipt uses the
*embedded original* sender, subject, and date instead of the forwarding
person's own address, "Fwd:"-prefixed subject, and forward time, wherever
those could be recovered from the header block -- so it's filed and dated
as if it had arrived directly, not as of whenever it happened to get
forwarded to you. `forwarder_senders` is empty by default; who you trust to
forward you real receipts is personal, not something to guess at.

The rendered PDF still shows the whole forward (any note the person added,
plus the quoted original) rather than isolating just the receipt -- pulling
out only the embedded portion would take much more parsing than recovering
a few header values, and this keeps the useful part (correct filing/dating)
without that complexity.

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
link first, rather than running the whole archive -- see `--receipt-url`
just below, which works for this too.

## Rendering a receipt link directly (e.g. one texted to you)

Sometimes a receipt link shows up somewhere other than an archived email --
texted to you, in a Slack DM, wherever. `--receipt-url` runs that single
link through the exact same rendering pipeline as the main archive scan
(single Legal-size page fit to content, no clickable links, real-content
images kept, missing-date footer if needed) and writes one PDF.

All PDF rendering, including this, forces Chromium's "screen" CSS media
instead of the default "print". Real hosted web pages -- unlike raw email
HTML, which almost never bothers with this -- commonly ship an actual
`@media print` stylesheet that strips decorative styling (colors, icons,
backgrounds) to save ink on paper. Left at the default, that stylesheet
would kick in and the PDF would look noticeably plainer than the rich page
you'd see in a normal browser tab; forcing "screen" media keeps the real
styling.

One side effect of that: forcing "screen" media can also un-hide site-level
chrome that a page's own print stylesheet would normally suppress -- most
commonly a navigation bar (Toast's own receipt pages, for example, wrap
their logo in `<div class="navbar navbar-inverse navbar-fixed-top">`,
nested a couple of levels inside `<body>`) sitting above the actual
receipt. Three checks run before printing, in order of reliability:

1. Elements matching known nav/banner markers -- `<nav>`, `role="navigation"`,
   `role="banner"`, or a class containing `"navbar"`. This is unconditional
   on content, even if the element contains a real logo image: an actual
   site nav bar is never legitimate receipt content regardless of what's in
   it, and doesn't depend on the element actually computing to CSS
   `position: fixed` at render time -- which turns out not to be reliable
   (responsive breakpoints and stylesheet timing can mean a real nav bar
   computes to `position: static` in headless rendering even though it's
   visually a fixed top bar in a normal browser -- confirmed against
   Toast's real markup). Deliberately *not* a bare `<header>` tag, though --
   that's ambiguous enough (a receipt could legitimately use it for the
   business's own name/address block) that including it caused real receipt
   content to disappear in testing.
2. Any `position: fixed`/`sticky` element anchored near the top, for chrome
   that isn't caught by the selector match above.
3. Among the first few direct children of `<body>` specifically, anything
   with a solid background but no visible text and no image -- catches
   decorative empty bars that don't match either check above.

```bash
python3 receipt_archiver.py --receipt-url "https://www.toasttab.com/receipts/PGPK4uf-Q9igP7LrasiqJA/yW9cK7NfYsXgY" -o ~/receipts/manual/toast-2026-03-13.pdf
```

If `-o` isn't given, it writes `receipt.pdf` in the current directory.

**Date handling:** unlike the main scan, there's no email `Date` header to
draw from here, so the date used for the PDF metadata and (if needed) the
footer is auto-detected from the page's own visible text (same date-shape
patterns as the missing-date footer feature, fed through dateutil's fuzzy
parser). If the page doesn't have a recognizable date anywhere, it falls
back to the time you ran the command and the footer says so explicitly
(`PDF rendered: ... (actual receipt date not found on page)`) rather than
presenting a guess as the real receipt date. Use `--date` if you know the
actual date and don't want to rely on auto-detection:
```bash
python3 receipt_archiver.py --receipt-url "https://..." --date "March 13, 2026 5:44pm" -o ./receipt.pdf
```
`--date` accepts pretty much any format a human would write
(`2026-03-13 17:44`, `3/13/26 5:44pm`, `March 13, 2026`, ...) via dateutil.

Requires `python-dateutil` in addition to the playwright/pikepdf
dependencies already needed for live rendering:
```bash
pip3 install --break-system-packages python-dateutil
```

**Debugging a specific page:** if a `--receipt-url` render looks wrong
(missing content, unwanted chrome, odd layout) and you want to see exactly
what our headless Chromium renders -- not what `curl` would show, which is
the pre-JavaScript source and can be very different for a client-rendered
page -- `--dump-dom` loads a URL the same way `--receipt-url` does and
saves the fully-hydrated page HTML to a file instead of printing a PDF:
```bash
python3 receipt_archiver.py --dump-dom "https://..." -o ./dom_dump.html
```

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
- If a matched message has an HTML body, that's used. If not, but it has a
  PDF attachment (GitHub's monthly payment receipts, for example, arrive
  this way -- no HTML body at all, just an attached PDF), the attachment is
  archived directly: copied to the standard output path, links stripped,
  and PDF metadata (CreationDate/ModDate/Title/Author) stamped from the
  email the same as every other receipt. There's no live DOM to inject a
  missing-date footer into for this path, so instead the attachment's text
  is checked and a warning is logged if no date is found in it (the
  metadata date from the email is set either way). If a message has neither
  an HTML body nor a PDF attachment, it's logged but not rendered (nothing
  to convert).
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
