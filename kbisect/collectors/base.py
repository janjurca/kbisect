#!/usr/bin/env python3
"""Abstract base class for console log collectors.

Provides interface for asynchronous console log collection during kernel boot.
"""

import time
from abc import ABC, abstractmethod
from collections import deque
from typing import Deque, Optional


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
