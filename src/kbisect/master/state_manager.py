#!/usr/bin/env python3
"""State Manager - Persistent state storage using SQLite.

Tracks bisection progress, test results, and generates reports.
"""

import hashlib
import json
import logging
import sqlite3
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


logger = logging.getLogger(__name__)

# Constants
DEFAULT_DB_PATH = "bisect.db"


class DatabaseError(Exception):
    """Base exception for database-related errors."""


@dataclass
class BisectSession:
    """Bisection session data.

    Attributes:
        session_id: Unique session identifier
        good_commit: Known good commit hash
        bad_commit: Known bad commit hash
        start_time: Session start timestamp
        end_time: Session end timestamp (None if running)
        status: Session status (running, completed, failed)
        result_commit: First bad commit found (None until complete)
    """

    session_id: int
    good_commit: str
    bad_commit: str
    start_time: str
    end_time: Optional[str] = None
    status: str = "running"
    result_commit: Optional[str] = None


@dataclass
class TestIteration:
    """Test iteration record.

    Attributes:
        iteration_id: Unique iteration identifier
        session_id: Parent session ID
        iteration_num: Iteration number (1-indexed)
        commit_sha: Commit hash being tested
        commit_message: Commit message
        build_result: Build result (success, failure, skip)
        boot_result: Boot result (success, failure, timeout)
        test_result: Test result (pass, fail)
        final_result: Final verdict (good, bad, skip)
        start_time: Iteration start timestamp
        end_time: Iteration end timestamp
        duration: Duration in seconds
        error_message: Error message if iteration failed
        kernel_version: Kernel version that was built
    """

    iteration_id: int
    session_id: int
    iteration_num: int
    commit_sha: str
    commit_message: str
    build_result: Optional[str] = None
    boot_result: Optional[str] = None
    test_result: Optional[str] = None
    final_result: Optional[str] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    duration: Optional[int] = None
    error_message: Optional[str] = None
    kernel_version: Optional[str] = None


