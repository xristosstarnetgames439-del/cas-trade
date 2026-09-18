#!/usr/bin/env bash
# 把通达信协议库 eltdx 克隆到 apps/api/vendor/eltdx（不提交 git）。
# 用法：在 apps/api 或仓库根目录执行 ./scripts/fetch-eltdx.sh
set -euo pipefail

ELTDX_REPO="${ELTDX_REPO:-https://github.com/electkismet/eltdx.git}"
ELTDX_REF="${ELTDX_REF:-v2.0.5}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
API_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VENDOR_DIR="$API_ROOT/vendor/eltdx"
LOCAL_FALLBACK="$(cd "$API_ROOT/../../.." && pwd)/eltdx"

clone_from_git() {
  git clone --depth 1 --branch "$ELTDX_REF" "$ELTDX_REPO" "$VENDOR_DIR"
}

copy_local() {
  if [ ! -f "$LOCAL_FALLBACK/pyproject.toml" ]; then
    return 1
  fi
  mkdir -p "$VENDOR_DIR"
  git clone --depth 1 "$LOCAL_FALLBACK" "$VENDOR_DIR"
}

if [ -f "$VENDOR_DIR/pyproject.toml" ]; then
  echo "eltdx already present: $VENDOR_DIR"
  exit 0
fi

rm -rf "$VENDOR_DIR"
mkdir -p "$(dirname "$VENDOR_DIR")"

if clone_from_git; then
  echo "cloned $ELTDX_REPO@$ELTDX_REF -> $VENDOR_DIR"
  exit 0
fi

echo "git clone from GitHub failed, trying local checkout $LOCAL_FALLBACK"
if copy_local; then
  echo "copied local eltdx -> $VENDOR_DIR"
  exit 0
fi

echo "failed to fetch eltdx into $VENDOR_DIR" >&2
exit 1
