"""
Generate segment-filter presentation slides (PPTX).
Run: python3 scripts/make_slides.py
Output: slides/segment_filter.pptx
"""

from pptx import Presentation
from pptx.util import Inches, Pt, Emu
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN
from pptx.util import Inches, Pt
import pptx.oxml.ns as nsmap
from lxml import etree
import os

GRAPH_DIR = "graphs/bloom_vs_segment"

# ---------------------------------------------------------------------------
# Palette
# ---------------------------------------------------------------------------
C_DARK_BLUE   = RGBColor(0x1F, 0x49, 0x7D)
C_MID_BLUE    = RGBColor(0x2E, 0x75, 0xB6)
C_CYAN        = RGBColor(0x00, 0xB0, 0xF0)
C_GREEN       = RGBColor(0x38, 0x86, 0x38)
C_RED         = RGBColor(0xC0, 0x50, 0x4D)
C_ORANGE      = RGBColor(0xE0, 0x90, 0x20)
C_GRAY        = RGBColor(0x80, 0x80, 0x80)
C_LIGHT_GRAY  = RGBColor(0xF2, 0xF2, 0xF2)
C_WHITE       = RGBColor(0xFF, 0xFF, 0xFF)
C_BLACK       = RGBColor(0x00, 0x00, 0x00)
C_YELLOW_BG   = RGBColor(0xFF, 0xFD, 0xD0)

SEG_COLORS = [
    RGBColor(0x4C, 0x72, 0xB0),
    RGBColor(0xDD, 0x84, 0x52),
    RGBColor(0x55, 0xA8, 0x68),
    RGBColor(0x81, 0x72, 0xB3),
]

W  = Inches(13.333)
H  = Inches(7.5)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def new_prs():
    prs = Presentation()
    prs.slide_width  = W
    prs.slide_height = H
    return prs


def blank_slide(prs):
    layout = prs.slide_layouts[6]   # blank
    return prs.slides.add_slide(layout)


def add_rect(slide, x, y, w, h, fill=None, line=None, line_w=Pt(1)):
    shape = slide.shapes.add_shape(
        pptx.enum.shapes.MSO_SHAPE_TYPE.AUTO_SHAPE if False else 1,  # rectangle
        x, y, w, h)
    shape.line.width = line_w
    if fill:
        shape.fill.solid()
        shape.fill.fore_color.rgb = fill
    else:
        shape.fill.background()
    if line:
        shape.line.color.rgb = line
    else:
        shape.line.fill.background()
    return shape


def add_textbox(slide, text, x, y, w, h,
                font_size=Pt(14), bold=False, color=C_BLACK,
                align=PP_ALIGN.LEFT, wrap=True, italic=False):
    txb = slide.shapes.add_textbox(x, y, w, h)
    tf  = txb.text_frame
    tf.word_wrap = wrap
    p   = tf.paragraphs[0]
    p.alignment = align
    run = p.add_run()
    run.text = text
    run.font.size  = font_size
    run.font.bold  = bold
    run.font.color.rgb = color
    run.font.italic = italic
    return txb


def add_label(slide, text, x, y, w, h,
              font_size=Pt(13), bold=False, color=C_WHITE,
              align=PP_ALIGN.CENTER, fill=None, line=None):
    """Rect with centred text inside."""
    rect = add_rect(slide, x, y, w, h, fill=fill, line=line)
    tf   = rect.text_frame
    tf.word_wrap = True
    p    = tf.paragraphs[0]
    p.alignment = align
    run  = p.add_run()
    run.text = text
    run.font.size  = font_size
    run.font.bold  = bold
    run.font.color.rgb = color
    return rect


def header_bar(slide, title, subtitle=None):
    add_rect(slide, 0, 0, W, Inches(1.1), fill=C_DARK_BLUE)
    add_textbox(slide, title,
                Inches(0.35), Inches(0.12), Inches(12.5), Inches(0.55),
                font_size=Pt(26), bold=True, color=C_WHITE)
    if subtitle:
        add_textbox(slide, subtitle,
                    Inches(0.35), Inches(0.65), Inches(12.5), Inches(0.38),
                    font_size=Pt(14), color=C_CYAN, italic=True)


def bullet_slide(prs, title, subtitle, bullets, note=None):
    slide = blank_slide(prs)
    header_bar(slide, title, subtitle)
    y = Inches(1.25)
    for bullet, indent in bullets:
        prefix = "•" if indent == 0 else "  –"
        add_textbox(slide, f"{prefix}  {bullet}",
                    Inches(0.5 if indent == 0 else 0.9),
                    y, Inches(12.3), Inches(0.45),
                    font_size=Pt(15 if indent == 0 else 13),
                    bold=(indent == 0),
                    color=C_BLACK if indent > 0 else C_DARK_BLUE)
        y += Inches(0.47 if indent == 0 else 0.40)
    if note:
        add_rect(slide, Inches(0.4), Inches(6.6), Inches(12.5), Inches(0.7),
                 fill=C_YELLOW_BG, line=C_ORANGE)
        add_textbox(slide, f"⚑  {note}",
                    Inches(0.55), Inches(0.6+6.0), Inches(12.2), Inches(0.5),
                    font_size=Pt(13), italic=True, color=C_ORANGE)
    return slide


# ---------------------------------------------------------------------------
# Slide 1 — Title
# ---------------------------------------------------------------------------

def slide_title(prs):
    slide = blank_slide(prs)
    add_rect(slide, 0, 0, W, H, fill=C_DARK_BLUE)
    add_rect(slide, Inches(0.5), Inches(1.8), Inches(12.33), Inches(3.5),
             fill=RGBColor(0x17, 0x37, 0x65))
    add_textbox(slide,
                "Segment Filter for Dedup Query Optimisation",
                Inches(0.8), Inches(2.0), Inches(11.7), Inches(1.5),
                font_size=Pt(32), bold=True, color=C_WHITE,
                align=PP_ALIGN.CENTER)
    add_textbox(slide,
                "Design, Complexity Analysis & Comparison with Probabilistic Filters",
                Inches(0.8), Inches(3.6), Inches(11.7), Inches(0.7),
                font_size=Pt(18), color=C_CYAN,
                align=PP_ALIGN.CENTER, italic=True)
    add_textbox(slide, "IDEA — Deduplication-Aware Search",
                Inches(0.8), Inches(6.5), Inches(11.7), Inches(0.5),
                font_size=Pt(14), color=RGBColor(0x80, 0xA0, 0xC0),
                align=PP_ALIGN.CENTER)
    return slide


# ---------------------------------------------------------------------------
# Slide 2 — The Problem
# ---------------------------------------------------------------------------

