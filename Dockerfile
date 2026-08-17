# syntax=docker/dockerfile:1
#
# BoxMedia — multi-stage build to a distroless, non-root, read-only runtime.
# All base images pinned by digest (never a mutable tag). The final image has no
# shell, package manager, or build tools, and carries no secrets in any layer.

# --- Stage 1: compile Tailwind CSS (keeps Node out of the final image) ---
# Pinned to $BUILDPLATFORM: the output is a CSS file, identical for every target, so in
# a multi-arch build this stage runs once, natively, instead of once per target under
# emulation. The Tailwind binary is selected by the BUILD machine's arch (that is where
# it runs), each with its own pinned checksum from the same release.
FROM --platform=$BUILDPLATFORM debian:bookworm-slim@sha256:abd67ffcfa541b485a3dff59865ab629aa048a6c613e639d36e7456b0b229241 AS assets
ARG BUILDARCH
ARG TAILWIND_VERSION=v3.4.17
ARG TAILWIND_SHA256_AMD64=7d24f7fa191d2193b78cd5f5a42a6093e14409521908529f42d80b11fde1f1d4
ARG TAILWIND_SHA256_ARM64=69b1378b8133192d7d2feb12a116fa12d035594f58db3eff215879e4ad8cf39b
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /build
RUN case "${BUILDARCH}" in \
      amd64) TW_ARCH=x64   TW_SHA256="${TAILWIND_SHA256_AMD64}" ;; \
      arm64) TW_ARCH=arm64 TW_SHA256="${TAILWIND_SHA256_ARM64}" ;; \
      *) echo "unsupported build arch: ${BUILDARCH}" >&2; exit 1 ;; \
    esac \
 && curl -fsSL -o tailwindcss \
      "https://github.com/tailwindlabs/tailwindcss/releases/download/${TAILWIND_VERSION}/tailwindcss-linux-${TW_ARCH}" \
 && echo "${TW_SHA256}  tailwindcss" | sha256sum -c - \
 && chmod +x tailwindcss
COPY tailwind.config.js ./
COPY styles ./styles
COPY app/templates ./app/templates
# Templates must be present before this runs — Tailwind tree-shakes unused classes.
RUN ./tailwindcss -c tailwind.config.js -i styles/tailwind.css -o /assets/app.css --minify

# --- Stage 2: install Python dependencies (hash-pinned) into a target dir ---
FROM python:3.11-slim-bookworm@sha256:d29f48a31a8b408ed19272ca1e7b10ebae13b240a27e862d3d4217c528e2e0c3 AS builder
WORKDIR /build
COPY requirements-runtime.txt ./
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --require-hashes --no-deps --target=/install -r requirements-runtime.txt

# --- Stage 3: distroless runtime ---
FROM gcr.io/distroless/python3-debian12:nonroot@sha256:7d1042ce588ab97019fe95c24ffca7bc5a82ccdac572511d5e09bda4435c89c5 AS runtime
# OCI labels: the source label is what links the GHCR package to the repo.
LABEL org.opencontainers.image.source="https://github.com/aiulian25/boxmedia" \
      org.opencontainers.image.description="Weekly box-office hits, matched against your Radarr library for one-click adding" \
      org.opencontainers.image.licenses="MIT"
ENV PYTHONPATH=/app/deps:/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app
COPY --from=builder /install /app/deps
COPY app /app/app
COPY --from=assets /assets/app.css /app/app/static/css/app.css

# Non-root numeric UID (distroless "nonroot" = 65532); read-only rootfs at runtime.
USER 65532:65532
EXPOSE 8686

# No shell/curl exists — the healthcheck is a plain Python one-liner.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["/usr/bin/python3.11", "-c", \
       "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8686/health').status==200 else 1)"]

ENTRYPOINT ["/usr/bin/python3.11", "-m", "uvicorn", "app.main:create_app", \
            "--factory", "--host", "0.0.0.0", "--port", "8686", "--proxy-headers"]
