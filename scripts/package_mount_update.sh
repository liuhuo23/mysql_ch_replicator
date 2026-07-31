#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'EOF'
Package mysql-ch-replicator code for bind-mount updates.

The zip root matches Docker image WORKDIR /app, so you can unzip directly
into the mounted application directory.

Usage:
  scripts/package_mount_update.sh [mode] [output_zip]

Modes:
  slim    Runtime code only (default, recommended for hotfix mount updates)
  full    Mirror Dockerfile "COPY . ." layout (excludes .git, dist, binlog data)
  docker  Build image with Dockerfile, then extract /app from the container

Examples:
  scripts/package_mount_update.sh
  scripts/package_mount_update.sh slim
  scripts/package_mount_update.sh full dist/mysql-ch-replicator-full.zip
  scripts/package_mount_update.sh docker

Deploy on server:
  unzip -o mysql-ch-replicator-mount-<sha>-<time>.zip -d /path/to/mount/app
  docker restart <replicator-container>
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    usage
    exit 0
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODE="${1:-slim}"
OUTPUT_ZIP="${2:-}"

STAMP="$(date +%Y%m%d_%H%M%S)"
GIT_SHA="$(git -C "$ROOT_DIR" rev-parse --short HEAD 2>/dev/null || echo unknown)"
DEFAULT_ZIP_NAME="mysql-ch-replicator-mount-${GIT_SHA}-${STAMP}.zip"
OUTPUT_DIR="${ROOT_DIR}/dist"
OUTPUT_ZIP="${OUTPUT_ZIP:-${OUTPUT_DIR}/${DEFAULT_ZIP_NAME}}"

if [[ "$OUTPUT_ZIP" != /* ]]; then
    OUTPUT_ZIP="${ROOT_DIR}/${OUTPUT_ZIP}"
fi

mkdir -p "$(dirname "$OUTPUT_ZIP")"

STAGING="$(mktemp -d "${TMPDIR:-/tmp}/mysql-ch-replicator-pack.XXXXXX")"
cleanup() {
    rm -rf "$STAGING"
}
trap cleanup EXIT

write_package_info() {
    cat > "${STAGING}/MOUNT_PACKAGE_INFO.txt" <<EOF
package_mode: ${MODE}
git_sha: ${GIT_SHA}
built_at: $(date -u +%Y-%m-%dT%H:%M:%SZ)
source_dir: ${ROOT_DIR}
mount_target: /app
EOF
}

rsync_common_excludes=(
    --exclude '__pycache__'
    --exclude '*.pyc'
    --exclude '*.pyo'
    --exclude '.DS_Store'
)

package_slim() {
    rsync -a "${rsync_common_excludes[@]}" \
        "${ROOT_DIR}/main.py" \
        "${ROOT_DIR}/requirements.txt" \
        "${ROOT_DIR}/requirements-dev.txt" \
        "${STAGING}/"

    rsync -a "${rsync_common_excludes[@]}" \
        "${ROOT_DIR}/mysql_ch_replicator/" \
        "${STAGING}/mysql_ch_replicator/"

    chmod +x "${STAGING}/main.py"
}

package_full() {
    rsync -a "${rsync_common_excludes[@]}" \
        --exclude '.git' \
        --exclude '.github' \
        --exclude '.cursor' \
        --exclude '.idea' \
        --exclude '.vscode' \
        --exclude 'binlog' \
        --exclude 'dist' \
        --exclude 'config.yaml' \
        --exclude '*cmake_build*' \
        --exclude 'monitoring.log' \
        "${ROOT_DIR}/" \
        "${STAGING}/"

    chmod +x "${STAGING}/main.py"
}

package_docker() {
    local image_tag="mysql-ch-replicator-pack:local-${GIT_SHA}-${STAMP}"
    local container_id=""

    echo "[INFO] Building Docker image: ${image_tag}"
    docker build -t "${image_tag}" "${ROOT_DIR}"

    container_id="$(docker create "${image_tag}")"
    echo "[INFO] Extracting /app from container ${container_id}"
    docker cp "${container_id}:/app/." "${STAGING}/"
    docker rm -f "${container_id}" >/dev/null
}

case "${MODE}" in
    slim)
        package_slim
        ;;
    full)
        package_full
        ;;
    docker)
        package_docker
        ;;
    *)
        echo "[ERROR] Unknown mode: ${MODE}" >&2
        usage >&2
        exit 1
        ;;
esac

write_package_info

if [[ -f "${OUTPUT_ZIP}" ]]; then
    rm -f "${OUTPUT_ZIP}"
fi

echo "[INFO] Creating zip: ${OUTPUT_ZIP}"
(
    cd "${STAGING}"
    zip -qr "${OUTPUT_ZIP}" .
)

echo "[SUCCESS] Package created:"
echo "  zip: ${OUTPUT_ZIP}"
echo "  mode: ${MODE}"
echo "  git: ${GIT_SHA}"
echo
echo "Zip contents (top level):"
unzip -l "${OUTPUT_ZIP}" | head -n 20
echo
echo "Deploy:"
echo "  unzip -o $(basename "${OUTPUT_ZIP}") -d /path/to/mount/app"
echo "  docker restart <replicator-container>"
