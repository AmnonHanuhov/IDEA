#!/usr/bin/env python3
"""
IDEA Experiment Runner
======================
Automates destor query experiments across implementations and query regimes,
then outputs comparison tables.

Before running the NOT suites, generate the keyword dictionaries with:
    destor -z    # naive index  → keywords/linux/test/file_{low,med,high}/
    destor -y    # dedup index  → keywords/linux/test/chunk_{low,med,high}/

Usage:
    python3 scripts/run_experiments.py [suite ...] [--reps N] [--no-cache-clear] [--save FILE]

Available suites:
    and_impl      Compare AND implementations (inc vs bitmap) across keyword counts/frequencies
    not_regimes   NOT queries across regimes that stress different optimizations
    not_baseline  NOT dedup vs naive — correctness cross-check + baseline timing
    all           Run all suites (default)

Available datasets (--dataset):
    linux         keywords/linux  (default)
    wiki          keywords/wiki

Keyword formats (--kw-format):
    new           keywords/<dataset>/test/file_{level}/keywords_{count}.txt  (default)
    old           keywords/<dataset>/{count}/file-{level}.txt  (legacy 100MB-era files)
                  Single-keyword variants: 1a/1b/1c/1d subdirectories.
                  Available counts: 4, 16, 32, 64, 128.

Query server (--server):
    By default every run is a separate destor process, so each query also pays
    to open the indexes (searcher load) and its companion structures.  With
    --server a single `destor --serve` process opens the dedup and naive
    indexes, the reverse mapping, the pathstore and every segment index the
    suite needs once, then answers all the queries; the tables and graphs show
    query time only.  Only the -i, -j and -k experiments (the NOT suites and
    seg_filter) can run this way.
      cold (default)    before each query drop the page cache and, with
                        --flush-file, read --flush-gib GiB of that file with
                        O_DIRECT so that the RAID controller cache holds none
                        of the query's data either
      hot (--no-cache-clear)
                        no drops; --prewarm reads the given files or
                        directories into the page cache first

Examples:
    python3 scripts/run_experiments.py and_impl --reps 3
    python3 scripts/run_experiments.py not_regimes --no-cache-clear
    python3 scripts/run_experiments.py all --save results.json
    python3 scripts/run_experiments.py all --dataset wiki --kw-format old --graph-dir graphs/wiki_100mb --save results_wiki_100mb.json
    python3 scripts/run_experiments.py seg_filter --dataset wiki --server --flush-file /data/.../container.pool \
        --save results_server_cold.json --graph-dir graphs/server_cold --resume
"""

import subprocess
import re
import sys
import time
import os
import json
import mmap
import argparse
import datetime
import threading
from dataclasses import dataclass, asdict, fields
from typing import Optional
from collections import defaultdict

try:
    import matplotlib
    import matplotlib.patches
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np
    _MATPLOTLIB_AVAILABLE = True
except ImportError:
    _MATPLOTLIB_AVAILABLE = False

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DESTOR_BIN = "destor"
CLEAR_CACHE_CMD = ["sudo", "bash", "-c", "echo 1 > /proc/sys/vm/drop_caches"]

# ---------------------------------------------------------------------------
# Keyword file helpers (dataset-agnostic)
# ---------------------------------------------------------------------------

def kw_file(freq: str, count: int, kw_base: str) -> str:
    """Path to a multi-keyword file, e.g. keywords/<dataset>/test/file_low/keywords_4.txt"""
    return f"{kw_base}/test/{freq}/keywords_{count}.txt"


def read_keywords(freq: str, count: int, kw_base: str, index: str = "file") -> list:
    """Read the first `count` keywords from a generated keyword file."""
    path = kw_file(f"{index}_{freq}", count, kw_base)
    try:
        with open(path) as f:
            return [line.strip() for line in f if line.strip()][:count]
    except FileNotFoundError:
        return []


def check_kw_files(kw_base: str):
    """Warn if keyword dictionaries haven't been generated yet."""
    missing_file  = [kw_file(f"file_{b}",  4, kw_base) for b in ("low", "med", "high")
                     if not os.path.exists(kw_file(f"file_{b}", 4, kw_base))]
    missing_chunk = [kw_file(f"chunk_{b}", 4, kw_base) for b in ("low", "med", "high")
                     if not os.path.exists(kw_file(f"chunk_{b}", 4, kw_base))]
    if missing_file:
        print(f"WARNING: file keyword dictionaries are missing under {kw_base}/test/ — run  `destor --kw-dataset <name> -z`  first.")
        for p in missing_file:
            print(f"  missing: {p}")
        print()
    if missing_chunk:
        print(f"NOTE: chunk keyword dictionaries not found under {kw_base}/test/ (run `destor --kw-dataset <name> -y` if needed).")
        for p in missing_chunk:
            print(f"  missing: {p}")
        print()


# ---------------------------------------------------------------------------
# Old keyword format helpers  (legacy 100MB-era files)
# ---------------------------------------------------------------------------
# Layout:  keywords/<dataset>/{count}/file-{level}.txt   (multi-keyword)
#          keywords/<dataset>/1a/file-{level}.txt         (single keyword, variant A)
#          keywords/<dataset>/1b/file-{level}.txt         (single keyword, variant B)
#          keywords/<dataset>/1c/file-{level}.txt         (single keyword, variant C)
#          keywords/<dataset>/1d/file-{level}.txt         (single keyword, variant D)
# Available counts: 4, 16, 32, 64, 128.

_OLD_KW_COUNTS = (4, 16, 32, 64, 128)
_OLD_KW_VARIANTS = ("1a", "1b", "1c", "1d")


def old_kw_file(freq_key: str, count_or_variant, kw_base: str) -> str:
    """Old-format path.  freq_key like 'file_high'; count_or_variant like 4 or '1a'."""
    fname = freq_key.replace("_", "-") + ".txt"   # file_high → file-high.txt
    return f"{kw_base}/{count_or_variant}/{fname}"


def read_old_keywords(freq_key: str, count_or_variant, kw_base: str) -> list:
    path = old_kw_file(freq_key, count_or_variant, kw_base)
    try:
        with open(path) as f:
            return [line.strip() for line in f if line.strip()]
    except FileNotFoundError:
        return []


def check_old_kw_files(kw_base: str):
    missing = []
    for freq in ("file-low", "file-med", "file-high"):
        for v in ("1a", "1b"):
            p = f"{kw_base}/{v}/{freq}.txt"
            if not os.path.exists(p):
                missing.append(p)
        p4 = f"{kw_base}/4/{freq}.txt"
        if not os.path.exists(p4):
            missing.append(p4)
    if missing:
        print("WARNING: some old-format keyword files are missing:")
        for p in missing:
            print(f"  missing: {p}")
        print()

# ---------------------------------------------------------------------------
# Experiment dataclass
# ---------------------------------------------------------------------------

@dataclass
class Experiment:
    name: str
    cmd: list           # args passed to destor (not including the binary)
    desc: str = ""
    group: str = ""     # groups experiments into comparison blocks in the table
    query_plain: str = ""   # human-readable query, e.g. "foo AND bar NOT baz"
    purpose: str = ""       # one-liner: what bottleneck this regime exercises
    kw_files: list = None   # source keyword file(s) used to build the query

    def __post_init__(self):
        if self.kw_files is None:
            self.kw_files = []


def build_query(incl_terms, excl_terms) -> str:
    """Build the Lucene query string for (all incl) AND NOT (any excl).

    Included terms carry an explicit '+' (MUST).  This matters: Lucene's default
    operator is OR, so an unmarked term parses as SHOULD (optional).  The naive
    path hands the parsed query to Lucene and honours that, while the dedup path
    intersects its included terms regardless -- so without the '+' the two
    implementations answer different questions and their result counts diverge.
    """
    return (" ".join(f"+{t}" for t in incl_terms) + " "
            + " ".join(f"-{t}" for t in excl_terms))


def lucene_to_plain(query: str) -> str:
    """Convert a Lucene query string to plain English.
    '+foo +bar -baz'  →  'foo AND bar NOT baz'
    """
    parts = query.split()
    included = [p.lstrip("+") for p in parts if not p.startswith("-")]
    excluded = [p.lstrip("-") for p in parts if p.startswith("-")]
    result = " AND ".join(included)
    if excluded:
        result += " NOT " + " NOT ".join(excluded)
    return result


# ---------------------------------------------------------------------------
# Suite: AND implementation comparison
# ---------------------------------------------------------------------------
# Each group = same keyword file run with both --and-impl values (inc, bitmap).
# Dimensions: keyword count (4, 8, 16) × frequency (file_low, file_med, file_high).
#
# What each regime tests:
#   file_low  – rare terms → very few files per term, small AND result set
#   file_med  – medium frequency → moderate intersection sizes
#   file_high – common terms → large per-term hit lists, AND is expensive
#
# Varying keyword count: more keywords = tighter AND = fewer results, but
# more Lucene queries and more chunk fetches.