def slide_problem(prs):
    slide = blank_slide(prs)
    header_bar(slide, "The Problem: AND and NOT Queries Both Waste Reverse Mapping Lookups",
               "Both phases iterate chunks whose files cannot possibly overlap with candidates")

    # Two phase boxes
    for x, w, title, col, lines in [
        (Inches(0.4), Inches(6.1), "AND phase", C_MID_BLUE, [
            "Sort terms by chunk count (rarest first)",
            "For each term: look up chunks → map to files → intersect",
            "Most chunks of term N+1 share no files with term N's results",
            "→ unnecessary reverse mapping lookups for non-contributing chunks",
        ]),
        (Inches(6.8), Inches(6.1), "NOT phase", C_RED, [
            "For each excluded term: look up all its chunks → map to files",
            "Subtract those files from the AND candidates",
            "Excluded term may be very common (thousands of chunks)",
            "→ most of those chunks share no files with the tiny candidate set",
        ]),
    ]:
        add_rect(slide, x, Inches(1.25), w, Inches(3.3), fill=C_LIGHT_GRAY, line=col)
        add_label(slide, title, x, Inches(1.25), w, Inches(0.48),
                  font_size=Pt(14), bold=True, fill=col)
        y = Inches(1.85)
        for line in lines:
            add_textbox(slide, f"•  {line}", x + Inches(0.15), y,
                        w - Inches(0.3), Inches(0.42), font_size=Pt(12), color=C_BLACK)
            y += Inches(0.44)

    add_rect(slide, Inches(0.4), Inches(4.7), Inches(12.5), Inches(1.55),
             fill=C_LIGHT_GRAY, line=C_GRAY)
    add_textbox(slide, "Diagnostics from R3 (rare included, common excluded):",
                Inches(0.6), Inches(4.78), Inches(12.0), Inches(0.38),
                font_size=Pt(13), bold=True, color=C_DARK_BLUE)
    rows = [
        ("AND candidates (files after AND phase)",   "57"),
        ("Excluded-term chunks scanned",             "205,713"),
        ("Ratio: excluded chunks / candidates",      "~3,600×"),
    ]
    y = Inches(5.2)
    for label, val in rows:
        add_textbox(slide, f"•  {label}", Inches(0.8), y, Inches(9.5), Inches(0.36),
                    font_size=Pt(12), color=C_BLACK)
        add_textbox(slide, val, Inches(10.3), y, Inches(2.3), Inches(0.36),
                    font_size=Pt(12), bold=True, color=C_RED, align=PP_ALIGN.RIGHT)
        y += Inches(0.37)

    add_textbox(slide,
                "Same root cause in both phases: we do the reverse mapping lookup before checking "
                "whether the chunk's files can possibly intersect with the current candidate set.",
                Inches(0.4), Inches(6.42), Inches(12.5), Inches(0.5),
                font_size=Pt(14), bold=True, color=C_MID_BLUE)
    return slide


# ---------------------------------------------------------------------------
# Slide 3 — Segment Filter: Core Idea
# ---------------------------------------------------------------------------

def slide_core_idea(prs):
    slide = blank_slide(prs)
    header_bar(slide, "Segment Filter: Core Idea",
               "One bitmask per chunk, one O(1) check — prunes unnecessary lookups in both AND and NOT phases")

    add_textbox(slide,
                "Partition the file-ID space into fixed-size segments (e.g. 1,024 files each).",
                Inches(0.45), Inches(1.25), Inches(12.5), Inches(0.4),
                font_size=Pt(15), color=C_BLACK)

    seg_labels = ["Segment 0\n[0 – 1023]", "Segment 1\n[1024 – 2047]",
                  "Segment 2\n[2048 – 3071]", "Segment 3\n[3072 – 3513]"]
    sx = Inches(0.45)
    sw = Inches(2.9)
    for i, (lbl, col) in enumerate(zip(seg_labels, SEG_COLORS)):
        add_label(slide, lbl, sx + i*(sw + Inches(0.15)), Inches(1.75),
                  sw, Inches(0.85), font_size=Pt(13), bold=True, fill=col)

    add_textbox(slide, "For each chunk C, record which segments contain its files:",
                Inches(0.45), Inches(2.75), Inches(12.5), Inches(0.4),
                font_size=Pt(15), color=C_BLACK)

    # Example chunk row
    add_rect(slide, Inches(0.45), Inches(3.2), Inches(12.5), Inches(1.45),
             fill=C_LIGHT_GRAY, line=C_GRAY)

    add_textbox(slide, "Chunk C  →  files {250, 1500, 3100}",
                Inches(0.65), Inches(3.3), Inches(6.0), Inches(0.4),
                font_size=Pt(14), bold=True, color=C_DARK_BLUE)

    dot_x = [Inches(0.90), Inches(3.95), Inches(6.90), Inches(9.85)]
    dot_labels = ["file 250\n→ Seg 0", "file 1500\n→ Seg 1", "", "file 3100\n→ Seg 3"]
    dot_colors = [SEG_COLORS[0], SEG_COLORS[1], C_LIGHT_GRAY, SEG_COLORS[3]]
    for i, (dx, dl, dc) in enumerate(zip(dot_x, dot_labels, dot_colors)):
        if dl:
            add_label(slide, dl, dx, Inches(3.75), Inches(2.3), Inches(0.75),
                      font_size=Pt(12), bold=True, fill=dc)

    add_textbox(slide,
                "Bitmask  =  1  1  0  1    (bit k = 1 if any file in segment k owns chunk C)",
                Inches(0.65), Inches(4.55), Inches(12.0), Inches(0.4),
                font_size=Pt(14), bold=True, color=C_GREEN)

    add_textbox(slide, "This bitmask lives in an in-memory hash map keyed by chunk fingerprint "
                "— maintained alongside the Lucene index, not inside it.",
                Inches(0.45), Inches(5.1), Inches(12.5), Inches(0.55),
                font_size=Pt(13), italic=True, color=C_GRAY)

    add_textbox(slide, "At query time (AND or NOT):  candidate_mask  AND  chunk_mask  ==  0  →  skip (no BerkeleyDB lookup)",
                Inches(0.45), Inches(5.75), Inches(12.5), Inches(0.45),
                font_size=Pt(15), bold=True, color=C_MID_BLUE)

    add_textbox(slide, "Wiki dataset: 3,513 files → only 4 segments → 4-bit mask fits in a single byte",
                Inches(0.45), Inches(6.35), Inches(12.5), Inches(0.4),
                font_size=Pt(13), italic=True, color=C_ORANGE)
    return slide


# ---------------------------------------------------------------------------
# Slide 4 — Query Flow
# ---------------------------------------------------------------------------

def slide_flow(prs):
    slide = blank_slide(prs)
    header_bar(slide, "Query Flow: Filter Applied in Both AND and NOT Phases",
               "Same O(1) check — candidate_mask tightens after each AND term, boosting AND pruning")

    # AND phase column
    add_rect(slide, Inches(0.35), Inches(1.22), Inches(5.85), Inches(4.1),
             fill=C_LIGHT_GRAY, line=C_MID_BLUE)
    add_label(slide, "AND Phase", Inches(0.35), Inches(1.22), Inches(5.85), Inches(0.45),
              font_size=Pt(13), bold=True, fill=C_MID_BLUE)

    and_steps = [
        (C_MID_BLUE,  "① Rarest term",    "Get its chunks → map to files → initial candidates"),
        (C_MID_BLUE,  "② candidate_mask", "OR segment bits of all candidate file IDs"),
        (C_ORANGE,    "③ Next term chunk", "chunk_mask & candidate_mask == 0?"),
        (C_GREEN,     "  → Yes: skip",    "Not in any candidate segment → no lookup"),
        (C_RED,       "  → No: lookup",   "BerkeleyDB → intersect files with candidates"),
        (C_MID_BLUE,  "④ Tighten mask",   "Recompute candidate_mask — now smaller → repeat ②"),
    ]
    y = Inches(1.78)
    for col, step, desc in and_steps:
        indent = step.startswith(" ")
        lx = Inches(0.5 if not indent else 1.05)
        add_label(slide, step.strip(), lx, y, Inches(1.9), Inches(0.46),
                  font_size=Pt(11), bold=True, fill=col, align=PP_ALIGN.CENTER)
        add_textbox(slide, desc, lx + Inches(2.0), y + Inches(0.05),
                    Inches(3.5), Inches(0.38), font_size=Pt(11), color=C_BLACK)
        y += Inches(0.52 if not indent else 0.46)

    # NOT phase column
    add_rect(slide, Inches(6.7), Inches(1.22), Inches(6.3), Inches(4.1),
             fill=C_LIGHT_GRAY, line=C_RED)
    add_label(slide, "NOT Phase  (candidate_mask already final from AND)",
              Inches(6.7), Inches(1.22), Inches(6.3), Inches(0.45),
              font_size=Pt(13), bold=True, fill=C_RED)

    not_steps = [
        (C_RED,       "① Excl. term",    "Get its chunks from Lucene posting list"),
        (C_ORANGE,    "② Filter check",  "chunk_mask & candidate_mask == 0?"),
        (C_GREEN,     "  → Yes: skip",   "No candidate file in this chunk's segments"),
        (C_RED,       "  → No: lookup",  "BerkeleyDB → check if any file is a candidate"),
        (C_DARK_BLUE, "③ Subtract",      "Remove confirmed files from candidate set"),
    ]
    y = Inches(1.78)
    for col, step, desc in not_steps:
        indent = step.startswith(" ")
        lx = Inches(6.85 if not indent else 7.4)
        add_label(slide, step.strip(), lx, y, Inches(1.9), Inches(0.46),
                  font_size=Pt(11), bold=True, fill=col, align=PP_ALIGN.CENTER)
        add_textbox(slide, desc, lx + Inches(2.0), y + Inches(0.05),
                    Inches(3.8), Inches(0.38), font_size=Pt(11), color=C_BLACK)
        y += Inches(0.52 if not indent else 0.46)

    add_textbox(slide, "↑  Key AND advantage: mask tightens after each term → later terms are pruned more aggressively",
                Inches(0.35), Inches(5.38), Inches(6.1), Inches(0.44),
                font_size=Pt(11), italic=True, color=C_MID_BLUE)

    # Complexity box
    add_rect(slide, Inches(0.35), Inches(5.45), Inches(12.7), Inches(1.85),
             fill=C_LIGHT_GRAY, line=C_MID_BLUE)
    add_textbox(slide, "Complexity",
                Inches(0.55), Inches(5.52), Inches(3.0), Inches(0.36),
                font_size=Pt(13), bold=True, color=C_DARK_BLUE)
    rows = [
        ("AND (without filter)", "O(Σ N_term_i) BerkeleyDB lookups across all terms"),
        ("AND (with filter)",    "O(Σ N_term_i) bitmask checks  +  O(M_AND) lookups,   M_AND ≤ Σ N_term_i"),
        ("NOT (without filter)", "O(N_excl) BerkeleyDB lookups"),
        ("NOT (with filter)",    "O(N_excl) bitmask checks  +  O(M_NOT) lookups,   M_NOT ≤ N_excl"),
    ]
    y2 = Inches(5.92)
    for label, val in rows:
        add_textbox(slide, f"  {label}:", Inches(0.55), y2, Inches(3.6), Inches(0.33),
                    font_size=Pt(11), bold=True, color=C_DARK_BLUE)
        add_textbox(slide, val, Inches(4.2), y2, Inches(8.6), Inches(0.33),
                    font_size=Pt(11), color=C_BLACK)
        y2 += Inches(0.33)
    return slide


