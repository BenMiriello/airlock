#!/bin/bash
# Build and install HAMi-core's libvgpu.so — the libcuda interposer airlock
# uses to enforce hard per-process VRAM caps via LD_PRELOAD.
#
# Why: NVIDIA's proprietary driver exposes no per-process VRAM limit on
# consumer GPUs. cgroups can't enforce. NVML polling is too slow to catch
# fast allocations. libcuda interception is the only mechanism that works.
#
# What it does:
#   1. Clones HAMi-core (Apache-2.0, github.com/Project-HAMi/HAMi-core)
#   2. Builds libvgpu.so against your system's CUDA stub libraries
#   3. Installs to /usr/local/lib/airlock/libvgpu.so
#
# Requires: cmake, gcc, CUDA toolkit (or just libcuda stubs).
#
# Usage:
#   ./install-hami.sh                  # build + install to /usr/local/lib/airlock/
#   PREFIX=$HOME/.local ./install-hami.sh
#   sudo ./install-hami.sh             # if installing system-wide
#
# After install, airlock's `airlock run` / `airlock start` will detect the
# library and use it automatically by setting LD_PRELOAD + CUDA_DEVICE_MEMORY_LIMIT.

set -euo pipefail

PREFIX="${PREFIX:-/usr/local}"
DEST_DIR="$PREFIX/lib/airlock"
SRC_DIR="${SRC_DIR:-/tmp/HAMi-core-build}"
HAMI_REPO="${HAMI_REPO:-https://github.com/Project-HAMi/HAMi-core.git}"
HAMI_REF="${HAMI_REF:-main}"

log() { echo "[install-hami] $*"; }

log "checking prerequisites…"
for cmd in cmake gcc git make; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        echo "error: $cmd not installed. apt-get install $cmd" >&2
        exit 1
    fi
done

if [[ ! -e /usr/lib/x86_64-linux-gnu/libcuda.so ]] && \
   [[ ! -e /usr/lib/libcuda.so ]] && \
   [[ ! -e /usr/lib/x86_64-linux-gnu/libcuda.so.1 ]]; then
    echo "warning: libcuda.so not found in standard paths. Build may fail." >&2
    echo "         Install NVIDIA driver or CUDA toolkit first." >&2
fi

log "cloning HAMi-core to $SRC_DIR…"
rm -rf "$SRC_DIR"
git clone --depth 1 --branch "$HAMI_REF" "$HAMI_REPO" "$SRC_DIR"

log "building…"
cd "$SRC_DIR"
make build

if [[ ! -f build/libvgpu.so ]]; then
    echo "error: build did not produce build/libvgpu.so" >&2
    exit 1
fi

log "installing to $DEST_DIR…"
mkdir -p "$DEST_DIR"
cp build/libvgpu.so "$DEST_DIR/libvgpu.so"
chmod 0644 "$DEST_DIR/libvgpu.so"

log "done."
log "library: $DEST_DIR/libvgpu.so"
log ""
log "to enable: airlock will auto-detect this path. or set explicitly:"
log "  export AIRLOCK_LIBVGPU=$DEST_DIR/libvgpu.so"
log ""
log "to test:"
log "  LD_PRELOAD=$DEST_DIR/libvgpu.so CUDA_DEVICE_MEMORY_LIMIT=1g \\"
log "    python3 -c 'import torch; print(torch.cuda.mem_get_info())'"
