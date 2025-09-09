#!/bin/bash
cd LucenePlusPlus/build
make -j16
cd ../../
sudo cp LucenePlusPlus/build/src/contrib/liblucene++*.so* /usr/local/lib/ && \
sudo cp LucenePlusPlus/build/src/core/liblucene++*.so* /usr/local/lib/ && \
rm -rf build && mkdir build && \
bash -c "$(python3 create_build_command.py)" && \
sudo cp build/destor /usr/local/bin/destor