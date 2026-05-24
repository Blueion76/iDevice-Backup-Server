import subprocess
import os
import shutil
import logging
import plistlib
import time
import re
import shlex

logger = logging.getLogger(__name__)

OPENSSL_WEAK_TEMPLATE = """\
.include /etc/ssl/openssl.cnf
[openssl_init]
alg_section = evp_properties
[evp_properties]
rh-allow-sha1-signatures = yes
"""


class LibIMobileDevice:
    _ALLOWED_COMMANDS = {"idevice_id", "ideviceinfo", "idevicepair", "idevicebackup2"}
    _UDID_RE = re.compile(r"^[A-Za-z0-9-]+$")
    _SAFE_ARG_RE = re.compile(r"^[A-Za-z0-9_./:=+\-]+$")

    @classmethod
    def _is_valid_udid(cls, udid: str) -> bool:
        return bool(udid) and bool(cls._UDID_RE.fullmatch(udid))

    @staticmethod
    def _ensure_openssl_conf(path_hint: str | None = None) -> str:
        conf_path = os.environ.get(
            "OPENSSL_WEAK_CONF",
            os.path.join(path_hint or "/backups", "openssl-weak.conf"),
        )
        conf_dir = os.path.dirname(conf_path) or "."
        os.makedirs(conf_dir, exist_ok=True)
        if not os.path.exists(conf_path):
            with open(conf_path, "w", encoding="utf-8") as fh:
                fh.write(OPENSSL_WEAK_TEMPLATE)
        return conf_path

    @staticmethod
    def _run_cmd(cmd: list, timeout: int = 30, env: dict | None = None):
        args = cmd[1:] if cmd else []
        if (
            not cmd
            or cmd[0] not in LibIMobileDevice._ALLOWED_COMMANDS
            or any(not isinstance(arg, str) or any(c in arg for c in "\r\n\0") for arg in cmd)
            or any(
                shlex.quote(arg) != arg
                and not LibIMobileDevice._SAFE_ARG_RE.fullmatch(arg)
                for arg in args
            )
        ):
            return False, "", "Invalid command arguments"
        merged_env = os.environ.copy()
        if env:
            merged_env.update(env)
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=merged_env,
            )
            return True, result.stdout.strip(), result.stderr.strip()
        except subprocess.TimeoutExpired:
            return False, "", "Command timed out"
        except Exception as e:
            return False, "", str(e)

    @classmethod
    def get_connected_devices(cls):
        """Returns list of dicts: [{'udid': '...', 'type': 'usb|network'}]"""
        devices: dict[str, str] = {}
        success_usb, out_usb, _ = cls._run_cmd(["idevice_id", "--usb"])
        if success_usb:
            for line in out_usb.splitlines():
                udid = line.strip()
                if udid:
                    devices[udid] = "usb"

        success_network, out_network, _ = cls._run_cmd(["idevice_id", "--network"])
        if success_network:
            for line in out_network.splitlines():
                udid = line.strip()
                if udid:
                    devices[udid] = "network"

        if not devices:
            success_all, out_all, _ = cls._run_cmd(["idevice_id", "-l"])
            if success_all:
                for line in out_all.splitlines():
                    udid = line.strip()
                    if udid:
                        devices[udid] = "usb"

        return [{"udid": udid, "type": conn_type} for udid, conn_type in devices.items()]

    @classmethod
    def get_device_info(cls, udid: str, is_network: bool = False):
        if not cls._is_valid_udid(udid):
            return None
        cmd = ["ideviceinfo", "-u", udid, "-x"]
        if is_network:
            cmd.insert(1, "-n")
        success, out, err = cls._run_cmd(cmd)
        if not success:
            return None

        try:
            # Output is XML plist
            plist = plistlib.loads(out.encode("utf-8"))
            return plist
        except Exception:
            return None

    @classmethod
    def is_paired(cls, udid: str, is_network: bool = False):
        if not cls._is_valid_udid(udid):
            return False
        cmd = ["idevicepair", "-u", udid, "validate"]
        if is_network:
            cmd.insert(1, "-n")
        openssl_conf = cls._ensure_openssl_conf()
        success, out, err = cls._run_cmd(cmd, env={"OPENSSL_CONF": openssl_conf})
        return success and "SUCCESS" in f"{out}\n{err}".upper()

    @classmethod
    def pair_device(cls, udid: str, is_network: bool = False):
        if not cls._is_valid_udid(udid):
            return False, "Invalid device UDID"
        cmd = ["idevicepair", "-u", udid, "pair"]
        if is_network:
            cmd.insert(1, "-n")
        openssl_conf = cls._ensure_openssl_conf()
        success, out, err = cls._run_cmd(cmd, env={"OPENSSL_CONF": openssl_conf})
        pair_output = f"{out}\n{err}".strip()
        if success and "SUCCESS" in pair_output.upper():
            if not is_network:
                cls._run_cmd(
                    ["idevicepair", "-u", udid, "wifi", "on"],
                    env={"OPENSSL_CONF": openssl_conf},
                )
            return True, "Paired successfully. Wi-Fi sync enabled."
        elif pair_output:
            return False, pair_output
        return False, "Unknown error during pairing"

    @classmethod
    def _start_netmuxd_for_network_backup(cls, backup_root: str):
        netmuxd_bin = os.environ.get("NETMUXD_BIN")
        if not netmuxd_bin:
            netmuxd_bin = shutil.which("netmuxd")
        if not netmuxd_bin:
            candidate = os.path.join(backup_root, "netmuxd")
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                netmuxd_bin = candidate
        if not netmuxd_bin:
            return None, None

        host = os.environ.get("NETMUXD_HOST", "127.0.0.1")
        port = os.environ.get("NETMUXD_PORT", "27015")

        process = subprocess.Popen(
            [netmuxd_bin, "--disable-unix", "--host", host],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        time.sleep(float(os.environ.get("NETMUXD_DISCOVERY_WAIT_SECONDS", "5")))
        return process, f"{host}:{port}"

    @classmethod
    def backup_device(
        cls,
        udid: str,
        dest_path: str,
        strategy: str = "incremental",
        is_network: bool = False,
    ):
        if not cls._is_valid_udid(udid):
            return False, "Invalid device UDID"
        backup_root = os.path.abspath(dest_path)
        device_backup_dir = os.path.abspath(os.path.join(backup_root, udid))
        if os.path.commonpath([backup_root, device_backup_dir]) != backup_root:
            return False, "Invalid backup path configuration"

        if strategy == "full":
            logger.info(
                f"Full backup requested for {udid}. Removing old backup dir: {device_backup_dir}"
            )
            if os.path.isdir(device_backup_dir):
                shutil.rmtree(device_backup_dir, ignore_errors=True)

        os.makedirs(backup_root, exist_ok=True)
        openssl_conf = cls._ensure_openssl_conf(backup_root)
        env = {"OPENSSL_CONF": openssl_conf}
        netmuxd_process = None

        if is_network:
            netmuxd_process, socket_address = cls._start_netmuxd_for_network_backup(
                backup_root
            )
            if socket_address:
                env["USBMUXD_SOCKET_ADDRESS"] = socket_address
            else:
                logger.warning("netmuxd not found. Trying native --network mode.")

        cmd = ["idevicebackup2", "backup"]
        if is_network:
            cmd.append("--network")
        if strategy == "full":
            cmd.append("--full")
        cmd.extend(["-u", udid, backup_root])
        logger.info(f"Running backup command: {' '.join(cmd)}")

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env={**os.environ, **env},
        )

        try:
            if process.stdout:
                for line in process.stdout:
                    logger.info(f"[BACKUP {udid}] {line.strip()}")
            process.wait()
        finally:
            if netmuxd_process and netmuxd_process.poll() is None:
                netmuxd_process.terminate()
                try:
                    netmuxd_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    netmuxd_process.kill()

        if process.returncode == 0:
            return True, "Backup completed successfully"
        else:
            return False, f"Backup failed with code {process.returncode}"
