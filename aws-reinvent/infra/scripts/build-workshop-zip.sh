#!/usr/bin/env bash
# ==============================================================================
# build-workshop-zip.sh — build the workshop.zip artifact from aws-reinvent/workshop/.
#
# The zip is placed in aws-reinvent/infra/assets/workshop.zip, where deploy-stack.sh
# expects to find it. Treat this as a prerequisite step before running deploy-stack.sh
# whenever workshop content has changed (notebooks, helpers, logos, etc.).
#
# The zip is NOT committed to git — assets/workshop.zip is gitignored. For
# Workshop Studio deployments where this script isn't run, hand a pre-built
# zip to Workshop Studio automation at the expected S3 key.
#
# Layout: layer folders land at the archive root (l1-data-knowledge/, …) so
# the CFN lifecycle config can unzip it into
# /home/sagemaker-user/pluggable-agentic-ai-framework/ with no path shim.
#
# Usage:
#   ./scripts/build-workshop-zip.sh
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CFN_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
WORKSHOP_SRC="$(cd "$CFN_DIR/../workshop" && pwd)"
ASSETS_DIR="$CFN_DIR/assets"
WORKSHOP_ZIP="$ASSETS_DIR/workshop.zip"

log()  { printf "\033[0;36m==>\033[0m %s\n" "$*"; }
ok()   { printf "\033[0;32m✓\033[0m   %s\n" "$*"; }
die()  { printf "\033[0;31m✗\033[0m   %s\n" "$*" >&2; exit 1; }

command -v zip >/dev/null || die "zip CLI not on PATH"
[[ -d "$WORKSHOP_SRC" ]] || die "Workshop source not found: $WORKSHOP_SRC"

log "Building workshop.zip from $WORKSHOP_SRC"
mkdir -p "$ASSETS_DIR"
rm -f "$WORKSHOP_ZIP"

# Exclude macOS metadata, Python caches, and Jupyter checkpoint files so the
# zip is reproducible from a clean worktree.
(
  cd "$WORKSHOP_SRC"
  zip -qr "$WORKSHOP_ZIP" . \
    -x "*/.DS_Store" \
    -x "*/__pycache__/*" \
    -x "*/.ipynb_checkpoints/*" \
    -x "*.pyc" \
    -x "*~"
)

ok "Built $WORKSHOP_ZIP ($(ls -lh "$WORKSHOP_ZIP" | awk '{print $5}'))"
