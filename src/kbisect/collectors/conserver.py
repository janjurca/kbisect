#!/usr/bin/env python3
"""Conserver-based console log collector.

Uses the 'console' command to connect to conserver and capture output.
Assumes conserver authentication is already configured (Kerberos or config).
"""

import logging
import subprocess
import threading
import time
from typing import Optional

from kbisect.collectors.base import (
    ConsoleCollector,
    ConsoleCollectionError,
    DEFAULT_MAX_BUFFER_LINES,
    PROCESS_TERM_TIMEOUT,
)


logger = logging.getLogger(__name__)


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
                stdin=subprocess.DEVNULL,  # Prevent interactive prompts
                text=True,
                bufsize=1,  # Line buffered
                start_new_session=True,  # Detach from controlling terminal
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
