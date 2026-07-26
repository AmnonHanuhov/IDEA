"""
Segment Filter vs. Per-Segment Bloom Filter — Space & FP comparison graphs.
Outputs PDF + PNG to graphs/bloom_vs_segment/
Run: python3 scripts/bloom_vs_segment_graphs.py
"""

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import matplotlib.gridspec as gridspec
import os

OUT  = "graphs/bloom_vs_segment"
os.makedirs(OUT, exist_ok=True)

# --------------------------------------------------------------------------
# Dataset constants (wiki, 10 MB files)
# --------------------------------------------------------------------------
N_FILES  = 42_105         # total files (wiki split into 10 MB pieces)
N_CHUNKS = 14_937_917     # unique chunks in dedup index
TOTAL_CHUNK_FILE_PAIRS = 79_791_685   # sum of all chunk-file pairs (= naive chunk_num)
K_AVG    = TOTAL_CHUNK_FILE_PAIRS / N_CHUNKS   # avg files per chunk ≈ 5.34

# --------------------------------------------------------------------------
# Colour palette (matches PPTX theme)
# --------------------------------------------------------------------------
C_BLUE   = "#2E75B6"
C_GREEN  = "#388638"
C_RED    = "#C0504D"
C_ORANGE = "#E09020"
C_GRAY   = "#808080"
C_LIGHT  = "#F2F2F2"

FONT_TITLE  = 14
FONT_LABEL  = 12
FONT_TICK   = 10
FONT_ANNOT  = 10

def save(fig, name):
    fig.savefig(f"{OUT}/{name}.pdf", bbox_inches="tight")
    fig.savefig(f"{OUT}/{name}.png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  saved {name}")

# --------------------------------------------------------------------------
# Helper: total space (MB) for each structure at segment size S
# --------------------------------------------------------------------------
LN2   = np.log(2)
LOG2E = 1 / LN2   # = log₂(e) ≈ 1.4427

def bitmask_MB(S_arr):
    """Segment-filter bitmask total size in MB."""
    n_segs = np.ceil(N_FILES / np.asarray(S_arr, float))
    bits   = N_CHUNKS * n_segs
    return bits / 8 / 1e6

def bloom_MB(S_arr, eps):
    """
    Per-segment Bloom total size in MB.

    For each segment of size S:
      n_s = expected unique chunks in segment
          = N_CHUNKS × (1 - (1 - S/N_FILES)^K_AVG)
    Bloom bits/element at FP rate eps: m/n = -LOG2E × log2(eps) = 1.44 × log2(1/eps)
    Total = N_segs × n_s × (m/n) bits
    """
    S_arr  = np.asarray(S_arr, float)
    n_segs = np.ceil(N_FILES / S_arr)
    p_s    = 1 - (1 - S_arr / N_FILES) ** K_AVG   # prob a chunk lands in this segment
    n_s    = N_CHUNKS * p_s                        # chunks per segment
    bpe    = -LOG2E * np.log2(eps)                  # bits per element (optimal Bloom)
    return n_segs * n_s * bpe / 8 / 1e6

# ==========================================================================
# GRAPH 1 — Space vs Segment Size S
# ==========================================================================
S_vals = np.logspace(0, np.log10(N_FILES), 400)

fig, ax = plt.subplots(figsize=(8, 5))
ax.set_facecolor(C_LIGHT)
fig.patch.set_facecolor("white")

bm   = bitmask_MB(S_vals)
bl1  = bloom_MB(S_vals, 0.01)
bl01 = bloom_MB(S_vals, 0.001)
bl10 = bloom_MB(S_vals, 0.10)

ax.loglog(S_vals, bm,   color=C_BLUE,   lw=2.2, label="Segment Bitmask (exact)")
ax.loglog(S_vals, bl01, color=C_RED,    lw=2.0, label="Bloom per segment  ε = 0.1%", ls="--")
ax.loglog(S_vals, bl1,  color=C_ORANGE, lw=2.0, label="Bloom per segment  ε = 1%",   ls="-.")
ax.loglog(S_vals, bl10, color=C_GREEN,  lw=2.0, label="Bloom per segment  ε = 10%",  ls=":")

