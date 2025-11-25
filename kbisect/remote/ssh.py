#!/usr/bin/env python3
"""SSH client for remote command execution and file transfer.

Provides SSH-based remote client implementation for slave communication.
"""

import logging
import select
import shlex
import subprocess
import time
from typing import Callable, Optional, Tuple

from kbisect.remote.base import RemoteClient


logger = logging.getLogger(__name__)


class SSHClient(RemoteClient):
    """SSH client for slave communication.

    Provides methods to execute commands on slave via SSH and copy files.

    Attributes:
        host: Slave hostname or IP
        user: SSH username
        connect_timeout: SSH connection timeout in seconds
    """

    def __init__(self, host: str, user: str = "root", connect_timeout: int = 15) -> None:
        """Initialize SSH client.

        Args:
            host: Slave hostname or IP
            user: SSH username
            connect_timeout: SSH connection timeout in seconds
        """
        super().__init__(host, user)
        self.connect_timeout = connect_timeout

    def run_command(self, command: str, timeout: Optional[int] = None) -> Tuple[int, str, str]:
        """Run command on slave via SSH.

        Args:
            command: Command to execute
            timeout: Command timeout in seconds

        Returns:
            Tuple of (return_code, stdout, stderr)
        """
        ssh_command = [
            "ssh",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            f"ConnectTimeout={self.connect_timeout}",
            f"{self.user}@{self.host}",
            command,
        ]

        try:
            result = subprocess.run(
                ssh_command, capture_output=True, text=True, timeout=timeout, check=False
            )
            return result.returncode, result.stdout, result.stderr
        except subprocess.TimeoutExpired:
            logger.error(f"SSH command timed out after {timeout}s")
            return -1, "", "Timeout"
        except Exception as exc:
            logger.error(f"SSH command failed: {exc}")
            return -1, "", str(exc)

    def call_function(
        self,
        function_name: str,
        *args: str,
        library_path: str = "/root/kernel-bisect/lib/bisect-functions.sh",
        timeout: Optional[int] = None,
    ) -> Tuple[int, str, str]:
        """Call a bash function from the bisect library.

        Args:
            function_name: Name of the bash function to call
            *args: Arguments to pass to the function
            library_path: Path to the bisect library on slave
            timeout: Command timeout in seconds

        Returns:
            Tuple of (return_code, stdout, stderr)
        """
        # Properly escape arguments to prevent command injection
        args_str = " ".join(shlex.quote(str(arg)) for arg in args)

        # Source library and call function (quote paths for safety)
        command = f"source {shlex.quote(library_path)} && {function_name} {args_str}"

        return self.run_command(command, timeout=timeout)

    def call_function_streaming(
        self,
        function_name: str,
        *args: str,
        library_path: str = "/root/kernel-bisect/lib/bisect-functions.sh",
        timeout: Optional[int] = None,
        chunk_callback: Optional[Callable[[str, str], None]] = None,
    ) -> Tuple[int, str, str]:
        """Call a bash function and stream output in real-time.

        Args:
            function_name: Name of the bash function to call
            *args: Arguments to pass to the function
            library_path: Path to the bisect library on slave
            timeout: Command timeout in seconds
            chunk_callback: Optional callback function(stdout_chunk, stderr_chunk) called as output arrives

        Returns:
            Tuple of (return_code, stdout, stderr)
        """
        # Properly escape arguments to prevent command injection
        args_str = " ".join(shlex.quote(str(arg)) for arg in args)

        # Source library and call function
        command = f"source {shlex.quote(library_path)} && {function_name} {args_str}"

        ssh_command = [
            "ssh",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            f"ConnectTimeout={self.connect_timeout}",
            f"{self.user}@{self.host}",
            command,
        ]

        try:
            # Use Popen for streaming
            process = subprocess.Popen(
                ssh_command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,  # Line buffered
            )

            stdout_lines = []
            stderr_lines = []
            start_time = time.time()

            # Read output as it arrives
            while True:
                # Check timeout
                if timeout and (time.time() - start_time) > timeout:
                    process.kill()
                    logger.error(f"SSH command timed out after {timeout}s")
                    return -1, "".join(stdout_lines), "Timeout"

                # Use select to check which pipes have data
                # Note: select() doesn't work on Windows, but kbisect is Linux-focused
                readable, _, _ = select.select([process.stdout, process.stderr], [], [], 0.1)

                for stream in readable:
                    line = stream.readline()
                    if line:
                        if stream == process.stdout:
                            stdout_lines.append(line)
                            if chunk_callback:
                                chunk_callback(line, "")
                        else:
                            stderr_lines.append(line)
                            if chunk_callback:
                                chunk_callback("", line)

                # Check if process has ended
                if process.poll() is not None:
                    # Read any remaining output
                    remaining_stdout = process.stdout.read()
                    remaining_stderr = process.stderr.read()
                    if remaining_stdout:
                        stdout_lines.append(remaining_stdout)
                        if chunk_callback:
                            chunk_callback(remaining_stdout, "")
                    if remaining_stderr:
                        stderr_lines.append(remaining_stderr)
                        if chunk_callback:
                            chunk_callback("", remaining_stderr)
                    break

            return process.returncode, "".join(stdout_lines), "".join(stderr_lines)

        except Exception as exc:
            logger.error(f"SSH streaming command failed: {exc}")
            return -1, "", str(exc)

    def is_alive(self) -> bool:
        """Check if slave is reachable via SSH.

        Uses the configured connect_timeout instead of hardcoded default.

        Returns:
            True if host is reachable, False otherwise
        """
        ret, _, _ = self.run_command("echo alive", timeout=self.connect_timeout)
        return ret == 0

    def copy_file(self, local_path: str, remote_path: str) -> bool:
        """Copy file to slave.

        Args:
            local_path: Local file path
            remote_path: Remote destination path

        Returns:
            True if copy succeeded, False otherwise
        """
        scp_command = [
            "scp",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            f"ConnectTimeout={self.connect_timeout}",
            local_path,
            f"{self.user}@{self.host}:{remote_path}",
        ]

        try:
            result = subprocess.run(scp_command, capture_output=True, text=True, check=False)
            return result.returncode == 0
        except Exception as exc:
            logger.error(f"SCP failed: {exc}")
            return False
