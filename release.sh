#!/bin/bash
set -e

dch -i
dch -r

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

