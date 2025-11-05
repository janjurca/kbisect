#!/usr/bin/env python3
"""Console Log Collector - Asynchronous boot console log collection.

Provides abstract interface and implementations for collecting console output
during kernel boot process. Supports conserver and IPMI SOL.
"""

import logging
import subprocess
import threading
import time
from abc import ABC, abstractmethod
from collections import deque
from typing import Deque, List, Optional


logger = logging.getLogger(__name__)

# Constants
DEFAULT_MAX_BUFFER_LINES = 100000  # ~10MB at 100 bytes/line
DEFAULT_COLLECTION_TIMEOUT = 600  # 10 minutes maximum
PROCESS_TERM_TIMEOUT = 5  # Graceful termination timeout


class ConsoleCollectionError(Exception):
    """Base exception for console collection errors."""


class ConsoleCollector(ABC):
    """Abstract base class for console log collectors.

    Provides interface for asynchronous console log collection during boot.
    Implementations must handle starting, stopping, and retrieving output.

    Attributes:
        hostname: Target hostname for console connection
        max_buffer_lines: Maximum lines to buffer (prevent memory exhaustion)
        is_active: Whether collection is currently running
    """

    def __init__(self, hostname: str, max_buffer_lines: int = DEFAULT_MAX_BUFFER_LINES) -> None:
        """Initialize console collector.

        Args:
            hostname: Target hostname for console connection
            max_buffer_lines: Maximum lines to buffer
        """
        self.hostname = hostname
        self.max_buffer_lines = max_buffer_lines
        self.is_active = False
        # Use deque with maxlen for automatic size limiting and better performance
        self.buffer: Deque[str] = deque(maxlen=max_buffer_lines)
        self.start_time: Optional[float] = None

    @abstractmethod
    def start(self) -> bool:
        """Start console log collection asynchronously.

        Returns:
            True if collection started successfully, False otherwise
        """

    @abstractmethod
    def stop(self) -> str:
        """Stop console log collection and retrieve output.

        Returns:
            Collected console output as string
        """

    @abstractmethod
    def is_running(self) -> bool:
        """Check if collection is currently running.

        Returns:
            True if actively collecting, False otherwise
        """

    def get_duration(self) -> Optional[float]:
        """Get collection duration in seconds.

        Returns:
            Duration in seconds, or None if not started
        """
        if self.start_time:
            return time.time() - self.start_time
        return None

    def get_buffer_stats(self) -> dict:
        """Get current buffer statistics.

        Returns:
            Dictionary with buffer statistics (lines, approximate size)
        """
        return {"lines": len(self.buffer), "max_lines": self.max_buffer_lines}

    @abstractmethod
    def get_and_clear_buffer(self) -> str:
        """Get current buffer content and clear it (for streaming).

        Returns:
            Current buffered content as string, buffer is cleared after reading
        """


