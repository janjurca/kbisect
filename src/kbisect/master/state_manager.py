#!/usr/bin/env python3
"""State Manager - Persistent state storage using SQLAlchemy ORM.

Tracks bisection progress, test results, and generates reports.
Migrated from raw sqlite3 to SQLAlchemy 2.0 for better type safety and maintainability.
"""

import gzip
import hashlib
import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy import create_engine, select
from sqlalchemy.orm import scoped_session, sessionmaker

from kbisect.master.models import (
    Base,
    BuildLog,
    Iteration,
    Log,
    Metadata,
    MetadataFile,
    Session as SessionModel,
)


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
    """Manage bisection state using SQLAlchemy ORM.

    Provides methods to create and manage bisection sessions, iterations,
    metadata, and generate reports.

    Attributes:
        db_path: Path to SQLite database file
        engine: SQLAlchemy engine
        Session: Scoped session factory
    """

    def __init__(self, db_path: str = DEFAULT_DB_PATH) -> None:
        """Initialize state manager with SQLAlchemy.

        Args:
            db_path: Path to SQLite database file
        """
        self.db_path = db_path

        # Ensure directory exists
        db_parent = Path(db_path).parent
        if db_parent != Path():
            db_parent.mkdir(parents=True, exist_ok=True)

        # Create SQLAlchemy engine with connection pooling
        # Use check_same_thread=False for thread safety
        self.engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False},
            echo=False,  # Set to True for SQL debugging
        )

        # Create scoped session factory (thread-safe)
        session_factory = sessionmaker(bind=self.engine)
        self.Session = scoped_session(session_factory)

        # Initialize database schema
        self._init_database()

    def _init_database(self) -> None:
        """Initialize database schema."""
        try:
            # Create all tables if they don't exist
            Base.metadata.create_all(self.engine)
            logger.debug(f"Database initialized at {self.db_path}")
        except Exception as exc:
            msg = f"Failed to initialize database: {exc}"
            logger.error(msg)
            raise DatabaseError(msg) from exc

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
        session = self.Session()
        try:
            new_session = SessionModel(
                good_commit=good_commit,
                bad_commit=bad_commit,
                start_time=datetime.now(timezone.utc).isoformat(),
                status="running",
                config=json.dumps(config) if config else None,
            )

            session.add(new_session)
            session.commit()
            session_id = new_session.session_id

            logger.info(f"Created bisection session {session_id}")
            return session_id

        except Exception as exc:
            session.rollback()
            msg = f"Failed to create session: {exc}"
            logger.error(msg)
            raise DatabaseError(msg) from exc
        finally:
            session.close()

    def get_session(self, session_id: int) -> Optional[BisectSession]:
        """Get session by ID.

        Args:
            session_id: Session ID to retrieve

        Returns:
            BisectSession object or None if not found
        """
        session = self.Session()
        try:
            stmt = select(SessionModel).where(SessionModel.session_id == session_id)
            result = session.execute(stmt).scalar_one_or_none()

            if not result:
                return None

            return BisectSession(
                session_id=result.session_id,
                good_commit=result.good_commit,
                bad_commit=result.bad_commit,
                start_time=result.start_time,
                end_time=result.end_time,
                status=result.status,
                result_commit=result.result_commit,
            )
        finally:
            session.close()

    def get_latest_session(self) -> Optional[BisectSession]:
        """Get most recent session.

        Returns:
            BisectSession object or None if no sessions exist
        """
        session = self.Session()
        try:
            stmt = select(SessionModel).order_by(SessionModel.session_id.desc()).limit(1)
            result = session.execute(stmt).scalar_one_or_none()

            if not result:
                return None

            return BisectSession(
                session_id=result.session_id,
                good_commit=result.good_commit,
                bad_commit=result.bad_commit,
                start_time=result.start_time,
                end_time=result.end_time,
                status=result.status,
                result_commit=result.result_commit,
            )
        finally:
            session.close()

    def get_or_create_session(
        self, good_commit: str, bad_commit: str, config: Optional[Dict[str, Any]] = None
    ) -> int:
        """Get existing running session or create new one (atomic operation).

        This method prevents race conditions by atomically checking for and
        creating sessions within a single database transaction.

        Args:
            good_commit: Known good commit hash
            bad_commit: Known bad commit hash
            config: Optional configuration dict

        Returns:
            Session ID (existing or newly created)

        Raises:
            DatabaseError: If session operation fails
        """
        session = self.Session()
        try:
            # Check for existing running session
            stmt = (
                select(SessionModel)
                .where(SessionModel.status == "running")
                .order_by(SessionModel.session_id.desc())
                .limit(1)
            )
            existing = session.execute(stmt).scalar_one_or_none()

            if existing:
                session_id = existing.session_id
                logger.info(f"Found existing running session {session_id}")
                return session_id

            # No running session found, create new one
            new_session = SessionModel(
                good_commit=good_commit,
                bad_commit=bad_commit,
                start_time=datetime.now(timezone.utc).isoformat(),
                status="running",
                config=json.dumps(config) if config else None,
            )

            session.add(new_session)
            session.commit()
            session_id = new_session.session_id

            logger.info(f"Created new bisection session {session_id}")
            return session_id

        except Exception as exc:
            session.rollback()
            msg = f"Failed to get or create session: {exc}"
            logger.error(msg)
            raise DatabaseError(msg) from exc
        finally:
            session.close()

    def update_session(self, session_id: int, **kwargs: Any) -> None:
        """Update session fields.

        Args:
            session_id: Session ID to update
            **kwargs: Fields to update (end_time, status, result_commit, session_state)

        Raises:
            DatabaseError: If update fails
        """
        session = self.Session()
        try:
            stmt = select(SessionModel).where(SessionModel.session_id == session_id)
            db_session = session.execute(stmt).scalar_one_or_none()

            if not db_session:
                logger.warning(f"Session {session_id} not found for update")
                return

            # Update allowed fields
            valid_fields = {"end_time", "status", "result_commit", "session_state"}
            for field, value in kwargs.items():
                if field in valid_fields:
                    setattr(db_session, field, value)

            session.commit()

        except Exception as exc:
            session.rollback()
            msg = f"Failed to update session: {exc}"
            logger.error(msg)
            raise DatabaseError(msg) from exc
        finally:
            session.close()

    def update_session_state(self, session_id: int, state_dict: Dict[str, Any]) -> None:
        """Update session state JSON.

        Args:
            session_id: Session ID
            state_dict: State dictionary to store

        Raises:
            DatabaseError: If update fails
        """
        session = self.Session()
        try:
            stmt = select(SessionModel).where(SessionModel.session_id == session_id)
            db_session = session.execute(stmt).scalar_one_or_none()

            if not db_session:
                logger.warning(f"Session {session_id} not found for state update")
                return

            # Convert state to JSON
            db_session.session_state = json.dumps(state_dict)
            session.commit()

            logger.debug(f"Updated session state for session {session_id}")

        except Exception as exc:
            session.rollback()
            msg = f"Failed to update session state: {exc}"
            logger.error(msg)
            raise DatabaseError(msg) from exc
        finally:
            session.close()

    def get_session_state(self, session_id: int) -> Optional[Dict[str, Any]]:
        """Get session state JSON.

        Args:
            session_id: Session ID

        Returns:
            State dictionary or None if not found
        """
        session = self.Session()
        try:
            stmt = select(SessionModel).where(SessionModel.session_id == session_id)
            db_session = session.execute(stmt).scalar_one_or_none()

            if not db_session or not db_session.session_state:
                return None

            return json.loads(db_session.session_state)

        except Exception as exc:
            logger.error(f"Failed to get session state: {exc}")
            return None
        finally:
            session.close()

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
        session = self.Session()
        try:
            new_iteration = Iteration(
                session_id=session_id,
                iteration_num=iteration_num,
                commit_sha=commit_sha,
                commit_message=commit_message,
                start_time=datetime.now(timezone.utc).isoformat(),
            )

            session.add(new_iteration)
            session.commit()
            iteration_id = new_iteration.iteration_id

            logger.debug(f"Created iteration {iteration_id}")
            return iteration_id

        except Exception as exc:
            session.rollback()
            msg = f"Failed to create iteration: {exc}"
            logger.error(msg)
            raise DatabaseError(msg) from exc
        finally:
            session.close()

    def update_iteration(self, iteration_id: int, **kwargs: Any) -> None:
        """Update iteration fields.

        Args:
            iteration_id: Iteration ID to update
            **kwargs: Fields to update

        Raises:
            DatabaseError: If update fails
        """
        session = self.Session()
        try:
            stmt = select(Iteration).where(Iteration.iteration_id == iteration_id)
            db_iteration = session.execute(stmt).scalar_one_or_none()

            if not db_iteration:
                logger.warning(f"Iteration {iteration_id} not found for update")
                return

            # Update allowed fields
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
            for field, value in kwargs.items():
                if field in valid_fields:
                    setattr(db_iteration, field, value)

            session.commit()

        except Exception as exc:
            session.rollback()
            msg = f"Failed to update iteration: {exc}"
            logger.error(msg)
            raise DatabaseError(msg) from exc
        finally:
            session.close()

    def get_iterations(self, session_id: int) -> List[TestIteration]:
        """Get all iterations for a session.

        Args:
            session_id: Session ID

        Returns:
            List of TestIteration objects
        """
        session = self.Session()
        try:
            stmt = (
                select(Iteration)
                .where(Iteration.session_id == session_id)
                .order_by(Iteration.iteration_num)
            )
            results = session.execute(stmt).scalars().all()

            iterations = []
            for row in results:
                iterations.append(
                    TestIteration(
                        iteration_id=row.iteration_id,
                        session_id=row.session_id,
                        iteration_num=row.iteration_num,
                        commit_sha=row.commit_sha,
                        commit_message=row.commit_message,
                        build_result=row.build_result,
                        boot_result=row.boot_result,
                        test_result=row.test_result,
                        final_result=row.final_result,
                        start_time=row.start_time,
                        end_time=row.end_time,
                        duration=row.duration,
                        error_message=row.error_message,
                        kernel_version=row.kernel_version,
                    )
                )

            return iterations
        finally:
            session.close()

    def add_log(self, iteration_id: int, log_type: str, message: str) -> None:
        """Add log entry for an iteration.

        Args:
            iteration_id: Iteration ID
            log_type: Type of log entry
            message: Log message

        Raises:
            DatabaseError: If log creation fails
        """
        session = self.Session()
        try:
            new_log = Log(
                iteration_id=iteration_id,
                log_type=log_type,
                timestamp=datetime.now(timezone.utc).isoformat(),
                message=message,
            )

            session.add(new_log)
            session.commit()

        except Exception as exc:
            session.rollback()
            msg = f"Failed to add log: {exc}"
            logger.error(msg)
            raise DatabaseError(msg) from exc
        finally:
            session.close()

    def get_logs(self, iteration_id: int) -> List[Dict[str, Any]]:
        """Get logs for an iteration.

        Args:
            iteration_id: Iteration ID

        Returns:
            List of log dictionaries
        """
        session = self.Session()
        try:
            stmt = (
                select(Log)
                .where(Log.iteration_id == iteration_id)
                .order_by(Log.timestamp)
            )
            results = session.execute(stmt).scalars().all()

            return [
                {
                    "log_id": log.log_id,
                    "iteration_id": log.iteration_id,
                    "log_type": log.log_type,
                    "timestamp": log.timestamp,
                    "message": log.message,
                }
                for log in results
            ]
        finally:
            session.close()

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
        session = self.Session()
        try:
            # Compress log content
            compressed_content = gzip.compress(content.encode("utf-8"))
            size_bytes = len(compressed_content)

            new_log = BuildLog(
                iteration_id=iteration_id,
                log_type=log_type,
                timestamp=datetime.now(timezone.utc).isoformat(),
                log_content=compressed_content,
                compressed=True,
                size_bytes=size_bytes,
                exit_code=exit_code,
            )

            session.add(new_log)
            session.commit()
            log_id = new_log.log_id

            logger.debug(f"Stored {log_type} log {log_id} ({size_bytes} bytes compressed)")
            return log_id

        except Exception as exc:
            session.rollback()
            msg = f"Failed to store build log: {exc}"
            logger.error(msg)
            raise DatabaseError(msg) from exc
        finally:
            session.close()

    def get_build_log(self, log_id: int) -> Optional[Dict[str, Any]]:
        """Get and decompress build log by ID.

        Args:
            log_id: Log ID to retrieve

        Returns:
            Dictionary with log data including decompressed content, or None if not found
        """
        session = self.Session()
        try:
            stmt = (
                select(BuildLog, Iteration)
                .join(Iteration, BuildLog.iteration_id == Iteration.iteration_id)
                .where(BuildLog.log_id == log_id)
            )
            result = session.execute(stmt).first()

            if not result:
                return None

            build_log, iteration = result

            # Decompress content
            content = build_log.log_content
            if build_log.compressed:
                content = gzip.decompress(content).decode("utf-8")
            else:
                content = content.decode("utf-8")

            return {
                "log_id": build_log.log_id,
                "iteration_id": build_log.iteration_id,
                "iteration_num": iteration.iteration_num,
                "commit_sha": iteration.commit_sha,
                "commit_message": iteration.commit_message,
                "log_type": build_log.log_type,
                "timestamp": build_log.timestamp,
                "content": content,
                "size_bytes": build_log.size_bytes,
                "exit_code": build_log.exit_code,
                "compressed": build_log.compressed,
            }
        finally:
            session.close()

    def get_iteration_build_logs(self, iteration_id: int) -> List[Dict[str, Any]]:
        """Get all build logs for an iteration.

        Args:
            iteration_id: Iteration ID

        Returns:
            List of log metadata dictionaries (without content)
        """
        session = self.Session()
        try:
            stmt = (
                select(BuildLog)
                .where(BuildLog.iteration_id == iteration_id)
                .order_by(BuildLog.timestamp)
            )
            results = session.execute(stmt).scalars().all()

            return [
                {
                    "log_id": log.log_id,
                    "log_type": log.log_type,
                    "timestamp": log.timestamp,
                    "size_bytes": log.size_bytes,
                    "exit_code": log.exit_code,
                }
                for log in results
            ]
        finally:
            session.close()

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
        session = self.Session()
        try:
            stmt = (
                select(BuildLog, Iteration)
                .join(Iteration, BuildLog.iteration_id == Iteration.iteration_id)
            )

            # Add filters
            if session_id is not None:
                stmt = stmt.where(Iteration.session_id == session_id)
            if log_type is not None:
                stmt = stmt.where(BuildLog.log_type == log_type)

            stmt = stmt.order_by(BuildLog.timestamp.desc())

            results = session.execute(stmt).all()

            logs = []
            for build_log, iteration in results:
                logs.append({
                    "log_id": build_log.log_id,
                    "iteration_id": build_log.iteration_id,
                    "iteration_num": iteration.iteration_num,
                    "commit_sha": iteration.commit_sha,
                    "log_type": build_log.log_type,
                    "timestamp": build_log.timestamp,
                    "size_bytes": build_log.size_bytes,
                    "exit_code": build_log.exit_code,
                    "status": "SUCCESS" if build_log.exit_code == 0 else "FAILED",
                })

            return logs
        finally:
            session.close()

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
        session = self.Session()
        try:
            # Convert metadata to JSON
            metadata_json = json.dumps(metadata_dict, sort_keys=True)

            # Calculate hash for deduplication
            metadata_hash = hashlib.sha256(metadata_json.encode()).hexdigest()

            # Check if identical metadata already exists
            stmt = select(Metadata).where(Metadata.metadata_hash == metadata_hash)
            existing = session.execute(stmt).scalar_one_or_none()

            if existing:
                logger.debug(f"Metadata already exists with hash {metadata_hash[:8]}")
                return existing.metadata_id

            # Insert new metadata
            new_metadata = Metadata(
                session_id=session_id,
                iteration_id=iteration_id,
                collection_time=metadata_dict.get(
                    "collection_time", datetime.now(timezone.utc).isoformat()
                ),
                collection_type=metadata_dict.get("collection_type", "unknown"),
                metadata_json=metadata_json,
                metadata_hash=metadata_hash,
            )

            session.add(new_metadata)
            session.commit()
            metadata_id = new_metadata.metadata_id

            logger.info(f"Stored metadata {metadata_id} for session {session_id}")
            return metadata_id

        except Exception as exc:
            session.rollback()
            msg = f"Failed to store metadata: {exc}"
            logger.error(msg)
            raise DatabaseError(msg) from exc
        finally:
            session.close()

    def get_metadata(self, metadata_id: int) -> Optional[Dict[str, Any]]:
        """Get metadata by ID.

        Args:
            metadata_id: Metadata ID

        Returns:
            Metadata dictionary or None if not found
        """
        session = self.Session()
        try:
            stmt = select(Metadata).where(Metadata.metadata_id == metadata_id)
            result = session.execute(stmt).scalar_one_or_none()

            if not result:
                return None

            return {
                "metadata_id": result.metadata_id,
                "session_id": result.session_id,
                "iteration_id": result.iteration_id,
                "collection_time": result.collection_time,
                "collection_type": result.collection_type,
                "metadata": json.loads(result.metadata_json),
                "metadata_hash": result.metadata_hash,
            }
        finally:
            session.close()

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
        session = self.Session()
        try:
            stmt = (
                select(Metadata)
                .where(Metadata.session_id == session_id)
                .order_by(Metadata.collection_time)
            )

            if collection_type:
                stmt = stmt.where(Metadata.collection_type == collection_type)

            results = session.execute(stmt).scalars().all()

            return [
                {
                    "metadata_id": meta.metadata_id,
                    "session_id": meta.session_id,
                    "iteration_id": meta.iteration_id,
                    "collection_time": meta.collection_time,
                    "collection_type": meta.collection_type,
                    "metadata": json.loads(meta.metadata_json),
                    "metadata_hash": meta.metadata_hash,
                }
                for meta in results
            ]
        finally:
            session.close()

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
        session = self.Session()
        try:
            # Calculate file hash and size
            file_hash = None
            file_size = 0

            path = Path(file_path)
            if path.exists():
                with path.open("rb") as f:
                    content = f.read()
                    file_hash = hashlib.sha256(content).hexdigest()
                    file_size = len(content)

            new_file = MetadataFile(
                metadata_id=metadata_id,
                file_type=file_type,
                file_path=file_path,
                file_hash=file_hash,
                file_size=file_size,
                compressed=compressed,
            )

            session.add(new_file)
            session.commit()
            file_id = new_file.file_id

            return file_id

        except Exception as exc:
            session.rollback()
            msg = f"Failed to store metadata file: {exc}"
            logger.error(msg)
            raise DatabaseError(msg) from exc
        finally:
            session.close()

    def store_metadata_file_content(
        self, metadata_id: int, file_type: str, content: bytes, compress: bool = True
    ) -> int:
        """Store metadata file content directly in database.

        Args:
            metadata_id: Metadata ID
            file_type: Type of file
            content: File content as bytes
            compress: Whether to compress content (default: True)

        Returns:
            File ID

        Raises:
            DatabaseError: If file storage fails
        """
        session = self.Session()
        try:
            # Optionally compress content
            file_content = content
            if compress:
                file_content = gzip.compress(content)

            # Calculate hash and size
            file_hash = hashlib.sha256(content).hexdigest()
            file_size = len(content)

            new_file = MetadataFile(
                metadata_id=metadata_id,
                file_type=file_type,
                file_path=None,  # No file path for DB-only storage
                file_content=file_content,
                file_hash=file_hash,
                file_size=file_size,
                compressed=compress,
            )

            session.add(new_file)
            session.commit()
            file_id = new_file.file_id

            logger.debug(
                f"Stored {file_type} file in DB (file_id: {file_id}, "
                f"size: {file_size} bytes, compressed: {compress})"
            )
            return file_id

        except Exception as exc:
            session.rollback()
            msg = f"Failed to store metadata file content: {exc}"
            logger.error(msg)
            raise DatabaseError(msg) from exc
        finally:
            session.close()

    def get_metadata_file_content(self, file_id: int) -> Optional[bytes]:
        """Get metadata file content from database.

        Args:
            file_id: File ID

        Returns:
            File content as bytes, or None if not found

        Raises:
            DatabaseError: If file retrieval fails
        """
        session = self.Session()
        try:
            stmt = select(MetadataFile).where(MetadataFile.file_id == file_id)
            file_record = session.execute(stmt).scalar_one_or_none()

            if not file_record or not file_record.file_content:
                return None

            # Decompress if needed
            content = file_record.file_content
            if file_record.compressed:
                content = gzip.decompress(content)

            return content

        except Exception as exc:
            msg = f"Failed to get metadata file content: {exc}"
            logger.error(msg)
            raise DatabaseError(msg) from exc
        finally:
            session.close()

    def get_metadata_files(self, metadata_id: int) -> List[Dict[str, Any]]:
        """Get all files associated with metadata.

        Args:
            metadata_id: Metadata ID

        Returns:
            List of file dictionaries
        """
        session = self.Session()
        try:
            stmt = select(MetadataFile).where(MetadataFile.metadata_id == metadata_id)
            results = session.execute(stmt).scalars().all()

            return [
                {
                    "file_id": file.file_id,
                    "metadata_id": file.metadata_id,
                    "file_type": file.file_type,
                    "file_path": file.file_path,
                    "file_hash": file.file_hash,
                    "file_size": file.file_size,
                    "compressed": file.compressed,
                }
                for file in results
            ]
        finally:
            session.close()

    def generate_summary(self, session_id: int) -> Dict[str, Any]:
        """Generate summary of bisection session.

        Args:
            session_id: Session ID

        Returns:
            Summary dictionary
        """
        session_data = self.get_session(session_id)
        if not session_data:
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
            "good_commit": session_data.good_commit,
            "bad_commit": session_data.bad_commit,
            "start_time": session_data.start_time,
            "end_time": session_data.end_time,
            "status": session_data.status,
            "result_commit": session_data.result_commit,
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
        """Close database connection and cleanup."""
        try:
            # Remove scoped session
            self.Session.remove()
            # Dispose of engine connection pool
            self.engine.dispose()
            logger.debug("Database connections closed")
        except Exception as exc:
            logger.error(f"Error closing database: {exc}")


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
