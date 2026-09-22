"""
toc_ocr.py

Reads chapter start pages (and, when possible, chapter numbers) from a scanned
table-of-contents page using Tesseract OCR. Free and offline.

No single OCR pass is reliable on stylised TOC fonts, so the page is read
several times with different crops / scales / contrast, and the start pages are
chosen by vote: the increasing sequence of N page numbers that the most passes
agree on. Numbers only one pass saw, or that had a close rival, are flagged so
the user can check them.

Pillow is used for cropping/scaling if installed; without it only a single
whole-page pass is made.
"""

import io
import os
import re
import shutil
import subprocess
import unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

try:
    from PIL import Image
except ImportError:  # Pillow is optional.
    Image = None

TESSERACT_FALLBACK_PATHS = [
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
]

# Right-hand crops (fraction of page width where the crop starts) used to read
# page numbers that sit at the end of dot/dash leaders. Overridable per series.
DEFAULT_COLUMN_CROPS = [0.84, 0.88, 0.90]

# English chapter markers, e.g. "STEP 123", "Chapter 12", "Ch. 7", "#033".
# Searched anywhere in a line, since artwork beside the TOC often OCRs as junk
# in front of it. The number is captured loosely because OCR often garbles it
# (e.g. "U7").
DEFAULT_MARKER_RE = (r'(?<![A-Za-z])(?:STEP|CHAPTER|CH\.?|EPISODE|EP\.?|ACT|PART|#)'
                     r'(?![A-Za-z])\s*([0-9A-Za-z|]*)')
# Japanese chapter markers, e.g. "第二百十九訓", "第12話".
JP_MARKER_RE = re.compile(r'第\s*([〇零一二三四五六七八九十百千0-9０-９]+)\s*[話訓回章幕]')

# Trailing page-number token at the end of a line, after leaders like "-----"
# or "____". Allows letters OCR confuses with digits ("IOI" -> 101, "oo9" -> 9).
TRAILING_PAGE_RE = re.compile(
    r'''(?:^|[^0-9A-Za-z])([0-9IlOo|]+)[\s.,:;'"’)\]』」]*$''')
OCR_DIGIT_FIXES = str.maketrans({'I': '1', 'l': '1', '|': '1', 'O': '0', 'o': '0'})

KANJI_DIGITS = {'〇': 0, '零': 0, '一': 1, '二': 2, '三': 3, '四': 4,
                '五': 5, '六': 6, '七': 7, '八': 8, '九': 9}
KANJI_UNITS = {'十': 10, '百': 100, '千': 1000}

MIN_CHAPTER_PAGES = 3
# Cost of skipping a value inside the chosen run, scaled by how many passes saw
# it: skipping a number most passes read suggests a missed entry, skipping one
# a single pass read is just dropping noise.
SKIP_WEIGHT = 0.5
# Cost of a chapter length deviating from the typical length (per typical length).
LENGTH_WEIGHT = 1.0
# Chapters shorter/longer than these multiples of the typical length are flagged.
ODD_LENGTH_LOW = 0.6
ODD_LENGTH_HIGH = 1.5


# --------------------------------------------------------------------------- #
# Running Tesseract
# --------------------------------------------------------------------------- #

def find_tesseract() -> str | None:
    found = shutil.which('tesseract')
    if found:
        return found
    for p in TESSERACT_FALLBACK_PATHS:
        if Path(p).is_file():
            return p
    return None


def installed_languages(tesseract: str) -> set[str]:
    out = subprocess.run([tesseract, '--list-langs'], capture_output=True,
                         text=True).stdout
    return set(out.split()[1:]) if out else set()  # first token is a header


def run_tesseract(tesseract: str, image_bytes: bytes, lang: str = 'eng',
                  digits_only: bool = False) -> str:
    """OCR image bytes. Fed via stdin, since Tesseract can't open some
    non-ASCII paths (e.g. "ō") on Windows."""
    cmd = [tesseract, 'stdin', 'stdout', '--psm', '6', '-l', lang]
    if digits_only:
        cmd += ['-c', 'tessedit_char_whitelist=0123456789']
    # Passes run in parallel; stop each Tesseract from also using every core.
    env = dict(os.environ, OMP_THREAD_LIMIT='1')
    result = subprocess.run(cmd, input=image_bytes, capture_output=True, env=env)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode(errors='replace').strip())
    return result.stdout.decode('utf-8', errors='replace')


def prepare_image(path: Path, crop_left: float | None, scale: int,
                  threshold: bool) -> bytes:
    im = Image.open(path).convert('L')
    if crop_left:
        im = im.crop((int(im.width * crop_left), 0, im.width, im.height))
    if scale > 1:
        im = im.resize((im.width * scale, im.height * scale), Image.LANCZOS)
    if threshold:
        im = im.point(lambda p: 255 if p > 150 else 0)
    buf = io.BytesIO()
    im.save(buf, 'PNG')
    return buf.getvalue()