# Mark S = 1024 (current wiki config)
S_cur = 1024
bm_cur  = bitmask_MB(S_cur)
bl1_cur = bloom_MB(S_cur, 0.01)
n_segs_cur = int(np.ceil(N_FILES / S_cur))
ax.axvline(S_cur, color=C_GRAY, lw=1.2, ls="--", alpha=0.8)
ax.text(S_cur * 1.08, bm_cur * 1.3,
        f"S = {S_cur}\n({n_segs_cur} segs)\nBitmask: {bm_cur:.1f} MB",
        fontsize=FONT_ANNOT, color=C_BLUE, va="bottom")

# Find crossover bitmask == bloom(1%)
cross_idx = np.argmin(np.abs(bm - bl1))
S_cross   = S_vals[cross_idx]
ax.axvline(S_cross, color=C_ORANGE, lw=1.0, ls=":", alpha=0.7)
ax.text(S_cross * 0.45, bl1[cross_idx] * 1.5, f"Crossover\nS ≈ {S_cross:.0f}",
        fontsize=FONT_ANNOT, color=C_ORANGE, ha="right")

ax.set_xlabel("Segment size  S  (files per segment)", fontsize=FONT_LABEL)
ax.set_ylabel("Total memory  (MB)", fontsize=FONT_LABEL)
ax.set_title("Space Cost: Segment Bitmask vs. Bloom Filter per Segment\n"
             f"(Wiki: {N_FILES:,} files, {N_CHUNKS/1e6:.1f}M unique chunks, k̄ = {K_AVG:.2f})",
             fontsize=FONT_TITLE)
ax.legend(fontsize=FONT_TICK, framealpha=0.9)
ax.tick_params(labelsize=FONT_TICK)
ax.grid(True, which="both", ls="--", alpha=0.4)
ax.set_xlim(1, N_FILES)

save(fig, "graph1_space_vs_segment_size")

# ==========================================================================
# GRAPH 2 — Space vs Bloom FP rate ε (at fixed S = 1024)
# ==========================================================================
S_fixed = 1024
eps_vals = np.logspace(-4, -0.3, 300)   # 0.01% to 50%

bloom_sizes = bloom_MB(S_fixed, eps_vals)
bm_fixed    = bitmask_MB(S_fixed)

fig, ax = plt.subplots(figsize=(7.5, 5))
ax.set_facecolor(C_LIGHT)
fig.patch.set_facecolor("white")

ax.semilogx(eps_vals * 100, bloom_sizes, color=C_ORANGE, lw=2.2,
            label=f"Bloom per segment (S = {S_fixed})")
ax.axhline(bm_fixed, color=C_BLUE, lw=2.0, ls="--",
           label=f"Segment Bitmask = {bm_fixed:.1f} MB (exact)")

# Crossover ε where Bloom == bitmask
cross_eps_idx = np.argmin(np.abs(bloom_sizes - bm_fixed))
eps_cross     = eps_vals[cross_eps_idx]
ax.axvline(eps_cross * 100, color=C_RED, lw=1.2, ls=":", alpha=0.8)
ax.text(eps_cross * 100 * 1.1, bm_fixed + 1,
        f"Bloom = Bitmask\nε ≈ {eps_cross*100:.0f}%",
        fontsize=FONT_ANNOT, color=C_RED)

# Mark ε = 1%
ax.axvline(1.0, color=C_GRAY, lw=1.0, ls="--", alpha=0.6)
bl1_fixed = bloom_MB(S_fixed, 0.01)
ax.text(1.05, bl1_fixed + 1, f"ε=1%\n{bl1_fixed:.0f} MB", fontsize=FONT_ANNOT, color=C_GRAY)

ax.set_xlabel("Bloom FP rate  ε  (%)", fontsize=FONT_LABEL)
ax.set_ylabel("Total memory  (MB)", fontsize=FONT_LABEL)
ax.set_title(f"Bloom Filter Space vs. FP Rate  (S = {S_fixed}, wiki dataset)\n"
             "Bitmask is smaller than Bloom for any practical FP rate",
             fontsize=FONT_TITLE)
ax.legend(fontsize=FONT_TICK, framealpha=0.9)
ax.tick_params(labelsize=FONT_TICK)
ax.grid(True, which="both", ls="--", alpha=0.4)
ax.set_ylim(0, 120)

save(fig, "graph2_bloom_space_vs_eps")