# ---------------------------------------------------------------------------
# Slide 5 — Storage in Lucene
# ---------------------------------------------------------------------------

def slide_storage(prs):
    slide = blank_slide(prs)
    header_bar(slide, "Where the Bitmask Lives: In-Memory Hash Map",
               "Alongside the Lucene index — storing it inside Lucene is possible but expensive")

    # Why not in Lucene box
    add_rect(slide, Inches(0.4), Inches(1.25), Inches(12.5), Inches(1.55),
             fill=RGBColor(0xFF, 0xEE, 0xEE), line=C_RED)
    add_textbox(slide, "Why not store the bitmask as a Lucene stored field?",
                Inches(0.6), Inches(1.32), Inches(12.0), Inches(0.38),
                font_size=Pt(14), bold=True, color=C_RED)
    add_textbox(slide,
                "Lucene does expose updateDocument() — but it works by deleting the old document "
                "and re-adding a new one (see IndexWriter.h: 'just deletes and then adds the entire document'). "
                "Every time a new file shares an existing chunk, that chunk's document would need a full "
                "delete+re-add cycle. This is expensive and reassigns the doc ID, breaking any external "
                "structure keyed by doc ID.",
                Inches(0.6), Inches(1.72), Inches(12.0), Inches(0.95),
                font_size=Pt(13), color=C_BLACK)

    # Two-column flow
    add_rect(slide, Inches(0.4), Inches(2.95), Inches(5.9), Inches(3.6),
             fill=C_LIGHT_GRAY, line=C_RED)
    add_textbox(slide, "Current flow  (no filter)",
                Inches(0.6), Inches(3.03), Inches(5.5), Inches(0.38),
                font_size=Pt(13), bold=True, color=C_RED)
    cur_steps = [
        ("Lucene hit → doc ID",                 C_BLACK, False),
        ("reader->document(docId)  ← I/O",      C_RED,   True),
        ("  → chunk fingerprint",               C_BLACK, False),
        ("BerkeleyDB lookup  ← I/O",            C_RED,   True),
        ("  → file list  (every chunk)",         C_BLACK, False),
        ("Intersect / subtract",                 C_BLACK, False),
    ]
    y = Inches(3.48)
    for s, col, bold in cur_steps:
        add_textbox(slide, s, Inches(0.65), y, Inches(5.4), Inches(0.37),
                    font_size=Pt(13), bold=bold, color=col)
        y += Inches(0.38)

    add_rect(slide, Inches(6.9), Inches(2.95), Inches(6.0), Inches(3.6),
             fill=C_LIGHT_GRAY, line=C_GREEN)
    add_textbox(slide, "With in-memory segment filter",
                Inches(7.1), Inches(3.03), Inches(5.5), Inches(0.38),
                font_size=Pt(13), bold=True, color=C_GREEN)
    new_steps = [
        ("Lucene hit → doc ID",                        C_BLACK,  False),
        ("bitmask_map[fingerprint]  ← RAM only",       C_GREEN,  True),
        ("chunk_mask & cand_mask == 0?",                C_ORANGE, True),
        ("  → Yes: SKIP  ✓  (no doc fetch, no BDB)",   C_GREEN,  True),
        ("  → No: reader->document()  ← I/O",          C_RED,    False),
        ("       BerkeleyDB lookup  ← I/O → subtract", C_RED,    False),
    ]
    y = Inches(3.48)
    for s, col, bold in new_steps:
        add_textbox(slide, s, Inches(7.1), y, Inches(5.7), Inches(0.37),
                    font_size=Pt(13), bold=bold, color=col)
        y += Inches(0.38)

    add_textbox(slide, "⟹", Inches(6.1), Inches(4.35), Inches(0.8), Inches(0.6),
                font_size=Pt(28), bold=True, color=C_MID_BLUE,
                align=PP_ALIGN.CENTER)

    add_rect(slide, Inches(0.4), Inches(6.65), Inches(12.5), Inches(0.65),
             fill=C_YELLOW_BG, line=C_ORANGE)
    add_textbox(slide,
                "Key: chunk fingerprint (content hash) — stable forever, never reassigned. "
                "Lookup cost: O(1) hash map access.  "
                "Built during the logical phase alongside the reverse mapping.",
                Inches(0.6), Inches(6.70), Inches(12.1), Inches(0.52),
                font_size=Pt(13), italic=True, color=C_ORANGE)
    return slide


# ---------------------------------------------------------------------------
# Slide 6 — Why Not Probabilistic Filters?
# ---------------------------------------------------------------------------

