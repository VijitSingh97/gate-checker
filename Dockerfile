FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
    build-essential bash bc binutils bzip2 ca-certificates cpio \
    debianutils file g++ gcc git gzip libncurses5-dev libssl-dev \
    make patch perl python3 rsync sed tar unzip wget \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -ms /bin/bash builder

RUN echo "builder soft nofile 65536" >> /etc/security/limits.conf && \
    echo "builder hard nofile 65536" >> /etc/security/limits.conf

WORKDIR /workspace

COPY --chown=builder:builder . /workspace

RUN chmod +x /workspace/build.sh

USER builder

ENTRYPOINT ["bash", "/workspace/build.sh"]