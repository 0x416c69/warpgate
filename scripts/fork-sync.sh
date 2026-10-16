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
# The fork deletes upstream's other workflows (publishing, release builds and
# checks for parts it doesn't change).
kept_workflows=(fork-image.yml test.yml clippy.yml)

stable_tags() {
    git tag --list 'v*' | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | sort -V
}

rebase_in_progress() {
    [ -d "$(git rev-parse --git-path rebase-merge)" ] || [ -d "$(git rev-parse --git-path rebase-apply)" ]
}

# A release that edits a workflow the fork deleted stops the rebase with a
# modify/delete conflict. Keep those deleted; fail on any other conflict.
drop_deleted_workflows() {
    local line paths=()
    while IFS= read -r line; do
        case "$line" in
            "UD .github/workflows/"* | "DU .github/workflows/"*) paths+=("${line:3}") ;;
            *) return 1 ;;
        esac
    done < <(git status --porcelain | grep -E '^(DD|AU|UD|UA|DU|AA|UU) ')
    [ ${#paths[@]} -gt 0 ] || return 1
    git rm --quiet -- "${paths[@]}"
    echo "Kept deleted: ${paths[*]}"
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
if ! git rebase --onto "$target" "$base"; then
    while rebase_in_progress && drop_deleted_workflows; do
        if GIT_EDITOR=true git rebase --continue; then
            break
        fi
    done
    if rebase_in_progress; then
        echo "Resolve the conflicts above, then: git rebase --continue" >&2
        exit 1
    fi
fi

for workflow in .github/workflows/*; do
    name=${workflow##*/}
    if [[ " ${kept_workflows[*]} " != *" $name "* ]]; then
        echo "Note: $target adds workflow $name; delete it if it doesn't apply to the fork"
    fi
done
echo "Now based on $target. Build/test, then: git push --force-with-lease origin main"