class ConserverCollector(ConsoleCollector):
    """Conserver-based console log collector.

    Uses the 'console' command to connect to conserver and capture output.
    Assumes conserver authentication is already configured (Kerberos or config).

    Attributes:
        proc: Subprocess running console command
        reader_thread: Background thread reading output
        lock: Thread lock for buffer access
    """

    def __init__(self, hostname: str, max_buffer_lines: int = DEFAULT_MAX_BUFFER_LINES) -> None:
        """Initialize conserver collector.

        Args:
            hostname: Target hostname for console connection
            max_buffer_lines: Maximum lines to buffer
        """
        super().__init__(hostname, max_buffer_lines)
        self.proc: Optional[subprocess.Popen] = None
        self.reader_thread: Optional[threading.Thread] = None
        self.lock = threading.Lock()
        self.stop_requested = False

    def start(self) -> bool:
        """Start console log collection via conserver.

        Spawns background process running 'console <hostname>' and starts
        reader thread to capture output.

        Returns:
            True if started successfully, False on error
        """
        try:
            logger.debug(f"Starting conserver collection for {self.hostname}")

            # Start console process
            self.proc = subprocess.Popen(
                ["console", self.hostname],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,  # Line buffered
            )

            # Give process a moment to fail fast if auth/connection issues
            time.sleep(0.5)

            if self.proc.poll() is not None:
                # Process already terminated
                _, stderr = self.proc.communicate()
                raise ConsoleCollectionError(f"Console command failed: {stderr.strip()}")

            # Start background reader thread
            self.reader_thread = threading.Thread(
                target=self._read_output, daemon=True, name=f"console-{self.hostname}"
            )
            self.reader_thread.start()

            self.is_active = True
            self.start_time = time.time()
            logger.info(f"✓ Started console log collection (conserver: {self.hostname})")
            return True

        except FileNotFoundError:
            logger.error("Console command not found - is conserver installed?")
            return False
        except Exception as exc:
            logger.error(f"Failed to start conserver collection: {exc}")
            return False

    def _read_output(self) -> None:
        """Background thread function to read console output.

        Continuously reads lines from stdout and buffers them.
        Enforces maximum buffer size to prevent memory exhaustion.
        """
        if not self.proc or not self.proc.stdout:
            return

        try:
            for line in self.proc.stdout:
                if self.stop_requested:
                    break

                with self.lock:
                    # deque with maxlen automatically maintains size limit
                    self.buffer.append(line)

        except Exception as exc:
            logger.debug(f"Console reader thread exception: {exc}")

    def stop(self) -> str:
        """Stop console log collection and retrieve output.

        Terminates the console process gracefully and retrieves all buffered output.

        Returns:
            Collected console output as string
        """
        self.stop_requested = True
        output = ""

        try:
            if self.proc and self.proc.poll() is None:
                # Process still running, terminate it
                logger.debug("Terminating console process...")
                self.proc.terminate()

                try:
                    self.proc.wait(timeout=PROCESS_TERM_TIMEOUT)
                except subprocess.TimeoutExpired:
                    logger.warning("Console process did not terminate, forcing kill")
                    self.proc.kill()
                    self.proc.wait()

            # Wait for reader thread to finish (with timeout)
            if self.reader_thread and self.reader_thread.is_alive():
                self.reader_thread.join(timeout=2.0)

                # Check if thread is still running after timeout
                if self.reader_thread.is_alive():
                    logger.warning(
                        "Reader thread still running after timeout. "
                        "Waiting additional time to prevent race condition..."
                    )
                    # Give it more time to finish
                    self.reader_thread.join(timeout=3.0)

                    if self.reader_thread.is_alive():
                        logger.error(
                            "Reader thread did not terminate. Buffer may be incomplete "
                            "or contain concurrent access artifacts."
                        )

            # Retrieve buffered output (with lock to ensure thread safety)
            with self.lock:
                output = "".join(self.buffer)
                buffer_size = len(self.buffer)

            duration = self.get_duration()
            logger.debug(
                f"Stopped console collection: {buffer_size} lines, {duration:.1f}s duration"
            )

        except Exception as exc:
            logger.error(f"Error stopping console collection: {exc}")

        finally:
            self.is_active = False

        return output

    def is_running(self) -> bool:
        """Check if collection is currently running.

        Returns:
            True if process is alive and collecting, False otherwise
        """
        return self.is_active and self.proc is not None and self.proc.poll() is None

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


class IPMISOLCollector(ConsoleCollector):
    """IPMI Serial-Over-LAN console log collector.

    Uses IPMI SOL to capture console output. This is a synchronous wrapper
    around the existing IPMI controller functionality.

    Attributes:
        ipmi_controller: IPMI controller instance
        collection_thread: Background thread running SOL capture
    """

    def __init__(
        self,
        hostname: str,
        ipmi_controller: "IPMIController",  # noqa: F821
        max_buffer_lines: int = DEFAULT_MAX_BUFFER_LINES,
    ) -> None:
        """Initialize IPMI SOL collector.

        Args:
            hostname: Target hostname (for logging only)
            ipmi_controller: IPMI controller instance
            max_buffer_lines: Maximum lines to buffer
        """
        super().__init__(hostname, max_buffer_lines)
        self.ipmi_controller = ipmi_controller
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

        Runs activate_serial_console with a very long timeout.
        Stores output in buffer when complete or interrupted.
        """
        try:
            # Use a very long duration - we'll stop it manually
            output = self.ipmi_controller.activate_serial_console(
                duration=DEFAULT_COLLECTION_TIMEOUT
            )

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


def create_console_collector(
    hostname: str,
    collector_type: str = "auto",
    ipmi_controller: Optional["IPMIController"] = None,  # noqa: F821
) -> Optional[ConsoleCollector]:
    """Factory function to create appropriate console collector.

    Args:
        hostname: Target hostname for console connection
        collector_type: Type of collector ("conserver", "ipmi", or "auto")
        ipmi_controller: IPMI controller instance (required for IPMI collector)

    Returns:
        ConsoleCollector instance, or None if creation failed
    """
    if collector_type == "conserver" or collector_type == "auto":
        collector = ConserverCollector(hostname)
        if collector_type == "conserver":
            return collector

        # Auto mode: try conserver first
        try:
            # Test if console command exists
            result = subprocess.run(
                ["which", "console"], capture_output=True, text=True, timeout=2, check=False
            )
            if result.returncode == 0:
                logger.debug("Using conserver for console log collection")
                return collector
            logger.debug("Console command not found, trying IPMI SOL")
        except Exception as exc:
            logger.debug(f"Conserver availability check failed: {exc}")

    # Try IPMI SOL
    if collector_type == "ipmi" or collector_type == "auto":
        if not ipmi_controller:
            logger.warning("IPMI SOL requested but no IPMI controller provided")
            return None

        logger.debug("Using IPMI SOL for console log collection")
        return IPMISOLCollector(hostname, ipmi_controller)

    logger.error(f"Unknown console collector type: {collector_type}")
    return None
