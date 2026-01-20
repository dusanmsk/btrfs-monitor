#!/usr/bin/env python3

import json
import ssl
import glob
import os
import platform
import smtplib
import requests
import subprocess
import re
import threading
import time
import logging
import psutil
from email.message import EmailMessage
import yaml
from box import Box
import argparse

# Global configuration object
cfg = Box(default_box=True)

# todo loglevel
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger("btrfs_monitor")

def load_config(config_path):
    global cfg
    # Load configuration from YAML file
    try:
        with open(config_path, 'r') as f:
            cfg = Box(yaml.safe_load(f), default_box=True)
            log.info(f"Loaded configuration from {config_path}")
    except FileNotFoundError:
        log.error(f"Configuration file {config_path} not found.")
        exit(1)

    # Set default values for email configuration
    cfg.email.smtp_server = cfg.email.get('smtp_server', None)
    cfg.email.smtp_port = cfg.email.get('smtp_port', None)
    cfg.email.sender_email = cfg.email.get('sender_email', None)
    cfg.email.sender_password = cfg.email.get('sender_password', None)
    cfg.email.receiver_email = cfg.email.get('receiver_email', None)

    # Set default values for timing constants
    cfg.timing.stats_sleep_sec = cfg.timing.get('stats_sleep_sec', 600)
    cfg.timing.monitor_sleep_sec = cfg.timing.get('monitor_sleep_sec', 600)
    cfg.timing.journal_error_wait_sec = cfg.timing.get('journal_error_wait_sec', 60)
    cfg.timing.error_debounce_sec = cfg.timing.get('error_debounce_sec', 3600)

    # Set default values for mountpoints
    cfg.mountpoints = cfg.get('mountpoints')


# Regex pre zachytenie error alebo warn (case-insensitive)
pattern = re.compile(r"(error|warn)", re.IGNORECASE)

# cyclic buffer for kernel errors, cleared after each report (because we are watching kernel journal so no old messages come when restarted)
journal_errors = []


class StateMachine:
    def __init__(self):
        self.missing_map = {}
        self.error_count = {}
        self.last_error_notification = {}
        self.current_debounce = {}

    def updateMissingDevice(self, uuid, is_missing):
        old_missing = self.missing_map.get(uuid, False)
        if old_missing == False and is_missing == True:
            msg = f"Missing device detected for {uuid}"
            log.error(msg)
            sendNotification("Missing device", [msg])
        elif old_missing == True and is_missing == False:
            msg=f"Missing device back to normal on {uuid}"
            log.info(msg)
            sendNotification("Missing device OK", [msg])
        self.missing_map[uuid] = is_missing # Update the state


    # update count of errors per mountpoint. Handles sending notification if number of errors has increased, debouncing when it is increasing
    # continuously (to prevent spamming), reports when errors get back to zero. Maximum debounce time is 24 hours (so it will always sent 1 notification
    # per day in case of error)
    def updateErrorCount(self, mountpoint, err_cnt):
        last_err_cnt = self.error_count.get(mountpoint, 0)
        
        if last_err_cnt < err_cnt:
            current_time = time.time()
            last_notif_time = self.last_error_notification.get(mountpoint, 0)
            current_delay = self.current_debounce.get(mountpoint, 0)
            
            if (current_time - last_notif_time) > current_delay:
                sendNotification(f"BTRFS errors", f"BTRFS error count increased on {mountpoint} to {err_cnt}\nRun sudo btrfs device stats {mountpoint} to check.")
                self.last_error_notification[mountpoint] = current_time
                
                if current_delay == 0:
                    new_delay = cfg.timing.error_debounce_sec
                else:
                    new_delay = current_delay * 2
                
                self.current_debounce[mountpoint] = min(new_delay, 86400)       # max debounce 24h
            else:
                log.debug(f"Error count increased on {mountpoint} to {err_cnt}, notification suppressed (debounce active, wait {current_delay}s)")
        elif last_err_cnt > 0 and err_cnt == 0:
            sendNotification(f"BTRFS errors back to normal", f"Error count back to normal on {mountpoint}\nRun sudo btrfs device stats {mountpoint} to check.")
            if mountpoint in self.current_debounce:
                del self.current_debounce[mountpoint]
        
        self.error_count[mountpoint] = err_cnt

