#!/bin/zsh
# Publish a release to GitHub (maintainers).
#
#   ./scripts/release.sh            release the version in mosaic/__init__.py
#
# First time: creates the git repository and the public GitHub repository
# (kev-williams/mosaic-photo-editor), pushes, tags and creates the GitHub release.
# Later: commits any changes, tags and releases the new version (bump __version__ and
# CHANGELOG.md first).
# Needs the GitHub CLI, signed in:  brew install gh && gh auth login
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
GH_REPO="kev-williams/mosaic-photo-editor"
cd "$REPO"
VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' mosaic/__init__.py)
TAG="v$VERSION"

command -v gh >/dev/null || { echo "Install the GitHub CLI first: brew install gh && gh auth login"; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "Sign in to GitHub first: gh auth login"; exit 1; }

if [[ ! -d .git ]]; then
  git init -q -b main
  echo "Created a new git repository."
fi
git config core.hooksPath .githooks
# Commits in a public repository show the author's email. Use the private GitHub address
# (<id>+<login>@users.noreply.github.com) for this repository, unless one is already set here.
if [[ -z "$(git config --local user.email 2>/dev/null || true)" ]]; then
  GH_LOGIN=$(gh api user --jq .login)
  git config --local user.name "$(gh api user --jq '.name // .login')"
  git config --local user.email "$(gh api user --jq .id)+$GH_LOGIN@users.noreply.github.com"
  echo "Commits will use $(git config --local user.name) <$(git config --local user.email)>"
fi
chmod +x scripts/*.sh .githooks/pre-commit launcher.py

if git rev-parse -q --verify "refs/tags/$TAG" >/dev/null; then
  echo "$TAG already exists. Bump __version__ in mosaic/__init__.py and add a CHANGELOG.md entry first."
  exit 1
fi
grep -q "^## \[$VERSION\]" CHANGELOG.md || { echo "CHANGELOG.md has no entry for $VERSION."; exit 1; }

git add -A
echo ""
echo "Files in this release commit (everything else is ignored by .gitignore):"
git status --short
echo ""
scripts/check-privacy.sh
git ls-files --cached | grep -qiE '\.(jpe?g|png|webp|heic|tiff?|gif)$' && { echo "Image files are staged. Stopping."; exit 1; }
print -n "Publish $TAG of Mosaic Photo-Editor to github.com/$GH_REPO (public)? [y/N] "
read -r answer
[[ "$answer" == [yY]* ]] || { echo "Nothing published. Your files are staged; 'git reset' unstages them."; exit 0; }

if ! git diff --cached --quiet; then
  git commit -q -m "Mosaic Photo-Editor $VERSION"
fi
git tag -a "$TAG" -m "Mosaic Photo-Editor $VERSION"

if ! git remote get-url origin >/dev/null 2>&1; then
  if gh repo view "$GH_REPO" >/dev/null 2>&1; then
    if [[ "$(gh config get git_protocol 2>/dev/null || true)" == "ssh" ]]; then
      git remote add origin "git@github.com:$GH_REPO.git"
    else
      git remote add origin "https://github.com/$GH_REPO.git"
    fi
  else
    gh repo create "$GH_REPO" --public --source . --remote origin \
      --description "Convert, crop and size photos for websites and social media. Consistent headshots, AI background replacement, runs on your Mac." \
      --homepage "https://github.com/$GH_REPO"
  fi
fi
git push -u origin main
git push origin "$TAG"

NOTES=$(awk -v v="$VERSION" '$0 ~ "^## \\[" v "\\]" {f=1; next} /^## \[/ {f=0} f' CHANGELOG.md)
gh release create "$TAG" --repo "$GH_REPO" --title "Mosaic Photo-Editor $VERSION" --notes "$NOTES"
gh repo edit "$GH_REPO" --add-topic photo-editor --add-topic headshots --add-topic image-processing \
  --add-topic webp --add-topic streamlit --add-topic macos --add-topic background-removal >/dev/null 2>&1 || true
echo "Published: https://github.com/$GH_REPO/releases/tag/$TAG"
