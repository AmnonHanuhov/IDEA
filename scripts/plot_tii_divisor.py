#!/usr/bin/env python3
"""
Plot the terms-index-divisor sweep for the A4 regime.

Reads the four result JSONs produced by run_experiments.py:
    results_a4_tii_d{1,4}_{cold,warm}.json
and emits a two-panel comparison:
    Panel A  searcher load time   -- what the divisor is meant to cut
    Panel B  doc lookup time      -- what it costs (longer .tis scans)

Encoding: hue = divisor, hatch = cache state, so the four configurations stay
distinguishable without relying on colour alone.  A CSV table view is written
alongside the figure.
"""

import json
import os
import sys
from collections import defaultdict

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

# Categorical slots 1 and 2, light mode (references/palette.md, unmodified).
C_D1 = "#2a78d6"   # blue   -- divisor 1 (stock)
C_D4 = "#eb6834"   # orange -- divisor 4
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
GRID = "#d8d7d2"

CONFIGS = [
    ("d1_cold", "divisor 1, cold", C_D1, "///"),
    ("d1_warm", "divisor 1, warm", C_D1, ""),
    ("d4_cold", "divisor 4, cold", C_D4, "///"),
    ("d4_warm", "divisor 4, warm", C_D4, ""),
]

METRICS = [
    ("searcher_load_time", "Searcher load time (s)",
     "Opening the index: eager .tii decode"),
    ("doc_lookup_time", "Doc lookup time (s)",
     "Lucene search: linear scans in .tis"),
]

SHORT = {
    "dedup": "dedup",
    "dedup+1024seg": "+1024seg",
    "dedup+1024seg-sparse": "+1024sp",
    "dedup+256seg": "+256seg",
    "dedup+256seg-sparse": "+256sp",
    "dedup+64seg": "+64seg",
    "dedup+64seg-sparse": "+64sp",
    "naive": "naive",
}
ORDER = list(SHORT.keys())


def load(root):
    """cfg -> variant -> metric -> list of per-rep values"""
    data = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    for cfg, _, _, _ in CONFIGS:
        path = os.path.join(root, f"results_a4_tii_{cfg}.json")
        if not os.path.exists(path):
            sys.exit(f"missing {path} -- run the sweep first")
        for rec in json.load(open(path)):
            if "A4" not in (rec.get("exp_group") or ""):
                continue
            if rec.get("returncode") != 0:
                continue
            variant = rec["exp_name"].split(" ")[0]
            for metric, _, _ in METRICS:
                v = rec.get(metric)
                if v is not None:
                    data[cfg][variant][metric].append(v)
    return data


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else "."
    outdir = sys.argv[2] if len(sys.argv) > 2 else "graphs/tii_divisor"
    os.makedirs(outdir, exist_ok=True)
    data = load(root)

    variants = [v for v in ORDER if any(v in data[c] for c, _, _, _ in CONFIGS)]
    x = np.arange(len(variants), dtype=float)
    n = len(CONFIGS)
    # 2px-equivalent surface gap between adjacent bars
    slot = 0.82 / n
    width = slot * 0.88

    fig, axes = plt.subplots(
        1, 2, figsize=(13.5, 4.9), facecolor=SURFACE,
        gridspec_kw={"wspace": 0.18},
    )

    for ax, (metric, ylabel, subtitle) in zip(axes, METRICS):
        ax.set_facecolor(SURFACE)
        for i, (cfg, label, colour, hatch) in enumerate(CONFIGS):
            means, lo, hi = [], [], []
            for v in variants:
                vals = data[cfg].get(v, {}).get(metric, [])
                if vals:
                    m = float(np.mean(vals))
                    means.append(m)
                    lo.append(m - min(vals))
                    hi.append(max(vals) - m)
                else:
                    means.append(np.nan); lo.append(0.0); hi.append(0.0)
            ax.bar(
                x + (i - (n - 1) / 2) * slot, means, width,
                color=colour, hatch=hatch, edgecolor=SURFACE, linewidth=1.2,
                label=label, zorder=3,
            )
            ax.errorbar(
                x + (i - (n - 1) / 2) * slot, means, yerr=[lo, hi],
                fmt="none", ecolor=INK_2, elinewidth=1.0, capsize=2.5,
                alpha=0.7, zorder=4,
            )

        ax.set_ylabel(ylabel, color=INK, fontsize=10.5)
        ax.set_title(subtitle, color=INK_2, fontsize=9.5, loc="left", pad=8)
        ax.set_xticks(x)
        ax.set_xticklabels([SHORT[v] for v in variants], rotation=30,
                           ha="right", fontsize=9, color=INK_2)
        ax.tick_params(axis="y", labelsize=9, colors=INK_2)
        ax.grid(axis="y", color=GRID, linewidth=0.8, zorder=0)
        ax.set_axisbelow(True)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        for side in ("left", "bottom"):
            ax.spines[side].set_color(GRID)

    axes[0].legend(
        frameon=False, fontsize=9, labelcolor=INK_2, ncol=2,
        loc="upper left",
    )
    fig.suptitle(
        "A4 regime — terms-index divisor vs. cache state",
        color=INK, fontsize=13, x=0.09, ha="left", y=1.02,
    )

    for ext in ("png", "pdf"):
        p = os.path.join(outdir, f"tii_divisor_A4.{ext}")
        fig.savefig(p, bbox_inches="tight", dpi=150, facecolor=SURFACE)
        print(f"  graph -> {p}")
    plt.close(fig)

    # Table view (relief rule): the same numbers, readable without colour.
    csv = os.path.join(outdir, "tii_divisor_A4.csv")
    with open(csv, "w") as f:
        f.write("variant,config,metric,mean,min,max,n\n")
        for v in variants:
            for cfg, label, _, _ in CONFIGS:
                for metric, _, _ in METRICS:
                    vals = data[cfg].get(v, {}).get(metric, [])
                    if not vals:
                        continue
                    f.write(f"{v},{label},{metric},{np.mean(vals):.4f},"
                            f"{min(vals):.4f},{max(vals):.4f},{len(vals)}\n")
    print(f"  table -> {csv}")

    # Console summary of the headline metric.
    print(f"\n  {'variant':<22}" + "".join(f"{l:>18}" for _, l, _, _ in CONFIGS))
    for v in variants:
        row = f"  {v:<22}"
        for cfg, _, _, _ in CONFIGS:
            vals = data[cfg].get(v, {}).get("searcher_load_time", [])
            row += f"{(np.mean(vals) if vals else float('nan')):>18.3f}"
        print(row)


if __name__ == "__main__":
    main()
