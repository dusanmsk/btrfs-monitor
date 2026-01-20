# btrfs-monitor
BTRFS health monitoring daemon

Monitors BTRFS filesystems for errors and missing devices, sending notifications via email or Pushover.

## Features
- Monitors multiple BTRFS filesystems
- Sends email notifications on errors or missing devices
- Supports Pushover notifications
- Configurable monitoring intervals


## Requirements
- Python 3.6+
- BTRFS tools installed on the system
- Access to send emails or Pushover notifications

## Configuration
After installation, edit config file /etc/btrfs-monitor.yml to set up your monitoring preferences.
To test notifications, use /usr/bin/btrfs-monitor --test [--debug]. It will send a test notification based on your config.