state_machine = StateMachine()

# return mountpoints from configuration, or use autodetect if not specified
def list_mountpoints():
    if cfg.mountpoints:
        return cfg.mountpoints
    else:
        btrfs_mountpoints = []
        for part in psutil.disk_partitions(all=True):
            if part.fstype == 'btrfs':
                btrfs_mountpoints.append(part.mountpoint)
        if not btrfs_mountpoints:
            log.warning("No BTRFS mountpoints found and none specified in config.yaml. Monitoring will not work.")
        return btrfs_mountpoints

def watch_journal():
    cmd = ["journalctl", "-f", "-t", "kernel"]
    with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as proc:
        for line in proc.stdout:
            if "btrfs" in line.lower() and pattern.search(line):
                journal_errors.append(line.strip())
                log.error(f"BTRFS ERROR: {line.strip()}")

def watch_btrfs_stats():
    try:
        while True:
            # get and check device stats
            for mountpoint in list_mountpoints():
                stats_cmd = ["btrfs", "--format", "json", "device", "stats", mountpoint]
                result = subprocess.run(stats_cmd, capture_output=True, text=True, check=True)
                stats_json = json.loads(result.stdout.strip())
                err_cnt = 0
                for device in stats_json['device-stats']:
                    device_name = device['device']
                    err_cnt += int(device['write_io_errs'] + device['read_io_errs'] + device['flush_io_errs'] + device['corruption_errs'] + device['generation_errs'])
                state_machine.updateErrorCount(mountpoint, err_cnt)
            # check for missing devices (degraded arrays)
            btrfs_sys_path = "/sys/fs/btrfs/"
            for fs_uuid_path in glob.glob(os.path.join(btrfs_sys_path, "*")):
                missing_count = 0
                if os.path.isdir(fs_uuid_path):
                    fs_uuid = os.path.basename(fs_uuid_path)
                    for devinfo_path in glob.glob(os.path.join(fs_uuid_path, "devinfo", "*")):
                        if os.path.isdir(devinfo_path):
                            device_name = os.path.basename(devinfo_path)
                            missing_file_path = os.path.join(devinfo_path, "missing")
                            if os.path.exists(missing_file_path):
                                with open(missing_file_path, 'r') as f:
                                    if f.read().strip() == '1':
                                        msg=f"Missing BTRFS device detected: UUID={fs_uuid}, Device={device_name}"
                                        log.warning(msg)
                                        missing_count+=1
                state_machine.updateMissingDevice(fs_uuid, True if missing_count > 0 else False)
            time.sleep(cfg.timing.stats_sleep_sec)
    except Exception as e:
        log.error("Failure during btrfs stats monitoring", e)
        time.sleep(cfg.timing.stats_sleep_sec)

def monitor_and_report():
    last_journal_notif_time = 0
    current_journal_debounce = 0
    try:
        while True:
            report_body = []

            if journal_errors:
                current_time = time.time()
                if (current_time - last_journal_notif_time) > current_journal_debounce:
                    log.debug(f"Journal errors detected, waiting {cfg.timing.journal_error_wait_sec} for more to come before reporting")
                    time.sleep(cfg.timing.journal_error_wait_sec)        # if errors in journal raised, wait a while for others to come, then report
                    report_body.append("\n\n------- Journal errors -------\n\n")
                    report_body.append("Check with sudo journalctl -t kernel | grep -i btrfs\n\n")
                    report_body += journal_errors
                    journal_errors.clear()
                    last_journal_notif_time = current_time
                    if current_journal_debounce == 0:
                        current_journal_debounce = cfg.timing.error_debounce_sec
                    else:
                        current_journal_debounce = min(current_journal_debounce * 2, 86400)
                else:
                    log.debug(f"Journal errors detected, notification suppressed (debounce active, wait {current_journal_debounce}s)")
                    # Errors remain in journal_errors and will be reported after debounce expires

            if report_body:
                sendNotification(f"BTRFS kernel errors detected", report_body)


            time.sleep(cfg.timing.monitor_sleep_sec)

    except Exception as e:
        log.exception("Failure in monitor_and_report loop")


