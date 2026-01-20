#!/bin/bash
set -e

IMAGE_NAME="btrfs-monitor-builder"
echo "Building Docker image..."
docker build -t $IMAGE_NAME -f Dockerfile.build .

echo "Building Debian package..."
docker run --rm -v $(pwd):/workspace $IMAGE_NAME
echo "Build complete. Package is in the dist directory"
ls -la dist