# ==========================================================================
# GRAPH 3 — FP rate breakdown (file-level) for both structures
# ==========================================================================
# "FP at file level" = filter fires (says possible) but specific file F doesn't have chunk C.
# Granularity FP (shared by both): 1 - k_s/S  where k_s = files in segment with chunk C
# Bloom additional FP: ε  (fires even when segment truly has no match)
#
# We plot total file-level FP vs chunk frequency k (files per chunk).
# k_s | k files total, S files in segment ≈ k × S / N_FILES  (expected k_s if chunk is in segment)

k_vals   = np.arange(1, 1001)
S_plot   = 1024
k_s_avg  = k_vals * (S_plot / N_FILES)   # expected k_s given chunk is in segment

# Clip k_s_avg to S_plot (can't exceed segment size)
k_s_avg  = np.minimum(k_s_avg, S_plot)
gran_fp  = 1 - k_s_avg / S_plot

eps_bloom = 0.01   # Bloom FP rate
# Bloom FP_file = eps (structural) + (1-eps) * gran_fp
bloom_fp  = eps_bloom + (1 - eps_bloom) * gran_fp

fig, ax = plt.subplots(figsize=(8, 5))
ax.set_facecolor(C_LIGHT)
fig.patch.set_facecolor("white")

ax.plot(k_vals, gran_fp  * 100, color=C_BLUE,   lw=2.2, label="Segment Bitmask (granularity FP only)")
ax.plot(k_vals, bloom_fp * 100, color=C_ORANGE, lw=2.0, ls="--",
        label=f"Bloom per segment (granularity + ε={eps_bloom*100:.0f}% structural FP)")

# Fill the gap (structural FP added by Bloom)
ax.fill_between(k_vals, gran_fp*100, bloom_fp*100,
                color=C_RED, alpha=0.25, label="Bloom structural FP overhead")

k_avg_gran = K_AVG * (S_plot / N_FILES)
gran_baseline = (1 - k_avg_gran / S_plot) * 100
ax.axhline(gran_baseline, color=C_GRAY, lw=0.8, ls=":", alpha=0.6)
ax.text(380, gran_baseline - 0.2,
        f"~{gran_baseline:.1f}% (k={K_AVG:.2f} avg)",
        fontsize=8, color=C_GRAY, ha="right")

ax.set_xlabel("Chunk frequency  k  (number of files containing this chunk)", fontsize=FONT_LABEL)
ax.set_ylabel("File-level false positive rate  (%)", fontsize=FONT_LABEL)
ax.set_title(f"File-level FP Rate: Filter fires but specific file F doesn't have chunk C\n"
             f"(S = {S_plot}, ε = {eps_bloom*100:.0f}% for Bloom)",
             fontsize=FONT_TITLE)
ax.legend(fontsize=FONT_TICK, framealpha=0.9)
ax.tick_params(labelsize=FONT_TICK)
ax.grid(True, which="both", ls="--", alpha=0.4)
ax.set_xlim(1, 1000)
ax.set_ylim(95, 100.5)

# Annotation: structural FP gap is tiny
ax.annotate("Bloom overhead\n≈ 1% structural FP\n(tiny vs baseline)",
            xy=(100, (gran_fp[99] + bloom_fp[99]) * 50),
            xytext=(300, 97.0),
            fontsize=FONT_ANNOT, color=C_RED,
            arrowprops=dict(arrowstyle="->", color=C_RED, lw=1.2))

save(fig, "graph3_fp_rate_breakdown")

# ==========================================================================
# GRAPH 4 — Skip probability loss due to Bloom FPs
# ==========================================================================
# When chunk C is truly ABSENT from all candidate segments, the filter should skip.
# Bitmask: always skips (P=1).
# Bloom: skips only if ALL k_cand segment Bloom filters return "no".
# For k_cand candidate segments each with independent FP rate ε:
#   P(skip | chunk absent) = (1-ε)^k_cand

eps_range = np.logspace(-4, -0.3, 300)   # ε from 0.01% to ~50%

fig, ax = plt.subplots(figsize=(7.5, 5))
ax.set_facecolor(C_LIGHT)
fig.patch.set_facecolor("white")

