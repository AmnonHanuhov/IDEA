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

Examples:
    python3 scripts/run_experiments.py and_impl --reps 3
    python3 scripts/run_experiments.py not_regimes --no-cache-clear
    python3 scripts/run_experiments.py all --save results.json
    python3 scripts/run_experiments.py all --dataset wiki --kw-format old --graph-dir graphs/wiki_100mb --save results_wiki_100mb.json
"""

import subprocess
import re
import sys
import time
import os
import json
import argparse
from dataclasses import dataclass, asdict
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

def _not_seg_experiment(regime_id: str, query: str, purpose: str, kw_files: list) -> Experiment:
    return Experiment(
        name=f"dedup+seg {regime_id}",
        cmd=["-j", query],
        desc=f"[dedup+seg] {purpose}",
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
    ("not_killed",          r"NOT killed:\s*(\d+)"),
    ("excl_chunk_count",    r"Excluded chunk count:\s*(\d+)"),
]


def parse_output(stdout: str) -> dict:
    result = {}
    int_keys = {"num_results", "and_candidates", "opt1_skips", "seg_filter_skips", "not_killed",
                "excl_chunk_count", "seg_skips_and", "seg_skips_not", "candidate_segments",
                "excl_chunks_total", "unique_chunks_fetched"}
    for key, pat in _TIME_PATTERNS:
        m = re.search(pat, stdout, re.IGNORECASE)
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


def clear_cache():
    subprocess.run(CLEAR_CACHE_CMD, capture_output=True)


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
    parsed = parse_output(proc.stdout)
    r.searcher_load_time = parsed.get("searcher_load_time")
    r.doc_lookup_time    = parsed.get("doc_lookup_time")
    r.and_eval_time       = parsed.get("and_eval_time")
    r.not_eval_time       = parsed.get("not_eval_time")
    r.gather_results_time = parsed.get("gather_results_time")
    r.fetch_files_time    = parsed.get("fetch_files_time")
    r.total_time          = parsed.get("total_time")
    r.num_results         = parsed.get("num_results")
    r.term_chunk_counts   = parsed.get("term_chunk_counts")
    r.excl_chunk_count    = parsed.get("excl_chunk_count")
    r.and_candidates      = parsed.get("and_candidates")
    r.opt1_skips          = parsed.get("opt1_skips")
    r.not_killed          = parsed.get("not_killed")
    r.excl_chunks_scanned = parsed.get("excl_chunks_scanned")
    r.excl_chunks_total   = parsed.get("excl_chunks_total")
    r.seg_filter_skips    = parsed.get("seg_filter_skips")
    r.seg_skips_and       = parsed.get("seg_skips_and")
    r.seg_skips_not       = parsed.get("seg_skips_not")
    r.seg_skip_pct        = parsed.get("seg_skip_pct")
    r.candidate_segments  = parsed.get("candidate_segments")
    r.unique_chunks_fetched = parsed.get("unique_chunks_fetched")
    r.and_not_eval_time   = parsed.get("and_not_eval_time")
    r.seg_load_time       = parsed.get("seg_load_time")
    r.dtor_time           = parsed.get("dtor_time")
    return r


def run_suite(experiments: list, reps: int, do_clear: bool, verbose: bool = False) -> list:
    results = []
    for exp in experiments:
        print(f"\n  {exp.name}", flush=True)
        if exp.query_plain:
            print(f"  query  : {exp.query_plain}", flush=True)
        if exp.purpose:
            print(f"  purpose: {exp.purpose}", flush=True)
        if exp.kw_files:
            for f in exp.kw_files:
                print(f"  file   : {f}", flush=True)
        for rep in range(reps):
            r = run_one(exp, rep, do_clear)
            lt = f"{r.total_time:.3f}s" if r.total_time is not None else "N/A"
            nr = str(r.num_results) if r.num_results is not None else "?"
            tag = " [ERROR]" if r.returncode != 0 else ""
            print(f"    rep {rep+1}/{reps}: {lt}, {nr} results{tag}", flush=True)
            if verbose:
                print(r.raw_stdout, flush=True)
            if r.error:
                print(f"    stderr: {r.error[:120]}", flush=True)
            results.append(r)
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
        }
    return out


def print_comparison_table(agg: dict, suite_name: str):
    groups = {}
    for name, s in agg.items():
        g = s["group"] or name
        groups.setdefault(g, []).append((name, s))

    print(f"\n{'='*80}")
    print(f"  Suite: {suite_name}")
    print(f"{'='*80}")
    print(f"  {'Experiment':<44} {'Avg(s)':>7} {'Min(s)':>7} {'Results':>10}")
    print(f"  {'-'*70}")

    for group_name, entries in groups.items():
        for name, s in entries:
            avg = f"{s['avg']:.3f}" if s["avg"] is not None else "N/A"
            mn  = f"{s['min']:.3f}" if s["min"] is not None else "N/A"
            nr  = str(s["num_results"]) if s["num_results"] is not None else "?"
            print(f"  {name[:44]:<44} {avg:>7} {mn:>7} {nr:>10}")

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


def _is_naive(name: str) -> bool:
    return name.lower().startswith("naive")

def _is_dedup_seg(name: str) -> bool:
    return name.lower().startswith("dedup+seg")

def _bar_components(name: str) -> list:
    if _is_naive(name):     return _NAIVE_COMPONENTS
    if _is_dedup_seg(name): return _DEDUP_SEG_COMPONENTS
    return _DEDUP_COMPONENTS


def _short_label(name: str) -> str:
    """Compact x-axis tick label derived from the experiment name."""
    if _is_naive(name):
        return "Naive"
    if _is_dedup_seg(name):
        return "Dedup+Seg"
    if "[inc]" in name:
        return "Dedup (inc)"
    if "[bitmap]" in name:
        return "Dedup (bitmap)"
    if "dedup" in name.lower():
        return "Dedup"
    return name.split()[-1]


def generate_graphs(agg: dict, output_dir: str, suite_name: str):
    """
    Emit one PDF (+ PNG) stacked-bar chart per experiment group.

    Bar segments (bottom → top):
        Dedup path: Searcher Load | Doc Lookup | AND+NOT Evaluation
        Naive path: Searcher Load | Doc Lookup | Fetch Files

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
        for i, (name, s) in enumerate(zip(exp_names, stats)):
            bottom = 0.0
            for comp_key, color, lbl in _bar_components(name):
                val = s.get(comp_key) or 0.0
                ax.bar(i, val, bar_w, bottom=bottom,
                       color=color, edgecolor="white", linewidth=0.6)
                # Label inside the segment (skip tiny segments)
                if val > 0:
                    ax.text(i, bottom + val / 2, f"{val:.3f}s",
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
                ax.text(i, bottom, f"{bottom:.3f}s",
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
        ax.set_ylabel("Time (seconds)", labelpad=6)
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
        title_parts = [f"Regime: {group_name}"]
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
    args = parser.parse_args()
    do_clear = not args.no_cache_clear

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

    all_results = []
    for suite_name in suite_names:
        fn_new, fn_old = _SUITE_FNS[suite_name]
        experiments = (fn_old if use_old else fn_new)(kw_base)
        if args.filter:
            pat = args.filter.lower()
            experiments = [e for e in experiments if pat in e.group.lower()]
        if not experiments:
            print(f"[{suite_name}] No experiments to run.")
            continue

        print(f"\n[{suite_name}]  "
              f"({len(experiments)} experiments × {args.reps} reps, cache_clear={do_clear})")
        results = run_suite(experiments, args.reps, do_clear, verbose=args.verbose)
        all_results.extend(results)

        agg = aggregate(results)
        print_comparison_table(agg, suite_name)

        if suite_name in ("not_baseline", "not_regimes"):
            print_diagnostics_table(agg, suite_name)

        if args.graph_dir:
            print(f"\n  Generating graphs → {args.graph_dir}/")
            generate_graphs(agg, args.graph_dir, suite_name)

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

    if args.save:
        with open(args.save, "w") as f:
            json.dump([asdict(r) for r in all_results], f, indent=2)
        print(f"\nResults saved to {args.save}")


if __name__ == "__main__":
    main()
