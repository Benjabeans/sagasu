# syntax=docker/dockerfile:1
#
# Sagasu session image — one container == one browser session.
#
# Holds the virtual display (TigerVNC Xvnc = X server + VNC server in one
# process), a window manager (openbox), the browser (Helium by default), and
# the VNC -> web pipeline (websockify + vendored noVNC static files).
#
# Safe by default: the VNC port binds to container-loopback only and is never
# published; CDP is reachable through an in-container socat relay because
# headful Chromium ignores --remote-debugging-address and binds CDP to
# container-loopback.
#
# Build:
#   docker build -t sagasu/session:dev .
#   docker build -t sagasu/session:chromium --build-arg BROWSER=chromium .

# debian:trixie-slim, pinned by multi-arch OCI index digest (resolved 2026-07-28).
ARG BASE_IMAGE=debian:trixie-slim@sha256:020c0d20b9880058cbe785a9db107156c3c75c2ac944a6aa7ab59f2add76a7bd


# ---------------------------------------------------------------------------
# Stage 1: fetcher (discarded) — download and verify the browser payload and
# the noVNC static files. Nothing from this stage's toolchain (curl, gnupg,
# xz-utils) reaches the runtime image.
# ---------------------------------------------------------------------------
FROM ${BASE_IMAGE} AS fetcher

ARG BROWSER=helium
ARG HELIUM_VERSION=0.14.9.1
ARG NOVNC_VERSION=1.6.0
# sha256 of https://github.com/novnc/noVNC/archive/refs/tags/v1.6.0.tar.gz
ARG NOVNC_SHA256=5066103959ef4e9b10f37e5a148627360dd8414e4cf8a7db92bdbd022e728aaa
# Trust anchor for the Helium release signature. Asserted below so that a
# swapped-out docker/helium-pubkey.asc fails the build instead of signing off
# on an attacker's tarball.
ARG HELIUM_SIGNING_FPR=BE677C1989D35EAB2C5F26C9351601AD01D6378E
ARG TARGETARCH

