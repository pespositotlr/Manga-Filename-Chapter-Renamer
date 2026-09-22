#!/usr/bin/env python3
"""
manga_renamer.py

Interactively renames a folder of raw scan pages (e.g. "001.jpg", "020.jpg",
"191.psd") into the chapter-tagged filename format consumed by Manga Chapter Zipper:

    <prefix>_ch<NNN>_<page>.<ext>     e.g. Manga_Title_v52_ch456_008.jpg
    <prefix>_extra_<page>.<ext>       e.g. Manga_Title_v52_extra_191.jpg

The page number stays continuous across the whole volume; only the tag changes.

How it works
------------
For one volume folder it asks you:
    1. The filename prefix, e.g. "Manga_Title_v52".
    2. Optionally, to auto-detect the table of contents (Tesseract OCR, see
       toc_ocr.py), which fills in steps 3-5 for you to confirm or correct.
    3. How many chapters are in the volume.
    4. The first chapter's number (chapters increment by 1 from there).
    5. Which page each chapter starts on.
    6. Which page the last chapter ends on.
    7. Whether the pages are numbered correctly per the table of contents.

Then, for each page:
    - Pages before the first chapter's start  -> ch000
    - Pages within [chapter N start, chapter N+1 start) -> ch<N>
    - Pages after the last chapter's end       -> extra

Already-renamed folders
-----------------------
If there are no raw files but the pages are already named like
"Manga_Title_v52_ch000_152.jpg", it asks "Do you want to update the chapter
values?" and re-tags them: page numbers stay as they are and only the chapter
tag (and optionally the prefix) changes.

Series config
-------------
configs/<series folder name>.toml next to this script supplies defaults, so
most prompts are just Enter. E.g. configs/Manga_Title.toml applies
to any folder inside a "Manga_Title" folder:

    prefix = "Manga_Title_v{volume:02}"
    toc_page = "004.jpg"
    first_chapter_file = "005.jpg"
    typical_chapter_pages = 16

{volume} comes from the volume folder's name ("Volume 13", "Manga_Title_v26_...").
A chapter range in a folder name, e.g. "(Ch219-228)", fills in the chapter
count and first chapter number. After a run without a config, it offers to
save your answers as one. configs/ is git-ignored (except example.toml). See
README.md for all keys.

Numbering correction
--------------------
If you answer "No" to "Are the pages numbered correctly?", it asks for the
current filename of the first page of the first chapter (the anchor). It then
shifts every page number by a constant offset so the anchor lands on the
chapter's true table-of-contents page, and tags the corrected numbers.

    anchor file 004.jpg, first chapter starts on page 3
        offset = 3 - 4 = -1
        001 -> 000a, 002 -> 001, 003 -> 002, 004 -> 003, ...

Pages that land on 0 or below become 000a, 000b, ... in order:

    anchor file 005.jpg, first chapter starts on page 3
        offset = 3 - 5 = -2
        001 -> 000a, 002 -> 000b, 003 -> 001, 004 -> 002, 005 -> 003, ...

The corrected number is baked straight into the final filename, so no separate
renumbering pass is needed.

Safety
------
Nothing is renamed until you confirm the previewed plan. Renames go through
temporary names so shifted numbers can't clobber each other. Use --dry-run to
preview without being prompted to apply.

Usage
-----
    python manga_renamer.py "/path/to/Volume_Folder"
    python manga_renamer.py "/path/to/Volume_Folder" --dry-run
"""

import argparse
import json
import re
import sys
import tomllib
from collections import Counter
from pathlib import Path
from string import ascii_lowercase

import toc_ocr

IMAGE_EXTS = {'.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp', '.tiff', '.psd'}

# A "raw" page file has a purely numeric stem, e.g. "001", "20", "191".
NUMERIC_STEM_RE = re.compile(r'^0*(\d+)$')
# An already-renamed page, e.g. "Manga_Title_v12_ch114_003",
# "..._ch000_000b2", "..._ch100_105_color", "..._extra_credits_245".
NAMED_RE = re.compile(
    r'^(?P<prefix>.+?)_(?P<tag>ch\d{3,}|extra_credits|extra)_(?P<page>\d+)(?P<rest>.*)$')