def and_impl_suite(kw_base: str) -> list:
    purposes = {
        "file_low":  "Rare terms → small per-term hit lists, tight AND result",
        "file_med":  "Medium-frequency terms → moderate intersection sizes",
        "file_high": "Common terms → large per-term hit lists, AND phase is expensive",
    }
    experiments = []
    for freq in ("file_low", "file_med", "file_high"):
        for count in (4, 8, 16):
            group = f"AND {count}-kw {freq}"
            f = kw_file(freq, count, kw_base)
            if not os.path.exists(f):
                continue
            kws = read_keywords(freq.split("_")[1], count, kw_base)
            plain = " AND ".join(kws) if kws else f"({count} keywords from {f})"
            for impl in ("inc", "bitmap"):
                experiments.append(Experiment(
                    name=f"{group} [{impl}]",
                    cmd=["-v", f"-f{f}", f"--and-impl={impl}"],
                    desc=f"Dedup AND, {count} keywords from {freq}, impl={impl}",
                    group=group,
                    query_plain=plain,
                    purpose=purposes[freq],
                    kw_files=[f],
                ))
    return experiments


# ---------------------------------------------------------------------------
# Suite: NOT query regimes
# ---------------------------------------------------------------------------
# Queries are built dynamically from the generated keyword files so the
# experiments always use real terms from the actual dataset.
#
# Each regime isolates a different bottleneck in the NOT pipeline:
#
#  Regime            | included freq | excluded freq | Bottleneck
#  ------------------|---------------|---------------|-----------------------------
#  common_incl_rare  | high          | low           | AND phase (many candidates)
#  common_both       | high          | high          | NOT filter scan
#  rare_incl_common  | low           | high          | Wasted NOT scan (few survivors)
#                    |               |               | ← where Opt 2 would help most
#  med_incl_med      | med           | med           | Balanced
#  and2_not          | 2 × high      | high          | AND cost + NOT filter
#  and2_rare_not     | 2 × low       | high          | Tight AND → tiny set + big NOT
#
# Pairs use DIFFERENT terms from the same frequency band (index 0 = included,
# index 1 = excluded) so the query is never trivially empty.

def _not_experiment(regime_id: str, query: str, purpose: str, kw_files: list) -> Experiment:
    return Experiment(
        name=f"dedup {regime_id}",
        cmd=["-i", query],
        desc=f"[dedup] {purpose}",
        group=regime_id,
        query_plain=lucene_to_plain(query),
        purpose=purpose,
        kw_files=kw_files,
    )

def _not_seg_experiment(regime_id: str, query: str, purpose: str, kw_files: list,
                        files_per_seg: int = 1024, sparse: bool = False) -> Experiment:
    suffix = "-sparse" if sparse else ""
    cmd = ["-j", query, "-P", str(files_per_seg)] + (["-S"] if sparse else [])
    return Experiment(
        name=f"dedup+{files_per_seg}seg{suffix} {regime_id}",
        cmd=cmd,
        desc=f"[dedup+{files_per_seg}seg{suffix}] {purpose}",
        group=regime_id,
        query_plain=lucene_to_plain(query),
        purpose=purpose,
        kw_files=kw_files,
    )

def _naive_experiment(regime_id: str, query: str, purpose: str, kw_files: list) -> Experiment:
    return Experiment(
        name=f"naive {regime_id}",
        cmd=["-k", query],
        desc=f"[naive] {purpose}",
        group=regime_id,
        query_plain=lucene_to_plain(query),
        purpose=purpose,
        kw_files=kw_files,
    )


def _build_not_experiments(include_naive: bool, kw_base: str) -> list:
    """
    Build NOT experiments.  Returns empty list with a warning if keyword
    files haven't been generated yet.
    """
    experiments = []

    def add(regime_id, incl_terms: list, excl_terms: list, purpose: str,
            incl_files: list = None, excl_files: list = None):
        if not incl_terms or not excl_terms:
            print(f"  [skip] {regime_id}: missing keywords (run destor --kw-dataset <name> -z first)")
            return
        query = build_query(incl_terms, excl_terms)
        kw_files = (incl_files or []) + (excl_files or [])
        experiments.append(_not_experiment(regime_id, query, purpose, kw_files))
        experiments.append(_not_seg_experiment(regime_id, query, purpose, kw_files))
        if include_naive:
            experiments.append(_naive_experiment(regime_id, query, purpose, kw_files))

    high = lambda n=1: read_keywords("high", 4, kw_base)[:n]
    med  = lambda n=1: read_keywords("med",  4, kw_base)[:n]
    low  = lambda n=1: read_keywords("low",  4, kw_base)[:n]

    hf = kw_file("file_high", 4, kw_base)
    mf = kw_file("file_med",  4, kw_base)
    lf = kw_file("file_low",  4, kw_base)

    add("R1_common_incl_rare_excl",
        high(1), low(1),
        "Common included, rare excluded → cheap NOT filter, AND dominates",
        incl_files=[hf], excl_files=[lf])

    add("R2_common_incl_common_excl",
        high(1), [read_keywords("high", 4, kw_base)[1]],
        "Common included, common excluded → heavy NOT filter scan",
        incl_files=[hf], excl_files=[hf])

    add("R3_rare_incl_common_excl",
        low(1), high(1),
        "Rare included, common excluded → few candidates but huge excluded list (Opt 2 regime)",
        incl_files=[lf], excl_files=[hf])

    add("R4_med_incl_med_excl",
        med(1), [read_keywords("med", 4, kw_base)[1]],
        "Medium both → balanced workload",
        incl_files=[mf], excl_files=[mf])

    add("R5_and2_high_not_high",
        high(2), [read_keywords("high", 4, kw_base)[2]],
        "2-term AND (common) + NOT (common) → AND phase + NOT filter both costly",
        incl_files=[hf], excl_files=[hf])

    add("R6_and2_low_not_high",
        low(2), high(1),
        "2-term AND (rare) + NOT (common) → tight AND result, huge NOT list",
        incl_files=[lf], excl_files=[hf])

    add("R7_and3_med_not_high",
        med(3), [read_keywords("high", 4, kw_base)[1]],
        "3-term AND (medium) + NOT (common)",
        incl_files=[mf], excl_files=[hf])

    return experiments


def not_regimes_suite(kw_base: str) -> list:
    return _build_not_experiments(include_naive=False, kw_base=kw_base)


def not_baseline_suite(kw_base: str) -> list:
    return _build_not_experiments(include_naive=True, kw_base=kw_base)


# ---------------------------------------------------------------------------
# Suite: segment-filter significance
# ---------------------------------------------------------------------------
# The offline segment bitmask lets the NOT/AND phases skip an excluded chunk
# (and its reverse-mapping fetch) whenever the chunk's per-chunk segment
# bitmask does not overlap the candidate mask.  A skip is only possible when
# the AND survivors are concentrated in FEW segments, so this suite sweeps the
# two knobs that control segment locality of the candidate set:
#
#   A. AND tightness   — more included terms → smaller, more segment-local
#                        candidate set → more skips.  (Excluded term fixed.)
#   B. Excluded size   — a larger excluded posting list gives more chunks that
#                        the filter can potentially skip.  (Included AND fixed.)
#
# Each experiment runs both plain dedup (-i) and dedup+seg (-j) so the skip
# counts, avoided reverse-mapping fetches, and AND+NOT eval time can be
# compared directly.

# Segment granularities (files per segment) to compare, coarse → fine.
SEG_SIZES = [1024, 256, 64]
# When True, also emit the sparse (sorted segment-id list) variant next to each
# dense one, for a dense-vs-sparse head-to-head.
SEG_COMPARE_SPARSE = True

# Mid-frequency excluded terms (wiki-specific): ~10^4 chunks each and
# segment-local, so a tight AND + one of these yields a large *and* highly
# skippable excluded list.  (term, short-tag-for-regime-name)
MID_EXCLUDED_TERMS = [("glowing", "glowing")]

