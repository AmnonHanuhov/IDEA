#!/bin/bash
# Apply the project's lucene++ modifications to the pinned submodule checkout.
#
# The submodule tracks an unmodified public upstream commit; every change this
# project needs (the TrackingTerm* instrumentation the dedup lookup paths rely
# on, plus a boost-compat fix) lives in patches/lucene++-tracking-scorers.patch
# so it is reviewable here and reproducible from public sources.
#
# Idempotent: re-running is a no-op once the patch is in the working tree.
set -e

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SUBMODULE="$ROOT/LucenePlusPlus"
PATCH="$ROOT/patches/lucene++-tracking-scorers.patch"

if [ ! -d "$SUBMODULE/src/core" ]; then
	echo "LucenePlusPlus is empty -- run: git submodule update --init" >&2
	exit 1
fi

cd "$SUBMODULE"

if git apply --reverse --check "$PATCH" 2>/dev/null; then
	echo "lucene++ patch already applied."
	exit 0
fi

if ! git apply --check "$PATCH" 2>/dev/null; then
	echo "lucene++ patch does not apply cleanly to $(git rev-parse --short HEAD)." >&2
	echo "The submodule is probably not at the pinned commit; try:" >&2
	echo "    git submodule update --init --force" >&2
	exit 1
fi

git apply "$PATCH"
echo "lucene++ patch applied."
