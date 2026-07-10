# Pinned to an immutable digest (supply-chain hardening) — a tag
# re-push upstream can't silently change what builds start from.
# Dependabot's docker ecosystem bumps this; to refresh manually:
#     docker buildx imagetools inspect ubuntu:22.04 | grep -i 'Digest:'
# ubuntu:22.04 as of 2026-07-09
FROM ubuntu@sha256:b7f48194d4d8b763a478a621cdc81c27be222ba2206ca3ca6bc42b49685f3d9e

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
    build-essential bash bc binutils bzip2 ca-certificates cpio \
    debianutils file g++ gcc git gzip libncurses5-dev libssl-dev \
    make patch perl python3 rsync sed tar unzip wget \
    && rm -rf /var/lib/apt/lists/*

# Builder UID/GID are passed by remote_build_inner.sh from the host user
# via `--build-arg BUILDER_UID=$(id -u) --build-arg BUILDER_GID=$(id -g)`.
# Matching them to the host user means bind-mounted dirs (dl-cache,
# ccache-dir, releases) are writable inside the container without a
# chmod/chown dance. Default 1000:1000 keeps the local docker run path
# (run_build.sh) working unchanged for the common case.
ARG BUILDER_UID=1000
ARG BUILDER_GID=1000
RUN groupadd -g "${BUILDER_GID}" builder && \
    useradd -u "${BUILDER_UID}" -g builder -ms /bin/bash builder

RUN echo "builder soft nofile 65536" >> /etc/security/limits.conf && \
    echo "builder hard nofile 65536" >> /etc/security/limits.conf

WORKDIR /workspace

COPY --chown=builder:builder . /workspace

RUN chmod +x /workspace/build.sh

USER builder

ENTRYPOINT ["bash", "/workspace/build.sh"]