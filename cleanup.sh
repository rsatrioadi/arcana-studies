#!/usr/bin/env bash
set -euo pipefail

WORK_DIR=$(pwd)
TMP_DIR=$(mktemp -d)

# Step 1: move all nested x.json/x.json files to temp dir
find . -type f -name '*.json' | while read -r file; do
    dir=$(dirname "$file")
    base=$(basename "$dir")
    if [[ "$base" == "$(basename "$file")" ]]; then
        echo "Collecting: $file"
        mv "$file" "$TMP_DIR/$base"
    fi
done

# Step 2: remove the now-empty misnamed directories
find . -type d -name '*.json' -empty | while read -r dir; do
    echo "Removing dir: $dir"
    rmdir "$dir"
done

# Step 3: move everything from temp dir back
mv "$TMP_DIR"/*.json "$WORK_DIR/"
rmdir "$TMP_DIR"
