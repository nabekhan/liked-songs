#!/usr/bin/env bash
# Store only sync ownership/checkpoint data; never switch or push the main branch.
set -euo pipefail
state_file="${1:?State file required}"
parent_file="${LISTENBRAINZ_STATE_PARENT_FILE:-.cache/listenbrainz-state-parent}"
branch='listenbrainz-sync-state'
parent_args=()
if [[ -s "$parent_file" ]]; then
  parent_args=(-p "$(cat "$parent_file")")
fi
blob=$(git hash-object -w "$state_file")
tree=$(printf '100644 blob %s\tstate.json\n' "$blob" | git mktree)
commit=$(printf 'Checkpoint ListenBrainz collection sync\n' | \
  git -c user.name='github-actions[bot]' \
      -c user.email='41898282+github-actions[bot]@users.noreply.github.com' \
      commit-tree "$tree" "${parent_args[@]}")
# A competing update fails instead of being overwritten. Initial ownership is
# published before API writes, so even a killed runner cannot orphan new loves.
git push origin "$commit:refs/heads/$branch"
printf '%s\n' "$commit" > "$parent_file"
