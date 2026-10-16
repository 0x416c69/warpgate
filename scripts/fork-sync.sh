#!/usr/bin/env bash
# Moves this fork onto another upstream Warpgate release by replaying the
# fork's own commits (everything after the upstream release tag the branch is
# based on) on top of that release.
#
#   scripts/fork-sync.sh            rebase onto the latest stable upstream release
#   scripts/fork-sync.sh v0.30.1    rebase onto a specific release
#   scripts/fork-sync.sh --info     print the current base release and the latest one
#
# On conflicts the rebase is left in progress: resolve, `git rebase --continue`.
# Then build/test and publish with `git push --force-with-lease origin main`.
set -euo pipefail

upstream="${WARPGATE_UPSTREAM:-https://github.com/warp-tech/warpgate.git}"

stable_tags() {
    git tag --list 'v*' | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | sort -V
}

git fetch --quiet --no-tags "$upstream" '+refs/tags/v*:refs/tags/v*'

latest=$(stable_tags | tail -n 1)
base=
for tag in $(stable_tags | sort -rV); do
    if git merge-base --is-ancestor "$tag" HEAD; then
        base=$tag
        break
    fi
done
if [ -z "$base" ]; then
    echo "HEAD doesn't contain any upstream release tag" >&2
    exit 1
fi

if [ "${1:-}" = "--info" ]; then
    echo "base=$base"
    echo "latest=$latest"
    exit 0
fi

target="${1:-$latest}"
if ! git rev-parse --verify --quiet "refs/tags/$target^{commit}" >/dev/null; then
    echo "Unknown upstream tag: $target" >&2
    exit 1
fi

if [ "$target" = "$base" ]; then
    echo "Already based on $base"
    exit 0
fi

echo "Replaying $(git rev-list --count "$base..HEAD") commit(s) from $base onto $target"
git rebase --onto "$target" "$base"
echo "Now based on $target. Build/test, then: git push --force-with-lease origin main"