class StateManager:
    """Manage bisection state in SQLite database.

    Provides methods to create and manage bisection sessions, iterations,
    metadata, and generate reports.

    Attributes:
        db_path: Path to SQLite database file
        conn: Database connection (None until initialized)
    """

    def __init__(self, db_path: str = DEFAULT_DB_PATH) -> None:
        """Initialize state manager.

        Args:
            db_path: Path to SQLite database file
        """
        self.db_path = db_path
        self.conn: Optional[sqlite3.Connection] = None
        self.db_lock = threading.Lock()  # Thread safety for SQLite operations

        # Ensure directory exists (only needed if db_path contains subdirectories)
        db_parent = Path(db_path).parent
        if db_parent != Path():
            db_parent.mkdir(parents=True, exist_ok=True)

        # Initialize database
        self._init_database()

    def _init_database(self) -> None:
        """Initialize database schema."""
        # Allow connection to be used from different threads
        try:
            self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self.conn.row_factory = sqlite3.Row
        except sqlite3.Error as exc:
            self.conn = None
            msg = f"Failed to connect to database: {exc}"
            logger.error(msg)
            raise DatabaseError(msg) from exc

        with self.db_lock:
            # Create tables
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    good_commit TEXT NOT NULL,
                    bad_commit TEXT NOT NULL,
                    start_time TEXT NOT NULL,
                    end_time TEXT,
                    status TEXT NOT NULL DEFAULT 'running',
                    result_commit TEXT,
                    config JSON
                )
            """)

            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS iterations (
                    iteration_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id INTEGER NOT NULL,
                    iteration_num INTEGER NOT NULL,
                    commit_sha TEXT NOT NULL,
                    commit_message TEXT,
                    build_result TEXT,
                    boot_result TEXT,
                    test_result TEXT,
                    final_result TEXT,
                    start_time TEXT,
                    end_time TEXT,
                    duration INTEGER,
                    error_message TEXT,
                    kernel_version TEXT,
                    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
                )
            """)

            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS logs (
                    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    iteration_id INTEGER NOT NULL,
                    log_type TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    message TEXT NOT NULL,
                    FOREIGN KEY (iteration_id) REFERENCES iterations(iteration_id)
                )
            """)

            # Build logs table
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS build_logs (
                    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    iteration_id INTEGER NOT NULL,
                    log_type TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    log_content BLOB NOT NULL,
                    compressed BOOLEAN DEFAULT 1,
                    size_bytes INTEGER,
                    exit_code INTEGER,
                    FOREIGN KEY (iteration_id) REFERENCES iterations(iteration_id)
                )
            """)

            # Metadata tables
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS metadata (
                    metadata_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id INTEGER NOT NULL,
                    iteration_id INTEGER NULL,
                    collection_time TEXT NOT NULL,
                    collection_type TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    metadata_hash TEXT,
                    FOREIGN KEY (session_id) REFERENCES sessions(session_id),
                    FOREIGN KEY (iteration_id) REFERENCES iterations(iteration_id)
                )
            """)

            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS metadata_files (
                    file_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    metadata_id INTEGER NOT NULL,
                    file_type TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    file_hash TEXT,
                    file_size INTEGER,
                    compressed BOOLEAN DEFAULT 0,
                    FOREIGN KEY (metadata_id) REFERENCES metadata(metadata_id)
                )
            """)

            self.conn.commit()

    def create_session(
        self, good_commit: str, bad_commit: str, config: Optional[Dict[str, Any]] = None
    ) -> int:
        """Create new bisection session.

        Args:
            good_commit: Known good commit hash
            bad_commit: Known bad commit hash
            config: Optional configuration dict

        Returns:
            Session ID

        Raises:
            DatabaseError: If session creation fails
        """
        if not self.conn:
            raise DatabaseError("Database not initialized")

        with self.db_lock:
            cursor = self.conn.cursor()

            try:
                cursor.execute(
                    """
                    INSERT INTO sessions (good_commit, bad_commit, start_time, config)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        good_commit,
                        bad_commit,
                        datetime.now(timezone.utc).isoformat(),
                        json.dumps(config) if config else None,
                    ),
                )

                self.conn.commit()
                session_id = cursor.lastrowid

                logger.info(f"Created bisection session {session_id}")
                return session_id
            except sqlite3.Error as exc:
                msg = f"Failed to create session: {exc}"
                logger.error(msg)
                raise DatabaseError(msg) from exc

    def get_session(self, session_id: int) -> Optional[BisectSession]:
        """Get session by ID.

        Args:
            session_id: Session ID to retrieve

        Returns:
            BisectSession object or None if not found
        """
        if not self.conn:
            raise DatabaseError("Database not initialized")

        with self.db_lock:
            cursor = self.conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            )

            row = cursor.fetchone()
            if not row:
                return None

            return BisectSession(
                session_id=row["session_id"],
                good_commit=row["good_commit"],
                bad_commit=row["bad_commit"],
                start_time=row["start_time"],
                end_time=row["end_time"],
                status=row["status"],
                result_commit=row["result_commit"],
            )

    def get_latest_session(self) -> Optional[BisectSession]:
        """Get most recent session.

        Returns:
            BisectSession object or None if no sessions exist
        """
        if not self.conn:
            raise DatabaseError("Database not initialized")

        with self.db_lock:
            cursor = self.conn.execute(
                "SELECT * FROM sessions ORDER BY session_id DESC LIMIT 1"
            )

            row = cursor.fetchone()
            if not row:
                return None

            return BisectSession(
                session_id=row["session_id"],
                good_commit=row["good_commit"],
                bad_commit=row["bad_commit"],
                start_time=row["start_time"],
                end_time=row["end_time"],
                status=row["status"],
                result_commit=row["result_commit"],
            )

    def get_or_create_session(
        self, good_commit: str, bad_commit: str, config: Optional[Dict[str, Any]] = None
    ) -> int:
        """Get existing running session or create new one (atomic operation).

        This method prevents race conditions by atomically checking for and
        creating sessions within a single database lock.

        Args:
            good_commit: Known good commit hash
            bad_commit: Known bad commit hash
            config: Optional configuration dict

        Returns:
            Session ID (existing or newly created)

        Raises:
            DatabaseError: If session operation fails
        """
        if not self.conn:
            raise DatabaseError("Database not initialized")

        with self.db_lock:
            # Check for existing running session within the lock
            cursor = self.conn.execute(
                "SELECT session_id FROM sessions WHERE status = 'running' "
                "ORDER BY session_id DESC LIMIT 1"
            )
            row = cursor.fetchone()

            if row:
                session_id = row["session_id"]
                logger.info(f"Found existing running session {session_id}")
                return session_id

            # No running session found, create new one (still within lock)
            cursor = self.conn.cursor()
            try:
                cursor.execute(
                    """
                    INSERT INTO sessions (good_commit, bad_commit, start_time, config)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        good_commit,
                        bad_commit,
                        datetime.now(timezone.utc).isoformat(),
                        json.dumps(config) if config else None,
                    ),
                )

                self.conn.commit()
                session_id = cursor.lastrowid

                logger.info(f"Created new bisection session {session_id}")
                return session_id
            except sqlite3.Error as exc:
                msg = f"Failed to create session: {exc}"
                logger.error(msg)
                raise DatabaseError(msg) from exc

    def update_session(self, session_id: int, **kwargs: Any) -> None:
        """Update session fields.

        Args:
            session_id: Session ID to update
            **kwargs: Fields to update (end_time, status, result_commit)

        Raises:
            DatabaseError: If update fails
        """
        if not self.conn:
            raise DatabaseError("Database not initialized")

        # Explicitly validate and map field names to prevent SQL injection
        valid_fields = {"end_time", "status", "result_commit"}
        updates = {k: v for k, v in kwargs.items() if k in valid_fields}

        if not updates:
            return

        # Build SET clause with explicit field names (safe from SQL injection)
        set_parts = []
        values = []
        for field in ["end_time", "status", "result_commit"]:
            if field in updates:
                set_parts.append(f"{field} = ?")
                values.append(updates[field])

        set_clause = ", ".join(set_parts)
        values.append(session_id)

        with self.db_lock:
            try:
                self.conn.execute(
                    f"UPDATE sessions SET {set_clause} WHERE session_id = ?", values
                )
                self.conn.commit()
            except sqlite3.Error as exc:
                msg = f"Failed to update session: {exc}"
                logger.error(msg)
                raise DatabaseError(msg) from exc

    def create_iteration(
        self, session_id: int, iteration_num: int, commit_sha: str, commit_message: str
    ) -> int:
        """Create new iteration.

        Args:
            session_id: Parent session ID
            iteration_num: Iteration number
            commit_sha: Commit hash being tested
            commit_message: Commit message

        Returns:
            Iteration ID

        Raises:
            DatabaseError: If iteration creation fails
        """
        if not self.conn:
            raise DatabaseError("Database not initialized")

        with self.db_lock:
            cursor = self.conn.cursor()

            try:
                cursor.execute(
                    """
                    INSERT INTO iterations (
                        session_id, iteration_num, commit_sha, commit_message, start_time
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        iteration_num,
                        commit_sha,
                        commit_message,
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )

                self.conn.commit()
                iteration_id = cursor.lastrowid

                logger.debug(f"Created iteration {iteration_id}")
                return iteration_id
            except sqlite3.Error as exc:
                msg = f"Failed to create iteration: {exc}"
                logger.error(msg)
                raise DatabaseError(msg) from exc

    def update_iteration(self, iteration_id: int, **kwargs: Any) -> None:
        """Update iteration fields.

        Args:
            iteration_id: Iteration ID to update
            **kwargs: Fields to update

        Raises:
            DatabaseError: If update fails
        """
        if not self.conn:
            raise DatabaseError("Database not initialized")

        # Explicitly validate and map field names to prevent SQL injection
        valid_fields = {
            "build_result",
            "boot_result",
            "test_result",
            "final_result",
            "end_time",
            "duration",
            "error_message",
            "kernel_version",
        }
        updates = {k: v for k, v in kwargs.items() if k in valid_fields}

        if not updates:
            return

        # Build SET clause with explicit field names (safe from SQL injection)
        set_parts = []
        values = []
        for field in [
            "build_result",
            "boot_result",
            "test_result",
            "final_result",
            "end_time",
            "duration",
            "error_message",
            "kernel_version",
        ]:
            if field in updates:
                set_parts.append(f"{field} = ?")
                values.append(updates[field])

        set_clause = ", ".join(set_parts)
        values.append(iteration_id)

        with self.db_lock:
            try:
                self.conn.execute(
                    f"UPDATE iterations SET {set_clause} WHERE iteration_id = ?", values
                )
                self.conn.commit()
            except sqlite3.Error as exc:
                msg = f"Failed to update iteration: {exc}"
                logger.error(msg)
                raise DatabaseError(msg) from exc

    def get_iterations(self, session_id: int) -> List[TestIteration]:
        """Get all iterations for a session.

        Args:
            session_id: Session ID

        Returns:
            List of TestIteration objects
        """
        if not self.conn:
            raise DatabaseError("Database not initialized")

        with self.db_lock:
            cursor = self.conn.execute(
                """
                SELECT * FROM iterations
                WHERE session_id = ?
                ORDER BY iteration_num
                """,
                (session_id,),
            )

            iterations = []
            for row in cursor.fetchall():
                iterations.append(
                    TestIteration(
                        iteration_id=row["iteration_id"],
                        session_id=row["session_id"],
                        iteration_num=row["iteration_num"],
                        commit_sha=row["commit_sha"],
                        commit_message=row["commit_message"],
                        build_result=row["build_result"],
                        boot_result=row["boot_result"],
                        test_result=row["test_result"],
                        final_result=row["final_result"],
                        start_time=row["start_time"],
                        end_time=row["end_time"],
                        duration=row["duration"],
                        error_message=row["error_message"],
                        kernel_version=row["kernel_version"],
                    )
                )

            return iterations

    def add_log(self, iteration_id: int, log_type: str, message: str) -> None:
        """Add log entry for an iteration.

        Args:
            iteration_id: Iteration ID
            log_type: Type of log entry
            message: Log message

        Raises:
            DatabaseError: If log creation fails
        """
        if not self.conn:
            raise DatabaseError("Database not initialized")

        with self.db_lock:
            try:
                self.conn.execute(
                    """
                    INSERT INTO logs (iteration_id, log_type, timestamp, message)
                    VALUES (?, ?, ?, ?)
                    """,
                    (iteration_id, log_type, datetime.now(timezone.utc).isoformat(), message),
                )
                self.conn.commit()
            except sqlite3.Error as exc:
                msg = f"Failed to add log: {exc}"
                logger.error(msg)
                raise DatabaseError(msg) from exc

    def get_logs(self, iteration_id: int) -> List[Dict[str, Any]]:
        """Get logs for an iteration.

        Args:
            iteration_id: Iteration ID

        Returns:
            List of log dictionaries
        """
        if not self.conn:
            raise DatabaseError("Database not initialized")

        with self.db_lock:
            cursor = self.conn.execute(
                """
                SELECT * FROM logs
                WHERE iteration_id = ?
                ORDER BY timestamp
                """,
                (iteration_id,),
            )

            return [dict(row) for row in cursor.fetchall()]

    def store_build_log(
        self, iteration_id: int, log_type: str, content: str, exit_code: int = 0
    ) -> int:
        """Store build log with compression.

        Args:
            iteration_id: Iteration ID
            log_type: Type of log (build, boot, test)
            content: Log content to store
            exit_code: Exit code of the command that generated the log

        Returns:
            Log ID

        Raises:
            DatabaseError: If log storage fails
        """
        if not self.conn:
            raise DatabaseError("Database not initialized")

        import gzip

        # Compress log content
        compressed_content = gzip.compress(content.encode("utf-8"))
        size_bytes = len(compressed_content)

        with self.db_lock:
            cursor = self.conn.cursor()
            try:
                cursor.execute(
                    """
                    INSERT INTO build_logs (
                        iteration_id, log_type, timestamp, log_content,
                        compressed, size_bytes, exit_code
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        iteration_id,
                        log_type,
                        datetime.now(timezone.utc).isoformat(),
                        compressed_content,
                        True,
                        size_bytes,
                        exit_code,
                    ),
                )

                self.conn.commit()
                log_id = cursor.lastrowid

                logger.debug(f"Stored {log_type} log {log_id} ({size_bytes} bytes compressed)")
                return log_id
            except sqlite3.Error as exc:
                msg = f"Failed to store build log: {exc}"
                logger.error(msg)
                raise DatabaseError(msg) from exc

    def get_build_log(self, log_id: int) -> Optional[Dict[str, Any]]:
        """Get and decompress build log by ID.

        Args:
            log_id: Log ID to retrieve

        Returns:
            Dictionary with log data including decompressed content, or None if not found
        """
        if not self.conn:
            raise DatabaseError("Database not initialized")

        import gzip

        with self.db_lock:
            cursor = self.conn.execute(
                """
                SELECT bl.*, i.iteration_num, i.commit_sha, i.commit_message
                FROM build_logs bl
                JOIN iterations i ON bl.iteration_id = i.iteration_id
                WHERE bl.log_id = ?
                """,
                (log_id,),
            )

            row = cursor.fetchone()
            if not row:
                return None

            # Decompress content
            content = row["log_content"]
            if row["compressed"]:
                content = gzip.decompress(content).decode("utf-8")
            else:
                content = content.decode("utf-8")

            return {
                "log_id": row["log_id"],
                "iteration_id": row["iteration_id"],
                "iteration_num": row["iteration_num"],
                "commit_sha": row["commit_sha"],
                "commit_message": row["commit_message"],
                "log_type": row["log_type"],
                "timestamp": row["timestamp"],
                "content": content,
                "size_bytes": row["size_bytes"],
                "exit_code": row["exit_code"],
                "compressed": row["compressed"],
            }

    def get_iteration_build_logs(self, iteration_id: int) -> List[Dict[str, Any]]:
        """Get all build logs for an iteration.

        Args:
            iteration_id: Iteration ID

        Returns:
            List of log metadata dictionaries (without content)
        """
        if not self.conn:
            raise DatabaseError("Database not initialized")

        with self.db_lock:
            cursor = self.conn.execute(
                """
                SELECT log_id, log_type, timestamp, size_bytes, exit_code
                FROM build_logs
                WHERE iteration_id = ?
                ORDER BY timestamp
                """,
                (iteration_id,),
            )

            return [dict(row) for row in cursor.fetchall()]

    def list_build_logs(
        self, session_id: Optional[int] = None, log_type: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """List all build logs with metadata.

        Args:
            session_id: Optional session ID filter
            log_type: Optional log type filter (build, boot, test)

        Returns:
            List of log metadata dictionaries
        """
        if not self.conn:
            raise DatabaseError("Database not initialized")

        query = """
            SELECT
                bl.log_id,
                bl.iteration_id,
                i.iteration_num,
                i.commit_sha,
                bl.log_type,
                bl.timestamp,
                bl.size_bytes,
                bl.exit_code,
                CASE WHEN bl.exit_code = 0 THEN 'SUCCESS' ELSE 'FAILED' END as status
            FROM build_logs bl
            JOIN iterations i ON bl.iteration_id = i.iteration_id
        """

        conditions = []
        params = []

        if session_id is not None:
            conditions.append("i.session_id = ?")
            params.append(session_id)

        if log_type is not None:
            conditions.append("bl.log_type = ?")
            params.append(log_type)

        if conditions:
            query += " WHERE " + " AND ".join(conditions)

        query += " ORDER BY bl.timestamp DESC"

        with self.db_lock:
            cursor = self.conn.execute(query, params)
            return [dict(row) for row in cursor.fetchall()]

    def store_metadata(
        self,
        session_id: int,
        metadata_dict: Dict[str, Any],
        iteration_id: Optional[int] = None,
    ) -> int:
        """Store metadata in database.

        Args:
            session_id: Session ID
            metadata_dict: Metadata dictionary
            iteration_id: Optional iteration ID

        Returns:
            Metadata ID

        Raises:
            DatabaseError: If metadata storage fails
        """
        if not self.conn:
            raise DatabaseError("Database not initialized")

        # Convert metadata to JSON
        metadata_json = json.dumps(metadata_dict, sort_keys=True)

        # Calculate hash for deduplication
        metadata_hash = hashlib.sha256(metadata_json.encode()).hexdigest()

        with self.db_lock:
            # Check if identical metadata already exists
            cursor = self.conn.execute(
                "SELECT metadata_id FROM metadata WHERE metadata_hash = ?", (metadata_hash,)
            )
            existing = cursor.fetchone()

            if existing:
                logger.debug(f"Metadata already exists with hash {metadata_hash[:8]}")
                return existing["metadata_id"]

            # Insert new metadata
            cursor = self.conn.cursor()
            try:
                cursor.execute(
                    """
                    INSERT INTO metadata (
                        session_id, iteration_id, collection_time,
                        collection_type, metadata_json, metadata_hash
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        iteration_id,
                        metadata_dict.get("collection_time", datetime.now(timezone.utc).isoformat()),
                        metadata_dict.get("collection_type", "unknown"),
                        metadata_json,
                        metadata_hash,
                    ),
                )

                self.conn.commit()
                metadata_id = cursor.lastrowid

                logger.info(f"Stored metadata {metadata_id} for session {session_id}")
                return metadata_id
            except sqlite3.Error as exc:
                msg = f"Failed to store metadata: {exc}"
                logger.error(msg)
                raise DatabaseError(msg) from exc

    def get_metadata(self, metadata_id: int) -> Optional[Dict[str, Any]]:
        """Get metadata by ID.

        Args:
            metadata_id: Metadata ID

        Returns:
            Metadata dictionary or None if not found
        """
        if not self.conn:
            raise DatabaseError("Database not initialized")

        with self.db_lock:
            cursor = self.conn.execute(
                "SELECT * FROM metadata WHERE metadata_id = ?", (metadata_id,)
            )

            row = cursor.fetchone()
            if not row:
                return None

            return {
                "metadata_id": row["metadata_id"],
                "session_id": row["session_id"],
                "iteration_id": row["iteration_id"],
                "collection_time": row["collection_time"],
                "collection_type": row["collection_type"],
                "metadata": json.loads(row["metadata_json"]),
                "metadata_hash": row["metadata_hash"],
            }

    def get_session_metadata(
        self, session_id: int, collection_type: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Get all metadata for a session.

        Args:
            session_id: Session ID
            collection_type: Optional filter by collection type

        Returns:
            List of metadata dictionaries
        """
        if not self.conn:
            raise DatabaseError("Database not initialized")

        with self.db_lock:
            if collection_type:
                cursor = self.conn.execute(
                    """
                    SELECT * FROM metadata
                    WHERE session_id = ? AND collection_type = ?
                    ORDER BY collection_time
                    """,
                    (session_id, collection_type),
                )
            else:
                cursor = self.conn.execute(
                    """
                    SELECT * FROM metadata
                    WHERE session_id = ?
                    ORDER BY collection_time
                    """,
                    (session_id,),
                )

            results = []
            for row in cursor.fetchall():
                results.append(
                    {
                        "metadata_id": row["metadata_id"],
                        "session_id": row["session_id"],
                        "iteration_id": row["iteration_id"],
                        "collection_time": row["collection_time"],
                        "collection_type": row["collection_type"],
                        "metadata": json.loads(row["metadata_json"]),
                        "metadata_hash": row["metadata_hash"],
                    }
                )

            return results

    def get_baseline_metadata(self, session_id: int) -> Optional[Dict[str, Any]]:
        """Get baseline metadata for a session.

        Args:
            session_id: Session ID

        Returns:
            Baseline metadata dictionary or None
        """
        metadata_list = self.get_session_metadata(session_id, "baseline")
        return metadata_list[0] if metadata_list else None

    def store_metadata_file(
        self, metadata_id: int, file_type: str, file_path: str, compressed: bool = False
    ) -> int:
        """Store reference to a metadata file.

        Args:
            metadata_id: Metadata ID
            file_type: Type of file
            file_path: Path to file
            compressed: Whether file is compressed

        Returns:
            File ID

        Raises:
            DatabaseError: If file storage fails
        """
        if not self.conn:
            raise DatabaseError("Database not initialized")

        # Calculate file hash and size
        file_hash = None
        file_size = 0

        path = Path(file_path)
        if path.exists():
            with path.open("rb") as f:
                content = f.read()
                file_hash = hashlib.sha256(content).hexdigest()
                file_size = len(content)

        with self.db_lock:
            cursor = self.conn.cursor()
            try:
                cursor.execute(
                    """
                    INSERT INTO metadata_files (
                        metadata_id, file_type, file_path,
                        file_hash, file_size, compressed
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (metadata_id, file_type, file_path, file_hash, file_size, compressed),
                )

                self.conn.commit()
                return cursor.lastrowid
            except sqlite3.Error as exc:
                msg = f"Failed to store metadata file: {exc}"
                logger.error(msg)
                raise DatabaseError(msg) from exc

    def get_metadata_files(self, metadata_id: int) -> List[Dict[str, Any]]:
        """Get all files associated with metadata.

        Args:
            metadata_id: Metadata ID

        Returns:
            List of file dictionaries
        """
        if not self.conn:
            raise DatabaseError("Database not initialized")

        with self.db_lock:
            cursor = self.conn.execute(
                """
                SELECT * FROM metadata_files
                WHERE metadata_id = ?
                """,
                (metadata_id,),
            )

            return [dict(row) for row in cursor.fetchall()]

    def generate_summary(self, session_id: int) -> Dict[str, Any]:
        """Generate summary of bisection session.

        Args:
            session_id: Session ID

        Returns:
            Summary dictionary
        """
        session = self.get_session(session_id)
        if not session:
            return {}

        iterations = self.get_iterations(session_id)

        # Count results
        results = {"good": 0, "bad": 0, "skip": 0, "unknown": 0}

        for it in iterations:
            if it.final_result:
                results[it.final_result] = results.get(it.final_result, 0) + 1
            else:
                results["unknown"] += 1

        # Calculate total time
        total_duration = sum(it.duration for it in iterations if it.duration)

        return {
            "session_id": session_id,
            "good_commit": session.good_commit,
            "bad_commit": session.bad_commit,
            "start_time": session.start_time,
            "end_time": session.end_time,
            "status": session.status,
            "result_commit": session.result_commit,
            "total_iterations": len(iterations),
            "results": results,
            "total_duration_seconds": total_duration,
            "iterations": [asdict(it) for it in iterations],
        }

    def export_report(self, session_id: int, format: str = "json") -> str:
        """Export bisection report.

        Args:
            session_id: Session ID
            format: Output format (json or text)

        Returns:
            Report string
        """
        summary = self.generate_summary(session_id)

        if format == "json":
            return json.dumps(summary, indent=2)

        if format == "text":
            report = []
            report.append("=" * 70)
            report.append("KERNEL BISECTION REPORT")
            report.append("=" * 70)
            report.append(f"\nSession ID: {summary['session_id']}")
            report.append(f"Good commit: {summary['good_commit']}")
            report.append(f"Bad commit:  {summary['bad_commit']}")
            report.append(f"Status: {summary['status']}")

            if summary["result_commit"]:
                report.append(f"\nFirst bad commit: {summary['result_commit']}")

            report.append(f"\nTotal iterations: {summary['total_iterations']}")
            report.append(f"Total time: {summary['total_duration_seconds']}s")

            report.append("\nResults breakdown:")
            for result, count in summary["results"].items():
                report.append(f"  {result}: {count}")

            report.append("\n" + "-" * 70)
            report.append("Iteration Details:")
            report.append("-" * 70)

            for it in summary["iterations"]:
                report.append(
                    f"\n{it['iteration_num']:3d}. {it['commit_sha'][:7]} | "
                    f"{it['final_result'] or 'unknown':7s} | "
                    f"{it['duration'] or 0:4d}s"
                )
                report.append(f"     {it['commit_message']}")

                if it["error_message"]:
                    report.append(f"     Error: {it['error_message']}")

            report.append("\n" + "=" * 70)

            return "\n".join(report)

        return ""

    def close(self) -> None:
        """Close database connection."""
        with self.db_lock:
            if self.conn:
                try:
                    self.conn.close()
                    self.conn = None
                except sqlite3.Error as exc:
                    logger.error(f"Error closing database connection: {exc}")


def main() -> int:
    """Test state manager."""
    logging.basicConfig(level=logging.INFO)

    state = StateManager("/tmp/test-bisect.db")

    # Create test session
    session_id = state.create_session("abc123", "def456", {"test": True})
    print(f"Created session: {session_id}")

    # Create test iterations
    it1 = state.create_iteration(session_id, 1, "commit1", "First commit")
    state.update_iteration(it1, final_result="good", duration=120)

    it2 = state.create_iteration(session_id, 2, "commit2", "Second commit")
    state.update_iteration(it2, final_result="bad", duration=150)

    # Generate report
    print("\nReport:")
    print(state.export_report(session_id, format="text"))

    state.close()
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