# Series configs, one per series folder name, e.g. "Manga_Title.toml".
CONFIG_DIR = Path(__file__).resolve().parent / 'configs'
# "Volume 13", "Vol. 13", "Manga_Title_v26_(Ch219-228)", "Manga_Title V26 Digital Raw".
DEFAULT_VOLUME_PATTERN = r'(?i)(?<![a-z])(?:volume|vol\.?|v)[\s_]*(\d+)'
# "(Ch219-228)", "(Ch033-036)".
DEFAULT_CHAPTER_RANGE_PATTERN = r'(?i)\(\s*ch\.?\s*(\d+)\s*-\s*(\d+)\s*\)'
# An output folder/file like "Manga_Title_v13_[Group]" gives the prefix.
PREFIX_HINT_RE = re.compile(r'^(.+_v\d+)_\[')


def is_page_file(path: Path) -> bool:
    return (
        path.is_file()
        and path.suffix.lower() in IMAGE_EXTS
        and NUMERIC_STEM_RE.match(path.stem) is not None
    )


def page_number(path: Path) -> int:
    return int(NUMERIC_STEM_RE.match(path.stem).group(1))


# --------------------------------------------------------------------------- #
# Prompt helpers. A default is shown in [brackets] and taken on plain Enter.
# --------------------------------------------------------------------------- #

def _with_default(prompt: str, default) -> str:
    return f"{prompt} [{default}] " if default is not None else f"{prompt} "


def ask_str(prompt: str, default: str | None = None) -> str:
    while True:
        value = input(_with_default(prompt, default)).strip()
        if value:
            return value
        if default is not None:
            return default
        print("  Please enter a value.")


def ask_int(prompt: str, minimum: int | None = None, default: int | None = None) -> int:
    while True:
        raw = input(_with_default(prompt, default)).strip()
        if not raw and default is not None:
            return default
        try:
            value = int(raw)
        except ValueError:
            print("  Please enter a whole number.")
            continue
        if minimum is not None and value < minimum:
            print(f"  Please enter a number >= {minimum}.")
            continue
        return value


def ask_yes_no(prompt: str) -> bool:
    while True:
        raw = input(f"{prompt} [y/n] ").strip().lower()
        if raw in ('y', 'yes'):
            return True
        if raw in ('n', 'no'):
            return False
        print("  Please answer y or n.")


# --------------------------------------------------------------------------- #
# Series config and folder-name hints
# --------------------------------------------------------------------------- #

def load_config(folder: Path) -> tuple[dict, Path | None]:
    """The config named after the nearest folder (or parent) that has one."""
    for d in [folder, *folder.parents]:
        if not d.name:
            continue  # drive root
        path = CONFIG_DIR / f"{d.name}.toml"
        if path.is_file():
            try:
                with open(path, 'rb') as f:
                    return tomllib.load(f), path
            except tomllib.TOMLDecodeError as e:
                sys.exit(f"Couldn't read {path}: {e}")
    return {}, None


def find_volume(folder: Path, pattern: str) -> tuple[int | None, Path | None]:
    """Volume number and folder, from the topmost folder name that has one
    (so "Manga_Title_v26_(Ch219-228)" wins over its "Manga_Title V26 Digital Raw")."""
    for d in reversed([folder, *folder.parents]):
        m = re.search(pattern, d.name)
        if m:
            return int(m.group(1)), d
    return None, None


def find_chapter_range(folder: Path, pattern: str) -> tuple[int, int] | None:
    """(first_num, count) from the nearest folder name like "(Ch219-228)"."""
    for d in [folder, *folder.parents]:
        m = re.search(pattern, d.name)
        if m:
            first, last = int(m.group(1)), int(m.group(2))
            if last >= first:
                return first, last - first + 1
    return None


def default_prefix(config: dict, volume: int | None, volume_dir: Path | None) -> str | None:
    template = config.get('prefix')
    if template and volume is not None:
        try:
            return template.format(volume=volume)
        except (KeyError, ValueError, IndexError):
            print(f"  [warn] Couldn't fill in config prefix {template!r}.")
    if volume_dir is not None:
        for p in sorted(volume_dir.iterdir()):
            m = PREFIX_HINT_RE.match(p.name)
            if m:
                return m.group(1)
    return None


