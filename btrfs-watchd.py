import json
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
log = logging.getLogger("btrfs_logwatch")

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

    # Set default values for mountpoints
    cfg.mountpoints = cfg.get('mountpoints')


# Regex pre zachytenie error alebo warn (case-insensitive)
pattern = re.compile(r"(error|warn)", re.IGNORECASE)

# cyclic buffer for kernel errors, cleared after each report (because we are watching kernel journal so no old messages come when restarted)
journal_errors = []

# map of device:error counter (sum of all errors of device)
failing_devices = {}

# devices that was already reported as failing
reported_failing_devices = []

# set of reported lines (to prevent repeated reporting of the same errors)
already_reported_stats_lines = set()

lock = threading.Lock()


class StateMachine:
    def __init__(self):
        self.missing_map = {}
        pass

    def updateMissingDevice(self, uuid, is_missing):
        old_missing = self.missing_map.get(uuid, False)
        if old_missing == False and is_missing == True:
            msg = f"Missing device detected for {uuid}"
            log.error(msg)
            sendNotification("Missing device", [msg])
            # todo report missing
        if old_missing == True and is_missing == False:
            msg=f"Missing device back to normal on {uuid}"
            log.info(msg)
            sendNotification("Missing device OK", [msg])

        self.missing_map[uuid] = is_missing # Update the state



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
    cmd = ["sudo", "journalctl", "-f", "-t", "kernel"]
    with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as proc:
        for line in proc.stdout:
            if "btrfs" in line.lower() and pattern.search(line):
                with lock:
                    journal_errors.append(line.strip())
                    log.error(f"BTRFS ERROR: {line.strip()}")

def watch_btrfs_stats():
    try:
        while True:
            # get and check device stats
            for mountpoint in list_mountpoints():
                stats_cmd = ["sudo", "btrfs", "--format", "json", "device", "stats", mountpoint]
                result = subprocess.run(stats_cmd, capture_output=True, text=True, check=True)
                stats_json = json.loads(result.stdout.strip())
                for device in stats_json['device-stats']:
                    device_name = device['device']
                    err_cnt = int(device['write_io_errs'] + device['read_io_errs'] + device['flush_io_errs'] + device['corruption_errs'] + device['generation_errs'])
                    failing_devices[device_name] = [err_cnt, mountpoint]
                    if err_cnt > 0:
                        log.error(f"Failing device warning: {device_name} at mountpoint {mountpoint} with {err_cnt} errors")
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
    try:
        while True:
            report_body = []
            for device in failing_devices:

                errcnt = failing_devices[device][0]
                mountpoint = failing_devices[device][1]
                if errcnt > 0:
                    if device not in reported_failing_devices:
                        report_body.append(f"Device with FAILURES: {device} at mountpoint {mountpoint} with {errcnt} errors")
                        reported_failing_devices.append(device)

            if journal_errors:
                log.debug(f"Journal errors detected, waiting {cfg.timing.journal_error_wait_sec} for more to come before reporting")
                time.sleep(cfg.timing.journal_error_wait_sec)        # if errors in journal raised, wait a whilew for others to come, then report
                report_body.append("\n\n------- Journal errors -------\n\n")
                report_body+=journal_errors
                journal_errors.clear()

            if report_body:
                sendNotification(f"BTRFS Errors detected on {platform.node()}", report_body)


            time.sleep(cfg.timing.monitor_sleep_sec)

    except Exception as e:
        log.exception("Failure in monitor_and_report loop")


def shorten(body_lines, limit):
    if len(body_lines) > limit:
        body = "\n".join(body_lines[-limit:])
        body.join("\n ... shortened ...")
    else:
        body = "\n".join(body_lines)
    return body


def sendEmailNotification(subject, body_lines):
    if not cfg.email.sender_email:
        log.debug("No email configured, noop")
        return
    body = shorten(body_lines, 1000)
    for address in cfg.email.receiver_email.split(","):
        address = address.strip()
        msg = EmailMessage()
        msg['Subject'] = subject
        msg['From'] = cfg.email.sender_email
        msg['To'] = address
        msg.set_content(body)

        try:
            with smtplib.SMTP(cfg.email.smtp_server, cfg.email.smtp_port) as smtp:
                #smtp.set_debuglevel(1)
                if cfg.email.sender_password is not None:
                    smtp.login(cfg.email.sender_email, cfg.email.sender_password)
                smtp.send_message(msg)
            log.debug(f"Email sent to {address}")
        except Exception as e:
            print(f"Chyba: {e}")


def send_pushover(subject, body_lines, priority=1):
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
        "priority": priority  # 1 je vysoká priorita, 0 je normálna
    }

    try:
        response = requests.post(url, data=data)
        response.raise_for_status()
        log.debug("Pushover notification sent")
    except requests.exceptions.RequestException as e:
        log.error(f"Failed to send pushover notification: {e}")


def sendNotification(subject, report_body_lines, priority=1):
    log.info("Sending following report:\n\n\t" + subject + "\n" + "\t" + "\n\t".join(report_body_lines))
    sendEmailNotification(subject, report_body_lines)
    send_pushover(subject, report_body_lines, priority)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("-c", "--config", default="/etc/btrfswatchd.yml", help="Path to config file")
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