SHELL ["/bin/bash", "-euo", "pipefail", "-c"]

ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates \
      curl \
      gnupg \
      xz-utils \
 && rm -rf /var/lib/apt/lists/*

# Helium signing key. Fingerprint: BE67 7C19 89D3 5EAB 2C5F  26C9 3516 01AD 01D6 378E
COPY docker/helium-pubkey.asc /tmp/helium-pubkey.asc

# Helium: signed tar.xz. The upstream .deb declares no library Depends, so apt
# would resolve nothing for it — hence the tarball plus an explicit runtime-lib
# list in the final stage.
RUN mkdir -p /opt/helium; \
    if [ "${BROWSER}" != "helium" ]; then \
      echo "BROWSER=${BROWSER}: skipping the Helium payload"; \
      exit 0; \
    fi; \
    case "${TARGETARCH:-amd64}" in \
      amd64) helium_arch=x86_64 ;; \
      arm64) helium_arch=arm64 ;; \
      *) echo "FATAL: unsupported TARGETARCH=${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    asset="helium-${HELIUM_VERSION}-${helium_arch}_linux.tar.xz"; \
    base="https://github.com/imputnet/helium-linux/releases/download/${HELIUM_VERSION}"; \
    curl -fsSL --retry 3 --retry-delay 2 -o "/tmp/${asset}" "${base}/${asset}"; \
    curl -fsSL --retry 3 --retry-delay 2 -o "/tmp/${asset}.asc" "${base}/${asset}.asc"; \
    export GNUPGHOME="$(mktemp -d)"; \
    gpg --batch --quiet --import /tmp/helium-pubkey.asc; \
    gpg --batch --with-colons --fingerprint \
      | grep -qx "fpr:::::::::${HELIUM_SIGNING_FPR}:" \
      || { echo "FATAL: docker/helium-pubkey.asc is not the expected Helium key" >&2; exit 1; }; \
    gpg --batch --verify "/tmp/${asset}.asc" "/tmp/${asset}"; \
    tar -xJf "/tmp/${asset}" -C /opt/helium --strip-components=1; \
    rm -rf "/tmp/${asset}" "/tmp/${asset}.asc" "${GNUPGHOME}"; \
    test -x /opt/helium/helium

# noVNC: vendored static files. Debian's novnc package hard-depends on nodejs
# (libnode + libicu, ~95MB) to serve what is plain HTML/JS — the single biggest
# size win in this image. websockify still comes from apt.
RUN curl -fsSL --retry 3 --retry-delay 2 -o /tmp/novnc.tar.gz \
      "https://github.com/novnc/noVNC/archive/refs/tags/v${NOVNC_VERSION}.tar.gz" \
 && echo "${NOVNC_SHA256}  /tmp/novnc.tar.gz" | sha256sum -c - \
 && mkdir -p /opt/novnc \
 && tar -xzf /tmp/novnc.tar.gz -C /opt/novnc --strip-components=1 \
      "noVNC-${NOVNC_VERSION}/app" \
      "noVNC-${NOVNC_VERSION}/core" \
      "noVNC-${NOVNC_VERSION}/vendor" \
      "noVNC-${NOVNC_VERSION}/vnc.html" \
      "noVNC-${NOVNC_VERSION}/vnc_lite.html" \
      "noVNC-${NOVNC_VERSION}/defaults.json" \
      "noVNC-${NOVNC_VERSION}/mandatory.json" \
      "noVNC-${NOVNC_VERSION}/LICENSE.txt" \
 && ln -s vnc.html /opt/novnc/index.html \
 && rm -f /tmp/novnc.tar.gz


# ---------------------------------------------------------------------------
# Stage 2: runtime
# ---------------------------------------------------------------------------
FROM ${BASE_IMAGE} AS runtime

ARG BROWSER=helium
ARG HELIUM_VERSION=0.14.9.1
ARG HUMANCURSOR_VERSION=1.1.5
ARG NOVNC_VERSION=1.6.0
ARG SAGASU_UID=1000
ARG SAGASU_GID=1000

SHELL ["/bin/bash", "-euo", "pipefail", "-c"]

# --- apt layer -------------------------------------------------------------
# Kept above the browser payload so that a Helium version bump does not
# invalidate this (large, slow) layer.
#
# Deliberately NOT installed, in service of the lightweight discipline:
#   dbus / dbus-x11  only the libdbus-1-3 client library is needed
#                    (paired with --password-store=basic in the session script)
#   novnc            drags in nodejs; vendored statically instead
#   locales          C.UTF-8 is built into glibc
#   pulseaudio/pipewire, xfonts-base, tigervnc-tools, any DE, xterm, curl, gnupg
#
# HumanCursor/PyAutoGUI is the primary X cursor backend. xdotool remains the
# low-level X cursor/keyboard fallback, and scrot captures the matching full
# display (browser chrome included). Python dependencies live in an isolated
# venv below rather than modifying Debian's externally-managed system Python.
#
# The Mesa software-rasteriser stack cannot be excluded by dependency choice:
# tigervnc-standalone-server hard-Depends on libgl1 -> libglx0 -> libglx-mesa0
# -> mesa-libgallium -> libllvm19, so --no-install-recommends does not stop it.
# That chain is ~165MB of LLVM/Gallium for a rasteriser nothing here uses: the
# browser renders through its own bundled SwiftShader/ANGLE (/opt/helium/lib*),
# and no GLX client ever runs on this display. dpkg path-excludes drop the
# payload while leaving the package database consistent — libGL.so itself stays,
# so Xvnc still links and starts; only real GLX contexts would be unavailable.
ARG DEBIAN_FRONTEND=noninteractive
RUN printf '%s\n' \
      'path-exclude=/usr/lib/*/dri/*' \
      'path-exclude=/usr/lib/*/libgallium*.so*' \
      'path-exclude=/usr/lib/*/libLLVM*.so*' \
      'path-exclude=/usr/lib/*/vdpau/*' \
      'path-exclude=/usr/share/doc/*' \
      'path-exclude=/usr/share/man/*' \
    > /etc/dpkg/dpkg.cfg.d/sagasu-slim; \
    apt-get update; \
    pkgs=( \
      tigervnc-standalone-server \
      openbox \
      websockify \
      socat \
      xdotool \
      scrot \
      python3-venv \
      python3-numpy \
      python3-pil \
      python3-setuptools \
      python3-tk \
      python3-xlib \
      python3-wheel \
      tini \
      ca-certificates \
      fontconfig \
      fonts-liberation \
      fonts-noto-cjk \
      fonts-noto-color-emoji \
      libasound2t64 \
      libatk-bridge2.0-0t64 \
      libatk1.0-0t64 \
      libatspi2.0-0t64 \
      libcairo2 \
      libcups2t64 \
      libdbus-1-3 \
      libdrm2 \
      libexpat1 \
      libfontconfig1 \
      libgbm1 \
      libglib2.0-0t64 \
      libnspr4 \
      libnss3 \
      libpango-1.0-0 \
      libudev1 \
      libx11-6 \
      libxcb1 \
      libxcomposite1 \
      libxdamage1 \
      libxext6 \
      libxfixes3 \
      libxkbcommon0 \
      libxrandr2 \
    ); \
    if [ "${BROWSER}" = "chromium" ]; then \
      pkgs+=( chromium chromium-sandbox ); \
    fi; \
    apt-get install -y --no-install-recommends "${pkgs[@]}"; \
    apt-get clean; \
    rm -rf /var/lib/apt/lists/*; \
    fc-cache -f

# --- human cursor ----------------------------------------------------------
# HumanCursor's SystemCursor uses PyAutoGUI to inject mouse events into the
# session's real Xvnc display. --system-site-packages reuses Debian's
# architecture-matched NumPy and Pillow. HumanCursor currently imports its
# Selenium-backed WebCursor at package import time, so installing the complete
# pinned distribution is intentional even though Sagasu's default interaction
# path only uses SystemCursor.
ARG WEBSOCKET_CLIENT_VERSION=1.9.0
RUN python3 -m venv --system-site-packages /opt/sagasu-humancursor \
 && /opt/sagasu-humancursor/bin/python -m pip install \
      --disable-pip-version-check \
      --no-cache-dir \
      "HumanCursor==${HUMANCURSOR_VERSION}" \
      "websocket-client==${WEBSOCKET_CLIENT_VERSION}" \
 && test "$(/opt/sagasu-humancursor/bin/python -c \
      'from importlib.metadata import version; print(version("HumanCursor"))')" = "${HUMANCURSOR_VERSION}" \
 && test "$(/opt/sagasu-humancursor/bin/python -c \
      'from importlib.metadata import version; print(version("websocket-client"))')" = "${WEBSOCKET_CLIENT_VERSION}"

# --- noVNC static files ----------------------------------------------------
COPY --from=fetcher /opt/novnc /usr/share/novnc

# --- unprivileged session user --------------------------------------------
# Named volumes inherit ownership/mode from the image, so creating /profile
# here with tight permissions is what makes profile volumes safe by default.
RUN groupadd -g "${SAGASU_GID}" sagasu \
 && useradd -u "${SAGASU_UID}" -g "${SAGASU_GID}" -m -d /home/sagasu -s /usr/sbin/nologin sagasu \
 && mkdir -p /profile /run/sagasu \
 && chown sagasu:sagasu /profile /home/sagasu /run/sagasu \
 && chmod 700 /profile /home/sagasu /run/sagasu

# --- browser payload -------------------------------------------------------
# Empty directory in the chromium variant; removed by the step below.
COPY --from=fetcher /opt/helium /opt/helium

# Build assertions — Helium-drift insurance. If upstream starts linking a
# library this image does not carry, or renames a binary, the build fails here
# rather than at the operator's first `docker run`.
RUN if [ "${BROWSER}" != "helium" ]; then \
      rm -rf /opt/helium; \
      test -x /usr/bin/chromium; \
      test -u /usr/lib/chromium/chrome-sandbox || echo "warning: chromium-sandbox is not setuid"; \
      /usr/bin/chromium --version; \
      exit 0; \
    fi; \
    ln -sf /opt/helium/helium-wrapper /usr/bin/helium; \
    for bin in /opt/helium/helium /opt/helium/helium_crashpad_handler; do \
      echo "== ldd ${bin}"; \
      ldd "${bin}"; \
      if ldd "${bin}" | grep -F "not found"; then \
        echo "FATAL: unresolved shared libraries in ${bin}" >&2; exit 1; \
      fi; \
    done; \
    if [ -e /opt/helium/chrome-sandbox ]; then chmod 4755 /opt/helium/chrome-sandbox; fi; \
    /usr/bin/helium --version

# --- session-control executor ----------------------------------------------
# The host CLI and private executor intentionally ship as one distribution.
# Install it into HumanCursor's venv so the executor imports the exact pinned
# HumanCursor/PyAutoGUI stack above, then expose the private entry point on the
# container's ordinary PATH for non-interactive `docker exec`. Keep the former
# xcontrol name as a compatibility alias while callers migrate.
COPY pyproject.toml README.md /opt/sagasu-package/
COPY src /opt/sagasu-package/src
RUN /opt/sagasu-humancursor/bin/python -m pip install \
      --disable-pip-version-check \
      --no-cache-dir \
      --no-build-isolation \
      --no-deps \
      /opt/sagasu-package \
 && ln -sf /opt/sagasu-humancursor/bin/sagasu-session-exec \
      /usr/local/bin/sagasu-session-exec \
 && ln -sf /opt/sagasu-humancursor/bin/sagasu-idle-daemon \
      /usr/local/bin/sagasu-idle-daemon \
 && ln -sf /opt/sagasu-humancursor/bin/sagasu-xcontrol \
      /usr/local/bin/sagasu-xcontrol \
 && test -x /usr/local/bin/sagasu-idle-daemon \
 && test -x /usr/local/bin/sagasu-session-exec \
 && test -x /usr/local/bin/sagasu-xcontrol

# --- session scripts -------------------------------------------------------
# chmod is explicit rather than `COPY --chmod` so the image also builds on the
# legacy (non-BuildKit) builder, and so it does not depend on the exec bits
# surviving in the checkout.
COPY docker/entrypoint.sh        /usr/local/bin/sagasu-entrypoint
COPY docker/sagasu-session       /usr/local/bin/sagasu-session
COPY docker/sagasu-healthcheck   /usr/local/bin/sagasu-healthcheck
RUN chmod 0755 \
      /usr/local/bin/sagasu-entrypoint \
      /usr/local/bin/sagasu-session \
      /usr/local/bin/sagasu-healthcheck

# --- runtime configuration -------------------------------------------------
# BROWSER_BIN resolves to /usr/bin/helium (symlink -> helium-wrapper) or
# /usr/bin/chromium depending on the variant.
# Xvnc is reachable only through its in-container Unix socket and does not use
# an Xauthority database. Python Xlib nevertheless insists on opening one, so
# /dev/null explicitly represents the empty database SystemCursor should use.
ENV DISPLAY=:1 \
    XAUTHORITY=/dev/null \
    SCREEN_GEOMETRY=1366x768 \
    SCREEN_DEPTH=24 \
    START_URL=about:blank \
    PROFILE_DIR=/profile \
    VNC_PORT=5901 \
    NOVNC_PORT=6080 \
    NOVNC_ROOT=/usr/share/novnc \
    CDP_PORT=9222 \
    CDP_INTERNAL_PORT=9229 \
    BROWSER_BIN=/usr/bin/${BROWSER} \
    BROWSER_EXTRA_ARGS= \
    HUMANCURSOR_PYTHON=/opt/sagasu-humancursor/bin/python \
    SAGASU_IDLE_ENABLED=1 \
    SAGASU_IDLE_AFTER_SECONDS=5 \
    SAGASU_IDLE_RADIUS_PX=300 \
    SAGASU_IDLE_MIN_DURATION_SECONDS=0.3 \
    SAGASU_IDLE_MAX_DURATION_SECONDS=2 \
    SAGASU_IDLE_STATIONARY_CHANCE=0.25 \
    SAGASU_SEQUENCE_MAX_ACTIONS=3 \
    SAGASU_SEQUENCE_SETTLE_MS=1000 \
    SAGASU_NO_SANDBOX= \
    SAGASU_USER=sagasu \
    HOME=/home/sagasu \
    LANG=C.UTF-8

RUN test -x "${BROWSER_BIN}" || { echo "FATAL: BROWSER_BIN=${BROWSER_BIN} is not executable" >&2; exit 1; }

# 6080 = noVNC/websockify (reached over the compose network by the panel).
# 9222 = CDP via socat (compose publishes it on 127.0.0.1 only).
# 5901 = VNC. Deliberately NOT exposed: websockify reaches it in-namespace.
EXPOSE 6080 9222

HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
    CMD ["/usr/local/bin/sagasu-healthcheck"]

LABEL org.opencontainers.image.title="sagasu-session" \
      org.opencontainers.image.description="Sagasu browser session: TigerVNC + openbox + Helium/Chromium + noVNC, CDP over a loopback socat relay" \
      org.opencontainers.image.source="https://github.com/Benjabeans/sagasu" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.base.name="debian:trixie-slim" \
      computer.sagasu.browser="${BROWSER}" \
      computer.sagasu.helium.version="${HELIUM_VERSION}" \
      computer.sagasu.humancursor.version="${HUMANCURSOR_VERSION}" \
      computer.sagasu.novnc.version="${NOVNC_VERSION}"

# tini is baked in rather than left to compose `init:` so PID 1 reaps zombies
# correctly even under a raw `docker run`.
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/sagasu-entrypoint"]