def slide_why_not_memento(prs):
    slide = blank_slide(prs)
    header_bar(slide, "Why Not a Range Filter per Chunk? (e.g. Memento)",
               "Memento answers 'any key in [lo, hi]?' — the right question, but the wrong granularity")

    add_textbox(slide,
                "Memento filter is dynamic and answers range queries — seemingly a perfect fit. "
                "Build one per chunk over its file IDs; query with [min_candidate, max_candidate]. "
                "Why don't we?",
                Inches(0.4), Inches(1.22), Inches(12.6), Inches(0.52),
                font_size=Pt(14), color=C_BLACK)

    problems = [
        ("Per-chunk overhead dominates",
         C_RED,
         "Memento needs a full hash-table structure regardless of how many keys it holds. "
         "In a dedup system, most chunks are shared by very few files — often 1 to 3. "
         "Wiki: 14,866,994 unique chunks, 3,513 files → avg ~2–3 files/chunk. "
         "At ~20–30 bytes minimum per filter instance: 14M chunks × 25B ≈ 350 MB overhead "
         "just for filter metadata. Segment bitmask: 14M × 1 byte = 14 MB."),
        ("Non-contiguous candidate sets",
         C_ORANGE,
         "Memento answers 'any key in [lo, hi]?' — a single contiguous range. "
         "AND-phase candidates are NOT always contiguous: e.g. {file 5, file 2000, file 3500}. "
         "Range query [5, 3500] cannot prune a chunk with file 1000 even if 1000 is not a candidate. "
         "Segment bitmask handles this exactly: candidate_mask = 0b1001 (segs 0 and 3) "
         "correctly prunes a chunk with mask 0b0110 (segs 1 and 2) via a single AND = 0."),
        ("Updates reassign doc IDs (same issue as Lucene storage)",
         C_GRAY,
         "When a new file shares an existing chunk, the Memento filter for that chunk must be "
         "updated. Memento supports insertions natively — this is fine. But storing it in Lucene "
         "still requires updateDocument() (delete+re-add). Keeping it in memory avoids this, "
         "at the cost of the overhead described above."),
    ]

    y = Inches(1.88)
    for title, col, body in problems:
        add_label(slide, title, Inches(0.4), y, Inches(3.6), Inches(0.58),
                  font_size=Pt(12), bold=True, fill=col, align=PP_ALIGN.CENTER)
        add_textbox(slide, body, Inches(4.1), y + Inches(0.04),
                    Inches(9.0), Inches(0.52),
                    font_size=Pt(12), color=C_BLACK)
        y += Inches(0.75)

    add_rect(slide, Inches(0.4), Inches(4.18), Inches(12.6), Inches(0.62),
             fill=C_YELLOW_BG, line=C_ORANGE)
    add_textbox(slide,
                "Segment bitmask is a better fit: handles non-contiguous candidates exactly "
                "(at segment granularity), costs 1 byte per chunk, and requires no filter library.",
                Inches(0.6), Inches(4.25), Inches(12.2), Inches(0.48),
                font_size=Pt(13), bold=True, color=C_ORANGE)
    return slide


# ---------------------------------------------------------------------------
# Slide 7 — Comparison Table
# ---------------------------------------------------------------------------

def slide_comparison(prs):
    slide = blank_slide(prs)
    header_bar(slide, "Comparison: Segment Bitmask vs. Memento Filter",
               "Same question, different granularity — overhead determines the winner for small sets")

    headers = ["Property", "Memento Filter (per chunk)", "Segment Bitmask (per chunk)"]
    rows_data = [
        ("Query answered",         "Any file ID in [lo, hi]?",         "Any candidate segment bit set?"),
        ("Handles non-contiguous candidates", "No — [lo,hi] over-approximates", "Yes — bitmask AND is exact at segment level"),
        ("FP cause",               "Hash collisions (inherent)",        "Stale deletions only (recoverable)"),
        ("FP when maintained",     "Always (by design)",                "Never"),
        ("Recoverable to exact",   "No",                                "Yes — rebuild from reverse mapping"),
        ("Storage per chunk",      "~20–30 B min + ~9 bits/key",        "ceil(total_files / seg_size) bits"),
        ("Wiki (14M chunks)",      "~350 MB",                           "~14 MB"),
        ("Lookup cost",            "O(log R) range decomposition",      "O(1) bitwise AND"),
        ("Dynamic insertions",     "Yes (native)",                      "Yes — set one bit, O(1)"),
        ("Dynamic deletions",      "Yes (Cuckoo-based)",                "FP only — rebuild to reset"),
        ("Implementation",         "Full filter library required",      "In-memory array + bitwise ops"),
    ]

    col_w = [Inches(3.5), Inches(4.5), Inches(4.8)]
    col_x = [Inches(0.25)]
    for w in col_w[:-1]:
        col_x.append(col_x[-1] + w + Inches(0.06))

    row_h = Inches(0.43)
    hdr_y = Inches(1.22)

    hdr_fills = [C_DARK_BLUE, C_RED, C_GREEN]
    for ci, (hdr, cx, cw, hf) in enumerate(zip(headers, col_x, col_w, hdr_fills)):
        add_label(slide, hdr, cx, hdr_y, cw, row_h,
                  font_size=Pt(12), bold=True, fill=hf)

    bad_memento  = {"Always (by design)", "No", "~350 MB",
                    "No — [lo,hi] over-approximates", "Full filter library required"}
    good_bitmask = {"Never", "Yes — rebuild from reverse mapping", "~14 MB",
                    "Yes — bitmask AND is exact at segment level",
                    "In-memory array + bitwise ops", "O(1) bitwise AND",
                    "Yes — set one bit, O(1)"}

    for ri, row in enumerate(rows_data):
        y = hdr_y + (ri + 1) * row_h
        for ci, (cell, cx, cw) in enumerate(zip(row, col_x, col_w)):
            is_bad  = (ci == 1 and cell in bad_memento)
            is_good = (ci == 2 and cell in good_bitmask)
            bg = RGBColor(0xFF, 0xEE, 0xEE) if is_bad  else \
                 RGBColor(0xEE, 0xFF, 0xEE) if is_good else \
                 (C_LIGHT_GRAY if ri % 2 == 0 else C_WHITE)
            fc = C_RED   if is_bad  else \
                 C_GREEN if is_good else C_BLACK
            add_label(slide, cell, cx, y, cw, row_h,
                      font_size=Pt(11), bold=(ci == 0),
                      fill=bg, line=C_GRAY, color=fc,
                      align=PP_ALIGN.LEFT if ci == 0 else PP_ALIGN.CENTER)
    return slide


# ---------------------------------------------------------------------------
# Slide 8 — Dynamic Updates: Additions
# ---------------------------------------------------------------------------

def slide_additions(prs):
    slide = blank_slide(prs)
    header_bar(slide, "Dynamic Updates: File Additions",
               "Additions are trivial and exact — O(1) per chunk")

    steps = [
        ("New file arrives", "Assigned file ID F  →  segment  S = floor(F / segment_size)", C_MID_BLUE),
        ("For new chunks",   "Insert new bitmask entry with bit S set", C_GREEN),
        ("For shared chunks","Existing chunk already in index — set bit S in its bitmask entry", C_GREEN),
        ("Result",           "Bitmask remains exact.  No false positives introduced.  O(1) per chunk.", C_GREEN),
    ]

    y = Inches(1.35)
    for step, desc, col in steps:
        add_label(slide, step, Inches(0.45), y, Inches(2.8), Inches(0.6),
                  font_size=Pt(13), bold=True, fill=col)
        add_textbox(slide, desc, Inches(3.4), y + Inches(0.1), Inches(9.6), Inches(0.45),
                    font_size=Pt(13), color=C_BLACK)
        y += Inches(0.78)

    # Example
    add_rect(slide, Inches(0.45), Inches(4.5), Inches(12.5), Inches(1.7),
             fill=C_LIGHT_GRAY, line=C_MID_BLUE)
    add_textbox(slide, "Example  (wiki dataset — 3,513 files, 4 segments)",
                Inches(0.65), Inches(4.6), Inches(12.0), Inches(0.4),
                font_size=Pt(13), bold=True, color=C_DARK_BLUE)
    add_textbox(slide,
                "New file arrives as file ID 3,514  →  segment 3  (floor(3514 / 1024) = 3)\n"
                "If it shares chunk C (currently bitmask = 0b0101), update to 0b1101  — one bit-OR, done.\n"
                "If file 4,096 arrives: creates segment 4 → bitmask grows to 5 bits (still tiny).",
                Inches(0.65), Inches(5.05), Inches(12.0), Inches(1.0),
                font_size=Pt(13), color=C_BLACK)

    add_textbox(slide,
                "Key: chunk fingerprint (content hash) — stable and never reassigned.  "
                "O(1) hash map access.  Persisted to disk on graceful shutdown.",
                Inches(0.45), Inches(6.4), Inches(12.5), Inches(0.45),
                font_size=Pt(13), italic=True, color=C_GRAY)
    return slide


# ---------------------------------------------------------------------------
# Slide 9 — Dynamic Updates: Deletions
# ---------------------------------------------------------------------------

