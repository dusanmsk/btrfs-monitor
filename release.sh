#!/bin/bash
set -e

git diff-index --quiet HEAD || { echo "Error: Uncommitted changes detected. Please commit or stash them before proceeding."; exit 1; }

dch -i
dch -r

git add debian/changelog
git commit -m "Release version $VERSION"


rm dist/* || true
./build.sh

VERSION=`dpkg-parsechangelog -S Version`
DEB="dist/btrfs-monitor_${VERSION}_all.deb"
if [ -f "$DEB" ]; then
    echo "Debian package built: $DEB"
else
    echo "Error: Debian package not found!"
    exit 1
fi
gh release create v${VERSION} $DEB --title "Release of $VERSION"
git add debian/changelog
git commit -m "Release version $VERSION"
git tag v$VERSION
git push --tags

echo "Build complete. Package is in the dist directory and release is on github."

