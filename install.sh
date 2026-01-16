#!/bin/bash

# Install script for btrfs-monitor

if [ "$EUID" -ne 0 ]; then
  echo "Please run as root"
  exit 1
fi

INSTALL_DIR="/usr/bin"
CONFIG_DIR="/etc"
SYSTEMD_DIR="/etc/systemd/system"

echo "Installing btrfs-monitor..."

# Install the python script
cp btrfs-monitor.py "$INSTALL_DIR/btrfs-monitor"
chmod +x "$INSTALL_DIR/btrfs-monitor"

# Install config file if it doesn't exist
if [ ! -f "$CONFIG_DIR/btrfs-monitor.yml" ]; then
    if [ -f "config.yaml.example" ]; then
        cp config.yaml.example "$CONFIG_DIR/btrfs-monitor.yml"
    else
        echo "Warning: config.yaml.example not found, creating empty config"
        touch "$CONFIG_DIR/btrfs-monitor.yml"
    fi
else
    echo "Config file already exists at $CONFIG_DIR/btrfs-monitor.yml, skipping."
fi

# Install systemd service
cp btrfs-monitor.service "$SYSTEMD_DIR/"
systemctl daemon-reload
systemctl enable btrfs-monitor.service

echo "Installation complete. You can start the service with: systemctl start btrfs-monitor"
