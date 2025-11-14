#!/usr/bin/env python3
"""Abstract base class for remote clients.

Provides interface for remote command execution and file transfer.
"""

from abc import ABC, abstractmethod
from typing import Optional, Tuple


# Constants
SSH_ALIVE_TIMEOUT = 15  # Timeout in seconds for is_alive() check


class RemoteConnectionError(Exception):
    """Base exception for remote connection errors."""


class RemoteClient(ABC):
    """Abstract base class for remote clients.

    Provides interface for executing commands on remote machines and
    transferring files. Implementations handle specific protocols
    (SSH, WinRM, custom APIs, etc.).

    Attributes:
        host: Remote host hostname or IP address
        user: Username for authentication
    """

    def __init__(self, host: str, user: str) -> None:
        """Initialize remote client.

        Args:
            host: Remote host hostname or IP address
            user: Username for authentication
        """
        self.host = host
        self.user = user

    @abstractmethod
    def run_command(
        self, command: str, timeout: Optional[int] = None
    ) -> Tuple[int, str, str]:
        """Run command on remote host.

        Args:
            command: Command to execute
            timeout: Command timeout in seconds (None for no timeout)

        Returns:
            Tuple of (return_code, stdout, stderr)
        """

    def is_alive(self) -> bool:
        """Check if remote host is reachable.

        Default implementation runs a simple echo command.
        Implementations can override for more specific checks.

        Returns:
            True if host is reachable, False otherwise
        """
        ret, _, _ = self.run_command("echo alive", timeout=SSH_ALIVE_TIMEOUT)
        return ret == 0

    @abstractmethod
    def copy_file(self, local_path: str, remote_path: str) -> bool:
        """Copy file to remote host.

        Args:
            local_path: Local file path
            remote_path: Remote destination path

        Returns:
            True if copy succeeded, False otherwise
        """
