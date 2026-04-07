#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
ENV_EXAMPLE="${REPO_ROOT}/.env.example"
ENV_FILE="${REPO_ROOT}/.env"
GENERATED_DIR_DEFAULT="${REPO_ROOT}/output/generated"
GENERATED_DIR=""
SHOULD_START=0

usage() {
  cat <<'EOF'
Usage: ./scripts/bootstrap.sh [--up] [--env-file PATH] [--generated-dir PATH]

Options:
  --up                  Initialize local config and start docker compose.
  --env-file PATH       Target env file. Defaults to ./.env
  --generated-dir PATH  Absolute or repo-relative generated output directory.
  --help                Show this help message.
EOF
}

ensure_absolute_path() {
  local path="$1"
  if [[ "$path" = /* ]]; then
    printf '%s\n' "$path"
  else
    printf '%s\n' "${REPO_ROOT}/${path#./}"
  fi
}

read_env_value() {
  local file="$1"
  local key="$2"
  if [[ ! -f "$file" ]]; then
    return 1
  fi
  local line
  line="$(grep -E "^${key}=" "$file" | tail -n 1 || true)"
  if [[ -z "$line" ]]; then
    return 1
  fi
  printf '%s\n' "${line#*=}"
}

write_env_value() {
  local file="$1"
  local key="$2"
  local value="$3"
  local tmp
  tmp="$(mktemp "${TMPDIR:-/tmp}/filmit-env.XXXXXX")"
  awk -v key="$key" -v value="$value" '
    BEGIN { updated = 0 }
    index($0, key "=") == 1 {
      print key "=" value
      updated = 1
      next
    }
    { print }
    END {
      if (!updated) {
        print key "=" value
      }
    }
  ' "$file" > "$tmp"
  mv "$tmp" "$file"
}

generate_secret_key() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 32
    return
  fi
  date +%s | shasum | awk '{print $1}'
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --up)
      SHOULD_START=1
      shift
      ;;
    --env-file)
      ENV_FILE="$(ensure_absolute_path "${2:?missing value for --env-file}")"
      shift 2
      ;;
    --generated-dir)
      GENERATED_DIR="$(ensure_absolute_path "${2:?missing value for --generated-dir}")"
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      printf 'Unknown argument: %s\n\n' "$1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [[ ! -f "$ENV_EXAMPLE" ]]; then
  printf 'Missing env template: %s\n' "$ENV_EXAMPLE" >&2
  exit 1
fi

mkdir -p "$(dirname "$ENV_FILE")"

if [[ ! -f "$ENV_FILE" ]]; then
  cp "$ENV_EXAMPLE" "$ENV_FILE"
  printf 'Created %s from .env.example\n' "$ENV_FILE"
fi

if [[ -z "$GENERATED_DIR" ]]; then
  current_generated_dir="$(read_env_value "$ENV_FILE" "N2V_GENERATED_DIR" || true)"
  case "$current_generated_dir" in
    ""|"/absolute/path/to/novel-to-video-demo-cases"|"/workspace/output/generated")
      GENERATED_DIR="$GENERATED_DIR_DEFAULT"
      ;;
    *)
      GENERATED_DIR="$(ensure_absolute_path "$current_generated_dir")"
      ;;
  esac
fi

mkdir -p "$GENERATED_DIR"
mkdir -p "${REPO_ROOT}/output/playwright"
mkdir -p "${REPO_ROOT}/.playwright-browsers"

write_env_value "$ENV_FILE" "N2V_GENERATED_DIR" "$GENERATED_DIR"

current_secret_key="$(read_env_value "$ENV_FILE" "N2V_SECRET_KEY" || true)"
case "$current_secret_key" in
  ""|"replace-me"|"change-me")
    write_env_value "$ENV_FILE" "N2V_SECRET_KEY" "$(generate_secret_key)"
    ;;
esac

printf 'Environment ready.\n'
printf '  env file: %s\n' "$ENV_FILE"
printf '  generated dir: %s\n' "$GENERATED_DIR"

if [[ "$SHOULD_START" -eq 1 ]]; then
  printf 'Starting docker compose...\n'
  (cd "$REPO_ROOT" && docker compose up -d --build)
  printf 'Web: http://localhost:3000\n'
  printf 'API: http://localhost:8000\n'
  exit 0
fi

cat <<EOF

Next:
  1. Edit ${ENV_FILE} if you want to add real model API keys.
  2. Run: docker compose up -d --build
EOF
