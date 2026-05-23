#!/bin/bash
# Run the Ranch OS build inside the local Docker container.
#
# Set RANCH_BUILD_PROFILE=development for an image with SSH + debug tools.
# Default is `production`. Dev images are written with a _dev suffix.
#
# Set RANCH_BUILD_TARGETS to a space-separated subset of "gate base" (default
# both) to skip one of the images when iterating — same semantics as
# remote_build.sh. build.sh inside the container validates the value.
set -euo pipefail

PROFILE="${RANCH_BUILD_PROFILE:-production}"
TARGETS="${RANCH_BUILD_TARGETS:-gate base}"

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

START=$SECONDS

docker run --rm -t \
  --ulimit nofile=65536:65536 \
  -e "RANCH_BUILD_PROFILE=$PROFILE" \
  -e "RANCH_BUILD_TARGETS=$TARGETS" \
  -v "$(pwd)/releases:/workspace/releases" \
  -v "$(pwd)/dl-cache:/workspace/buildroot/dl" \
  -v "$(pwd)/ccache-dir:/home/builder/.buildroot-ccache" \
  -v "$(pwd)/ranch_os:/workspace/ranch_os:ro" \
  -v "$(pwd)/build.sh:/workspace/build.sh:ro" \
  ranch-builder

echo "=== Local build complete in $(format_duration $(( SECONDS - START ))) ==="