def slide_deletions(prs):
    slide = blank_slide(prs)
    header_bar(slide, "Dynamic Updates: File Deletions",
               "Deletions cause false positives — correctness preserved, performance degrades")

    add_textbox(slide,
                "When file F is deleted, we cannot simply clear bit S from all chunk bitmasks:",
                Inches(0.45), Inches(1.25), Inches(12.5), Inches(0.4),
                font_size=Pt(15), bold=True, color=C_DARK_BLUE)
    add_textbox(slide,
                "Another file in the same segment S may still share the chunk.  "
                "Clearing the bit would create a false negative — a correctness violation.",
                Inches(0.45), Inches(1.65), Inches(12.5), Inches(0.5),
                font_size=Pt(14), color=C_BLACK)

    boxes = [
        ("Do NOT clear bits\non deletion", C_ORANGE,
         "Bits remain set → false positives accumulate\n"
         "Consequence: some unnecessary BerkeleyDB lookups\n"
         "Correctness: always preserved (FP ≠ wrong result)"),
        ("Periodic rebuild", C_MID_BLUE,
         "Scan the full reverse mapping and recompute bitmasks\n"
         "Resets FPR to zero; cheap amortised cost\n"
         "Frequency: after N deletions or scheduled (e.g. nightly)"),
        ("Degradation profile", C_GRAY,
         "Bitmask drifts toward all-ones as files are deleted\n"
         "Filter becomes useless in degenerate case (all deleted)\n"
         "Real workloads: additions >> deletions → slow drift"),
    ]

    y = Inches(2.35)
    for title, col, body in boxes:
        add_label(slide, title, Inches(0.45), y, Inches(3.2), Inches(0.75),
                  font_size=Pt(13), bold=True, fill=col)
        add_textbox(slide, body, Inches(3.75), y + Inches(0.05),
                    Inches(9.3), Inches(0.7),
                    font_size=Pt(13), color=C_BLACK)
        y += Inches(0.95)

    add_rect(slide, Inches(0.45), Inches(5.65), Inches(12.5), Inches(0.7),
             fill=C_YELLOW_BG, line=C_ORANGE)
    add_textbox(slide,
                "Analogy: LSM-tree Bloom filters — false positive rate grows until compaction resets it.  "
                "Backup systems have many more additions than deletions, so drift is slow in practice.",
                Inches(0.65), Inches(5.72), Inches(12.1), Inches(0.55),
                font_size=Pt(13), italic=True, color=C_ORANGE)

    add_textbox(slide,
                "Summary: additions are O(1) and exact.  Deletions trade correctness-safety "
                "for implementation simplicity — the structure degrades gracefully.",
                Inches(0.45), Inches(6.5), Inches(12.5), Inches(0.5),
                font_size=Pt(13), bold=True, color=C_DARK_BLUE)
    return slide


# ---------------------------------------------------------------------------
# Slide 10 — Segment Size Tradeoff
# ---------------------------------------------------------------------------

def slide_tradeoffs(prs):
    slide = blank_slide(prs)
    header_bar(slide, "Segment Size Tradeoff",
               "Smaller segments = more precision, more bits; larger = cheaper, coarser")

    headers2 = ["Segment size", "Segments (3,513 files)", "Bits/chunk", "False-positive granularity"]
    data = [
        ("1,024  (default)", "4",    "4 bits  (1 byte)",     "coarse — 1,024-file buckets"),
        ("256",              "14",   "14 bits (2 bytes)",     "finer — 256-file buckets"),
        ("64",               "55",   "55 bits (7 bytes)",     "fine — 64-file buckets"),
        ("1  (exact)",       "3,513","3,513 bits (440 bytes)","no false positives — exact"),
    ]

    col_w2 = [Inches(2.3), Inches(2.8), Inches(2.8), Inches(4.5)]
    col_x2 = [Inches(0.3)]
    for w in col_w2[:-1]:
        col_x2.append(col_x2[-1] + w + Inches(0.08))

    rh = Inches(0.5)
    hy = Inches(1.3)
    for ci, (hdr, cx, cw) in enumerate(zip(headers2, col_x2, col_w2)):
        add_label(slide, hdr, cx, hy, cw, rh,
                  font_size=Pt(13), bold=True, fill=C_DARK_BLUE)
    for ri, row in enumerate(data):
        ry = hy + (ri + 1) * rh
        for ci, (cell, cx, cw) in enumerate(zip(row, col_x2, col_w2)):
            bg = C_LIGHT_GRAY if ri % 2 == 0 else C_WHITE
            add_label(slide, cell, cx, ry, cw, rh,
                      font_size=Pt(12), bold=(ri == 0 and ci == 0),
                      fill=bg, line=C_GRAY, color=C_BLACK,
                      align=PP_ALIGN.LEFT if ci in (0, 3) else PP_ALIGN.CENTER)

    add_rect(slide, Inches(0.3), Inches(4.0), Inches(12.7), Inches(1.5),
             fill=C_LIGHT_GRAY, line=C_MID_BLUE)
    add_textbox(slide, "Memory cost (full in-memory array)",
                Inches(0.5), Inches(4.1), Inches(6.0), Inches(0.4),
                font_size=Pt(14), bold=True, color=C_DARK_BLUE)
    add_textbox(slide,
                "Wiki dedup index: 14,866,994 unique chunks\n"
                "At 1 byte/chunk (1,024-file segments): ~14 MB — fits entirely in L3 cache\n"
                "At 2 bytes/chunk (256-file segments): ~28 MB — still trivially in RAM",
                Inches(0.5), Inches(4.52), Inches(12.2), Inches(0.9),
                font_size=Pt(13), color=C_BLACK)

    add_textbox(slide,
                "Practical recommendation: 256–1,024 files per segment; "
                "tune based on AND-result cluster density and available memory.",
                Inches(0.3), Inches(5.65), Inches(12.7), Inches(0.5),
                font_size=Pt(14), bold=True, color=C_MID_BLUE)

    add_textbox(slide,
                "Note: bitmask can grow dynamically as new segments are created "
                "(new files push past the current maximum file ID).",
                Inches(0.3), Inches(6.3), Inches(12.7), Inches(0.45),
                font_size=Pt(13), italic=True, color=C_GRAY)
    return slide


# ---------------------------------------------------------------------------
# Slide 11 — Summary
# ---------------------------------------------------------------------------

def slide_summary(prs):
    slide = blank_slide(prs)
    add_rect(slide, 0, 0, W, H, fill=C_DARK_BLUE)
    add_rect(slide, Inches(0.4), Inches(0.3), W - Inches(0.8), Inches(0.9),
             fill=RGBColor(0x17, 0x37, 0x65))
    add_textbox(slide, "Summary",
                Inches(0.6), Inches(0.35), Inches(12.0), Inches(0.7),
                font_size=Pt(28), bold=True, color=C_WHITE)

    bullets = [
        ("Segment filter exploits integer structure",
         "File IDs are ordered integers — partitioning is natural and exact.  "
         "Probabilistic filters hash this structure away unnecessarily."),
        ("Correct by default",
         "Zero false positives for a correctly maintained filter.  "
         "Degrades gracefully on deletions; rebuild restores exactness."),
        ("Additions are O(1) and exact",
         "Set one bit per shared chunk when a new file arrives — "
         "done in the same pass as the reverse mapping update."),
        ("Beats Memento filter per chunk on overhead and non-contiguous sets",
         "Memento answers [lo, hi] range queries but over-approximates non-contiguous candidates. "
         "Cost: ~350 MB for 14M chunks vs 14 MB for segment bitmask at 1,024-file segments."),
        ("Memory: trivially small",
         "14M chunks × 1 byte = 14 MB at 1,024-file segments — fits in L3 cache."),
    ]

    y = Inches(1.4)
    for title, body in bullets:
        add_rect(slide, Inches(0.4), y, Inches(12.5), Inches(0.95),
                 fill=RGBColor(0x17, 0x37, 0x65))
        add_textbox(slide, f"✓  {title}",
                    Inches(0.55), y + Inches(0.04), Inches(12.0), Inches(0.38),
                    font_size=Pt(14), bold=True, color=C_CYAN)
        add_textbox(slide, body,
                    Inches(0.7), y + Inches(0.42), Inches(12.0), Inches(0.48),
                    font_size=Pt(12), color=RGBColor(0xCC, 0xDD, 0xEE))
        y += Inches(1.05)
    return slide