colors = [C_BLUE, C_GREEN, C_ORANGE, C_RED]
for k_cand, col in zip([1, 2, 3, 4], colors):
    skip_p = (1 - eps_range) ** k_cand * 100
    ax.semilogx(eps_range * 100, skip_p, color=col, lw=2.0,
                label=f"{k_cand} candidate segment{'s' if k_cand>1 else ''}")

ax.axhline(100, color=C_GRAY, lw=1.5, ls="--", label="Segment Bitmask (always skips)")
ax.axvline(1.0, color=C_GRAY, lw=1.0, ls=":", alpha=0.6)
ax.text(1.05, 78, "ε = 1%", fontsize=FONT_ANNOT, color=C_GRAY)

ax.set_xlabel("Bloom FP rate  ε  (%)", fontsize=FONT_LABEL)
ax.set_ylabel("P(correct skip when chunk absent)  (%)", fontsize=FONT_LABEL)
ax.set_title("Bloom Filter: Skip Probability when Chunk is Absent\n"
             "Each candidate segment independently may produce a false positive",
             fontsize=FONT_TITLE)
ax.legend(fontsize=FONT_TICK, framealpha=0.9, title="# candidate segments")
ax.tick_params(labelsize=FONT_TICK)
ax.grid(True, which="both", ls="--", alpha=0.4)
ax.set_ylim(60, 101)

save(fig, "graph4_bloom_skip_probability")

# ==========================================================================
# GRAPH 5 — Side-by-side summary bar chart at S = 1024
# ==========================================================================
labels     = ["Segment\nBitmask\n(exact)", "Bloom\nε = 10%", "Bloom\nε = 1%", "Bloom\nε = 0.1%"]
sizes_MB   = [
    bitmask_MB(1024),
    bloom_MB(1024, 0.10),
    bloom_MB(1024, 0.01),
    bloom_MB(1024, 0.001),
]
bar_colors = [C_BLUE, C_GREEN, C_ORANGE, C_RED]
fp_rates   = ["0%\n(exact)", "10%", "1%", "0.1%"]

fig, axes = plt.subplots(1, 2, figsize=(11, 5.5))
fig.patch.set_facecolor("white")

# Left: space bar chart
ax = axes[0]
ax.set_facecolor(C_LIGHT)
bars = ax.bar(labels, sizes_MB, color=bar_colors, edgecolor="white", width=0.55)
for bar, sz in zip(bars, sizes_MB):
    ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.5,
            f"{sz:.1f} MB", ha="center", va="bottom", fontsize=FONT_TICK, fontweight="bold")
ax.axhline(sizes_MB[0], color=C_BLUE, lw=1.2, ls="--", alpha=0.6)
ax.set_ylabel("Total memory (MB)", fontsize=FONT_LABEL)
ax.set_title(f"Space Cost  (S = 1024, wiki 10 MB files)", fontsize=FONT_TITLE)
ax.tick_params(labelsize=FONT_TICK)
ax.grid(axis="y", ls="--", alpha=0.4)
ax.set_ylim(0, max(sizes_MB) * 1.2)

# Right: FP rate bar chart
ax = axes[1]
ax.set_facecolor(C_LIGHT)
fp_vals = [0, 10, 1, 0.1]
bars2 = ax.bar(labels, fp_vals, color=bar_colors, edgecolor="white", width=0.55)
for bar, fp in zip(bars2, fp_vals):
    ax.text(bar.get_x() + bar.get_width()/2,
            bar.get_height() + 0.15 if fp > 0 else 0.15,
            f"{fp}%" if fp > 0 else "0%\n(exact)",
            ha="center", va="bottom", fontsize=FONT_TICK, fontweight="bold")
ax.set_ylabel("Structural FP rate  ε  (%)", fontsize=FONT_LABEL)
ax.set_title("Structural FP Rate (segment-level)", fontsize=FONT_TITLE)
ax.tick_params(labelsize=FONT_TICK)
ax.grid(axis="y", ls="--", alpha=0.4)
ax.set_ylim(0, 13)

_gran_bl = (1 - K_AVG * (1024 / N_FILES) / 1024) * 100
note = (f"Note: both structures share ≈{_gran_bl:.1f}% file-level FP\n"
        f"(granularity FP, S={1024}, k̄ = {K_AVG:.2f}). "
        f"Structural FP (ε) adds on top.")
