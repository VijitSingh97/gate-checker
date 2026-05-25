#!/bin/bash
set -e

WORKSPACE="/workspace"
BUILDROOT_DIR="$WORKSPACE/buildroot"
RANCH_OS_DIR="$WORKSPACE/ranch_os"
RELEASE_DIR="$WORKSPACE/releases"

OUT_GATE="/tmp/output_gate"
OUT_BASE="/tmp/output_base"

# Build profile selection. Production is the default; development adds
# dropbear SSH + a known root password + debug tools and tags the image
# filenames with `_dev` so they're never mistaken for shippable artifacts.
PROFILE="${RANCH_BUILD_PROFILE:-production}"
case "$PROFILE" in
    production|development) ;;
    *)
        echo "Unknown RANCH_BUILD_PROFILE: $PROFILE" >&2
        echo "Expected 'production' or 'development'." >&2
        exit 1
        ;;
esac

SUFFIX=""
if [ "$PROFILE" = "development" ]; then
    SUFFIX="_dev"
fi

# Target selection — space-separated subset of "gate base". Default builds
# both. Set RANCH_BUILD_TARGETS=base when iterating on base-station-only
# changes to skip the ~12 minutes of gate-client rebuild work.
TARGETS="${RANCH_BUILD_TARGETS:-gate base}"
BUILD_GATE=0
BUILD_BASE=0
for t in $TARGETS; do
    case "$t" in
        gate) BUILD_GATE=1 ;;
        base) BUILD_BASE=1 ;;
        *)
            echo "Unknown RANCH_BUILD_TARGETS entry: $t (expected 'gate' or 'base')" >&2
            exit 1
            ;;
    esac
done
if [ "$BUILD_GATE" -eq 0 ] && [ "$BUILD_BASE" -eq 0 ]; then
    echo "RANCH_BUILD_TARGETS must include at least one of 'gate' or 'base'" >&2
    exit 1
fi

# Compact h/m/s formatter for timing summaries.
format_duration() {
    local s=$1
    if (( s < 60 )); then
        printf "%ds" "$s"
    elif (( s < 3600 )); then
        printf "%dm%02ds" $(( s / 60 )) $(( s % 60 ))
    else
        printf "%dh%02dm%02ds" $(( s / 3600 )) $(( (s % 3600) / 60 )) $(( s % 60 ))
    fi
}

BUILD_START=$SECONDS

echo "=== Starting Automated Ranch OS Build (profile=$PROFILE) ==="
if [ "$PROFILE" = "development" ]; then
    echo "  !! Development overlay: SSH enabled, default root password."
    echo "     Output images get a '_dev' suffix. Do not ship to customers."
fi

GATE_FRAGMENTS=("$RANCH_OS_DIR/configs/gate.fragment")
BASE_FRAGMENTS=("$RANCH_OS_DIR/configs/base.fragment")
if [ "$PROFILE" = "development" ]; then
    GATE_FRAGMENTS+=("$RANCH_OS_DIR/configs/dev.fragment")
    # Gate-only dev bits (SSH over USB ethernet) — keeps systemd-networkd
    # off the base, where NetworkManager already owns every interface.
    GATE_FRAGMENTS+=("$RANCH_OS_DIR/configs/dev-gate.fragment")
    BASE_FRAGMENTS+=("$RANCH_OS_DIR/configs/dev.fragment")
fi

# $OUT_GATE / $OUT_BASE are bind-mounted from the host (see
# run_build.sh and scripts/remote_build_inner.sh), so build state
# persists across container runs and rebuilds are incremental.
# A `rm -rf` of these paths fails inside the container because you
# can't remove a bind-mount point from within. Wipe host-side
# (rm -rf output_base output_gate) for a clean rebuild — see
# docs/BUILDING.md "Cleaning out the persistent output dirs".
mkdir -p "$RELEASE_DIR" "$OUT_GATE" "$OUT_BASE"

export gl_cv_func_fcntl_f_dupfd_works=yes
export gl_cv_func_fcntl_f_dupfd_cloexec=yes

# Export BR2_EXTERNAL so all make and merge commands see your custom packages
export BR2_EXTERNAL="$RANCH_OS_DIR"

# Utilize all available CPU cores
CORES=$(nproc)

cd "$BUILDROOT_DIR"

GATE_SECS=0
BASE_SECS=0

if [ "$BUILD_GATE" -eq 1 ]; then
    echo "=== Building Gate Client (Pi Zero W) ==="
    GATE_START=$SECONDS
    make O="$OUT_GATE" raspberrypi0w_defconfig
    ./support/kconfig/merge_config.sh -O "$OUT_GATE" "$OUT_GATE/.config" "${GATE_FRAGMENTS[@]}"
    make O="$OUT_GATE" olddefconfig

    cd "$OUT_GATE"
    make -j"$CORES"
    echo "-> Saving Gate Client image to gate_client_pi0w${SUFFIX}.img"
    cp images/sdcard.img "$RELEASE_DIR/gate_client_pi0w${SUFFIX}.img"
    GATE_SECS=$(( SECONDS - GATE_START ))
    echo "-> Gate build took $(format_duration $GATE_SECS)"
    cd "$BUILDROOT_DIR"
fi

if [ "$BUILD_BASE" -eq 1 ]; then
    echo "=== Building Base Station (Pi 3B+) ==="
    BASE_START=$SECONDS
    make O="$OUT_BASE" raspberrypi3_64_defconfig
    ./support/kconfig/merge_config.sh -O "$OUT_BASE" "$OUT_BASE/.config" "${BASE_FRAGMENTS[@]}"
    make O="$OUT_BASE" olddefconfig

    cd "$OUT_BASE"
    make -j"$CORES"
    echo "-> Saving Base Station image to base_station_pi3${SUFFIX}.img"
    cp images/sdcard.img "$RELEASE_DIR/base_station_pi3${SUFFIX}.img"
    BASE_SECS=$(( SECONDS - BASE_START ))
    echo "-> Base build took $(format_duration $BASE_SECS)"
fi

TOTAL_SECS=$(( SECONDS - BUILD_START ))
echo "=== Build Complete! (targets: $TARGETS) ==="
[ "$BUILD_GATE" -eq 1 ] && echo "    Gate:  $(format_duration $GATE_SECS)"
[ "$BUILD_BASE" -eq 1 ] && echo "    Base:  $(format_duration $BASE_SECS)"
echo "    Total: $(format_duration $TOTAL_SECS)"
