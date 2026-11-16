# Central opencode image (NS-ADR-106): the official opencode 2.3.0 image
# (Alpine, opencode binary, ripgrep) plus the tools coding sessions need on
# Linux-buildable repos. Pinned by digest. Build with ./build-image.sh.
FROM ghcr.io/anomalyco/opencode:2.3.0@sha256:998e3e184c650e603d8bacb5cbeaeb090837078262c25521a7136b0375d2b652
RUN apk add --no-cache git openssh-client bash ca-certificates curl \
      python3 py3-pip nodejs npm make build-base \
 && addgroup -g 1000 opencode && adduser -D -u 1000 -G opencode -h /data/home opencode
USER 1000:1000
ENTRYPOINT ["opencode"]
