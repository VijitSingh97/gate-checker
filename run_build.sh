#!/bin/bash
# Run the Ranch OS build inside the local Docker container.
set -euo pipefail

docker run --rm -t \
  --ulimit nofile=65536:65536 \
  -v "$(pwd)/releases:/workspace/releases" \
  -v "$(pwd)/dl-cache:/workspace/buildroot/dl" \
  -v "$(pwd)/ccache-dir:/home/builder/.buildroot-ccache" \
  -v "$(pwd)/ranch_os:/workspace/ranch_os:ro" \
  -v "$(pwd)/build.sh:/workspace/build.sh:ro" \
  ranch-builder
