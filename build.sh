#!/bin/bash
set -e

echo "=== Starting Automated Ranch OS Build ==="

WORKSPACE="/workspace"
BUILDROOT_DIR="$WORKSPACE/buildroot"
RANCH_OS_DIR="$WORKSPACE/ranch_os"
RELEASE_DIR="$WORKSPACE/releases"

OUT_GATE="/tmp/output_gate"
OUT_BASE="/tmp/output_base"

echo "Cleaning up local container scratchpads..."
rm -rf "$OUT_GATE" "$OUT_BASE"
mkdir -p "$RELEASE_DIR" "$OUT_GATE" "$OUT_BASE"

export gl_cv_func_fcntl_f_dupfd_works=yes
export gl_cv_func_fcntl_f_dupfd_cloexec=yes

# Export BR2_EXTERNAL so all make and merge commands see your custom packages
export BR2_EXTERNAL="$RANCH_OS_DIR"

# Utilize all available CPU cores
CORES=$(nproc)

cd "$BUILDROOT_DIR"

echo "=== [1/2] Building Gate Client (Pi Zero W) ==="
# 1. Load the official Pi Zero W hardware config
make O="$OUT_GATE" raspberrypi0w_defconfig
# 2. Merge your custom software fragment on top of it
./support/kconfig/merge_config.sh -O "$OUT_GATE" "$OUT_GATE/.config" "$RANCH_OS_DIR/configs/gate.fragment"
# 3. Resolve dependencies and lock the final configuration
make O="$OUT_GATE" olddefconfig

cd "$OUT_GATE"
make -j"$CORES"
echo "-> Saving Gate Client Image..."
cp images/sdcard.img "$RELEASE_DIR/gate_client_pi0w.img"


echo "=== [2/2] Building Base Station (Pi 3B+) ==="
cd "$BUILDROOT_DIR"
# 1. Load the official Pi 3 64-bit hardware config
make O="$OUT_BASE" raspberrypi3_64_defconfig
# 2. Merge your custom software fragment on top of it
./support/kconfig/merge_config.sh -O "$OUT_BASE" "$OUT_BASE/.config" "$RANCH_OS_DIR/configs/base.fragment"
# 3. Resolve dependencies and lock the final configuration
make O="$OUT_BASE" olddefconfig

cd "$OUT_BASE"
make -j"$CORES"
echo "-> Saving Base Station Image..."
cp images/sdcard.img "$RELEASE_DIR/base_station_pi3.img"

echo "=== Build Complete! ==="