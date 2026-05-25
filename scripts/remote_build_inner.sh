#!/bin/bash
# Inner half of the remote-build pipeline. Lives in the repo, gets rsynced
# to the build host, and is executed there by remote_build.sh.
#
# Receives hook commands via environment variables (set by the caller in
# remote_build.sh):
#   PRE_HOOK              shell snippet to run before the build (default: no-op)
#   POST_HOOK             shell snippet to run after the build, via an EXIT
#                         trap so it fires on success, failure, and Ctrl-C
#   RANCH_BUILD_PROFILE   production (default) or development
#
# Must be invoked from the workspace root on the remote host.

set -euo pipefail

PRE_HOOK="${PRE_HOOK:-true}"
POST_HOOK="${POST_HOOK:-true}"
RANCH_BUILD_PROFILE="${RANCH_BUILD_PROFILE:-production}"
RANCH_BUILD_TARGETS="${RANCH_BUILD_TARGETS:-gate base}"

SUFFIX=""
if [ "$RANCH_BUILD_PROFILE" = "development" ]; then
    SUFFIX="_dev"
fi
BASE_IMG="releases/base_station_pi3${SUFFIX}.img"
GATE_IMG="releases/gate_client_pi0w${SUFFIX}.img"

# Which images do we actually verify+measure? Skip the one that wasn't built.
BUILT_BASE=0
BUILT_GATE=0
case " $RANCH_BUILD_TARGETS " in
    *" base "*) BUILT_BASE=1 ;;
esac
case " $RANCH_BUILD_TARGETS " in
    *" gate "*) BUILT_GATE=1 ;;
esac

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

INNER_START=$SECONDS
DOCKER_SECS=0
VERIFY_SECS=0

cleanup() {
    local status=$?
    local total=$(( SECONDS - INNER_START ))
    echo "   [remote] Running post-hook (exit status $status, total $(format_duration $total))..."
    # Post-hook is best-effort. A failure here must not mask the build's exit
    # status, otherwise a green build could hide e.g. xmrig failing to restart.
    eval "$POST_HOOK" || echo "   [remote] WARNING: post-hook failed" >&2
    exit "$status"
}
trap cleanup EXIT INT TERM HUP

echo "   [remote] Running pre-hook..."
eval "$PRE_HOOK"

# Always invoke docker build — layer caching makes this a few-second
# no-op when nothing changed, and it guarantees the BUILDER_UID/GID
# in the image match the host user. Without that match, Docker bind
# mounts (dl-cache, ccache-dir, releases) end up unwritable from
# inside the container: "Permission denied" on the first cmake fetch.
echo "   [remote] Ensuring ranch-builder image is up to date..."
docker build \
    --build-arg "BUILDER_UID=$(id -u)" \
    --build-arg "BUILDER_GID=$(id -g)" \
    -t ranch-builder .

# Ensure host-side bind mount targets exist BEFORE `docker run`.
# Docker creates missing bind-mount source dirs as root:root, which
# the container's builder user can't write to. mkdir -p creates them
# owned by the host user, matching the BUILDER_UID/GID baked into
# the image above.
#
# output_base / output_gate are bind-mounted so Buildroot's ~10-20GB
# of intermediate output (host tools, toolchain, target rootfs) lives
# on a host directory we control instead of growing the Docker overlay
# layer. Persistence across builds also makes rebuilds incremental.
# See docs/BUILDING.md "Cleaning out the persistent output dirs" for
# when to wipe these.
mkdir -p dl-cache ccache-dir releases output_base output_gate

echo "   [remote] Running build container (profile=$RANCH_BUILD_PROFILE, targets=$RANCH_BUILD_TARGETS)..."
DOCKER_START=$SECONDS
docker run --rm -t \
  --ulimit nofile=65536:65536 \
  -e "RANCH_BUILD_PROFILE=$RANCH_BUILD_PROFILE" \
  -e "RANCH_BUILD_TARGETS=$RANCH_BUILD_TARGETS" \
  -v "$(pwd)/releases:/workspace/releases" \
  -v "$(pwd)/dl-cache:/workspace/buildroot/dl" \
  -v "$(pwd)/ccache-dir:/home/builder/.buildroot-ccache" \
  -v "$(pwd)/output_base:/tmp/output_base" \
  -v "$(pwd)/output_gate:/tmp/output_gate" \
  -v "$(pwd)/ranch_os:/workspace/ranch_os:ro" \
  -v "$(pwd)/build.sh:/workspace/build.sh:ro" \
  ranch-builder
DOCKER_SECS=$(( SECONDS - DOCKER_START ))
echo "   [remote] Docker build took $(format_duration $DOCKER_SECS)"

# Verify the resulting images, then report rootfs usage. Both steps need
# passwordless sudo for a small set of read-only commands; if that isn't
# configured we skip rather than failing the whole build.
_can_sudo() { sudo -n "$@" >/dev/null 2>&1; }

if _can_sudo losetup --help && _can_sudo cat /dev/null; then
    echo "   [remote] Verifying built images..."
    VERIFY_START=$SECONDS
    [ "$BUILT_BASE" -eq 1 ] && scripts/verify_image.sh "$BASE_IMG"
    [ "$BUILT_GATE" -eq 1 ] && scripts/verify_image.sh "$GATE_IMG"
    echo "   [remote] Image verification passed."

    if _can_sudo du --version; then
        echo "   [remote] Measuring rootfs usage..."
        IMAGES=""
        [ "$BUILT_BASE" -eq 1 ] && IMAGES="$IMAGES $BASE_IMG"
        [ "$BUILT_GATE" -eq 1 ] && IMAGES="$IMAGES $GATE_IMG"
        # shellcheck disable=SC2086  # IMAGES is intentionally word-split.
        scripts/measure_image.sh $IMAGES \
            || echo "   [remote] WARNING: measurement step failed (non-fatal)"
    else
        echo "   [remote] Skipping measurement: passwordless sudo for du"
        echo "   [remote] is not configured. See docs/BUILDING.md to enable."
    fi
    VERIFY_SECS=$(( SECONDS - VERIFY_START ))
    echo "   [remote] Verify + measure took $(format_duration $VERIFY_SECS)"
else
    echo "   [remote] Skipping image verification and measurement:"
    echo "   [remote] passwordless sudo for losetup/cat is not configured."
    echo "   [remote] See docs/BUILDING.md (Passwordless image verification)."
fi

INNER_TOTAL=$(( SECONDS - INNER_START ))
echo "   [remote] Inner pipeline finished in $(format_duration $INNER_TOTAL)"
echo "   [remote]   docker:          $(format_duration $DOCKER_SECS)"
echo "   [remote]   verify+measure:  $(format_duration $VERIFY_SECS)"
