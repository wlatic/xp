#!/usr/bin/env bash
# Update the Linux launcher while retaining its saved credentials and cache.
set -euo pipefail

xp_update_dir=$(mktemp -d)
trap 'rm -rf -- "$xp_update_dir"' EXIT
xp_revision=${XP_REVISION:-refs/heads/feat/termix-offline}
curl -fsSL "https://github.com/wlatic/xp/archive/${xp_revision}.tar.gz" \
    -o "$xp_update_dir/xp.tar.gz"
mkdir "$xp_update_dir/source"
tar -xzf "$xp_update_dir/xp.tar.gz" -C "$xp_update_dir/source" --strip-components=1
python3 "$xp_update_dir/source/termix/install.py" --activate