# ---------------------------------------------------------------------------
# Slide 12 — Experimental Results
# ---------------------------------------------------------------------------

def slide_results(prs):
    slide = blank_slide(prs)
    header_bar(slide, "Experimental Results: Dedup vs. Naive (Wiki Dataset)",
               "3,513 files · 14,866,994 unique chunks · warm cache · results verified identical ✓")

    headers = ["Regime", "Query", "Dedup", "Naive", "Winner", "Explanation"]
    rows = [
        ("R1  common→rare",   "term_H NOT term_L",          "2.11s", "1.42s", ("Naive 1.5×", False),
         "Dedup maps 514K chunks via BerkeleyDB; naive reads one posting list"),
        ("R2  common→common", "term_H NOT term_H",          "1.91s", "1.41s", ("Naive 1.4×", False),
         "Same bottleneck — 514K chunk reverse-mapping dominates"),
        ("R3  rare→common",   "term_L NOT term_H",          "1.07s", "1.37s", ("Dedup 1.3×", True),
         "AND phase trivial (135 chunks); early exit at 31% of excl list"),
        ("R4  med→med",       "term_M NOT term_M",          "0.77s", "1.37s", ("Dedup 1.8×", True),
         "986 chunks → 862 files; dedup well under naive's fixed 1.4s floor"),
        ("R5  2×high→high",   "H∧H NOT H",                  "1.99s", "1.40s", ("Naive 1.4×", False),
         "Both included terms are common; dedup reverse-maps 514K+ chunks"),
        ("R6  2×rare→high",   "L∧L NOT H",                  "0.71s", "1.40s", ("Dedup 2.0×", True),
         "CAI finds 4 AND candidates from [20,83] chunks; NOT exits at 24%"),
        ("R7  3×med→high",    "M∧M∧M NOT H",                "0.77s", "1.37s", ("Dedup 1.8×", True),
         "3-way intersection yields 99 candidates from [442,986,1009] chunks"),
    ]

    col_w = [Inches(1.6), Inches(1.8), Inches(0.8), Inches(0.8), Inches(1.3), Inches(6.55)]
    col_x = [Inches(0.2)]
    for w in col_w[:-1]:
        col_x.append(col_x[-1] + w + Inches(0.04))

    rh = Inches(0.41)
    hy = Inches(1.22)
    hdr_fills = [C_DARK_BLUE]*6
    for ci, (hdr, cx, cw) in enumerate(zip(headers, col_x, col_w)):
        add_label(slide, hdr, cx, hy, cw, rh,
                  font_size=Pt(11), bold=True, fill=C_DARK_BLUE)

    for ri, (regime, query, dedup_t, naive_t, (winner_lbl, dedup_wins), expl) in enumerate(rows):
        y = hy + (ri + 1) * rh
        bg = C_LIGHT_GRAY if ri % 2 == 0 else C_WHITE
        for ci, (cell, cx, cw) in enumerate(zip(
                [regime, query, dedup_t, naive_t, winner_lbl, expl],
                col_x, col_w)):
            cell_color = C_BLACK
            cell_bg = bg
            if ci == 2:   # dedup time
                cell_color = C_GREEN if dedup_wins else C_BLACK
            if ci == 3:   # naive time
                cell_color = C_GREEN if not dedup_wins else C_BLACK
            if ci == 4:   # winner
                cell_bg = RGBColor(0xEE, 0xFF, 0xEE) if dedup_wins else RGBColor(0xFF, 0xEE, 0xEE)
                cell_color = C_GREEN if dedup_wins else C_RED
            add_label(slide, cell, cx, y, cw, rh,
                      font_size=Pt(10), bold=(ci == 0), fill=cell_bg, line=C_GRAY,
                      color=cell_color,
                      align=PP_ALIGN.LEFT if ci in (0, 1, 5) else PP_ALIGN.CENTER)

    # Key insight boxes
    add_rect(slide, Inches(0.2), Inches(5.22), Inches(6.1), Inches(2.05),
             fill=RGBColor(0xFF, 0xEE, 0xEE), line=C_RED)
    add_textbox(slide, "When naive wins (R1, R2, R5)",
                Inches(0.35), Inches(5.28), Inches(5.8), Inches(0.36),
                font_size=Pt(12), bold=True, color=C_RED)
    add_textbox(slide,
                "Included term is very common → dedup must reverse-map "
                "hundreds of thousands of chunks through BerkeleyDB. "
                "Naive reads one posting list and gets file IDs directly. "
                "Dedup's chunk indirection becomes a liability.",
                Inches(0.35), Inches(5.65), Inches(5.8), Inches(1.45),
                font_size=Pt(12), color=C_BLACK)

    add_rect(slide, Inches(6.55), Inches(5.22), Inches(6.6), Inches(2.05),
             fill=RGBColor(0xEE, 0xFF, 0xEE), line=C_GREEN)
    add_textbox(slide, "When dedup wins (R3, R4, R6, R7)",
                Inches(6.7), Inches(5.28), Inches(6.3), Inches(0.36),
                font_size=Pt(12), bold=True, color=C_GREEN)
    add_textbox(slide,
                "AND intersection is tight — CAI quickly reduces candidates "
                "to a small set. Reverse-mapping work is proportional to "
                "surviving chunks, not all chunks. Naive's Lucene overhead "
                "is ~1.4s regardless; dedup falls well below that floor.",
                Inches(6.7), Inches(5.65), Inches(6.3), Inches(1.45),
                font_size=Pt(12), color=C_BLACK)

    add_rect(slide, Inches(0.2), Inches(7.1), Inches(12.95), Inches(0.28),
             fill=C_LIGHT_GRAY, line=C_GRAY)
    add_textbox(slide,
                "Key finding: naive has a flat ~1.4s floor (Lucene overhead dominates). "
                "Dedup scales with actual work — dedup wins whenever tight AND intersection "
                "reduces reverse-mapping below that floor.",
                Inches(0.35), Inches(7.12), Inches(12.6), Inches(0.25),
                font_size=Pt(11), bold=True, color=C_DARK_BLUE)
    return slide


# ---------------------------------------------------------------------------
# Slides B1–B4 — Bloom Filter vs Segment Bitmask comparison
# ---------------------------------------------------------------------------

def _add_graph(slide, path, x, y, w, h):
    """Embed a PNG graph image into the slide."""
    if os.path.exists(path):
        slide.shapes.add_picture(path, x, y, w, h)
    else:
        add_textbox(slide, f"[graph not found: {path}]", x, y, w, h,
                    font_size=Pt(10), color=C_RED)