fig.text(0.5, -0.03, note, ha="center", fontsize=9, color=C_GRAY, style="italic")

fig.suptitle("Segment Bitmask vs. Bloom Filter per Segment — Space & FP Rate Summary",
             fontsize=FONT_TITLE, fontweight="bold", y=1.02)
fig.tight_layout()
save(fig, "graph5_summary_bars")

# ==========================================================================
# GRAPH 6 — Bloom filter mechanics illustration (bit array visual)
# ==========================================================================
np.random.seed(42)
m_bits  = 20    # visual bit array size
k_funcs = 3     # hash functions

fig, ax = plt.subplots(figsize=(10, 4))
fig.patch.set_facecolor("white")
ax.set_facecolor("white")
ax.axis("off")

# Draw bit array
cell_w, cell_h = 0.9, 0.6
bit_array  = np.zeros(m_bits, dtype=int)

# Insert elements (3 chunk fingerprints)
np.random.seed(7)
inserted_positions = []
for elem_idx in range(3):
    positions = np.random.choice(m_bits, k_funcs, replace=False)
    inserted_positions.append(positions.tolist())
    for p in positions:
        bit_array[p] = 1

# Draw bits
for i, bit in enumerate(bit_array):
    color = "#2E75B6" if bit else "#F2F2F2"
    ec    = "#1F497D" if bit else "#AAAAAA"
    rect = plt.Rectangle((i * cell_w, 1.5), cell_w * 0.92, cell_h,
                          facecolor=color, edgecolor=ec, linewidth=1.5)
    ax.add_patch(rect)
    ax.text(i * cell_w + cell_w * 0.46, 1.5 + cell_h/2,
            str(bit), ha="center", va="center",
            color="white" if bit else "#888888", fontsize=11, fontweight="bold")
    ax.text(i * cell_w + cell_w * 0.46, 1.35, str(i),
            ha="center", va="top", fontsize=7, color="#666666")

ax.text(-0.3, 1.5 + cell_h/2, "Bit array\n(m bits)", ha="right", va="center",
        fontsize=10, color="#333333")

# Show query chunk (non-member → FP demonstration)
query_positions = [inserted_positions[0][0], inserted_positions[0][1],
                   np.random.choice([j for j in range(m_bits) if bit_array[j]==1])]
query_positions = list(set(query_positions))[:k_funcs]

ax.text(m_bits * cell_w / 2, 3.1,
        'Query chunk C  →  hash functions h₁, h₂, h₃  →  probe positions',
        ha="center", va="center", fontsize=11, color="#333333",
        bbox=dict(boxstyle="round,pad=0.3", facecolor="#FFF8E0", edgecolor=C_ORANGE, lw=1.5))

colors_q = [C_RED, C_GREEN, C_ORANGE]
for qi, (pos, col) in enumerate(zip(query_positions, colors_q)):
    x_pos = pos * cell_w + cell_w * 0.46
    ax.annotate("", xy=(x_pos, 1.5 + cell_h),
                xytext=(x_pos, 2.6),
                arrowprops=dict(arrowstyle="->", color=col, lw=1.8))
    ax.text(x_pos, 2.65, f"h{qi+1}", ha="center", va="bottom", fontsize=9, color=col)

# Outcome
all_ones = all(bit_array[p] for p in query_positions)
outcome_text = "All probed bits = 1  →  POSSIBLE MEMBER  (may be FP!)" if all_ones else "Some bit = 0  →  DEFINITELY NOT a member  (safe skip)"
outcome_col  = C_ORANGE if all_ones else C_GREEN
ax.text(m_bits * cell_w / 2, 0.7, outcome_text,
        ha="center", va="center", fontsize=11, color=outcome_col, fontweight="bold",
        bbox=dict(boxstyle="round,pad=0.3", facecolor=C_LIGHT, edgecolor=outcome_col, lw=1.5))

ax.set_xlim(-2, m_bits * cell_w + 0.5)
ax.set_ylim(0.2, 3.6)
ax.set_title(f"Bloom Filter Mechanics: k={k_funcs} hash functions, m={m_bits} bits\n"
             "False positive = all k probed bits are 1 from OTHER elements' insertions",
             fontsize=FONT_TITLE, pad=8)

save(fig, "graph6_bloom_mechanics")

print(f"\nAll graphs saved to {OUT}/")
