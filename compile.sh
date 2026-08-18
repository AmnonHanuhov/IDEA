#!/bin/bash
set -e
# The submodule is pinned to unmodified public upstream; our lucene++ changes
# live in patches/ and are applied here (no-op if already applied).
bash scripts/apply_lucene_patch.sh
mkdir -p LucenePlusPlus/build
cd LucenePlusPlus/build
[ -f Makefile ] || cmake ..
make -j16
cd ../../
sudo cp LucenePlusPlus/build/src/contrib/liblucene++*.so* /usr/local/lib/ && \
sudo cp LucenePlusPlus/build/src/core/liblucene++*.so* /usr/local/lib/ && \
rm -rf build && mkdir build && \
bash -c "$(python3 create_build_command.py)" && \
sudo cp build/destor /usr/local/bin/destor