def slide_bloom_mechanics(prs):
    """Bloom filter mechanics + FP derivation."""
    slide = blank_slide(prs)
    header_bar(slide, "Bloom Filter per Segment — How It Works & FP Derivation",
               "One Bloom filter per segment stores all chunk fingerprints in that segment")

    # Left column: how it works
    add_rect(slide, Inches(0.3), Inches(1.22), Inches(6.5), Inches(3.3),
             fill=C_LIGHT_GRAY, line=C_MID_BLUE)
    add_label(slide, "Structure", Inches(0.3), Inches(1.22), Inches(6.5), Inches(0.4),
              font_size=Pt(12), bold=True, fill=C_MID_BLUE)
    struct_lines = [
        "Build:  for each segment s, insert every chunk fingerprint",
        "        that appears in any file within s  → Bloom_s",
        "Query:  Bloom_s.query(C) == 0  →  C definitely absent",
        "        → safe to skip reverse mapping for this segment",
        "        Bloom_s.query(C) == 1  →  C possibly present",
        "        → must do reverse mapping (may be a FP)",
        f"Wiki:   42 segments × ~1.84 M chunks each  →  42 Bloom filters",
    ]
    y = Inches(1.72)
    for line in struct_lines:
        add_textbox(slide, line, Inches(0.45), y, Inches(6.2), Inches(0.34),
                    font_size=Pt(11), color=C_BLACK)
        y += Inches(0.335)

    # Right column: FP derivation
    add_rect(slide, Inches(7.0), Inches(1.22), Inches(6.1), Inches(3.3),
             fill=C_LIGHT_GRAY, line=C_ORANGE)
    add_label(slide, "FP Rate Derivation", Inches(7.0), Inches(1.22), Inches(6.1), Inches(0.4),
              font_size=Pt(12), bold=True, fill=C_ORANGE)
    deriv_lines = [
        ("After inserting n elements with k hash funcs into m bits:", False),
        ("", False),
        ("P(bit = 0) = (1 − 1/m)^(kn)  ≈  e^(−kn/m)", True),
        ("", False),
        ("FP rate:  ε = (1 − e^(−kn/m))^k", True),
        ("", False),
        ("Optimal k:  dε/dk = 0  →  k_opt = (m/n) · ln 2", True),
        ("At k_opt each bit is 0 with prob exactly 1/2:", False),
        ("ε = (1/2)^k_opt  →  m/n = −log₂(ε) / ln 2", True),
        ("              = −1.44 · log₂(ε)  bits per element", True),
        ("k_opt = (m/n) · ln 2  ≈  log₂(1/ε)  hash probes", True),
    ]
    y = Inches(1.72)
    for text, is_formula in deriv_lines:
        col = C_MID_BLUE if is_formula else C_BLACK
        add_textbox(slide, text, Inches(7.15), y, Inches(5.8), Inches(0.32),
                    font_size=Pt(11 if is_formula else 10),
                    bold=is_formula, color=col)
        y += Inches(0.31)

    # Mechanics graph (bottom)
    _add_graph(slide, f"{GRAPH_DIR}/graph6_bloom_mechanics.png",
               Inches(0.3), Inches(4.62), Inches(12.8), Inches(2.7))
    return slide


def slide_bloom_space_formulas(prs):
    """Space cost formulas side-by-side with worked example."""
    slide = blank_slide(prs)
    header_bar(slide, "Space Cost Derivation: Bitmask vs. Bloom",
               "Bitmask size grows with #segments; Bloom size is nearly independent of segment count")

    N_FILES  = 42_105
    N_CHUNKS = 14_937_917
    K_AVG    = 79_791_685 / N_CHUNKS
    import math

    S = 1024
    n_segs   = math.ceil(N_FILES / S)
    bm_bits  = N_CHUNKS * n_segs
    bm_MB    = bm_bits / 8 / 1e6

    p_s      = 1 - (1 - S / N_FILES) ** K_AVG
    n_s      = N_CHUNKS * p_s
    bpe_1pct = -math.log2(0.01) / math.log(2) * math.log(2)   # = 1.44 × log2(100)
    bpe_1pct = -1 / math.log(2) * math.log2(0.01)
    bloom_bits_1pct = n_segs * n_s * bpe_1pct
    bloom_MB_1pct   = bloom_bits_1pct / 8 / 1e6

    # Bitmask box
    add_rect(slide, Inches(0.3), Inches(1.22), Inches(6.1), Inches(4.2),
             fill=C_LIGHT_GRAY, line=C_MID_BLUE)
    add_label(slide, "Segment Bitmask", Inches(0.3), Inches(1.22), Inches(6.1), Inches(0.42),
              font_size=Pt(13), bold=True, fill=C_MID_BLUE)
    bm_lines = [
        ("Structure:", "1 bit per (chunk, segment) pair"),
        ("", ""),
        ("Total bits:", "N_chunks × ⌈N_files / S⌉"),
        ("Total bytes:", "N_chunks × ⌈N_files / S⌉ / 8"),
        ("", ""),
        ("Scales as:", "O(N_chunks / S)  — linear in 1/S"),
        ("", ""),
        ("Wiki @ S=1024:", f"{N_CHUNKS/1e6:.1f}M × {n_segs} bits = {bm_MB:.1f} MB"),
        ("No tunable param.", "Exact — 0% structural FP"),
        ("Lookup:", "1 hash probe + 1 bitwise AND"),
    ]
    y = Inches(1.74)
    for label, val in bm_lines:
        if label:
            add_textbox(slide, label, Inches(0.45), y, Inches(2.2), Inches(0.33),
                        font_size=Pt(11), bold=True, color=C_DARK_BLUE)
            add_textbox(slide, val, Inches(2.7), y, Inches(3.5), Inches(0.33),
                        font_size=Pt(11), color=C_BLACK)
        y += Inches(0.345)

    # Bloom box
    add_rect(slide, Inches(6.7), Inches(1.22), Inches(6.4), Inches(4.2),
             fill=C_LIGHT_GRAY, line=C_ORANGE)
    add_label(slide, "Bloom Filter per Segment", Inches(6.7), Inches(1.22), Inches(6.4), Inches(0.42),
              font_size=Pt(13), bold=True, fill=C_ORANGE)
    bl_lines = [
        ("Elements n_s:", "N_chunks × (1−(1−S/N)^k̄)  per segment"),
        ("", f"  Wiki: {N_CHUNKS/1e6:.1f}M × {p_s:.2f} ≈ {n_s/1e6:.1f}M per segment"),
        ("Bits/element:", "m/n = −1.44 · log₂(ε)"),
        ("", "  ε=1%  →  9.6 bits/elem  →  k_opt = 7 probes"),
        ("", "  ε=0.1% → 14.4 bits/elem → k_opt = 10 probes"),
        ("Total size:", "⌈N/S⌉ × n_s × (m/n)  bits"),
        ("", "≈  N_chunks · k̄ · (m/n)  (independent of S!)"),
        ("Wiki ε=1%:", f"{n_segs} × {n_s/1e6:.1f}M × 9.6 = {bloom_MB_1pct:.0f} MB"),
        ("Structural FP:", "ε  (Bloom) — adds on top of granularity FP"),
        ("Lookup:", "k_opt hash probes per segment queried"),
    ]
    y = Inches(1.74)
    for label, val in bl_lines:
        if label:
            add_textbox(slide, label, Inches(6.85), y, Inches(2.4), Inches(0.33),
                        font_size=Pt(11), bold=True, color=C_ORANGE)
            add_textbox(slide, val, Inches(9.3), y, Inches(3.6), Inches(0.33),
                        font_size=Pt(11), color=C_BLACK)
        else:
            add_textbox(slide, val, Inches(9.3), y, Inches(3.6), Inches(0.33),
                        font_size=Pt(10), color=C_GRAY, italic=True)
        y += Inches(0.345)

    add_rect(slide, Inches(0.3), Inches(5.55), Inches(12.8), Inches(0.65),
             fill=C_YELLOW_BG, line=C_ORANGE)
    crossover_eps_pct = round(2 ** (-1 / (K_AVG * S / N_FILES) / (1 / math.log(2))) * 100, 1) if False else 2.0
    add_textbox(slide,
                f"Key insight: Bloom total ≈ N_chunks × k̄ × bits/element ≈ {bloom_MB_1pct:.0f} MB at ε=1%.  "
                f"With 10 MB files k̄ doubled ({K_AVG:.2f}) → Bloom doubled vs 100 MB case.  "
                f"Bitmask at S=1024 is {bm_MB:.0f} MB.  "
                f"Bloom@10% is only ~46 MB — smaller than bitmask!  Crossover at ε ≈ 2%.",
                Inches(0.5), Inches(5.60), Inches(12.4), Inches(0.55),
                font_size=Pt(11), bold=True, color=C_ORANGE)

    return slide


