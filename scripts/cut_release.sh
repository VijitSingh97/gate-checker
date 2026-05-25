#!/bin/bash
# Cut a new release of LoRa Ranch Sentinel.
#
# Walks through: verifying the production images in releases/, computing
# a SHA256SUMS file, creating a local git tag, and uploading a *draft*
# GitHub release with the images + checksums attached. The release
# stays as a draft on github.com until you click "Publish" — nothing
# is visible to the public until then.
#
# Usage:
#   scripts/cut_release.sh <tag> [options]
#
# Options:
#   --skip-verify     Skip scripts/verify_image.sh on each image.
#                     Only use if you already ran it manually.
#   --include-dev     Also attach the *_dev.img files. By default
#                     they're held back — dev images contain a known
#                     root password and should not be publicly hosted.
#   --allow-dirty     Allow a non-clean working tree. Off by default
#                     so a release always pins to a known commit.
#   --dry-run         Show what would happen; don't tag, don't upload.
#   --notes-file FILE Path to a markdown file used as the release body.
#                     If omitted, uses `gh --generate-notes` from the
#                     commits since the previous tag.
#
# Examples:
#   scripts/cut_release.sh v0.1.0
#   scripts/cut_release.sh v0.2.0 --dry-run
#   scripts/cut_release.sh v1.0.0 --notes-file CHANGELOG-v1.0.0.md
#
# Prerequisites:
#   - The `gh` CLI installed and authenticated (`gh auth status` returns ok)
#   - Production images already built and present in releases/
#   - On a commit you want the tag to point at (the current HEAD)

set -euo pipefail

# --------------------------------------------------------------------
# Argument parsing
# --------------------------------------------------------------------

TAG=""
SKIP_VERIFY=false
INCLUDE_DEV=false
ALLOW_DIRTY=false
DRY_RUN=false
NOTES_FILE=""

usage() {
    sed -n '2,28p' "$0" | sed 's/^# \{0,1\}//'
    exit "${1:-2}"
}

if [[ $# -eq 0 ]]; then
    usage 2
fi

while [[ $# -gt 0 ]]; do
    case "$1" in
        --skip-verify)   SKIP_VERIFY=true; shift ;;
        --include-dev)   INCLUDE_DEV=true; shift ;;
        --allow-dirty)   ALLOW_DIRTY=true; shift ;;
        --dry-run)       DRY_RUN=true; shift ;;
        --notes-file)    NOTES_FILE="${2:-}"; shift 2 ;;
        -h|--help)       usage 0 ;;
        v*|V*)           TAG="$1"; shift ;;
        *)
            echo "Unknown argument: $1" >&2
            usage 2
            ;;
    esac
done

if [[ -z "$TAG" ]]; then
    echo "Missing tag (e.g. v0.1.0)." >&2
    usage 2
fi