def offer_to_save_config(series_dir: Path, prefix: str, volume: int | None,
                         toc_page: str | None, anchor_name: str | None,
                         chapters: list[dict], last_end: int) -> None:
    path = CONFIG_DIR / f"{series_dir.name}.toml"
    if not ask_yes_no(f"\nSave these answers as defaults for this series?\n  ({path})"):
        return

    template = prefix
    m = re.search(r'(\d+)$', prefix)
    if volume is not None and m and int(m.group(1)) == volume:
        template = f"{prefix[:m.start()]}{{volume:0{len(m.group(1))}}}"

    starts = [c['start'] for c in chapters] + [last_end + 1]
    lengths = sorted(b - a for a, b in zip(starts, starts[1:]))
    typical = lengths[len(lengths) // 2]

    lines = [
        "# Manga Filename Chapter Renamer -- defaults for this series.",
        "# {volume} is taken from the volume folder's name (e.g. \"Volume 13\" -> 13).",
        f"prefix = {json.dumps(template, ensure_ascii=False)}",
    ]
    if toc_page:
        lines.append(f"toc_page = {json.dumps(toc_page, ensure_ascii=False)}")
    if anchor_name:
        lines.append(f"first_chapter_file = {json.dumps(anchor_name, ensure_ascii=False)}")
    lines += [
        f"typical_chapter_pages = {typical}",
        "",
        "# Optional table-of-contents OCR tuning (see README):",
        "# column_crops = [0.84, 0.88, 0.90]",
        "# chapter_marker = '(?<![A-Za-z])STEP\\s*(\\S+)'",
        "",
    ]
    CONFIG_DIR.mkdir(exist_ok=True)
    path.write_text("\n".join(lines), encoding='utf-8')
    print(f"  Saved {path}")


# --------------------------------------------------------------------------- #
# Chapters
# --------------------------------------------------------------------------- #

def gather_chapters(count: int, first_num: int,
                    defaults: list[int] | None = None) -> list[dict]:
    """Ask for each chapter's start page. Returns [{'num', 'start'}, ...]."""
    chapters = []
    prev_start = None
    for i in range(count):
        num = first_num + i
        default = defaults[i] if defaults and i < len(defaults) else None
        while True:
            start = ask_int(f"  Which page does chapter {num} start on?",
                            minimum=0, default=default)
            if prev_start is not None and start <= prev_start:
                print(f"  Chapter {num} must start after page {prev_start}.")
                continue
            break
        chapters.append({'num': num, 'start': start})
        prev_start = start
    return chapters


def read_toc_page(folder: Path, config: dict, chapter_range: tuple[int, int] | None,
                  max_page: int, toc_default: str | None) -> dict | None:
    """Offer to OCR the table of contents. Returns toc_ocr.read_toc()'s result
    plus 'toc_page', or None if skipped or impossible."""
    if not ask_yes_no("\nTry to auto-detect table of contents?"):
        return None
    tesseract = toc_ocr.find_tesseract()
    if tesseract is None:
        print("  Tesseract OCR not found -- install it from "
              "https://github.com/UB-Mannheim/tesseract/wiki. Entering manually.")
        return None

    toc_name = ask_str("  Filename of the table of contents page:",
                       default=toc_default)
    toc = folder / toc_name
    if not toc.is_file():
        print(f"  '{toc_name}' not found in that folder. Entering manually.")
        return None

    first_num, count = chapter_range if chapter_range else (None, None)
    print("  Reading table of contents...")
    try:
        result = toc_ocr.read_toc(
            tesseract, toc, max_page, count=count, first_num=first_num,
            typical=config.get('typical_chapter_pages'),
            marker=config.get('chapter_marker'),
            column_crops=config.get('column_crops'),
        )
    except (OSError, RuntimeError) as e:
        print(f"  OCR failed ({e}). Entering manually.")
        return None
    if chapter_range:
        result['numbers_from'] = 'folder name'
    result['toc_page'] = toc_name
    return result


def choose_chapters(toc: dict | None, chapter_range: tuple[int, int] | None,
                    config: dict, max_page: int,
                    current: list[dict] | None = None) -> list[dict]:
    """Show detected chapters for confirmation; otherwise ask, pre-filling
    whatever was detected (or, for an already-renamed folder, the `current`
    chapters) so Enter keeps it."""
    first_num, count = chapter_range if chapter_range else (None, None)
    if current and first_num is None:
        first_num, count = current[0]['num'], len(current)
    starts = None
    if toc:
        first_num, count, starts = toc['first_num'], toc['count'], toc['starts']
        if count is None:
            print("  Couldn't read the chapter numbers.")
        elif starts is None:
            print("  Couldn't read the start pages.")

    while True:
        if starts:
            source = f" (from the {toc['numbers_from']})" if toc['numbers_from'] else ""
            print(f"\n  Chapters {first_num}-{first_num + count - 1}{source}, "
                  f"start pages from the table of contents:")
            for i, (page, note) in enumerate(starts):
                flag = f"   (?) {note} -- please check" if note else ""
                print(f"    Chapter {first_num + i:>3} starts on page {page:>3}{flag}")
            if ask_yes_no("  Is this correct?"):
                return [{'num': first_num + i, 'start': p}
                        for i, (p, _note) in enumerate(starts)]
            print("  Press Enter to keep a value, or type the right one.")

        new_count = ask_int("\nHow many chapters are in this volume?", minimum=1,
                            default=count)
        first_num = ask_int("What is the first chapter's number?", minimum=0,
                            default=first_num)
        if toc and new_count != count:
            # Re-vote the page numbers already read for the corrected count.
            count = new_count
            starts = toc_ocr.vote_start_pages(toc['number_sets'], count, max_page,
                                              config.get('typical_chapter_pages'))
            if starts:
                continue
        count = new_count
        if starts:
            defaults = [p for p, _ in starts]
        elif current and len(current) == count:
            defaults = [c['start'] for c in current]
        else:
            defaults = None
        print()
        return gather_chapters(count, first_num, defaults)


# --------------------------------------------------------------------------- #
# Renaming
# --------------------------------------------------------------------------- #

def tag_for_page(page: int, chapters: list[dict], last_end: int) -> str:
    """Return the tag ('ch000' / 'ch<NNN>' / 'extra') for a corrected page."""
    first_start = chapters[0]['start']
    if page < first_start:
        return "ch000"
    if page > last_end:
        return "extra"
    for i, ch in enumerate(chapters):
        nxt = chapters[i + 1]['start'] if i + 1 < len(chapters) else last_end + 1
        if ch['start'] <= page < nxt:
            return f"ch{ch['num']:03d}"
    # Shouldn't happen given the ranges above, but stay safe.
    return "extra"


def build_plan(files: list[Path], prefix: str, offset: int,
               chapters: list[dict], last_end: int) -> tuple[list[tuple[Path, str]], list[str]]:
    """Return (renames, warnings) where renames is [(src, new_name), ...]."""
    renames = []
    warnings = []
    # Pages shifted to 0 or below become 000a, 000b, ... in order (a bare "000"
    # would sort after "000a", so page 0 is lettered too).
    n_front = sum(1 for f in files if page_number(f) + offset <= 0)
    if n_front > len(ascii_lowercase):
        warnings.append(
            f"{n_front} pages land on page 0 or below, but only "
            f"{len(ascii_lowercase)} letter suffixes (000a-000z) are available "
            f"-- check the anchor page / offset."
        )
        return [], warnings
    front_index = 0
    for f in files:
        corrected = page_number(f) + offset
        if corrected <= 0:
            page_str = f"000{ascii_lowercase[front_index]}"
            front_index += 1
        else:
            page_str = f"{corrected:03d}"
        tag = tag_for_page(corrected, chapters, last_end)
        new_name = f"{prefix}_{tag}_{page_str}{f.suffix.lower()}"
        renames.append((f, new_name))
    return renames, warnings


def apply_renames(renames: list[tuple[Path, str]], folder: Path) -> None:
    """Two-phase rename via temp names so shifted numbers can't collide."""
    temps = []
    for i, (src, _new) in enumerate(renames):
        tmp = folder / f".__renaming_{i}__{src.name}"
        src.rename(tmp)
        temps.append(tmp)
    for tmp, (_src, new_name) in zip(temps, renames):
        tmp.rename(folder / new_name)


# --------------------------------------------------------------------------- #
# Already-renamed folders: update only the chapter tags
# --------------------------------------------------------------------------- #

def current_chapters(named: list[tuple[Path, re.Match]]) -> tuple[list[dict], int] | None:
    """The chapters the files are currently tagged with, as ([{'num', 'start'}],
    last_end), or None if there are none (e.g. everything is ch000) or they
    aren't consecutive."""
    starts, ends = {}, {}
    for _f, m in named:
        if m['tag'].startswith('ch') and int(m['tag'][2:]) > 0:
            num, page = int(m['tag'][2:]), int(m['page'])
            starts[num] = min(starts.get(num, page), page)
            ends[num] = max(ends.get(num, page), page)
    nums = sorted(starts)
    if not nums or nums != list(range(nums[0], nums[-1] + 1)):
        return None
    return [{'num': n, 'start': starts[n]} for n in nums], ends[nums[-1]]


def describe_tags(named: list[tuple[Path, re.Match]]) -> str:
    tags = Counter(m['tag'] for _f, m in named)
    chapter_nums = sorted(int(t[2:]) for t in tags if t.startswith('ch') and t != 'ch000')
    parts = []
    if tags['ch000']:
        parts.append(f"ch000 x{tags['ch000']}")
    if chapter_nums:
        parts.append(f"ch{chapter_nums[0]:03d}-ch{chapter_nums[-1]:03d} "
                     f"({len(chapter_nums)} chapters)")
    for t in ('extra', 'extra_credits'):
        if tags[t]:
            parts.append(f"{t} x{tags[t]}")
    return ", ".join(parts)


def default_named_toc(named: list[tuple[Path, re.Match]], config_toc: str | None) -> str | None:
    """Guess the TOC page in an already-renamed folder. Renaming keeps page
    order, so a raw config default like "004.jpg" means the 4th page.
    Otherwise it's normally the last front-matter page before chapter 1."""
    in_order = sorted(named, key=lambda fm: (int(fm[1]['page']), fm[1]['rest']))
    if config_toc:
        if any(f.name == config_toc for f, _m in named):
            return config_toc
        raw = NUMERIC_STEM_RE.match(Path(config_toc).stem)
        if raw and 1 <= int(raw.group(1)) <= len(in_order):
            return in_order[int(raw.group(1)) - 1][0].name
    if any(m['tag'].startswith('ch') and m['tag'] != 'ch000' for _f, m in named):
        front = [f for f, m in in_order if m['tag'] == 'ch000' and int(m['page']) > 0]
        if front:
            return front[-1].name
    return None


def build_retag_plan(named: list[tuple[Path, re.Match]], prefix: str,
                     chapters: list[dict], last_end: int
                     ) -> tuple[list[tuple[Path, str]], list[str]]:
    """Re-tag already-renamed pages. The page part of each name (including
    suffixes like "000b2" or "105_color") is kept; only the prefix and tag can
    change. Returns (renames, warnings) with only files whose name changes."""
    targets = []
    for f, m in named:
        tag = tag_for_page(int(m['page']), chapters, last_end)
        if m['tag'] == 'extra_credits' and tag == 'extra':
            tag = 'extra_credits'  # keep the credits page marked as such
        targets.append((f, f"{prefix}_{tag}_{m['page']}{m['rest']}{f.suffix}"))

    clashes = [n for n, c in Counter(new for _f, new in targets).items() if c > 1]
    if clashes:
        return [], [f"Several files would be renamed to {n}" for n in clashes]
    return [(f, new) for f, new in targets if new != f.name], []


# --------------------------------------------------------------------------- #
# Main flow
# --------------------------------------------------------------------------- #

def process(folder: Path, dry_run: bool) -> None:
    images = sorted(p for p in folder.iterdir()
                    if p.is_file() and p.suffix.lower() in IMAGE_EXTS)
    files = sorted((p for p in images if is_page_file(p)), key=page_number)
    named = None
    current = None

    if files:
        skipped = [p for p in images if not is_page_file(p)]
        print(f"\nFound {len(files)} page file(s): "
              f"{files[0].name} ... {files[-1].name}")
    else:
        named = [(p, m) for p in images if (m := NAMED_RE.match(p.stem))]
        if not named:
            sys.exit("No page files found. Expected raw names like 001.jpg, or "
                     "already-renamed ones like Manga_Title_v52_ch456_008.jpg.")
        prefixes = Counter(m['prefix'] for _f, m in named)
        if len(prefixes) > 1:
            listing = ", ".join(f"{p} ({n})" for p, n in prefixes.most_common())
            sys.exit(f"The renamed files have different prefixes: {listing}. "
                     f"Make them match first.")
        existing_prefix = next(iter(prefixes))
        skipped = [p for p in images if not NAMED_RE.match(p.stem)]
        current = current_chapters(named)
        print(f"\nFound {len(named)} already-renamed page file(s): {existing_prefix}_...")
        print(f"  Currently tagged: {describe_tags(named)}")

    if skipped:
        print(f"  [warn] {len(skipped)} image(s) in another naming format will be "
              f"left untouched:")
        for s in skipped:
            print(f"         {s.name}")

    if named and not ask_yes_no("\nDo you want to update the chapter values?"):
        print("Nothing changed.")
        return

    config, config_path = load_config(folder)
    if config_path:
        print(f"Series config: {config_path}")
    volume, volume_dir = find_volume(
        folder, config.get('volume_pattern', DEFAULT_VOLUME_PATTERN))
    chapter_range = find_chapter_range(
        folder, config.get('chapter_range_pattern', DEFAULT_CHAPTER_RANGE_PATTERN))

    if named:
        # Page numbers are already final; they can't run far past the last one.
        max_page = max(int(m['page']) for _f, m in named) + 10
        prefix_default = existing_prefix
        config_prefix = default_prefix(config, volume, None)
        if config_prefix and config_prefix != existing_prefix:
            print(f"  [note] The series config's prefix would be {config_prefix}.")
        toc_default = default_named_toc(named, config.get('toc_page'))
    else:
        # Printed page numbers can't run past the number of scanned pages (plus
        # a little slack for pages missing from the scan).
        max_page = len(files) + 10
        prefix_default = default_prefix(config, volume, volume_dir)
        toc_default = config.get('toc_page')

    prefix = ask_str('\nFilename prefix (e.g. "Manga_Title_v52"):', default=prefix_default)
    toc = read_toc_page(folder, config, chapter_range, max_page, toc_default)
    chapters = choose_chapters(toc, chapter_range, config, max_page,
                               current[0] if current else None)

    last_num = chapters[-1]['num']
    end_default = (current[1] if current and current[0][-1]['num'] == last_num
                   else None)
    while True:
        last_end = ask_int(f"  Which page does the last chapter (chapter {last_num}) "
                           f"end on?", minimum=0, default=end_default)
        if last_end >= chapters[-1]['start']:
            break
        print(f"  The last chapter can't end before it starts "
              f"(page {chapters[-1]['start']}).")

    anchor_name = None
    if named:
        renames, warnings = build_retag_plan(named, prefix, chapters, last_end)
    else:
        # Numbering correction.
        offset = 0
        if not ask_yes_no("\nAre the pages numbered correctly based on the table of contents?"):
            anchor_name = ask_str(
                "  What is the current filename of the first page of the first chapter?",
                default=config.get('first_chapter_file'),
            )
            anchor = folder / anchor_name
            if not is_page_file(anchor):
                sys.exit(f"'{anchor_name}' is not a numerically-named page file in that folder.")
            offset = chapters[0]['start'] - page_number(anchor)
            print(f"  Offset = {offset:+d} "
                  f"(page {page_number(anchor)} -> {page_number(anchor) + offset}).")
        renames, warnings = build_plan(files, prefix, offset, chapters, last_end)

    print("\nPlanned renames:")
    for src, new_name in renames:
        print(f"  {src.name:>16}  ->  {new_name}")
    if named:
        print(f"  ({len(named) - len(renames)} file(s) already have the right name)")
    if warnings:
        print("\n[warn]")
        for w in warnings:
            print(f"  {w}")

    if dry_run:
        print("\n(dry run -- no files changed)")
    elif not renames:
        print("\nNothing to rename.")
    elif not ask_yes_no(f"\nApply {len(renames)} rename(s)?"):
        print("Aborted -- no files changed.")
        return
    else:
        apply_renames(renames, folder)
        print(f"Done -- renamed {len(renames)} file(s).")

    if config_path is None and volume_dir is not None:
        # A renamed folder's TOC filename isn't a useful default for raw folders.
        toc_page = toc['toc_page'] if toc and not named else None
        offer_to_save_config(volume_dir.parent, prefix, volume, toc_page,
                             anchor_name, chapters, last_end)


def main():
    ap = argparse.ArgumentParser(
        description="Interactively rename raw scan pages into chapter-tagged "
                    "filenames for Manga Chapter Zipper."
    )
    ap.add_argument('path', help="Path to a single volume folder of raw page files.")
    ap.add_argument('--dry-run', action='store_true',
                    help="Preview the rename plan without changing any files.")
    args = ap.parse_args()

    folder = Path(args.path).expanduser().resolve()
    if not folder.is_dir():
        sys.exit(f"Not a folder: {folder}")

    print(f"Volume folder: {folder}")
    process(folder, args.dry_run)


if __name__ == '__main__':
    main()