def seg_filter_suite(kw_base: str) -> list:
    experiments = []

    med_pool  = read_keywords("med",  8, kw_base)
    high_pool = read_keywords("high", 8, kw_base)
    low_pool  = read_keywords("low",  8, kw_base)
    mf = kw_file("file_med",  8, kw_base)
    hf = kw_file("file_high", 8, kw_base)
    lf = kw_file("file_low",  8, kw_base)

    def add(regime_id, incl, excl, purpose, files):
        if not incl or not excl:
            print(f"  [skip] {regime_id}: missing keywords (run destor --kw-dataset <name> -z first)")
            return
        query = build_query(incl, excl)
        experiments.append(_not_experiment(regime_id, query, purpose, files))
        for fps in SEG_SIZES:
            experiments.append(_not_seg_experiment(regime_id, query, purpose, files, fps, sparse=False))
            if SEG_COMPARE_SPARSE:
                experiments.append(_not_seg_experiment(regime_id, query, purpose, files, fps, sparse=True))
        experiments.append(_naive_experiment(regime_id, query, purpose, files))

    # --- A: AND-tightness sweep (excluded fixed = 1 common term) ---
    if high_pool and med_pool:
        excl1 = [high_pool[0]]
        for k in (1, 2, 3, 4):
            if len(med_pool) >= k:
                add(f"A{k}_and{k}med_not1high", med_pool[:k], excl1,
                    f"{k}-term med AND — candidate set (and its segment footprint) "
                    f"shrinks as k grows",
                    [mf, hf])

    # --- B: excluded-list-size sweep (included fixed = 3 med terms) ---
    if len(med_pool) >= 4:
        incl3 = med_pool[:3]
        if low_pool:
            add("B1_and3med_not1low", incl3, [low_pool[0]],
                "Tight AND + tiny excluded list", [mf, lf])
        add("B2_and3med_not1med", incl3, [med_pool[3]],
            "Tight AND + medium excluded list", [mf])
        if len(high_pool) >= 3:
            add("B3_and3med_not3high", incl3, high_pool[:3],
                "Tight AND + very large (3-term) excluded list", [mf, hf])

    # --- C: hero regime — tight AND (few candidate segments) + MANY rare
    #        excluded terms whose chunks are segment-local, so the filter can
    #        actually skip them.  Skips scale with the number of rare excluded
    #        terms while the per-chunk skip rate stays roughly constant. ---
    low_many = read_keywords("low", 64, kw_base)   # keywords_64.txt exists; slice below
    if len(med_pool) >= 4 and len(low_many) >= 15:
        incl4 = med_pool[:4]   # collapses the candidate set into ~2 segments
        for n in (15, 30, 60):
            if len(low_many) >= n:
                add(f"C{n}_and4med_not{n}rare", incl4, low_many[:n],
                    f"Tight AND (candidate ~2 segments) + {n} rare excluded terms "
                    f"→ segment filter skips segment-local excluded chunks",
                    [mf, lf])

    # --- D: mid-frequency excluded (wiki-specific) — tight AND + a single
    #        excluded term with ~a few ten thousand chunks that are also
    #        segment-local, so a HIGH skip % removes enough reverse-mapping
    #        decodes to show up in wall time (unlike the small-excluded C
    #        regimes, and with a higher skip % than the ubiquitous A4 term). ---
    for term, tag in MID_EXCLUDED_TERMS:
        if len(med_pool) >= 4:
            add(f"D_{tag}_and4med_not1mid", med_pool[:4], [term],
                f"Tight AND + mid-frequency excluded '{term}' "
                f"(~10^4 chunks, segment-local)",
                [mf, hf])

    return experiments


# ---------------------------------------------------------------------------
# Old-format suite builders
# ---------------------------------------------------------------------------

def and_impl_suite_old(kw_base: str) -> list:
    purposes = {
        "file_low":  "Rare terms → small per-term hit lists, tight AND result",
        "file_med":  "Medium-frequency terms → moderate intersection sizes",
        "file_high": "Common terms → large per-term hit lists, AND phase is expensive",
    }
    experiments = []
    for freq in ("file_low", "file_med", "file_high"):
        for count in (4, 16, 32):
            f = old_kw_file(freq, count, kw_base)
            if not os.path.exists(f):
                continue
            kws = read_old_keywords(freq, count, kw_base)
            group = f"AND {count}-kw {freq}"
            plain = " AND ".join(kws[:3]) + (" AND ..." if len(kws) > 3 else "")
            for impl in ("inc", "bitmap"):
                experiments.append(Experiment(
                    name=f"{group} [{impl}]",
                    cmd=["-v", f"-f{f}", f"--and-impl={impl}"],
                    desc=f"Dedup AND, {count} keywords from {freq} (old kw), impl={impl}",
                    group=group,
                    query_plain=plain,
                    purpose=purposes[freq],
                    kw_files=[f],
                ))
    return experiments


def _build_not_experiments_old(include_naive: bool, kw_base: str) -> list:
    experiments = []

    def read1(freq_key: str, variant: str) -> list:
        return read_old_keywords(freq_key, variant, kw_base)

    def fpath(freq_key: str, variant) -> str:
        return old_kw_file(freq_key, variant, kw_base)

    def add(regime_id, incl_terms, excl_terms, purpose, incl_files=None, excl_files=None):
        if not incl_terms or not excl_terms:
            print(f"  [skip] {regime_id}: missing keywords")
            return
        query = build_query(incl_terms, excl_terms)
        kw_files_list = (incl_files or []) + (excl_files or [])
        experiments.append(_not_experiment(regime_id, query, purpose, kw_files_list))
        if include_naive:
            experiments.append(_naive_experiment(regime_id, query, purpose, kw_files_list))

    # Single keywords by variant
    hi_a = read1("file_high", "1a");  hi_b = read1("file_high", "1b");  hi_c = read1("file_high", "1c")
    med_a = read1("file_med",  "1a"); med_b = read1("file_med",  "1b"); med_c = read1("file_med",  "1c")
    low_a = read1("file_low",  "1a"); low_b = read1("file_low",  "1b")

    add("R1_common_incl_rare_excl",
        hi_a, low_a,
        "Common included, rare excluded → cheap NOT filter, AND dominates",
        [fpath("file_high", "1a")], [fpath("file_low", "1a")])

    add("R2_common_incl_common_excl",
        hi_a, hi_b,
        "Common included, common excluded → heavy NOT filter scan",
        [fpath("file_high", "1a")], [fpath("file_high", "1b")])

    add("R3_rare_incl_common_excl",
        low_a, hi_a,
        "Rare included, common excluded → few candidates but huge excluded list (Opt 2 regime)",
        [fpath("file_low", "1a")], [fpath("file_high", "1a")])

    add("R4_med_incl_med_excl",
        med_a, med_b,
        "Medium both → balanced workload",
        [fpath("file_med", "1a")], [fpath("file_med", "1b")])

    add("R5_and2_high_not_high",
        hi_a + hi_b, hi_c,
        "2-term AND (common) + NOT (common) → AND phase + NOT filter both costly",
        [fpath("file_high", "1a"), fpath("file_high", "1b")], [fpath("file_high", "1c")])

    add("R6_and2_low_not_high",
        low_a + low_b, hi_a,
        "2-term AND (rare) + NOT (common) → tight AND result, huge NOT list",
        [fpath("file_low", "1a"), fpath("file_low", "1b")], [fpath("file_high", "1a")])

    add("R7_and3_med_not_high",
        med_a + med_b + med_c, hi_b,
        "3-term AND (medium) + NOT (common)",
        [fpath("file_med", "1a"), fpath("file_med", "1b"), fpath("file_med", "1c")],
        [fpath("file_high", "1b")])

    return experiments


def not_regimes_suite_old(kw_base: str) -> list:
    return _build_not_experiments_old(include_naive=False, kw_base=kw_base)


def not_baseline_suite_old(kw_base: str) -> list:
    return _build_not_experiments_old(include_naive=True, kw_base=kw_base)


# Suite registry: maps suite name → (new_fn, old_fn)
_SUITE_FNS = {
    "and_impl":     (and_impl_suite,     and_impl_suite_old),
    "not_regimes":  (not_regimes_suite,  not_regimes_suite_old),
    "not_baseline": (not_baseline_suite, not_baseline_suite_old),
    "seg_filter":   (seg_filter_suite,   seg_filter_suite),
}

# Keep backward-compatible SUITES dict (new format)
SUITES = {k: v[0] for k, v in _SUITE_FNS.items()}

# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