# Loose SemVer-ish format check. We don't enforce v<major>.<minor>.<patch>
# strictly — pre-release suffixes (-rc1, -beta) are useful — but bare
# numbers or freeform strings hurt the badge sort order.
if [[ ! "$TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+(-[a-zA-Z0-9.+-]+)?$ ]]; then
    echo "Tag '$TAG' doesn't look like SemVer (vMAJOR.MINOR.PATCH[-prerelease])." >&2
    echo "Examples: v0.1.0, v1.0.0, v1.0.0-rc1" >&2
    exit 2
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RELEASES_DIR="$REPO_ROOT/releases"
SUMS_FILE="$RELEASES_DIR/SHA256SUMS"

cd "$REPO_ROOT"

# --------------------------------------------------------------------
# Pre-flight checks
# --------------------------------------------------------------------

require() {
    if ! command -v "$1" >/dev/null 2>&1; then
        echo "Missing required tool: $1" >&2
        echo "$2" >&2
        exit 1
    fi
}

require git  "Install git and re-run."
require shasum "Install coreutils (provides shasum) and re-run."
if [[ "$DRY_RUN" == false ]]; then
    require gh "Install the GitHub CLI: https://cli.github.com/"
    if ! gh auth status >/dev/null 2>&1; then
        echo "gh is installed but not authenticated. Run 'gh auth login'." >&2
        exit 1
    fi
fi

if [[ ! -d "$RELEASES_DIR" ]]; then
    echo "No $RELEASES_DIR directory. Build images first (see docs/BUILDING.md)." >&2
    exit 1
fi

# Refuse on dirty tree unless explicitly opted in. A release tag pins to
# a commit, and a dirty tree means whatever's on the filesystem isn't
# fully captured by that commit — the tag would be misleading.
if [[ "$ALLOW_DIRTY" == false ]] && [[ -n "$(git status --porcelain)" ]]; then
    echo "Working tree is dirty. Commit or stash, or re-run with --allow-dirty." >&2
    git status --short >&2
    exit 1
fi

# Refuse if the tag already exists locally or on origin. Re-tagging is
# rarely what you want and is destructive on a published release.
if git rev-parse --verify --quiet "refs/tags/$TAG" >/dev/null; then
    echo "Tag $TAG already exists locally. Delete it manually if this was a mistake:" >&2
    echo "  git tag -d $TAG" >&2
    exit 1
fi
if git ls-remote --tags origin "refs/tags/$TAG" 2>/dev/null | grep -q .; then
    echo "Tag $TAG already exists on origin. Pick a higher version number." >&2
    exit 1
fi

# --------------------------------------------------------------------
# Image discovery
# --------------------------------------------------------------------

IMAGES=()
while IFS= read -r -d '' f; do
    IMAGES+=("$f")
done < <(find "$RELEASES_DIR" -maxdepth 1 -type f -name "*.img" -print0 | sort -z)

if [[ ${#IMAGES[@]} -eq 0 ]]; then
    echo "No .img files in $RELEASES_DIR. Build first." >&2
    exit 1
fi

ASSETS=()
for img in "${IMAGES[@]}"; do
    base="$(basename "$img")"
    if [[ "$base" == *_dev.img ]] && [[ "$INCLUDE_DEV" == false ]]; then
        echo "Skipping dev image (use --include-dev to publish): $base"
        continue
    fi
    ASSETS+=("$img")
done

if [[ ${#ASSETS[@]} -eq 0 ]]; then
    echo "No production images to publish. (All images had _dev suffix and " \
         "--include-dev was not set.)" >&2
    exit 1
fi

# --------------------------------------------------------------------
# Verify each image
# --------------------------------------------------------------------

if [[ "$SKIP_VERIFY" == false ]]; then
    echo
    echo "Running verify_image.sh on each asset..."
    for img in "${ASSETS[@]}"; do
        echo
        echo "--- verify_image.sh $(basename "$img") ---"
        if ! "$REPO_ROOT/scripts/verify_image.sh" "$img"; then
            echo "verify_image.sh failed on $(basename "$img"). Aborting." >&2
            exit 1
        fi
    done
fi

# --------------------------------------------------------------------
# Compute SHA256SUMS
# --------------------------------------------------------------------

echo
echo "Computing SHA256SUMS..."
# Use relative paths inside the sums file so `shasum -c SHA256SUMS`
# works from any directory after download.
(
    cd "$RELEASES_DIR"
    : > "$SUMS_FILE"
    for img in "${ASSETS[@]}"; do
        shasum -a 256 "$(basename "$img")" >> "$SUMS_FILE"
    done
)
echo
cat "$SUMS_FILE"

ASSETS+=("$SUMS_FILE")

# --------------------------------------------------------------------
# Summary + confirmation
# --------------------------------------------------------------------

COMMIT_SHA=$(git rev-parse HEAD)
COMMIT_SHORT=$(git rev-parse --short HEAD)
PREV_TAG=$(git tag --sort=-v:refname | head -1 || true)

echo
echo "================================================================"
echo " Release plan"
echo "================================================================"
echo " Tag:           $TAG"
echo " Commit:        $COMMIT_SHORT ($COMMIT_SHA)"
echo " Previous tag:  ${PREV_TAG:-<none>}"
echo " Assets:"
for f in "${ASSETS[@]}"; do
    size=$(du -h "$f" | cut -f1)
    echo "   - $(basename "$f")  ($size)"
done
echo " Notes:         ${NOTES_FILE:-<auto-generated from commits>}"
echo " Mode:          $([[ "$DRY_RUN" == true ]] && echo "DRY RUN" || echo "DRAFT release on GitHub")"
echo "================================================================"

if [[ "$DRY_RUN" == true ]]; then
    echo
    echo "Dry run complete. Re-run without --dry-run to actually cut the release."
    exit 0
fi

read -r -p "Proceed? [y/N] " reply
if [[ ! "$reply" =~ ^[Yy]$ ]]; then
    echo "Aborted. No tag created, no release published."
    exit 0
fi

# --------------------------------------------------------------------
# Tag + release
# --------------------------------------------------------------------

git tag -a "$TAG" -m "Release $TAG"
echo "Created local tag $TAG."

# Push the tag first — `gh release create` needs the tag to exist on
# the remote. Pushing only the one tag keeps the operation scoped.
git push origin "refs/tags/$TAG"
echo "Pushed tag $TAG to origin."

GH_ARGS=(release create "$TAG"
    --draft
    --title "$TAG"
    --target "$COMMIT_SHA"
)
if [[ -n "$NOTES_FILE" ]]; then
    if [[ ! -f "$NOTES_FILE" ]]; then
        echo "Notes file not found: $NOTES_FILE" >&2
        exit 1
    fi
    GH_ARGS+=(--notes-file "$NOTES_FILE")
else
    GH_ARGS+=(--generate-notes)
fi
GH_ARGS+=("${ASSETS[@]}")

gh "${GH_ARGS[@]}"

echo
echo "Draft release $TAG created. Review and publish at:"
gh release view "$TAG" --json url -q .url
