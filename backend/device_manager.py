import asyncio
import logging
import os
import shutil

from pymobiledevice3 import usbmux
from pymobiledevice3.exceptions import (
    NotPairedError,
    PairingDialogResponsePendingError,
    UserDeniedPairingError,
    ConnectionFailedToUsbmuxdError,
    MuxException,
)
from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.services.mobilebackup2 import Mobilebackup2Service

logger = logging.getLogger(__name__)


def _run_async(coro):
    """Run an async coroutine from a synchronous context."""
    return asyncio.run(coro)


class LibIMobileDevice:
    """
    Device management backed by pymobiledevice3 (pure-Python iOS device library).
    Presents the same synchronous interface as the old libimobiledevice wrapper so
    the rest of the app (main.py, scheduler.py) needs no structural changes.
    """

    @classmethod
    def get_connected_devices(cls):
        """Returns list of dicts: [{'udid': '...', 'type': 'usb|network'}]"""

        async def _list():
            return await usbmux.list_devices()

        try:
            devices = _run_async(_list())
            return [
                {
                    "udid": d.serial,
                    "type": "network" if d.is_network else "usb",
                }
                for d in devices
            ]
        except (ConnectionFailedToUsbmuxdError, MuxException) as e:
            logger.warning(f"usbmuxd not reachable, no devices returned: {e}")
            return []
        except Exception as e:
            logger.error(f"Failed to list devices: {e}")
            return []

    @classmethod
    def get_device_info(cls, udid: str, is_network: bool = False):
        """Return the lockdown all_values dict for the device, or None on error."""
        connection_type = "Network" if is_network else "USB"

        async def _get_info():
            lockdown = await create_using_usbmux(
                udid, autopair=False, connection_type=connection_type
            )
            async with lockdown:
                return dict(lockdown.all_values)

        try:
            return _run_async(_get_info())
        except Exception as e:
            logger.debug(f"Could not get device info for {udid}: {e}")
            return None

    @classmethod
    def is_paired(cls, udid: str, is_network: bool = False):
        """Return True if the device has a valid pairing record."""
        connection_type = "Network" if is_network else "USB"

        async def _check():
            try:
                lockdown = await create_using_usbmux(
                    udid, autopair=False, connection_type=connection_type
                )
                async with lockdown:
                    return lockdown.paired
            except NotPairedError:
                return False

        try:
            return _run_async(_check())
        except Exception:
            return False

    @classmethod
    def pair_device(cls, udid: str, is_network: bool = False):
        """Initiate pairing with the device. Returns (success, message)."""
        connection_type = "Network" if is_network else "USB"

        async def _pair():
            lockdown = await create_using_usbmux(
                udid, autopair=False, connection_type=connection_type
            )
            async with lockdown:
                if lockdown.paired:
                    return True, "Device is already paired."
                await lockdown.pair()
                return (
                    True,
                    "Paired successfully. You might need to accept Trust on the device.",
                )

        try:
            return _run_async(_pair())
        except PairingDialogResponsePendingError:
            return False, "Waiting for user to accept pairing dialog on the device."
        except UserDeniedPairingError:
            return False, "User denied pairing on the device."
        except Exception as e:
            return False, str(e)

    @classmethod
    def backup_device(
        cls,
        udid: str,
        dest_path: str,
        strategy: str = "incremental",
        is_network: bool = False,
    ):
        """
        Back up the device to dest_path.  pymobiledevice3 will create a
        subdirectory named after the device UDID inside dest_path, matching
        the layout produced by idevicebackup2.

        Returns (success, message).
        """
        if strategy == "full":
            logger.info(
                f"Full backup requested for {udid}. Removing old backup dir: {dest_path}"
            )
            if os.path.exists(dest_path) and len(dest_path) > 5:
                shutil.rmtree(dest_path, ignore_errors=True)

        os.makedirs(dest_path, exist_ok=True)
        connection_type = "Network" if is_network else "USB"

        async def _backup():
            lockdown = await create_using_usbmux(
                udid, autopair=False, connection_type=connection_type
            )
            async with lockdown:
                async with Mobilebackup2Service(lockdown) as backup_service:

                    def progress_callback(percentage):
                        logger.info(
                            f"[BACKUP {udid}] Progress: {percentage:.1f}%"
                        )

                    await backup_service.backup(
                        full=(strategy == "full"),
                        backup_directory=dest_path,
                        progress_callback=progress_callback,
                    )

        try:
            _run_async(_backup())
            return True, "Backup completed successfully"
        except Exception as e:
            logger.error(f"Backup failed for {udid}: {e}")
            return False, str(e)