def read_passes(tesseract: str, toc: Path,
                column_crops: list[float] | None = None) -> list[tuple[str, str]]:
    """OCR the TOC page several ways. Returns [(kind, text), ...] where kind is
    'page' (whole page, first entry is plain scale-1) or 'column' (right-hand
    crop, digits only)."""
    if Image is None:
        return [('page', run_tesseract(tesseract, toc.read_bytes()))]

    crops = DEFAULT_COLUMN_CROPS if column_crops is None else column_crops
    specs = [('page', None, 1, False), ('page', None, 2, False),
             ('page', None, 1, True), ('page', None, 2, True)]
    for c in crops:
        specs += [('column', c, 1, False), ('column', c, 2, True),
                  ('column', c, 3, False), ('column', c, 3, True),
                  ('column', c, 4, True)]

    def run(spec):
        kind, crop, scale, thr = spec
        data = prepare_image(toc, crop, scale, thr)
        return kind, run_tesseract(tesseract, data, digits_only=(kind == 'column'))

    with ThreadPoolExecutor(max_workers=os.cpu_count() or 4) as pool:
        return list(pool.map(run, specs))


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #

def ocr_int(token: str) -> int | None:
    fixed = token.translate(OCR_DIGIT_FIXES)
    return int(fixed) if fixed.isdigit() else None


def kanji_to_int(s: str) -> int | None:
    s = unicodedata.normalize('NFKC', s)
    if s.isdigit():
        return int(s)
    if all(ch in KANJI_DIGITS for ch in s):  # positional, e.g. 二〇五
        return int(''.join(str(KANJI_DIGITS[ch]) for ch in s))
    total, digit = 0, None
    for ch in s:
        if ch in KANJI_DIGITS:
            digit = KANJI_DIGITS[ch]
        elif ch in KANJI_UNITS:
            total += (1 if digit is None else digit) * KANJI_UNITS[ch]
            digit = None
        else:
            return None
    return total + (digit or 0)


def page_numbers(kind: str, text: str, marker_re: re.Pattern) -> set[int]:
    """Candidate page numbers in one pass's text."""
    if kind == 'column':
        return {int(t) for t in re.findall(r'\d+', text)}
    found = set()
    for line in text.splitlines():
        m = marker_re.search(line)
        rest = line[m.end():] if m else line  # skip the chapter number itself
        page = TRAILING_PAGE_RE.search(rest)
        if not page:
            continue
        token = page.group(1)
        if len(token) == 1 and not token.isdigit():  # a lone "I" / "O" word
            continue
        value = ocr_int(token)
        if value is not None:
            found.add(value)
    return found


def chapter_run(readings: list[list[int | None]]) -> tuple[int, int] | None:
    """Chapters are numbered consecutively, so take the longest run of
    consecutive chapter numbers seen across all passes (each pass misses or
    garbles different lines). A single number no pass read inside the run is
    assumed to exist. Returns (first_num, count), or None if no two passes /
    markers back it up."""
    votes = Counter(n for nums in readings for n in set(nums) if n is not None)
    best, best_score = None, 1
    for start in votes:
        if start - 1 in votes:
            continue  # not the start of a run
        n, score = start, votes[start]
        while n + 1 in votes or (n + 2 in votes and n + 1 not in votes):
            n = n + 1 if n + 1 in votes else n + 2
            score += votes[n]
        if n > start and score > best_score:
            best, best_score = (start, n - start + 1), score
    return best


def marker_numbers_en(text: str, marker_re: re.Pattern) -> list[int | None]:
    return [ocr_int(m.group(1)) for line in text.splitlines()
            if (m := marker_re.search(line))]


def marker_numbers_jp(text: str) -> list[int | None]:
    return [kanji_to_int(m.group(1)) for m in JP_MARKER_RE.finditer(text)]


# --------------------------------------------------------------------------- #
# Voting
# --------------------------------------------------------------------------- #