@dataclass
class RunResult:
    exp_name: str
    exp_desc: str
    exp_group: str
    rep: int
    returncode: int
    wall_time: float
    # Component timings (from libclucene-wrapper.cpp)
    searcher_load_time: Optional[float] = None   # opening the index
    doc_lookup_time: Optional[float] = None      # Lucene search → chunk IDs
    and_eval_time: Optional[float] = None         # CAI AND phase (dedup)
    not_eval_time: Optional[float] = None         # NOT filter phase (dedup)
    gather_results_time: Optional[float] = None   # collect surviving candidates (dedup)
    fetch_files_time: Optional[float] = None      # chunk→file resolution (naive)
    # Computed total: searcher_load + doc_lookup + (and+not+gather OR fetch_files)
    total_time: Optional[float] = None
    num_results: Optional[int] = None
    # Optimization diagnostics (dedup only)
    term_chunk_counts: Optional[list] = None  # per-term chunk count in sorted order
    excl_chunk_count: Optional[int] = None    # excluded posting list size
    and_candidates: Optional[int] = None      # files entering NOT filter
    opt1_skips: Optional[int] = None          # AND chunks skipped by Opt 1
    not_killed: Optional[int] = None          # AND candidates eliminated by NOT
    excl_chunks_scanned: Optional[int] = None # excluded chunks actually iterated
    excl_chunks_total: Optional[int] = None   # total excluded chunks (denominator)
    seg_filter_skips: Optional[int] = None    # excluded chunks skipped by segment filter
    seg_build_time: Optional[float] = None    # time to build the segment bitmask
    seg_load_time: Optional[float] = None     # time to load the segment bitmask (dedup+seg)
    dtor_time: Optional[float] = None         # index destructor / close time
    # Segment-filter diagnostics (dedup+seg only)
    candidate_segments: Optional[int] = None  # #segments the AND survivors span (mask popcount)
    seg_skips_and: Optional[int] = None       # included chunks skipped during AND phase
    seg_skips_not: Optional[int] = None       # excluded chunks skipped during NOT phase
    seg_skip_pct: Optional[float] = None      # seg_skips_not / excl chunks total (%)
    unique_chunks_fetched: Optional[int] = None  # chunks whose file list was fetched
    and_not_eval_time: Optional[float] = None    # AND+NOT in-memory phase (getResults...)
    and_majflt: Optional[int] = None          # major (disk) faults during the AND phase
    not_majflt: Optional[int] = None          # major (disk) faults during the NOT phase
    # The whole query as timed inside destor ("lookup time"): with --server
    # this excludes opening the indexes, which the server did at startup.
    query_time: Optional[float] = None
    indirection_time: Optional[float] = None  # file IDs -> paths (dedup)
    # --server only: what this query did to the process and the disk
    query_majflt: Optional[int] = None
    query_minflt: Optional[int] = None
    query_read_bytes: Optional[int] = None    # /proc/self/io read_bytes
    disk_reads: Optional[dict] = None         # --diskstats-dev: device -> [reads, ms reading]
    started_at: str = ""
    # Metadata for graphing/reporting
    query_plain: str = ""
    purpose: str = ""
    error: str = ""
    raw_stdout: str = ""


# Patterns are applied to the full stdout; first match wins per key.
_TIME_PATTERNS = [
    ("searcher_load_time",  r"searcher load time:\s*([\d.]+)"),
    ("seg_build_time",      r"seg filter build time:\s*([\d.]+)"),
    ("seg_load_time",       r"seg filter load time:\s*([\d.]+)"),
    ("dtor_time",           r"destructors' time:\s*([\d.]+)"),
    ("doc_lookup_time",     r"doc lookup time:\s*([\d.]+)"),
    ("and_eval_time",       r"AND evaluation time:\s*([\d.]+)"),
    ("not_eval_time",       r"NOT evaluation time:\s*([\d.]+)"),
    ("gather_results_time", r"Gather results time:\s*([\d.]+)"),
    ("fetch_files_time",    r"fetch files time:\s*([\d.]+)"),
    ("num_results",         r"Number of results:\s*(\d+)"),
    ("and_candidates",      r"AND candidates:\s*(\d+)"),
    ("opt1_skips",          r"Opt1 skips:\s*(\d+)"),
    ("seg_filter_skips",    r"Seg filter skips:\s*(\d+)"),
    ("seg_skips_and",       r"Seg filter skips AND:\s*(\d+)"),
    ("seg_skips_not",       r"Seg filter skips NOT:\s*(\d+)"),
    ("seg_skip_pct",        r"Seg filter skip pct:\s*([\d.]+)"),
    ("candidate_segments",  r"Candidate segments:\s*(-?\d+)"),
    ("excl_chunks_total",   r"Excluded chunks total:\s*(\d+)"),
    ("unique_chunks_fetched", r"Unique chunks fetched:\s*(\d+)"),
    ("and_not_eval_time",   r"AND\+NOT evaluation time:\s*([\d.]+)"),
    ("and_majflt",          r"AND major faults:\s*(\d+)"),
    ("not_majflt",          r"NOT major faults:\s*(\d+)"),
    ("not_killed",          r"NOT killed:\s*(\d+)"),
    ("excl_chunk_count",    r"Excluded chunk count:\s*(\d+)"),
    ("query_time",          r"^lookup time:\s*([\d.]+)"),
    ("indirection_time",    r"^indirection time:\s*([\d.]+)"),
    ("query_majflt",        r"^query major faults:\s*(\d+)"),
    ("query_minflt",        r"^query minor faults:\s*(\d+)"),
    ("query_read_bytes",    r"^query read bytes:\s*(\d+)"),
]


def parse_output(stdout: str) -> dict:
    result = {}
    int_keys = {"num_results", "and_candidates", "opt1_skips", "seg_filter_skips", "not_killed",
                "excl_chunk_count", "seg_skips_and", "seg_skips_not", "candidate_segments",
                "excl_chunks_total", "unique_chunks_fetched", "and_majflt", "not_majflt",
                "query_majflt", "query_minflt", "query_read_bytes"}
    for key, pat in _TIME_PATTERNS:
        m = re.search(pat, stdout, re.IGNORECASE | re.MULTILINE)
        if m:
            result[key] = int(m.group(1)) if key in int_keys else float(m.group(1))

    # Parse "Excluded chunks scanned: N / M"
    m = re.search(r"Excluded chunks scanned:\s*(\d+)\s*/\s*(\d+)", stdout, re.IGNORECASE)
    if m:
        result["excl_chunks_scanned"] = int(m.group(1))
        result["excl_chunks_total"]   = int(m.group(2))

    # Parse "Included term chunk counts (sorted): N1 N2 ..."
    m = re.search(r"Included term chunk counts \(sorted\):([\d ]+)", stdout, re.IGNORECASE)
    if m:
        result["term_chunk_counts"] = [int(x) for x in m.group(1).split()]

    # Compute total.
    # Dedup: searcher_load + doc_lookup + and_eval + not_eval + gather_results
    # Naive: searcher_load + doc_lookup + fetch_files
    sl  = result.get("searcher_load_time",  0.0)
    sb  = result.get("seg_build_time",      0.0)
    dl  = result.get("doc_lookup_time",     0.0)
    ae  = result.get("and_eval_time",       0.0)
    ne  = result.get("not_eval_time",       0.0)
    gr  = result.get("gather_results_time", 0.0)
    ane = result.get("and_not_eval_time",   0.0)   # combined AND+NOT phase (current impl)
    ff  = result.get("fetch_files_time",    0.0)
    dedup_phase = ae + ne + gr + ane
    if sl or dl or dedup_phase or ff:
        result["total_time"] = sl + sb + dl + (dedup_phase if dedup_phase else ff)

    return result


_RUN_FIELDS = {f.name for f in fields(RunResult)}


def _fill_parsed(r: RunResult, stdout: str):
    for key, value in parse_output(stdout).items():
        if key in _RUN_FIELDS:
            setattr(r, key, value)


def clear_cache():
    subprocess.run(CLEAR_CACHE_CMD, capture_output=True)


