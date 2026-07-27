#!/usr/bin/env python3
"""
review_receipts.py

Walk a directory tree of receipt PDFs (as produced by receipt_archiver.py,
or any folder of PDFs really) and interactively decide, one at a time,
whether to keep or delete each one.

For each PDF it prints a quick metadata + text preview right in the
terminal, opens the file in your system's default PDF viewer, and prompts
for an action. Deleted files go to the trash (via 'gio trash' on
GNOME/Ubuntu, or a local .review_trash folder as a fallback) rather than
being removed permanently, so a misclick isn't unrecoverable.

Usage:
    python3 review_receipts.py ~/receipts
    python3 review_receipts.py ~/receipts --no-auto-open
    python3 review_receipts.py ~/receipts --resume
    python3 review_receipts.py ~/receipts --viewer evince
    python3 review_receipts.py ~/receipts --permanent-delete

Controls at each prompt:
    Enter / k   keep this receipt, move to the next
    d           delete this receipt (to trash, unless --permanent-delete)
    o           (re)open it in the viewer
    s           skip for now without recording a decision (won't count
                as reviewed for --resume)
    q           quit; progress so far is saved (with --resume)
"""

import argparse
import json
import logging
import shutil
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("review_receipts")


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------

def find_pdfs(root: Path, sort: str):
    pdfs = [p for p in root.rglob("*.pdf") if p.is_file()]
    if sort == "mtime":
        pdfs.sort(key=lambda p: p.stat().st_mtime)
    else:
        pdfs.sort(key=lambda p: str(p).lower())
    return pdfs


# --------------------------------------------------------------------------
# Preview: metadata + a bit of extracted text, so you often don't even need
# to wait for the viewer to open to make a call.
# --------------------------------------------------------------------------

def preview_pdf(pdf_path: Path, max_chars: int = 280):
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(pdf_path))
        meta = reader.metadata or {}
        title = getattr(meta, "title", None)
        author = getattr(meta, "author", None)
        created = getattr(meta, "creation_date", None)

        text = ""
        for page in reader.pages[:2]:
            text += (page.extract_text() or "") + " "
            if len(text) >= max_chars:
                break
        text = " ".join(text.split())[:max_chars]

        return {
            "title": title,
            "author": author,
            "created": created.isoformat() if created else None,
            "pages": len(reader.pages),
            "text": text,
            "error": None,
        }
    except Exception as e:  # noqa: BLE001 - never let a bad PDF crash the review loop
        return {"title": None, "author": None, "created": None, "pages": None,
                "text": "", "error": str(e)}


def human_size(num_bytes: int) -> str:
    n = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


# --------------------------------------------------------------------------
# Viewer
# --------------------------------------------------------------------------

def default_viewer_cmd():
    if sys.platform == "darwin":
        return ["open"]
    if shutil.which("xdg-open"):
        return ["xdg-open"]
    return None