def pass_weight(numbers: set[int], count: int, max_page: int) -> float:
    """How much to trust one pass: the length of the longest chain of its
    numbers that could be chapter starts (increasing, plausible lengths), as a
    fraction of `count`, squared. A pass that cleanly read the whole list gets
    1.0; one that read a few stray digits gets close to 0. Passes are far from
    independent (similar crops make the same mistakes), so plain vote counting
    lets repeated junk win."""
    vals = sorted(v for v in numbers if 1 <= v <= max_page)
    max_gap = max(3 * max_page // max(count, 1), MIN_CHAPTER_PAGES + 1)
    best = [1] * len(vals)
    for i in range(len(vals)):
        for j in range(i):
            if MIN_CHAPTER_PAGES <= vals[i] - vals[j] <= max_gap:
                best[i] = max(best[i], best[j] + 1)
    chain = min(max(best, default=0), count)
    return (chain / count) ** 2 if count else 0.0


def vote_start_pages(number_sets: list[set[int]], count: int, max_page: int,
                     typical: int | None = None) -> list[tuple[int, str | None]] | None:
    """Pick `count` increasing start pages that the passes agree on most.

    Returns [(page, note), ...] where note explains why a page is doubtful, or
    None if there aren't enough plausible numbers.
    """
    votes = Counter()
    for s in number_sets:
        w = pass_weight(s, count, max_page)
        for v in s:
            if 1 <= v <= max_page:
                votes[v] += w
    total = sum(pass_weight(s, count, max_page) for s in number_sets)
    if len(votes) < count or total == 0:
        return None
    chosen = _best_sequence(votes, count, total, typical)
    if chosen and typical is None and count > 2:
        # Chapters in a volume are usually similar lengths; re-vote preferring
        # the median length of the first pick.
        typical = _median_gap(chosen)
        chosen = _best_sequence(votes, count, total, typical)
    if not chosen:
        return None
    typical = typical or _median_gap(chosen) or max_page

    vals = sorted(votes)
    result = []
    for t, v in enumerate(chosen):
        # Rivals are other readings of this entry: values between its
        # neighbours (within a chapter's length at the ends, since spin-offs
        # and extras listed after the last chapter aren't rivals).
        lo = chosen[t - 1] if t > 0 else v - ODD_LENGTH_HIGH * typical
        hi = chosen[t + 1] if t + 1 < len(chosen) else v + typical
        rivals = sorted((u for u in vals
                         if lo < u < hi and u != v and votes[u] * 2 >= votes[v]),
                        key=lambda u: -votes[u])
        gap = v - chosen[t - 1] if t > 0 else None
        if rivals:
            note = "also read as " + ", ".join(map(str, rivals[:3]))
        elif gap is not None and gap < ODD_LENGTH_LOW * typical:
            note = f"only {gap} pages after the previous chapter"
        elif gap is not None and gap > ODD_LENGTH_HIGH * typical:
            note = f"{gap} pages after the previous chapter -- one may be missing"
        elif votes[v] < 0.2 * total:
            note = "barely readable"
        else:
            note = None
        result.append((v, note))
    return result


def _median_gap(seq: list[int]) -> int | None:
    gaps = sorted(b - a for a, b in zip(seq, seq[1:]))
    return gaps[len(gaps) // 2] if gaps else None


def _best_sequence(votes: Counter, count: int, total_weight: float,
                   typical: int | None) -> list[int] | None:
    vals = sorted(votes)
    m = len(vals)
    # prefix[i] = total skip cost of vals[:i], to cost skipped values quickly.
    prefix = [0.0]
    for v in vals:
        prefix.append(prefix[-1] + SKIP_WEIGHT * votes[v] * votes[v] / total_weight)

    def length_cost(gap: int) -> float:
        return LENGTH_WEIGHT * abs(gap - typical) / typical if typical else 0.0

    neg = float('-inf')
    # dp[k][i]: best score choosing k+1 values, the last being vals[i].
    dp = [[neg] * m for _ in range(count)]
    back = [[-1] * m for _ in range(count)]
    for i in range(m):
        # Values before the first chapter are usually noise; don't penalise.
        dp[0][i] = votes[vals[i]]
    for k in range(1, count):
        for i in range(m):
            for j in range(i):
                if dp[k - 1][j] == neg:
                    continue
                gap = vals[i] - vals[j]
                if gap < MIN_CHAPTER_PAGES:
                    continue
                skipped = prefix[i] - prefix[j + 1]
                score = (dp[k - 1][j] + votes[vals[i]]
                         - skipped - length_cost(gap))
                if score > dp[k][i]:
                    dp[k][i], back[k][i] = score, j

    end = max(range(m), key=lambda i: dp[count - 1][i])
    if dp[count - 1][end] == neg:
        return None
    chosen = [end]
    for k in range(count - 1, 0, -1):
        chosen.append(back[k][chosen[-1]])
    return [vals[i] for i in reversed(chosen)]


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def read_toc(tesseract: str, toc: Path, max_page: int, *,
             count: int | None = None, first_num: int | None = None,
             typical: int | None = None, marker: str | None = None,
             column_crops: list[float] | None = None) -> dict:
    """Read a TOC page.

    `count` / `first_num` should be given when known (e.g. from the folder
    name); otherwise they're read from chapter markers if possible.

    Returns {'first_num', 'count', 'numbers_from', 'starts', 'number_sets'}
    where starts is [(page, note), ...] or None if the page numbers couldn't be
    read, and number_sets can be passed back to vote_start_pages() to re-vote
    with a corrected count without re-reading the page.
    """
    marker_re = re.compile(marker or DEFAULT_MARKER_RE, re.IGNORECASE)
    passes = read_passes(tesseract, toc, column_crops)

    numbers_from = 'given'
    if count is None or first_num is None:
        found = chapter_run([marker_numbers_en(text, marker_re)
                             for kind, text in passes if kind == 'page'])
        if found is None and 'jpn' in installed_languages(tesseract):
            jp_text = run_tesseract(tesseract, toc.read_bytes(), lang='jpn')
            found = chapter_run([marker_numbers_jp(jp_text)])
        if found:
            first_num, count = found
            numbers_from = 'table of contents'
        else:
            numbers_from = None

    sets = [page_numbers(kind, text, marker_re) for kind, text in passes]
    starts = vote_start_pages(sets, count, max_page, typical) if count else None
    return {'first_num': first_num, 'count': count, 'numbers_from': numbers_from,
            'starts': starts, 'number_sets': sets}