def flush_controller(path: str, gib: int, streams: int = 8, block: int = 4 << 20) -> float:
    """Read gib GiB of path with O_DIRECT, in parallel streams spread over it.

    drop_caches empties only the Linux page cache; the RAID controller (a PERC
    H755 has 8 GiB) still holds data a previous run read and serves rereads
    from it -- on 2 Nov the same cold query took 72 s straight after earlier
    runs and 146 s after reading 16 GiB of unrelated data.  Returns seconds.
    """
    size = os.path.getsize(path)
    per = (gib << 30) // streams
    stride = max(per, (size - per) // streams) // block * block

    def worker(k):
        fd = os.open(path, os.O_RDONLY | os.O_DIRECT)
        buf = mmap.mmap(-1, block)          # page-aligned, as O_DIRECT needs
        try:
            base = k * stride
            for off in range(0, per, block):
                os.preadv(fd, [buf], base + off)
        finally:
            os.close(fd)
            buf.close()

    t0 = time.perf_counter()
    threads = [threading.Thread(target=worker, args=(k,)) for k in range(streams)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return time.perf_counter() - t0


def diskstats(dev: str):
    """(completed reads, ms spent reading) of block device dev, or None."""
    if not dev:
        return None
    with open("/proc/diskstats") as f:
        for line in f:
            parts = line.split()
            if parts[2] == dev:
                return int(parts[3]), int(parts[6])
    raise SystemExit(f"no block device '{dev}' in /proc/diskstats")


def diskstats_all(devs: list) -> dict:
    return {d: diskstats(d) for d in devs}


def prewarm(paths: list):
    """Read every file under paths once so that hot runs start from the page cache."""
    files = []
    for p in paths:
        if os.path.isdir(p):
            for root, _, names in os.walk(p):
                files.extend(os.path.join(root, n) for n in names)
        elif os.path.isfile(p):
            files.append(p)
    total = sum(os.path.getsize(f) for f in files)
    print(f"  [prewarm] reading {len(files)} files, {total / 2**30:.1f} GiB", flush=True)
    t0 = time.perf_counter()
    for f in files:
        with open(f, "rb") as fh:
            while fh.read(1 << 24):
                pass
    print(f"  [prewarm] done in {time.perf_counter() - t0:.0f}s", flush=True)


def run_one(exp: Experiment, rep: int, do_clear: bool) -> RunResult:
    if do_clear:
        clear_cache()

    cmd = [DESTOR_BIN] + exp.cmd
    t0 = time.perf_counter()
    proc = subprocess.run(cmd, capture_output=True, text=True)
    wall = time.perf_counter() - t0

    r = RunResult(
        exp_name=exp.name,
        exp_desc=exp.desc,
        exp_group=exp.group,
        rep=rep,
        returncode=proc.returncode,
        wall_time=wall,
        query_plain=exp.query_plain,
        purpose=exp.purpose,
        error=proc.stderr.strip() if proc.returncode != 0 else "",
        raw_stdout=proc.stdout,
    )
    _fill_parsed(r, proc.stdout)
    return r


def server_request(exp: Experiment) -> str:
    """The `destor --serve` request line that runs exp's one-shot command."""
    cmd = exp.cmd
    if len(cmd) >= 2 and cmd[0] in ("-i", "-k"):
        return f"{cmd[0][1]} {cmd[1]}"
    if len(cmd) >= 2 and cmd[0] == "-j":
        fps, sparse = 1024, False
        rest = cmd[2:]
        for i, a in enumerate(rest):
            if a == "-P":
                fps = int(rest[i + 1])
            elif a == "-S":
                sparse = True
        return f"j {fps} {'s' if sparse else 'd'} {cmd[1]}"
    raise SystemExit(f"--server cannot run '{exp.name}' ({' '.join(cmd[:1])}): "
                     "only -i, -j and -k experiments")


def server_seg_spec(experiments: list) -> str:
    """The segment indexes (--serve-seg) the experiments' -j requests use."""
    spec = []
    for exp in experiments:
        req = server_request(exp).split()
        if req[0] == "j" and req[1] + req[2] not in spec:
            spec.append(req[1] + req[2])
    return ",".join(spec)


class QueryServer:
    """A `destor --serve` process: indexes opened once, queries over stdin."""

    END = "==== QUERY END ===="
    READY = "==== SERVER READY ===="

    def __init__(self, seg_spec: str, stderr_path: str):
        cmd = [DESTOR_BIN, "--serve"] + (["--serve-seg", seg_spec] if seg_spec else [])
        self.stderr = open(stderr_path, "a")
        t0 = time.perf_counter()
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=self.stderr, text=True, bufsize=1)
        self.startup_output, ok = self._read_until(self.READY)
        self.startup_wall = time.perf_counter() - t0
        if not ok:
            raise SystemExit(f"destor --serve exited during startup (rc={self.proc.wait()}); "
                             f"see {stderr_path}\n{self.startup_output}")

    def _read_until(self, sentinel: str):
        lines = []
        for line in self.proc.stdout:
            if line.startswith(sentinel):
                return "".join(lines), True
            lines.append(line)
        return "".join(lines), False

    def request(self, line: str):
        """Sends one request; returns (stdout, completed)."""
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()
        return self._read_until(self.END)

    def alive(self) -> bool:
        return self.proc.poll() is None

    def close(self):
        if self.alive():
            try:
                self.proc.stdin.write("quit\n")
                self.proc.stdin.flush()
            except BrokenPipeError:
                pass
        self.proc.wait()
        self.stderr.close()


@dataclass
class ServerRun:
    """How --server runs prepare each query."""
    server: QueryServer
    do_clear: bool                 # cold: drop the page cache first
    flush_file: str = ""           # cold: and flush the controller cache from this file
    flush_gib: int = 16
    disk_devs: tuple = ()          # record these devices' reads per query


def run_one_server(run: ServerRun, exp: Experiment, rep: int) -> RunResult:
    if run.do_clear:
        if run.flush_file:
            flush_controller(run.flush_file, run.flush_gib)
        clear_cache()
    started = datetime.datetime.now().isoformat(timespec="seconds")
    d0 = diskstats_all(run.disk_devs)
    t0 = time.perf_counter()
    stdout, completed = run.server.request(server_request(exp))
    wall = time.perf_counter() - t0
    d1 = diskstats_all(run.disk_devs)
    error = ""
    if not completed:
        error = f"destor --serve exited (rc={run.server.proc.wait()})"
    else:
        error = "\n".join(l for l in stdout.splitlines() if l.startswith("error:"))
    r = RunResult(
        exp_name=exp.name,
        exp_desc=exp.desc,
        exp_group=exp.group,
        rep=rep,
        returncode=0 if not error else 1,
        wall_time=wall,
        query_plain=exp.query_plain,
        purpose=exp.purpose,
        error=error,
        raw_stdout=stdout,
        started_at=started,
    )
    _fill_parsed(r, stdout)
    if run.disk_devs:
        r.disk_reads = {d: [d1[d][0] - d0[d][0], d1[d][1] - d0[d][1]] for d in run.disk_devs}
    return r


def run_suite(experiments: list, reps: int, do_clear: bool, verbose: bool = False,
              server_run: Optional[ServerRun] = None, rep_limits=None,
              done: Optional[set] = None, on_experiment=None) -> list:
    """Runs every experiment reps times.

    rep_limits = (two_reps_over, one_rep_over): an experiment whose first run
    takes longer than those seconds gets at most 2 or 1 reps.  Experiments
    named in done are skipped; on_experiment(runs) is called after each one.
    """
    results = []
    for exp in experiments:
        if done and exp.name in done:
            print(f"\n  {exp.name}: already done, skipping", flush=True)
            continue
        print(f"\n  {exp.name}", flush=True)
        if exp.query_plain:
            print(f"  query  : {exp.query_plain}", flush=True)
        if exp.purpose:
            print(f"  purpose: {exp.purpose}", flush=True)
        if exp.kw_files:
            for f in exp.kw_files:
                print(f"  file   : {f}", flush=True)
        runs = []
        target = reps
        rep = 0
        while rep < target:
            if server_run:
                r = run_one_server(server_run, exp, rep)
                t = r.query_time
            else:
                r = run_one(exp, rep, do_clear)
                t = r.total_time
            if rep == 0 and rep_limits:
                two_over, one_over = rep_limits
                if one_over is not None and r.wall_time > one_over:
                    target = 1
                elif two_over is not None and r.wall_time > two_over:
                    target = min(2, reps)
            lt = f"{t:.3f}s" if t is not None else "N/A"
            nr = str(r.num_results) if r.num_results is not None else "?"
            tag = " [ERROR]" if r.returncode != 0 else ""
            print(f"    rep {rep+1}/{target}: {lt}, {nr} results{tag}", flush=True)
            if verbose:
                print(r.raw_stdout, flush=True)
            if r.error:
                print(f"    stderr: {r.error[:120]}", flush=True)
            runs.append(r)
            rep += 1
            if server_run and not server_run.server.alive():
                break
        results.extend(runs)
        if on_experiment:
            on_experiment(runs)
        if server_run and not server_run.server.alive():
            print("  destor --serve has exited; stopping", flush=True)
            break
    return results

# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def _avg(vals):
    return sum(vals) / len(vals) if vals else None

def aggregate(results: list) -> dict:
    groups = defaultdict(list)
    for r in results:
        groups[r.exp_name].append(r)

    out = {}
    for name, reps in groups.items():
        times  = [r.total_time for r in reps if r.total_time is not None]
        counts = [r.num_results for r in reps if r.num_results is not None]

        def comp_avg(field):
            vals = [getattr(r, field) for r in reps if getattr(r, field) is not None]
            return _avg(vals)

        qtimes = [r.query_time for r in reps if r.query_time is not None]

        out[name] = {
            "group":              reps[0].exp_group,
            "desc":               reps[0].exp_desc,
            "query_plain":        reps[0].query_plain,
            "purpose":            reps[0].purpose,
            "n_reps":             len(reps),
            "avg":                _avg(times),
            "min":                min(times) if times else None,
            "max":                max(times) if times else None,
            "num_results":        counts[0] if counts else None,
            # Component averages for graphing
            "avg_searcher_load":    comp_avg("searcher_load_time"),
            "avg_seg_build":        comp_avg("seg_build_time"),
            "avg_seg_load":         comp_avg("seg_load_time"),
            "avg_dtor":             comp_avg("dtor_time"),
            "avg_doc_lookup":       comp_avg("doc_lookup_time"),
            "avg_and_eval":         comp_avg("and_eval_time"),
            "avg_not_eval":         comp_avg("not_eval_time"),
            "avg_and_not_eval":     comp_avg("and_not_eval_time"),
            "avg_gather_results":   comp_avg("gather_results_time"),
            "avg_fetch_files":      comp_avg("fetch_files_time"),
            # Segment-filter diagnostics (dedup+seg only)
            "avg_candidate_segments":  comp_avg("candidate_segments"),
            "avg_seg_skips_not":       comp_avg("seg_skips_not"),
            "avg_seg_skips_and":       comp_avg("seg_skips_and"),
            "avg_seg_skip_pct":        comp_avg("seg_skip_pct"),
            "avg_unique_chunks_fetched": comp_avg("unique_chunks_fetched"),
            "avg_and_majflt":          comp_avg("and_majflt"),
            "avg_not_majflt":          comp_avg("not_majflt"),
            # Optimization diagnostics (averages across reps; dedup only)
            "avg_and_candidates":      comp_avg("and_candidates"),
            "avg_opt1_skips":          comp_avg("opt1_skips"),
            "avg_seg_filter_skips":    comp_avg("seg_filter_skips"),
            "avg_not_killed":          comp_avg("not_killed"),
            "avg_excl_chunk_count":    comp_avg("excl_chunk_count"),
            "avg_excl_chunks_scanned": comp_avg("excl_chunks_scanned"),
            "avg_excl_chunks_total":   comp_avg("excl_chunks_total"),
            # First-rep term_chunk_counts for display (list, not averaged)
            "term_chunk_counts": next(
                (r.term_chunk_counts for r in reps if r.term_chunk_counts), None),
            # Query time (--server: excludes opening the indexes)
            "avg_query":            _avg(qtimes),
            "min_query":            min(qtimes) if qtimes else None,
            "max_query":            max(qtimes) if qtimes else None,
            "avg_indirection":      comp_avg("indirection_time"),
            "avg_query_other":      _avg([v for v in (_query_other(r) for r in reps) if v is not None]),
            "avg_query_majflt":     comp_avg("query_majflt"),
            "avg_query_read_bytes": comp_avg("query_read_bytes"),
            # device -> [avg reads, avg ms reading]
            "avg_disk_reads": {
                dev: [_avg([r.disk_reads[dev][0] for r in reps if r.disk_reads and dev in r.disk_reads]),
                      _avg([r.disk_reads[dev][1] for r in reps if r.disk_reads and dev in r.disk_reads])]
                for dev in sorted({d for r in reps if r.disk_reads for d in r.disk_reads})},
        }
    return out


def _query_other(r: RunResult) -> Optional[float]:
    """Query time not in any graphed phase: parsing the query, sorting the
    included terms, freeing the per-query state; for naive, also the result
    vector.  Clamped at 0 against the 1 ms rounding of each printed timer."""
    if r.query_time is None:
        return None
    if _is_naive(r.exp_name):
        parts = (r.doc_lookup_time, r.fetch_files_time)
    else:
        parts = (r.doc_lookup_time, r.and_eval_time, r.not_eval_time, r.indirection_time)
    return max(0.0, r.query_time - sum(p or 0.0 for p in parts))


def print_comparison_table(agg: dict, suite_name: str, query_time: bool = False):
    """query_time: tabulate the query time (--server) with its disk reads,
    instead of total_time."""
    groups = {}
    for name, s in agg.items():
        g = s["group"] or name
        groups.setdefault(g, []).append((name, s))

    avg_key, min_key = ("avg_query", "min_query") if query_time else ("avg", "min")
    devs = sorted({d for _, s in agg.items() for d in (s.get("avg_disk_reads") or {})})
    print(f"\n{'='*80}")
    print(f"  Suite: {suite_name}" + ("  (query time; indexes opened at server startup)"
                                    if query_time else ""))
    print(f"{'='*80}")
    extra = (f" {'Reps':>4} {'ReadMB':>8}" + "".join(f" {d + ' IOs':>10} {'ms/IO':>6}" for d in devs)
             if query_time else "")
    print(f"  {'Experiment':<44} {'Avg(s)':>7} {'Min(s)':>7} {'Results':>10}{extra}")
    print(f"  {'-'*(70 + len(extra))}")

    for group_name, entries in groups.items():
        for name, s in entries:
            prec = 4 if query_time else 3
            avg = f"{s[avg_key]:.{prec}f}" if s[avg_key] is not None else "N/A"
            mn  = f"{s[min_key]:.{prec}f}" if s[min_key] is not None else "N/A"
            nr  = str(s["num_results"]) if s["num_results"] is not None else "?"
            row = f"  {name[:44]:<44} {avg:>7} {mn:>7} {nr:>10}"
            if query_time:
                rb = s["avg_query_read_bytes"]
                row += f" {s['n_reps']:>4}"
                row += f" {rb / 2**20:>8.1f}" if rb is not None else f" {'—':>8}"
                for d in devs:
                    ios, ms = (s.get("avg_disk_reads") or {}).get(d, [None, None])
                    row += f" {ios:>10.0f}" if ios is not None else f" {'—':>10}"
                    row += f" {ms / ios:>6.2f}" if ios else f" {'—':>6}"
            print(row)

        counts = {s["num_results"] for _, s in entries if s["num_results"] is not None}
        if len(counts) > 1:
            print(f"  *** RESULT MISMATCH in '{group_name}': {counts}")
        print()


def print_speedup_table(agg: dict, baseline_impl: str, suite_name: str):
    groups = {}
    for name, s in agg.items():
        g = s["group"] or name
        groups.setdefault(g, []).append((name, s))

    print(f"\n{'='*80}")
    print(f"  Suite: {suite_name}  (speedup vs '{baseline_impl}')")
    print(f"{'='*80}")
    print(f"  {'Group':<36} {'Impl':<10} {'Avg(s)':>7} {'Speedup':>8}")
    print(f"  {'-'*66}")

    for group_name, entries in groups.items():
        baseline_time = next(
            (s["avg"] for name, s in entries if baseline_impl in name and s["avg"]),
            None
        )
        for name, s in entries:
            impl = name.split("[")[-1].rstrip("]") if "[" in name else name
            avg  = s["avg"]
            avg_s = f"{avg:.3f}" if avg is not None else "N/A"
            spd   = f"{baseline_time / avg:.2f}x" if avg and baseline_time else "-"
            print(f"  {group_name:<36} {impl:<10} {avg_s:>7} {spd:>8}")
        print()

def print_diagnostics_table(agg: dict, suite_name: str):
    """Print optimization diagnostic counters for dedup experiments."""
    dedup_entries = [(name, s) for name, s in agg.items() if not _is_naive(name)
                     and s.get("avg_and_candidates") is not None]
    if not dedup_entries:
        return

    print(f"\n{'='*100}")
    print(f"  Optimization diagnostics: {suite_name}")
    print(f"{'='*100}")
    hdr = (f"  {'Regime':<32} {'TermChunks(sorted)':>28} {'ExclChunks':>10} "
           f"{'ANDcands':>9} {'Opt1skip':>9} {'NOTkill':>8} "
           f"{'ExclScanned':>12} {'EarlyExit':>10}")
    print(hdr)
    print(f"  {'-'*96}")

    for name, s in dedup_entries:
        tcc   = s.get("term_chunk_counts") or []
        tcc_s = "[" + ", ".join(str(x) for x in tcc) + "]" if tcc else "—"
        excl  = s.get("avg_excl_chunk_count")
        cands = s.get("avg_and_candidates")
        skips = s.get("avg_opt1_skips")
        kill  = s.get("avg_not_killed")
        scan  = s.get("avg_excl_chunks_scanned")
        total = s.get("avg_excl_chunks_total")

        excl_s  = f"{excl:,.0f}"  if excl  is not None else "—"
        cands_s = f"{cands:,.0f}" if cands is not None else "—"
        skips_s = f"{skips:,.0f}" if skips is not None else "—"
        kill_s  = f"{kill:,.0f}"  if kill  is not None else "—"

        if scan is not None and total is not None and total > 0:
            scan_s  = f"{scan:,.0f}"
            early_s = f"{100.0*scan/total:.1f}%"
        else:
            scan_s  = "—"
            early_s = "—"

        regime = s["group"]
        print(f"  {regime:<32} {tcc_s:>28} {excl_s:>10} "
              f"{cands_s:>9} {skips_s:>9} {kill_s:>8} "
              f"{scan_s:>12} {early_s:>10}")
    print()


# ---------------------------------------------------------------------------
# Graphs
# ---------------------------------------------------------------------------

# Per-implementation component stacks (bottom → top).  Labels match the
# timings the current AND+NOT implementation actually emits:
#   searcher load → [seg filter load] → doc lookup (Lucene term search) →
#   AND+NOT evaluation (in-memory intersection + NOT, incl. file-list fetch) →
#   index close (destructors).
_DEDUP_COMPONENTS = [
    ("avg_searcher_load",  "#4C72B0", "Searcher Load"),
    ("avg_doc_lookup",     "#DD8452", "Doc Lookup (Lucene)"),
    ("avg_and_eval",       "#55A868", "AND Evaluation"),
    ("avg_not_eval",       "#8172B3", "NOT Evaluation"),
]
# dedup+seg adds the one-time bitmask load but otherwise uses the same phases
_DEDUP_SEG_COMPONENTS = [
    ("avg_searcher_load",  "#4C72B0", "Searcher Load"),
    ("avg_seg_load",       "#E09020", "Seg Filter Load"),
    ("avg_doc_lookup",     "#DD8452", "Doc Lookup (Lucene)"),
    ("avg_and_eval",       "#55A868", "AND Evaluation"),
    ("avg_not_eval",       "#8172B3", "NOT Evaluation"),
]
_NAIVE_COMPONENTS = [
    ("avg_searcher_load", "#4C72B0", "Searcher Load"),
    ("avg_doc_lookup",    "#DD8452", "Doc Lookup (Lucene)"),
    ("avg_fetch_files",   "#C44E52", "Fetch Files"),
]


# Query time only (--server): the indexes, the reverse mapping, the pathstore
# and the segment indexes were opened at server startup, so there is no
# searcher or segment-index load; the stack sums to the measured query time.
_DEDUP_QUERY_COMPONENTS = [
    ("avg_doc_lookup",     "#DD8452", "Doc Lookup (Lucene)"),
    ("avg_and_eval",       "#55A868", "AND Evaluation"),
    ("avg_not_eval",       "#8172B3", "NOT Evaluation"),
    ("avg_indirection",    "#937860", "Paths (pathstore)"),
    ("avg_query_other",    "#8C8C8C", "Other"),
]
_NAIVE_QUERY_COMPONENTS = [
    ("avg_doc_lookup",     "#DD8452", "Doc Lookup (Lucene)"),
    ("avg_fetch_files",    "#C44E52", "Fetch Files"),
    ("avg_query_other",    "#8C8C8C", "Other"),
]


def _is_naive(name: str) -> bool:
    return name.lower().startswith("naive")

def _is_dedup_seg(name: str) -> bool:
    return name.lower().startswith("dedup+")

def _bar_components(name: str, query_time: bool = False) -> list:
    if query_time:
        return _NAIVE_QUERY_COMPONENTS if _is_naive(name) else _DEDUP_QUERY_COMPONENTS
    if _is_naive(name):     return _NAIVE_COMPONENTS
    if _is_dedup_seg(name): return _DEDUP_SEG_COMPONENTS
    return _DEDUP_COMPONENTS


def _short_label(name: str) -> str:
    """Compact x-axis tick label derived from the experiment name."""
    if _is_naive(name):
        return "Naive"
    if _is_dedup_seg(name):
        m = re.match(r"dedup\+(\d+)seg(-sparse)?", name.lower())
        if m:
            return f"+{m.group(1)}/{'S' if m.group(2) else 'D'}"
        return "Dedup+Seg"
    if "[inc]" in name:
        return "Dedup (inc)"
    if "[bitmap]" in name:
        return "Dedup (bitmap)"
    if "dedup" in name.lower():
        return "Dedup"
    return name.split()[-1]


def generate_graphs(agg: dict, output_dir: str, suite_name: str, query_time: bool = False):
    """
    Emit one PDF (+ PNG) stacked-bar chart per experiment group.

    Bar segments (bottom → top):
        Dedup path: Searcher Load | Doc Lookup | AND+NOT Evaluation
        Naive path: Searcher Load | Doc Lookup | Fetch Files
    With query_time (--server runs) there is no load segment:
        Dedup path: Doc Lookup | AND | NOT | Paths | Other
        Naive path: Doc Lookup | Fetch Files | Other

    Axes, legend, query, and purpose are all annotated so the figure is
    self-contained for inclusion in a paper.
    """
    if not _MATPLOTLIB_AVAILABLE:
        print("  [graphs] matplotlib not available — skipping.")
        print("           Install with: pip install matplotlib numpy")
        return

    plt.rcParams.update({
        "font.family":      "serif",
        "font.size":        10,
        "axes.titlesize":   9,
        "axes.labelsize":   11,
        "legend.fontsize":  9,
        "xtick.labelsize":  11,
        "ytick.labelsize":  10,
        "figure.dpi":       150,
    })

    os.makedirs(output_dir, exist_ok=True)

    # Group aggregated entries by their comparison block.
    groups = defaultdict(list)
    for name, s in agg.items():
        g = s["group"] or name
        groups[g].append((name, s))

    generated = []
    for group_name, entries in groups.items():
        exp_names = [name for name, _ in entries]
        stats     = [s    for _, s   in entries]
        n = len(exp_names)

        fig_w = max(5.5, n * 2.0 + 2.5)
        fig, ax = plt.subplots(figsize=(fig_w, 5.0))

        x       = np.arange(n)
        bottoms = np.zeros(n)
        bar_w   = 0.55

        # Draw each bar individually so each only gets the components that
        # belong to its implementation type (dedup vs naive).
        seen_labels = set()
        legend_handles = []
        # min segment height (as fraction of y-range) below which we skip the label
        MIN_LABEL_FRAC = 0.05
        top = max((sum(s.get(k) or 0.0 for k, _, _ in _bar_components(nm, query_time))
                   for nm, s in zip(exp_names, stats)), default=0.0)
        # Hot query times are milliseconds; label them so.
        in_ms = query_time and top < 1.0
        fmt = (lambda v: f"{v * 1000:.2f}ms") if in_ms else (lambda v: f"{v:.3f}s")
        for i, (name, s) in enumerate(zip(exp_names, stats)):
            bottom = 0.0
            for comp_key, color, lbl in _bar_components(name, query_time):
                val = s.get(comp_key) or 0.0
                ax.bar(i, val, bar_w, bottom=bottom,
                       color=color, edgecolor="white", linewidth=0.6)
                # Label inside the segment (skip tiny segments)
                if val > 0 and (not query_time or val >= MIN_LABEL_FRAC * top):
                    ax.text(i, bottom + val / 2, fmt(val),
                            ha="center", va="center",
                            fontsize=7.5, color="white", fontweight="bold",
                            clip_on=True)
                bottom += val
                if lbl not in seen_labels:
                    legend_handles.append(
                        matplotlib.patches.Patch(facecolor=color, label=lbl))
                    seen_labels.add(lbl)
            bottoms[i] = bottom
            # Total time on top of bar (computed from visible segments, not stored field)
            if bottom > 0:
                ax.text(i, bottom, fmt(bottom),
                        ha="center", va="bottom",
                        fontsize=8.5, color="#222222", fontweight="bold")

        # Result counts printed to the right of the axes (not inside the plot)
        result_lines = []
        for name, s in zip(exp_names, stats):
            nr = s.get("num_results")
            if nr is not None:
                result_lines.append(f"{_short_label(name)}: {nr:,} results")
        if result_lines:
            fig.text(0.98, 0.5, "\n".join(result_lines),
                     ha="right", va="center", fontsize=8,
                     color="#555555", style="italic",
                     transform=fig.transFigure)

        # Axis formatting
        ax.set_xticks(x)
        ax.set_xticklabels([_short_label(n) for n in exp_names])
        ax.set_ylabel("Query time (seconds)" if query_time else "Time (seconds)", labelpad=6)
        ax.set_xlabel("")
        ax.set_xlim(-0.5, n - 0.5)
        ax.set_ylim(0, max(bottoms) * 1.25 if np.any(bottoms > 0) else 1.0)
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        ax.yaxis.grid(True, linestyle="--", alpha=0.4, zorder=0)
        ax.set_axisbelow(True)

        # Legend — placed below the axes so it never overlaps bars or annotations
        ax.legend(handles=legend_handles, loc="upper center",
                  bbox_to_anchor=(0.5, -0.08), ncol=len(legend_handles),
                  framealpha=0.9, edgecolor="#cccccc")

        # Title block: regime, query, purpose
        query_plain = next((s["query_plain"] for s in stats if s.get("query_plain")), "")
        purpose     = next((s["purpose"]     for s in stats if s.get("purpose")),     "")
        title_parts = [f"Regime: {group_name}" + (
            "   (query time; indexes opened once at server startup)" if query_time else "")]
        if query_plain:
            title_parts.append(f"Query: {query_plain}")
        if purpose:
            title_parts.append(f"Purpose: {purpose}")
        ax.set_title("\n".join(title_parts), loc="left", pad=8, linespacing=1.5)

        plt.tight_layout()
        plt.subplots_adjust(bottom=0.18)

        safe = re.sub(r"[^\w\-]", "_", f"{suite_name}_{group_name}")
        pdf_path = os.path.join(output_dir, f"{safe}.pdf")
        png_path = os.path.join(output_dir, f"{safe}.png")
        fig.savefig(pdf_path, bbox_inches="tight")
        fig.savefig(png_path, bbox_inches="tight", dpi=150)
        plt.close(fig)
        generated.append(pdf_path)

    for p in generated:
        print(f"  graph → {p}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="IDEA experiment runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "suites", nargs="*", default=["all"],
        choices=list(SUITES.keys()) + ["all"],
        help="Which suite(s) to run",
    )
    parser.add_argument("--reps", type=int, default=3,
                        help="Repetitions per experiment (default: 3)")
    parser.add_argument("--no-cache-clear", action="store_true",
                        help="Skip drop_caches between runs (warm-cache results)")
    parser.add_argument("--save", metavar="FILE",
                        help="Save raw results as JSON")
    parser.add_argument("--filter", metavar="PATTERN",
                        help="Only run experiments whose group name contains PATTERN "
                             "(case-insensitive). E.g. --filter R3")
    parser.add_argument("--verbose", action="store_true",
                        help="Print the full destor stdout after each run")
    parser.add_argument("--graph-dir", metavar="DIR", default=None,
                        help="Directory to write stacked-bar PDF/PNG graphs "
                             "(one file per experiment group). Created if absent.")
    parser.add_argument("--dataset", metavar="NAME", default=None,
                        choices=["linux", "wiki"],
                        help="Named dataset shorthand: linux → keywords/linux, "
                             "wiki → keywords/wiki (default: linux)")
    parser.add_argument("--kw-base", metavar="DIR", default=None,
                        help="Keyword base directory override, e.g. keywords/wiki. "
                             "Takes precedence over --dataset.")
    parser.add_argument("--kw-format", choices=["new", "old"], default="new",
                        help="Keyword file layout: 'new' (test/ subdirs, default) or "
                             "'old' (legacy {count}/file-{level}.txt from 100MB era)")
    parser.add_argument("--destor", metavar="BIN", default=DESTOR_BIN,
                        help="destor binary to run (default: destor on PATH)")
    parser.add_argument("--server", action="store_true",
                        help="Run every query in one `destor --serve` process that opened "
                             "the indexes at startup; report query time only")
    parser.add_argument("--flush-file", metavar="FILE", default="",
                        help="Cold --server runs: before each query also read --flush-gib GiB "
                             "of FILE (unrelated to the indexes) with O_DIRECT, to empty the "
                             "RAID controller cache")
    parser.add_argument("--flush-gib", type=int, default=16,
                        help="GiB read by --flush-file (default 16, twice the PERC H755 cache)")
    parser.add_argument("--prewarm", metavar="PATH", nargs="+", default=[],
                        help="Hot runs: read these files/directories into the page cache first")
    parser.add_argument("--diskstats-dev", metavar="DEV", nargs="+", default=[],
                        help="Record each --server query's reads on these block devices, e.g. sdb sda")
    parser.add_argument("--two-reps-over", type=float, default=None, metavar="S",
                        help="An experiment whose first run takes longer than S seconds "
                             "gets at most 2 reps")
    parser.add_argument("--one-rep-over", type=float, default=None, metavar="S",
                        help="... longer than S seconds gets 1 rep")
    parser.add_argument("--resume", action="store_true",
                        help="Keep the runs already in --save and skip their experiments")
    args = parser.parse_args()
    do_clear = not args.no_cache_clear
    globals()["DESTOR_BIN"] = args.destor

    _DATASET_PATHS = {"linux": "keywords/linux", "wiki": "keywords/wiki"}
    if args.kw_base:
        kw_base = args.kw_base
    elif args.dataset:
        kw_base = _DATASET_PATHS[args.dataset]
    else:
        kw_base = "keywords/linux"

    use_old = (args.kw_format == "old")
    if use_old:
        check_old_kw_files(kw_base)
    else:
        check_kw_files(kw_base)

    suite_names = list(_SUITE_FNS.keys()) if "all" in args.suites else args.suites

    # --save is rewritten after every experiment, so a long run that stops
    # keeps what it measured; --resume continues it.
    all_results = []
    if args.resume and args.save and os.path.exists(args.save):
        with open(args.save) as f:
            all_results = [RunResult(**{k: v for k, v in d.items() if k in _RUN_FIELDS})
                           for d in json.load(f)]
        print(f"Resuming: {len(all_results)} runs already in {args.save}")
    resumed = list(all_results)

    def save_all():
        if args.save:
            tmp = args.save + ".tmp"
            with open(tmp, "w") as f:
                json.dump([asdict(r) for r in all_results], f, indent=2)
            os.replace(tmp, args.save)

    rep_limits = None
    if args.two_reps_over is not None or args.one_rep_over is not None:
        rep_limits = (args.two_reps_over, args.one_rep_over)

    suites = []
    for suite_name in suite_names:
        fn_new, fn_old = _SUITE_FNS[suite_name]
        experiments = (fn_old if use_old else fn_new)(kw_base)
        if args.filter:
            pat = args.filter.lower()
            experiments = [e for e in experiments if pat in e.group.lower()]
        suites.append((suite_name, experiments))

    server_run = None
    if args.server:
        everything = [e for _, exps in suites for e in exps]
        for e in everything:
            server_request(e)          # refuses experiments the server cannot run
        if not do_clear and args.prewarm:
            prewarm(args.prewarm)
        seg_spec = server_seg_spec(everything)
        stderr_path = (os.path.splitext(args.save)[0] if args.save else "destor") + "_server.stderr"
        print(f"\n[server] starting {DESTOR_BIN} --serve --serve-seg {seg_spec or '(none)'}", flush=True)
        server = QueryServer(seg_spec, stderr_path)
        print(server.startup_output.rstrip(), flush=True)
        print(f"[server] ready after {server.startup_wall:.1f}s", flush=True)
        # Run each kind of request once, unrecorded, so that measured queries
        # do not pay for first use: code pages of the binary and libraries
        # (drop_caches spares only the pages already mapped), and per-thread
        # Lucene state.  Use the cheapest regime's query when there is one.
        warm = {}
        for e in everything:
            kind = " ".join(server_request(e).split()[:3 if e.cmd[0] == "-j" else 1])
            if kind not in warm or e.group.startswith("B1"):
                warm[kind] = e
        for kind, e in warm.items():
            t0 = time.perf_counter()
            _, ok = server.request(server_request(e))
            print(f"[server] warm-up '{kind}' ({e.group}): {time.perf_counter() - t0:.1f}s", flush=True)
            if not ok:
                raise SystemExit(f"destor --serve exited during warm-up; see {stderr_path}")
        server_run = ServerRun(server=server, do_clear=do_clear, flush_file=args.flush_file,
                               flush_gib=args.flush_gib, disk_devs=tuple(args.diskstats_dev))
        if args.save:
            meta = {
                "started_at": datetime.datetime.now().isoformat(timespec="seconds"),
                "mode": "cold" if do_clear else "hot",
                "cache_policy": (("drop_caches=1" + (f" + {args.flush_gib} GiB O_DIRECT read of "
                                                     f"{args.flush_file}" if args.flush_file else "")
                                  + " before every query") if do_clear else
                                 "no drops" + (f"; prewarmed {args.prewarm}" if args.prewarm else "")),
                "binary": os.path.realpath(DESTOR_BIN) if os.path.exists(DESTOR_BIN) else DESTOR_BIN,
                "git_head": subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                                           text=True).stdout.strip(),
                "serve_seg": seg_spec,
                "destor_config": (open("destor.config").read()
                                  if os.path.exists("destor.config") else None),
                "server_startup_wall_s": server.startup_wall,
                "server_startup_output": server.startup_output,
                "reps": args.reps, "rep_limits": rep_limits,
            }
            with open(os.path.splitext(args.save)[0] + "_meta.json", "w") as f:
                json.dump(meta, f, indent=1)

    for suite_name, experiments in suites:
        if not experiments:
            print(f"[{suite_name}] No experiments to run.")
            continue

        names = {e.name for e in experiments}
        earlier = [r for r in resumed if r.exp_name in names]
        print(f"\n[{suite_name}]  "
              f"({len(experiments)} experiments × {args.reps} reps, cache_clear={do_clear}"
              f"{', server' if server_run else ''})")

        def on_experiment(runs):
            all_results.extend(runs)
            save_all()

        results = earlier + run_suite(
            experiments, args.reps, do_clear, verbose=args.verbose,
            server_run=server_run, rep_limits=rep_limits,
            done={r.exp_name for r in earlier}, on_experiment=on_experiment)

        agg = aggregate(results)
        print_comparison_table(agg, suite_name, query_time=args.server)

        if suite_name in ("not_baseline", "not_regimes"):
            print_diagnostics_table(agg, suite_name)

        if args.graph_dir:
            print(f"\n  Generating graphs → {args.graph_dir}/")
            generate_graphs(agg, args.graph_dir, suite_name, query_time=args.server)

        if suite_name == "and_impl":
            print_speedup_table(agg, baseline_impl="inc", suite_name=suite_name)

        if suite_name == "not_baseline":
            groups = defaultdict(list)
            for name, s in agg.items():
                groups[s["group"]].append(s["num_results"])
            mismatches = [
                f"  {g}: {set(c for c in cs if c is not None)}"
                for g, cs in groups.items()
                if len({c for c in cs if c is not None}) > 1
            ]
            if mismatches:
                print("\n*** CORRECTNESS MISMATCHES ***")
                for m in mismatches:
                    print(m)
            else:
                print("\n  All result counts match between naive and dedup. ✓")

    if server_run:
        server_run.server.close()
    if args.save:
        save_all()
        print(f"\nResults saved to {args.save}")


if __name__ == "__main__":
    main()