def open_in_viewer(pdf_path: Path, viewer_cmd):
    cmd = list(viewer_cmd) if viewer_cmd else default_viewer_cmd()
    if not cmd:
        log.warning("No PDF viewer found (no xdg-open on PATH). "
                     "Pass --viewer to specify one, or use --no-auto-open.")
        return None
    try:
        return subprocess.Popen(cmd + [str(pdf_path)],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        log.warning("Could not launch viewer %s: %s", cmd, e)
        return None


def close_viewer(proc):
    if proc is None:
        return
    try:
        if proc.poll() is None:
            proc.terminate()
    except OSError:
        pass


# --------------------------------------------------------------------------
# Deletion
# --------------------------------------------------------------------------

def delete_file(pdf_path: Path, permanent: bool) -> str:
    """Delete pdf_path and return a short human-readable description of
    what happened, for the confirmation message."""
    if permanent:
        pdf_path.unlink()
        return "permanently deleted"

    if shutil.which("gio"):
        result = subprocess.run(["gio", "trash", str(pdf_path)],
                                 capture_output=True, text=True)
        if result.returncode == 0:
            return "moved to system trash"
        log.debug("gio trash failed (%s), falling back to local .review_trash",
                   result.stderr.strip())

    trash_dir = pdf_path.parent / ".review_trash"
    trash_dir.mkdir(exist_ok=True)
    dest = trash_dir / pdf_path.name
    i = 1
    while dest.exists():
        dest = trash_dir / f"{pdf_path.stem}_{i}{pdf_path.suffix}"
        i += 1
    pdf_path.rename(dest)
    return f"moved to {dest}"


# --------------------------------------------------------------------------
# Resume state
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
# Main review loop
# --------------------------------------------------------------------------

def print_preview(pdf_path: Path, root: Path, index: int, total: int):
    try:
        rel = pdf_path.relative_to(root)
    except ValueError:
        rel = pdf_path
    print(f"\n[{index}/{total}] {rel}")
    print(f"  size: {human_size(pdf_path.stat().st_size)}")

    info = preview_pdf(pdf_path)
    if info["error"]:
        print(f"  (could not read PDF metadata/text: {info['error']})")
        return
    if info["title"]:
        print(f"  title:   {info['title']}")
    if info["created"]:
        print(f"  created: {info['created']}")
    if info["text"]:
        text = info["text"]
        print(f"  preview: {text}{'...' if len(text) >= 280 else ''}")


def review(root: Path, args):
    pdfs = find_pdfs(root, args.sort)
    total = len(pdfs)
    if total == 0:
        print(f"No PDFs found under {root}")
        return

    state_path = args.state_file or (root / ".review_state.json")
    reviewed = load_state(state_path) if args.resume else {}

    stats = {"kept": 0, "deleted": 0, "skipped": 0, "already_done": 0}
    viewer_cmd = [args.viewer] if args.viewer else None

    try:
        for i, pdf in enumerate(pdfs, 1):
            key = str(pdf)
            if args.resume and key in reviewed:
                stats["already_done"] += 1
                continue
            if not pdf.exists():
                # Could happen if a prior delete in this run touched it somehow.
                continue

            print_preview(pdf, root, i, total)

            proc = None
            if not args.no_auto_open:
                proc = open_in_viewer(pdf, viewer_cmd)

            while True:
                try:
                    choice = input("  [Enter=keep] [d]elete [o]pen [s]kip [q]uit > ").strip().lower()
                except EOFError:
                    choice = "q"

                if choice in ("", "k", "keep"):
                    stats["kept"] += 1
                    reviewed[key] = "kept"
                    break
                elif choice in ("d", "delete"):
                    try:
                        where = delete_file(pdf, permanent=args.permanent_delete)
                        print(f"  -> {where}")
                        stats["deleted"] += 1
                        reviewed[key] = "deleted"
                    except OSError as e:
                        print(f"  ! failed to delete: {e}")
                        continue
                    break
                elif choice in ("o", "open"):
                    close_viewer(proc)
                    proc = open_in_viewer(pdf, viewer_cmd)
                    continue
                elif choice in ("s", "skip"):
                    stats["skipped"] += 1
                    break
                elif choice in ("q", "quit"):
                    close_viewer(proc)
                    raise KeyboardInterrupt
                else:
                    print("  ? enter one of: [enter], d, o, s, q")

            close_viewer(proc)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        if args.resume or reviewed:
            save_state(state_path, reviewed)

    print(
        f"\nDone. kept={stats['kept']} deleted={stats['deleted']} "
        f"skipped={stats['skipped']} already_reviewed={stats['already_done']} "
        f"total_found={total}"
    )


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root", nargs="?", type=Path, default=Path.home() / "receipts",
                     help="Directory tree to review (default: ~/receipts)")
    ap.add_argument("--no-auto-open", action="store_true",
                     help="Don't automatically open each PDF in a viewer; rely on the "
                          "printed metadata/text preview (or press 'o' to open manually)")
    ap.add_argument("--viewer", metavar="CMD",
                     help="Command to open PDFs with (default: xdg-open / 'open' on macOS). "
                          "E.g. --viewer evince or --viewer okular")
    ap.add_argument("--sort", choices=["path", "mtime"], default="path",
                     help="Review order (default: path, which follows the YYYY/MM layout "
                          "receipt_archiver.py produces)")
    ap.add_argument("--permanent-delete", action="store_true",
                     help="Delete files permanently instead of moving them to the trash")
    ap.add_argument("--resume", action="store_true",
                     help="Skip PDFs already decided on in a previous run (tracked in "
                          "<root>/.review_state.json). Without this, every run reviews "
                          "everything found, from scratch.")
    ap.add_argument("--state-file", type=Path, default=None,
                     help="Override the location of the resume-state JSON file")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-7s %(message)s",
    )

    if not args.root.exists():
        log.error("Directory does not exist: %s", args.root)
        sys.exit(1)

    review(args.root, args)


if __name__ == "__main__":
    main()
