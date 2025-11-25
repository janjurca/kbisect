#!/usr/bin/env python3
"""IPMI Serial-Over-LAN console log collector.

Uses IPMI SOL to capture console output during kernel boot.
"""

import logging
import threading
import time
from typing import Optional

from kbisect.collectors.base import (
    DEFAULT_COLLECTION_TIMEOUT,
    DEFAULT_MAX_BUFFER_LINES,
    ConsoleCollector,
)


logger = logging.getLogger(__name__)


class IPMISOLCollector(ConsoleCollector):
    """IPMI Serial-Over-LAN console log collector.

    Uses IPMI SOL to capture console output. This is a synchronous wrapper
    around the existing IPMI controller functionality.

    Attributes:
        ipmi_host: IPMI hostname or IP address
        ipmi_user: IPMI username
        ipmi_password: IPMI password
        collection_thread: Background thread running SOL capture
    """

    def __init__(
        self,
        hostname: str,
        ipmi_host: str,
        ipmi_user: str,
        ipmi_password: str,
        max_buffer_lines: int = DEFAULT_MAX_BUFFER_LINES,
    ) -> None:
        """Initialize IPMI SOL collector.

        Args:
            hostname: Target hostname (for logging only)
            ipmi_host: IPMI hostname or IP address
            ipmi_user: IPMI username
            ipmi_password: IPMI password
            max_buffer_lines: Maximum lines to buffer
        """
        super().__init__(hostname, max_buffer_lines)
        self.ipmi_host = ipmi_host
        self.ipmi_user = ipmi_user
        self.ipmi_password = ipmi_password
        self.collection_thread: Optional[threading.Thread] = None
        self.stop_requested = False
        self.lock = threading.Lock()

    def start(self) -> bool:
        """Start console log collection via IPMI SOL.

        Spawns background thread to activate IPMI SOL and capture output.
        Note: IPMI SOL is blocking, so we run it in a thread.

        Returns:
            True if started successfully, False on error
        """
        try:
            logger.debug(f"Starting IPMI SOL collection for {self.hostname}")

            # Start background SOL capture thread
            self.collection_thread = threading.Thread(
                target=self._capture_sol,
                daemon=True,
                name=f"ipmi-sol-{self.hostname}",
            )
            self.collection_thread.start()

            self.is_active = True
            self.start_time = time.time()
            logger.info(f"✓ Started console log collection (IPMI SOL: {self.hostname})")
            return True

        except Exception as exc:
            logger.error(f"Failed to start IPMI SOL collection: {exc}")
            return False

    def _capture_sol(self) -> None:
        """Background thread function to capture IPMI SOL output.

        Creates IPMI controller and runs activate_serial_console.
        Stores output in buffer when complete or interrupted.
        """
        try:
            # Import here to avoid circular dependency
            from kbisect.power import IPMIController

            # Create IPMI controller for SOL capture
            ipmi = IPMIController(
                host=self.ipmi_host,
                user=self.ipmi_user,
                password=self.ipmi_password,
            )

            # Use a very long duration - we'll stop it manually
            output = ipmi.activate_serial_console(duration=DEFAULT_COLLECTION_TIMEOUT)

            if not self.stop_requested and output:
                with self.lock:
                    # Split into lines for consistent buffer handling
                    lines = output.splitlines(keepends=True)
                    # deque with maxlen automatically maintains size limit
                    self.buffer.extend(lines)

        except Exception as exc:
            logger.debug(f"IPMI SOL capture exception: {exc}")

    def stop(self) -> str:
        """Stop console log collection and retrieve output.

        Note: IPMI SOL cannot be cleanly interrupted, so this waits for
        the SOL session to complete or timeout.

        Returns:
            Collected console output as string
        """
        self.stop_requested = True
        output = ""

        try:
            # SOL cannot be interrupted cleanly, wait for thread
            if self.collection_thread and self.collection_thread.is_alive():
                logger.debug("Waiting for IPMI SOL session to complete...")
                self.collection_thread.join(timeout=10.0)

                # Check if thread is still running after timeout
                if self.collection_thread.is_alive():
                    logger.warning(
                        "IPMI SOL thread still running after 10s timeout. "
                        "Waiting additional time..."
                    )
                    # SOL sessions can take a while, give it more time
                    self.collection_thread.join(timeout=30.0)

                    if self.collection_thread.is_alive():
                        logger.error(
                            "IPMI SOL thread did not terminate after 40s total. "
                            "Thread will be left running (orphaned). Buffer may be incomplete."
                        )

            # Retrieve buffered output (with lock to ensure thread safety)
            with self.lock:
                output = "".join(self.buffer)
                buffer_size = len(self.buffer)

            duration = self.get_duration()
            logger.debug(f"Stopped IPMI SOL collection: {buffer_size} lines, {duration:.1f}s")

        except Exception as exc:
            logger.error(f"Error stopping IPMI SOL collection: {exc}")

        finally:
            self.is_active = False

        return output

    def is_running(self) -> bool:
        """Check if collection is currently running.

        Returns:
            True if thread is alive and collecting, False otherwise
        """
        return (
            self.is_active
            and self.collection_thread is not None
            and self.collection_thread.is_alive()
        )

    def get_and_clear_buffer(self) -> str:
        """Get current buffer content and clear it for streaming.

        Returns:
            Current buffered content as string
        """
        with self.lock:
            if not self.buffer:
                return ""
            output = "".join(self.buffer)
            self.buffer.clear()
            return output
