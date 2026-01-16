#!/bin/bash
set -e

# Define the image name
IMAGE_NAME="btrfs-monitor-builder"

# Build the Docker image
echo "Building Docker image..."
docker build -t $IMAGE_NAME -f Dockerfile.build .

# Run the container to build the package
echo "Building Debian package..."
docker run --rm -v $(pwd):/workspace $IMAGE_NAME

echo "Build complete. Package should be in the current directory."