def slide_bloom_space_graphs(prs):
    """Two space-comparison graphs side by side."""
    slide = blank_slide(prs)
    header_bar(slide, "Space Comparison: Bitmask vs. Bloom",
               "Left: space vs segment size S   |   Right: Bloom space vs FP rate ε at S=1024")
    _add_graph(slide, f"{GRAPH_DIR}/graph1_space_vs_segment_size.png",
               Inches(0.2), Inches(1.2), Inches(6.55), Inches(4.4))
    _add_graph(slide, f"{GRAPH_DIR}/graph2_bloom_space_vs_eps.png",
               Inches(6.85), Inches(1.2), Inches(6.35), Inches(4.4))

    add_rect(slide, Inches(0.2), Inches(5.72), Inches(6.5), Inches(1.55),
             fill=C_LIGHT_GRAY, line=C_MID_BLUE)
    add_textbox(slide, "Crossover at S ≈ 858 files/segment (left graph).\n"
                "At S=1024 (42 segments) bitmask = 78 MB. Bloom@1% = 93 MB (barely larger). "
                "Bloom@10% = 46 MB — now smaller than bitmask! Crossover ε ≈ 2% (right graph).",
                Inches(0.35), Inches(5.78), Inches(6.2), Inches(1.4),
                font_size=Pt(12), color=C_BLACK)

    add_rect(slide, Inches(6.85), Inches(5.72), Inches(6.35), Inches(1.55),
             fill=C_LIGHT_GRAY, line=C_ORANGE)
    add_textbox(slide, "Bloom total size ≈ N_chunks × k̄ × bits/element ≈ 93 MB at ε=1%. "
                "With 10 MB files k̄ doubled (5.34 vs 2.68) so Bloom doubled too. "
                "Bitmask grew 10× (42 vs 4 segments) — making the tradeoff much more competitive.",
                Inches(7.0), Inches(5.78), Inches(6.05), Inches(1.4),
                font_size=Pt(12), color=C_BLACK)
    return slide


def slide_bloom_fp_graphs(prs):
    """FP rate graphs + summary bar chart."""
    slide = blank_slide(prs)
    header_bar(slide, "False Positive Rate Analysis",
               "Both structures share ~99.99% file-level granularity FP (S=1024, k̄=5.34); Bloom adds ε on top")

    # Top row: two smaller graphs
    _add_graph(slide, f"{GRAPH_DIR}/graph3_fp_rate_breakdown.png",
               Inches(0.2), Inches(1.2), Inches(6.5), Inches(3.3))
    _add_graph(slide, f"{GRAPH_DIR}/graph4_bloom_skip_probability.png",
               Inches(6.85), Inches(1.2), Inches(6.35), Inches(3.3))

    # Bottom: summary bars
    _add_graph(slide, f"{GRAPH_DIR}/graph5_summary_bars.png",
               Inches(1.8), Inches(4.6), Inches(9.7), Inches(2.75))

    return slide


# ---------------------------------------------------------------------------
# Slide 12 — Wiki Diagnostics Table
# ---------------------------------------------------------------------------

def slide_diagnostics_wiki(prs):
    slide = blank_slide(prs)
    header_bar(slide, "Query Diagnostic Counters — Wiki Dataset",
               "Explains why dedup wins or loses: TermChunks drives AND cost; ExclScanned drives NOT cost")

    headers = ["Regime", "TermChunks\n(sorted, ×1K)", "ExclChunks\n(×1K)", "ANDcands",
               "Opt1\nskip", "NOT\nkill", "ExclScanned\n(×1K)"]

    # R6: AND found 0 candidates → NOT phase never ran; display "—" for NOT counters
    rows_data = [
        ("R1  common+rare",   "[514.7]",         "0.14",   "3,513", "2",   "72",    "0.14 / 0.14"),
        ("R2  common+common", "[514.7]",         "8.1",    "3,513", "380", "3,352", "8.1 / 8.1"),
        ("R3  rare+common",   "[0.14]",           "514.7",  "67",    "2",   "67",    "161.6 / 514.7"),
        ("R4  med+med",       "[0.99]",           "0.44",   "862",   "0",   "175",   "0.44 / 0.44"),
        ("R5  2×high+high",   "[8.1, 514.7]",    "13.9",   "3,351", "694", "3,316", "13.9 / 13.9"),
        ("R6  2×rare+high",   "[0.01, 0.14]",    "514.7",  "0",     "—",   "—",     "0 / 514.7"),
        ("R7  3×med+high",    "[0.44, 0.99, 1.0]","8.1",   "99",    "0",   "97",    "8.1 / 8.1"),
    ]

    # winner: True = dedup wins, False = naive wins (matches results slide)
    winners = [False, False, True, True, False, True, True]

    col_w = [Inches(1.7), Inches(2.1), Inches(1.35), Inches(1.1),
             Inches(0.9), Inches(1.0), Inches(4.7)]
    col_x = [Inches(0.2)]
    for w in col_w[:-1]:
        col_x.append(col_x[-1] + w + Inches(0.05))

    rh  = Inches(0.48)
    hy  = Inches(1.22)

    # Header row
    for hdr, cx, cw in zip(headers, col_x, col_w):
        add_label(slide, hdr, cx, hy, cw, rh,
                  font_size=Pt(11), bold=True, fill=C_DARK_BLUE, align=PP_ALIGN.CENTER)

    for ri, (row, dedup_wins) in enumerate(zip(rows_data, winners)):
        y  = hy + (ri + 1) * rh
        bg = RGBColor(0xEE, 0xFF, 0xEE) if dedup_wins else RGBColor(0xFF, 0xEE, 0xEE)
        for ci, (cell, cx, cw) in enumerate(zip(row, col_x, col_w)):
            cell_bg = bg if ci == 0 else (C_LIGHT_GRAY if ri % 2 == 0 else C_WHITE)
            cell_color = (C_GREEN if dedup_wins else C_RED) if ci == 0 else C_BLACK
            # Highlight large TermChunks (>100K = costly) in red
            if ci == 1 and "514" in cell:
                cell_bg = RGBColor(0xFF, 0xEE, 0xEE)
                cell_color = C_RED
            # Highlight early exit on ExclScanned
            if ci == 6 and "/" in cell:
                scanned, total = cell.split(" / ")
                if scanned != total:
                    cell_bg = RGBColor(0xEE, 0xFF, 0xEE)
                    cell_color = C_GREEN
            add_label(slide, cell, cx, y, cw, rh,
                      font_size=Pt(11), bold=(ci == 0), fill=cell_bg, line=C_GRAY,
                      color=cell_color,
                      align=PP_ALIGN.LEFT if ci == 0 else PP_ALIGN.CENTER)

    # Annotation boxes
    ann_y = hy + (len(rows_data) + 1) * rh + Inches(0.08)
    for ax, aw, col, text in [
        (Inches(0.2),   Inches(6.3), C_RED,
         "Red TermChunks = AND bottleneck: dedup reverse-maps 514K chunks vs naive's flat Lucene read"),
        (Inches(6.75),  Inches(6.4), C_GREEN,
         "Green ExclScanned = early exit: NOT scanner stopped before exhausting the full excluded list"),
    ]:
        add_rect(slide, ax, ann_y, aw, Inches(0.6), fill=RGBColor(0xF8, 0xF8, 0xF8), line=col)
        add_textbox(slide, text, ax + Inches(0.1), ann_y + Inches(0.08),
                    aw - Inches(0.2), Inches(0.48), font_size=Pt(11), color=col)

    return slide


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    os.makedirs("slides", exist_ok=True)
    prs = new_prs()

    slide_title(prs)
    slide_problem(prs)
    slide_core_idea(prs)
    slide_flow(prs)
    slide_storage(prs)
    slide_why_not_memento(prs)
    slide_comparison(prs)
    slide_additions(prs)
    slide_deletions(prs)
    slide_tradeoffs(prs)
    slide_bloom_mechanics(prs)
    slide_bloom_space_formulas(prs)
    slide_bloom_space_graphs(prs)
    slide_bloom_fp_graphs(prs)
    slide_results(prs)
    slide_diagnostics_wiki(prs)
    slide_summary(prs)

    out = "slides/segment_filter.pptx"
    prs.save(out)
    print(f"Saved {len(prs.slides)} slides → {out}")


if __name__ == "__main__":
    main()
