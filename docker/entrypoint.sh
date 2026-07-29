#!/usr/bin/env bash
#
# Sagasu session entrypoint — runs as root under tini, does the four things
# that need root, then drops to the unprivileged session user for good.
#
#   1. create the private session-control runtime directory
#   2. make /profile and $HOME writable by the session user (bind-mount case)
#   3. remove stale Chromium Singleton* locks left by a kill -9'd container
#   4. exec into the session script as sagasu
#
set -euo pipefail

log() { printf '[sagasu/entrypoint] %s\n' "$*" >&2; }

: "${SAGASU_USER:=sagasu}"
: "${PROFILE_DIR:=/profile}"
session_runtime_dir="/run/sagasu"

if [[ "$(id -u)" -eq 0 ]]; then
    sagasu_uid="$(id -u "${SAGASU_USER}")"
    sagasu_gid="$(id -g "${SAGASU_USER}")"
    sagasu_home="$(getent passwd "${SAGASU_USER}" | cut -d: -f6)"

    # flock and the human-control marker live here. Keep the directory private
    # to the unprivileged executor user; every docker exec uses that identity.
    install -d \
        -m 0700 \
        -o "${sagasu_uid}" \
        -g "${sagasu_gid}" \
        "${session_runtime_dir}"

    # Named volumes inherit the image's ownership, so this is normally a no-op.
    # Bind mounts do not, hence the check — and the recursive chown is skipped
    # unless the top-level owner is actually wrong, because profiles get large.
    for dir in "${PROFILE_DIR}" "${sagasu_home}"; do
        [[ -n "${dir}" && -d "${dir}" ]] || continue
        if [[ "$(stat -c '%u:%g' "${dir}")" != "${sagasu_uid}:${sagasu_gid}" ]]; then
            log "taking ownership of ${dir} for ${SAGASU_USER}(${sagasu_uid}:${sagasu_gid})"
            chown -R "${sagasu_uid}:${sagasu_gid}" "${dir}"
        fi
        chmod 700 "${dir}"
    done
else
    log "already running as uid $(id -u); skipping the privileged setup"
    [[ -d "${session_runtime_dir}" ]] \
        || { log "FATAL: ${session_runtime_dir} does not exist"; exit 1; }
fi

# A container killed with SIGKILL leaves these behind and Chromium then refuses
# to open the profile ("The profile appears to be in use").
if [[ -d "${PROFILE_DIR}" ]]; then
    for lock in SingletonLock SingletonSocket SingletonCookie; do
        if [[ -e "${PROFILE_DIR}/${lock}" || -L "${PROFILE_DIR}/${lock}" ]]; then
            log "removing stale ${lock}"
            rm -f "${PROFILE_DIR}/${lock}"
        fi
    done
fi

if [[ "$(id -u)" -ne 0 ]]; then
    exec /usr/local/bin/sagasu-session "$@"
fi

log "dropping to ${SAGASU_USER}"
# setpriv ships in util-linux — no gosu/su-exec to vendor. --no-new-privs is
# deliberately NOT set: the chromium variant's sandbox helper is setuid.
exec setpriv \
    --reuid="${SAGASU_USER}" \
    --regid="${SAGASU_USER}" \
    --init-groups \
    --inh-caps=-all \
    -- /usr/local/bin/sagasu-session "$@"
