#!/bin/bash
set -e

IMAGE_NAME="btrfs-monitor-builder"
echo "Building Docker image..."
docker build -t $IMAGE_NAME -f Dockerfile.build .

echo "Building Debian package..."
rm dist/* || true
docker run --rm -v $(pwd):/workspace $IMAGE_NAME

# Predpokladajme, že váš súbor sa volá btrfs-logwatch_1.0_amd64.deb
VERSION=`dpkg-parsechangelog -S Version`
gh release create v$VERSION dist/btrfs-monitor_${VERSION}_all.deb --title "Release of $VERSION"
git tag v$VERSION
git push --tags

echo "Build complete. Package is in the dist directory and release is on github."

