# Manga Filename Chapter Renamer

Interactively renames a folder of raw scan pages (e.g. `001.jpg`, `020.jpg`,
`191.psd`) into the chapter-tagged filename format consumed by
[Manga Chapter Zipper](../Manga-Chapter-Zipper):

```
<prefix>_ch<NNN>_<page>.<ext>     e.g. Manga_Title_v52_ch456_008.jpg
<prefix>_extra_<page>.<ext>       e.g. Manga_Title_v52_extra_191.jpg
```

The page number stays continuous across the whole volume; only the tag changes.

## Requirements

Python 3.11+. Everything else is optional and only needed for
table-of-contents auto-detection (all free and offline):

- [Tesseract OCR](https://github.com/UB-Mannheim/tesseract/wiki), found on
  `PATH` or in `C:\Program Files\Tesseract-OCR\`. Add the Japanese language
  data (`jpn`, `jpn_vert`) so it can read `第…話` / `第…訓` chapter markers.
- [Pillow](https://pypi.org/project/Pillow/) (`pip install pillow`), so the page
  can be read several ways (see below). Without it only one plain pass is made.

## Usage

```bash
python manga_renamer.py "/path/to/Volume_Folder"
python manga_renamer.py "/path/to/Volume_Folder" --dry-run
```

It works on one volume folder at a time (the flow is interactive). Point it at a
folder whose page files have purely numeric names like `001.jpg`. Any image
without a numeric name is reported and left untouched.

## What it asks

Defaults are shown in `[brackets]`; press Enter to accept one.

1. **Filename prefix**, e.g. `Manga_Title_v52`. Defaults to the series config's
   prefix, or to the name of an output folder like
   `Manga_Title_v13_[Group]` next to the raw folder.
2. **Try to auto-detect table of contents?** If yes, give the TOC page's
   filename. It reads the chapters and start pages, shows them (flagging any it
   isn't sure of with `(?)`), and asks you to confirm. If you say no, the next
   questions are pre-filled with what it read, so you only type the wrong ones.
3. **How many chapters** are in the volume.
4. **The first chapter's number**. Chapters increment by 1 from there
   (12, 13, 14, …). If a folder name contains a range like `(Ch219-228)`, that
   fills in 3 and 4.
5. **Which page each chapter starts on.**
6. **Which page the last chapter ends on.**
7. **Whether the pages are numbered correctly** per the table of contents.

After a run in a series with no config, it offers to save your answers as one.

## Already-renamed folders

If a folder has no `001.jpg`-style files but its pages are already named like
`Manga_Title_v12_ch000_152.jpg`, it shows how they're currently
tagged and asks **"Do you want to update the chapter values?"** Use this to fix
chapter tags, e.g. a volume where every page ended up as `ch000`.

- Page numbers are kept as they are, so there's no numbering question.
  Suffixes like `000b2` or `105_color` are kept, and `extra_credits` pages stay
  `extra_credits`.
- The prefix defaults to the existing one. If the series config would give a
  different prefix, it says so; type the config's one to fix the prefix at the
  same time.
- The TOC page defaults to the right renamed file (the config's `toc_page`
  counted by position, so `004.jpg` means the 4th page), and the chapter
  questions default to the current tags.
- Only files whose name changes are renamed.

## Series config

`configs/<series folder name>.toml` supplies defaults for any volume under a
folder with that name. For example, `configs/Manga_Title.toml`
applies to `K:\Timing Programs\Manga_Title\Volume 13\Raw`. Every
key is optional. `configs/` is git-ignored, apart from
[`configs/example.toml`](configs/example.toml), so your own configs stay local.

```toml
# configs/Manga_Title.toml
prefix = "Manga_Title_v{volume:02}"
toc_page = "004.jpg"
first_chapter_file = "005.jpg"
typical_chapter_pages = 16
```

| Key | Meaning |
|---|---|
| `prefix` | Filename prefix. `{volume}` is the volume number from the folder name; `{volume:02}` zero-pads it. |
| `toc_page` | Default filename of the table-of-contents page. |
| `first_chapter_file` | Default filename of the first chapter's first page, for the numbering correction. |
| `typical_chapter_pages` | Usual chapter length. Helps the TOC reader reject misread page numbers. |
| `column_crops` | Where page-number columns start, as fractions of page width (default `[0.84, 0.88, 0.90]`). Tune for TOCs whose numbers sit at the right edge after dash leaders. |
| `chapter_marker` | Regex for a chapter marker with one group for its number (default matches `STEP 12`, `Chapter 12`, `Ch. 12`, `#012`, …). |
| `volume_pattern` | Regex for the volume number in a folder name (default matches `Volume 13`, `Vol. 13`, `_v26`). |
| `chapter_range_pattern` | Regex with two groups for a chapter range in a folder name (default matches `(Ch219-228)`). |

Example for a series whose page numbers need a tighter crop:

```toml
# configs/Manga_Title.toml
prefix = "Manga_Title_v{volume:02}"
column_crops = [0.90]
typical_chapter_pages = 20
```

## Table-of-contents detection

OCR on stylised TOC fonts is unreliable, and no single setting works for every
layout, so `toc_ocr.py` reads the page several times: the whole page and
right-hand crops, at different sizes and contrast. Then:

- **Chapter numbers** come from the folder name if it has a range. Otherwise
  they're read from chapter markers (`STEP 123`, `#033`, or `第…話` with
  Japanese installed), combining every pass, since each misses different lines.
- **Start pages** are chosen by vote: the rising sequence of page numbers most
  passes agree on, preferring similar chapter lengths. Passes that read a
  believable chapter list count for more than ones that picked up stray digits.
- **Doubtful pages** are flagged: ones another reading contradicts ("also read as
  …") or that were barely readable. Always check flagged pages.

Treat it as a quick way to fill in the answers, not something to trust blindly.
In testing on six volumes across five series, it got every page right for five.
In one series a bold `7` is misread as `1` in every pass, but those entries
are flagged.

## Tagging rules

For each (corrected) page number:

- Pages **before** the first chapter's start → `ch000`
- Pages within `[chapter N start, chapter N+1 start)` → `ch<N>`
- Pages **after** the last chapter's end → `extra`

Both the chapter number and page number are zero-padded to 3 digits; the
original file extension is preserved.

## Numbering correction

If you answer **No** to *"Are the pages numbered correctly?"*, it asks for the
current filename of the first page of the first chapter (the *anchor*). It then
shifts every page number by a constant offset so the anchor lands on the
chapter's true table-of-contents page, and tags the corrected numbers.

Example — anchor `004.jpg`, first chapter starts on page `3`:

```
offset = 3 - 4 = -1
001 -> 000a, 002 -> 001, 003 -> 002, 004 -> 003, ...
```

Pages that land on 0 or below become `000a`, `000b`, … in order. Example —
anchor `005.jpg`, first chapter starts on page `3`:

```
offset = 3 - 5 = -2
001 -> 000a, 002 -> 000b, 003 -> 001, 004 -> 002, 005 -> 003, ...
```

The corrected number is baked directly into the final filename, so there is no
separate renumbering pass.

## Safety

- Nothing is renamed until you confirm the previewed plan.
- Renames go through temporary names, so shifted page numbers can't clobber each
  other mid-operation.
- `--dry-run` previews the full plan and changes nothing.

## Notes / limitations

- **Chapter numbers are assumed sequential** from the first chapter number. If a
  volume skips a chapter number, run it once per contiguous run of chapters, or
  rename the odd chapter manually afterward.
- One volume folder per run.