def shorten(body_lines, limit):
    if len(body_lines) > limit:
        body = "\n".join(body_lines[-limit:])
        body.join(f"\n ... shortened, {len(body_lines) - limit} lines following ...")
    else:
        body = "\n".join(body_lines)
    return body


def sendEmailNotification(subject, body_lines):
    if not cfg.email.recipients:
        log.debug("No email configured, noop")
        return
    body = shorten(body_lines, 1000)
    for address in cfg.email.recipients.split(","):
        address = address.strip()
        msg = EmailMessage()
        msg['Subject'] = subject
        msg['From'] = cfg.email.sender_email
        msg['To'] = address
        msg.set_content(body)

        # Prepare SSL context (verifies certificates, uses modern TLS)
        context = ssl.create_default_context()
        if cfg.email.ignore_ssl_errors:
            log.warning("Configured to skip ssl checks on smtp")
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE

        # Decide which class to use based on the port
        # Port 465 requires SSL from the beginning (Implicit SSL)
        if cfg.email.smtp_port == 465:
            smtp_class = smtplib.SMTP_SSL
            smtp_kwargs = {"context": context}
        else:
            # Port 587 or 25 start as Plaintext and can be upgraded via STARTTLS
            smtp_class = smtplib.SMTP
            smtp_kwargs = {}

        try:
            with smtp_class(cfg.email.smtp_server, cfg.email.smtp_port, **smtp_kwargs) as smtp:
                #smtp.set_debuglevel(1) # Enable for deep debugging

                # If we aren't using SSL from the start, try STARTTLS
                if cfg.email.smtp_port != 465:
                    smtp.ehlo()  # Identify to the server
                    if smtp.has_extn("STARTTLS"):
                        log.debug("Server supports STARTTLS, upgrading to encrypted connection.")
                        smtp.starttls(context=context)
                        smtp.ehlo()  # Re-identify after encryption is active
                    else:
                        log.debug("Server does not support STARTTLS. Proceeding in Plaintext.")

                # Login if a password is provided in config
                if cfg.email.sender_password:
                    smtp.login(cfg.email.smtp_login, cfg.email.smtp_password)

                smtp.send_message(msg)
                log.info(f"Email successfully sent to {address}")
        except Exception as e:
            log.error(f"Error: {e}")


def sendPushoverNotification(subject, body_lines, priority=0):
    if not cfg.pushover.user_key:
        log.debug("No pushover configuration, noop")
        return
    body = shorten(body_lines, 50)
    url = "https://api.pushover.net/1/messages.json"
    data = {
        "token": cfg.pushover.application_key,
        "user": cfg.pushover.user_key,
        "message": body,
        "title": subject,
        "priority": priority  # 1 high, 0 normal
    }

    try:
        response = requests.post(url, data=data)
        response.raise_for_status()
        log.debug("Pushover notification sent")
    except requests.exceptions.RequestException as e:
        log.error(f"Failed to send pushover notification: {e}")


def sendNotification(subject, report_body_lines, priority=1):
    if not isinstance(report_body_lines, list):
        report_body_lines = [report_body_lines]
    hostname=platform.node()
    if not hostname in subject:
        subject = f"{subject} at {hostname}"
    log.info("Sending following report:\n\n\t" + subject + "\n" + "\t" + "\n\t".join(report_body_lines))
    sendEmailNotification(subject, report_body_lines)
    sendPushoverNotification(subject, report_body_lines, priority)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("-c", "--config", default="/etc/btrfs-monitor.yml", help="Path to config file")
    args = parser.parse_args()

    load_config(args.config)

    log.info("Starting to monitor BTRFS filesystems: " + ", ".join(list_mountpoints()))

    t1 = threading.Thread(target=watch_journal, daemon=True)
    t2 = threading.Thread(target=watch_btrfs_stats, daemon=True)
    t3 = threading.Thread(target=monitor_and_report, daemon=True)
    t1.start()
    t2.start()
    t3.start()
    t1.join()
    t2.join()
    t3.join()
