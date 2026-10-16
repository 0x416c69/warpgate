# Maintaining the fork

`main` is always an upstream Warpgate release tag plus this fork's commits.
`scripts/fork-sync.sh` replays those commits onto another release, and the
**Fork image** workflow (`.github/workflows/fork-image.yml`) builds and
publishes `ghcr.io/0x416c69/warpgate`.

## Following upstream releases

- Every day the workflow checks for a new upstream stable release. If the
  fork's commits apply to it cleanly, it publishes the image for that release
  (for example `:0.30.1` and `:latest`). If they don't, the run fails and its
  summary names the release to port to.
- To move `main` itself to a new release, run `scripts/fork-sync.sh` (latest
  stable) or `scripts/fork-sync.sh v0.30.1`, fix any conflicts, test, then
  `git push --force-with-lease origin main`.
- To build a specific release by hand: Actions → Fork image → Run workflow,
  with the upstream tag as input.
- Don't press GitHub's "Sync fork" button on `main`. It compares against
  upstream's development branch and offers to discard this fork's commits.

## Workflows

- The fork keeps three workflows: **Fork image**, **Test** (unit and end-to-end
  tests) and **Clippy** (pull requests). Upstream's others are deleted: they
  publish to upstream's registry, Helm repo and releases, or check parts of
  the code the fork doesn't change.
- When a release edits one of the deleted workflows, `scripts/fork-sync.sh`
  keeps it deleted. When a release adds a new workflow, the script names it
  so you can decide whether to delete it too.

## Repository settings

- GitHub pauses scheduled workflows in repositories with no commits for 60
  days. If that happens, re-enable **Fork image** in the Actions tab.
- The `warpgate` package (Profile → Packages) needs to be public for servers
  to pull it anonymously. Otherwise run `docker login ghcr.io` on the server
  with a token that has `read:packages`.
