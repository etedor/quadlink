FROM nixos/nix:latest AS builder

RUN echo "experimental-features = nix-command flakes" >> /etc/nix/nix.conf

WORKDIR /build
COPY . .

RUN nix build .#default --print-build-logs

RUN mkdir -p /output && \
    cp -rL result/* /output/

FROM python:3.13-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /output /app

RUN useradd -m -u 1000 -s /bin/bash quadlink

WORKDIR /app
RUN chown -R quadlink:quadlink /app

USER quadlink

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health').read()" || exit 1

ENV PYTHONUNBUFFERED=1 \
    QL_LOG_LEVEL=info

ENTRYPOINT ["/app/bin/quadlink"]
CMD ["--interval", "30"]
