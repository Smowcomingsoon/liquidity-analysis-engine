#!/bin/sh
# Whitespace-only minification of our own JS for the Docker image (overhaul
# plan, phase 4). esbuild parses each file and only drops comments and
# whitespace: no renaming, no syntax lowering, imports untouched, so the
# served code is the repo code. Vendor files are already minified.
set -eu
DIR="$1"
mkdir -p /tmp/esb && cd /tmp/esb && npm install --no-save --silent esbuild@0.24.2 >/dev/null
cd - >/dev/null
ESBUILD=/tmp/esb/node_modules/.bin/esbuild
find "$DIR" -name '*.js' \
  ! -path '*/engn/motion/three/*' \
  ! -name 'lightweight-charts*' \
  ! -name 'codemirror.js' \
  ! -name '*.min.js' | while read -r f; do
  "$ESBUILD" "$f" --minify-whitespace --log-level=error --outfile="$f" --allow-overwrite
done
