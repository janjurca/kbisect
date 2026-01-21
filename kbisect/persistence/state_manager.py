#!/usr/bin/env python3
"""State Manager - Persistent state storage using SQLAlchemy ORM.

Tracks bisection progress, test results, and generates reports.
Migrated from raw sqlite3 to SQLAlchemy 2.0 for better type safety and maintainability.
"""

import gzip
import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy import create_engine, select
from sqlalchemy.orm import scoped_session, sessionmaker

from kbisect.persistence.models import (
    Base,
    BuildLog,
    Host,
    Iteration,
    IterationResult,
    Log,
    Metadata,
)
from kbisect.persistence.models import (
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

            # Run migrations for existing databases
            self._run_migrations()

            logger.debug(f"Database initialized at {self.db_path}")
        except Exception as exc:
            msg = f"Failed to initialize database: {exc}"
            logger.error(msg)
            raise DatabaseError(msg) from exc

    def _run_migrations(self) -> None:
        """Run database migrations for schema changes.

        Adds new columns to existing tables if they don't exist.
        This ensures backward compatibility with existing databases.
        """
        import sqlite3

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        try:
            # Migration: Add host_id to logs table
            cursor.execute("PRAGMA table_info(logs)")
            logs_columns = [col[1] for col in cursor.fetchall()]
            if "host_id" not in logs_columns:
                logger.info("Adding host_id column to logs table")
                cursor.execute(
                    "ALTER TABLE logs ADD COLUMN host_id INTEGER REFERENCES hosts(host_id)"
                )
                conn.commit()

            # Migration: Add host_id to build_logs table
            cursor.execute("PRAGMA table_info(build_logs)")
            build_logs_columns = [col[1] for col in cursor.fetchall()]
            if "host_id" not in build_logs_columns:
                logger.info("Adding host_id column to build_logs table")
                cursor.execute(
                    "ALTER TABLE build_logs ADD COLUMN host_id INTEGER REFERENCES hosts(host_id)"
                )
                conn.commit()

            # Migration: Add host_id to metadata table
            cursor.execute("PRAGMA table_info(metadata)")
            metadata_columns = [col[1] for col in cursor.fetchall()]
            if "host_id" not in metadata_columns:
                logger.info("Adding host_id column to metadata table")
                cursor.execute(
                    "ALTER TABLE metadata ADD COLUMN host_id INTEGER REFERENCES hosts(host_id)"
                )
                conn.commit()

        except Exception as exc:
            conn.rollback()
            logger.error(f"Migration failed: {exc}")
            raise
        finally:
            conn.close()

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

    def create_host(
        self,
        session_id: int,
        hostname: str,
        ssh_user: str,
        kernel_path: str,
        bisect_path: str,
        test_script: str,
        power_control_type: Optional[str] = "ipmi",
        ipmi_host: Optional[str] = None,
        ipmi_user: Optional[str] = None,
        ipmi_password: Optional[str] = None,
    ) -> int:
        """Create new host configuration.

        Args:
            session_id: Parent session ID
            hostname: Hostname or IP address
            ssh_user: SSH username
            kernel_path: Path to kernel directory on host
            bisect_path: Path to bisect scripts on host
            test_script: Path to test script for this host
            power_control_type: Power control method ("ipmi", "beaker", or None)
            ipmi_host: Optional IPMI hostname
            ipmi_user: Optional IPMI username
            ipmi_password: Optional IPMI password

        Returns:
            Host ID

        Raises:
            DatabaseError: If host creation fails
        """
        session = self.Session()
        try:
            new_host = Host(
                session_id=session_id,
                hostname=hostname,
                ssh_user=ssh_user,
                kernel_path=kernel_path,
                bisect_path=bisect_path,
                test_script=test_script,
                power_control_type=power_control_type,
                ipmi_host=ipmi_host,
                ipmi_user=ipmi_user,
                ipmi_password=ipmi_password,
            )

            session.add(new_host)
            session.commit()
            host_id = new_host.host_id

            logger.info(f"Created host {host_id}: {hostname}")
            return host_id

        except Exception as exc:
            session.rollback()
            msg = f"Failed to create host: {exc}"
            logger.error(msg)
            raise DatabaseError(msg) from exc
        finally:
            session.close()

    def get_hosts(self, session_id: int) -> List[Dict[str, Any]]:
        """Get all hosts for a session.

        Args:
            session_id: Session ID

        Returns:
            List of host dictionaries
        """
        session = self.Session()
        try:
            stmt = select(Host).where(Host.session_id == session_id).order_by(Host.host_id)
            results = session.execute(stmt).scalars().all()

            return [
                {
                    "host_id": host.host_id,
                    "session_id": host.session_id,
                    "hostname": host.hostname,
                    "ssh_user": host.ssh_user,
                    "kernel_path": host.kernel_path,
                    "bisect_path": host.bisect_path,
                    "test_script": host.test_script,
                    "ipmi_host": host.ipmi_host,
                    "ipmi_user": host.ipmi_user,
                    "ipmi_password": host.ipmi_password,
                }
                for host in results
            ]
        finally:
            session.close()

    def get_host(self, host_id: int) -> Optional[Dict[str, Any]]:
        """Get host by ID.

        Args:
            host_id: Host ID

        Returns:
            Host dictionary or None if not found
        """
        session = self.Session()
        try:
            stmt = select(Host).where(Host.host_id == host_id)
            host = session.execute(stmt).scalar_one_or_none()

            if not host:
                return None

            return {
                "host_id": host.host_id,
                "session_id": host.session_id,
                "hostname": host.hostname,
                "ssh_user": host.ssh_user,
                "kernel_path": host.kernel_path,
                "bisect_path": host.bisect_path,
                "test_script": host.test_script,
                "ipmi_host": host.ipmi_host,
                "ipmi_user": host.ipmi_user,
                "ipmi_password": host.ipmi_password,
            }
        finally:
            session.close()

    def create_iteration_result(
        self,
        iteration_id: int,
        host_id: int,
        build_result: Optional[str] = None,
        boot_result: Optional[str] = None,
        test_result: Optional[str] = None,
        final_result: Optional[str] = None,
        error_message: Optional[str] = None,
        test_output: Optional[str] = None,
    ) -> int:
        """Create per-host iteration result.

        Args:
            iteration_id: Iteration ID
            host_id: Host ID
            build_result: Build result (success, failure)
            boot_result: Boot result (success, failure, timeout)
            test_result: Test result (pass, fail)
            final_result: Final verdict (good, bad, skip)
            error_message: Optional error message
            test_output: Optional test output

        Returns:
            Result ID

        Raises:
            DatabaseError: If result creation fails
        """
        session = self.Session()
        try:
            new_result = IterationResult(
                iteration_id=iteration_id,
                host_id=host_id,
                build_result=build_result,
                boot_result=boot_result,
                test_result=test_result,
                final_result=final_result,
                timestamp=datetime.now(timezone.utc).isoformat(),
                error_message=error_message,
                test_output=test_output,
            )

            session.add(new_result)
            session.commit()
            result_id = new_result.result_id

            logger.debug(f"Created iteration result {result_id} for host {host_id}")
            return result_id

        except Exception as exc:
            session.rollback()
            msg = f"Failed to create iteration result: {exc}"
            logger.error(msg)
            raise DatabaseError(msg) from exc
        finally:
            session.close()

    def create_iteration_results_bulk(self, results: List[Dict[str, Any]]) -> List[int]:
        """Create multiple per-host iteration results in a single transaction.

        Args:
            results: List of result dictionaries, each containing:
                - iteration_id: Iteration ID
                - host_id: Host ID
                - build_result: Build result (success, failure)
                - boot_result: Boot result (success, failure, timeout)
                - test_result: Test result (pass, fail)
                - final_result: Final verdict (good, bad, skip)
                - error_message: Optional error message
                - test_output: Optional test output

        Returns:
            List of result IDs

        Raises:
            DatabaseError: If bulk creation fails
        """
        session = self.Session()
        try:
            result_ids = []
            current_timestamp = datetime.now(timezone.utc).isoformat()

            for result_data in results:
                new_result = IterationResult(
                    iteration_id=result_data["iteration_id"],
                    host_id=result_data["host_id"],
                    build_result=result_data.get("build_result"),
                    boot_result=result_data.get("boot_result"),
                    test_result=result_data.get("test_result"),
                    final_result=result_data.get("final_result"),
                    timestamp=current_timestamp,
                    error_message=result_data.get("error_message"),
                    test_output=result_data.get("test_output"),
                )
                session.add(new_result)

            # Commit all results at once
            session.commit()

            # Get result IDs after commit
            for obj in session.new:
                if isinstance(obj, IterationResult):
                    result_ids.append(obj.result_id)

            logger.debug(f"Created {len(result_ids)} iteration results in bulk")
            return result_ids

        except Exception as exc:
            session.rollback()
            msg = f"Failed to create iteration results in bulk: {exc}"
            logger.error(msg)
            raise DatabaseError(msg) from exc
        finally:
            session.close()

    def update_iteration_result(self, result_id: int, **kwargs: Any) -> None:
        """Update iteration result fields.

        Args:
            result_id: Result ID to update
            **kwargs: Fields to update

        Raises:
            DatabaseError: If update fails
        """
        session = self.Session()
        try:
            stmt = select(IterationResult).where(IterationResult.result_id == result_id)
            db_result = session.execute(stmt).scalar_one_or_none()

            if not db_result:
                logger.warning(f"IterationResult {result_id} not found for update")
                return

            # Update allowed fields
            valid_fields = {
                "build_result",
                "boot_result",
                "test_result",
                "final_result",
                "error_message",
                "test_output",
            }
            for field, value in kwargs.items():
                if field in valid_fields:
                    setattr(db_result, field, value)

            # Update timestamp
            db_result.timestamp = datetime.now(timezone.utc).isoformat()

            session.commit()

        except Exception as exc:
            session.rollback()
            msg = f"Failed to update iteration result: {exc}"
            logger.error(msg)
            raise DatabaseError(msg) from exc
        finally:
            session.close()

    def get_iteration_results(self, iteration_id: int) -> List[Dict[str, Any]]:
        """Get all per-host results for an iteration.

        Args:
            iteration_id: Iteration ID

        Returns:
            List of result dictionaries with host information
        """
        session = self.Session()
        try:
            stmt = (
                select(IterationResult, Host)
                .join(Host, IterationResult.host_id == Host.host_id)
                .where(IterationResult.iteration_id == iteration_id)
                .order_by(Host.host_id)
            )
            results = session.execute(stmt).all()

            return [
                {
                    "result_id": result.result_id,
                    "iteration_id": result.iteration_id,
                    "host_id": result.host_id,
                    "hostname": host.hostname,
                    "build_result": result.build_result,
                    "boot_result": result.boot_result,
                    "test_result": result.test_result,
                    "final_result": result.final_result,
                    "timestamp": result.timestamp,
                    "error_message": result.error_message,
                    "test_output": result.test_output,
                }
                for result, host in results
            ]
        finally:
            session.close()

    def add_log(
        self, iteration_id: int, log_type: str, message: str, host_id: Optional[int] = None
    ) -> None:
        """Add log entry for an iteration.

        Args:
            iteration_id: Iteration ID
            log_type: Type of log entry
            message: Log message
            host_id: Optional host ID to link log to specific machine

        Raises:
            DatabaseError: If log creation fails
        """
        session = self.Session()
        try:
            new_log = Log(
                iteration_id=iteration_id,
                host_id=host_id,
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
            stmt = select(Log).where(Log.iteration_id == iteration_id).order_by(Log.timestamp)
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

    def create_build_log(
        self,
        iteration_id: int,
        log_type: str,
        initial_content: str = "",
        host_id: Optional[int] = None,
    ) -> int:
        """Create initial build log entry for streaming.

        Args:
            iteration_id: Iteration ID
            log_type: Type of log (build, boot, test)
            initial_content: Optional initial log content (e.g., header)
            host_id: Optional host ID to link log to specific machine

        Returns:
            Log ID

        Raises:
            DatabaseError: If log creation fails
        """
        session = self.Session()
        try:
            # Compress initial content if provided
            compressed_content = (
                gzip.compress(initial_content.encode("utf-8")) if initial_content else b""
            )
            size_bytes = len(compressed_content)

            new_log = BuildLog(
                iteration_id=iteration_id,
                host_id=host_id,
                log_type=log_type,
                timestamp=datetime.now(timezone.utc).isoformat(),
                log_content=compressed_content,
                compressed=True,
                size_bytes=size_bytes,
                exit_code=None,  # Will be set when build completes
            )

            session.add(new_log)
            session.commit()
            log_id = new_log.log_id

            logger.debug(f"Created {log_type} log {log_id} for streaming")
            return log_id

        except Exception as exc:
            session.rollback()
            msg = f"Failed to create build log: {exc}"
            logger.error(msg)
            raise DatabaseError(msg) from exc
        finally:
            session.close()

    def append_build_log_chunk(self, log_id: int, chunk: str) -> None:
        """Append content to existing build log.

        Args:
            log_id: Log ID to append to
            chunk: Log content chunk to append

        Raises:
            DatabaseError: If append fails
        """
        session = self.Session()
        try:
            # Get existing log
            stmt = select(BuildLog).where(BuildLog.log_id == log_id)
            build_log = session.execute(stmt).scalar_one_or_none()

            if not build_log:
                raise DatabaseError(f"Build log {log_id} not found")

            # Decompress existing content
            if build_log.compressed and build_log.log_content:
                existing_content = gzip.decompress(build_log.log_content).decode("utf-8")
            else:
                existing_content = (
                    build_log.log_content.decode("utf-8") if build_log.log_content else ""
                )

            # Append new chunk
            updated_content = existing_content + chunk

            # Recompress
            compressed_content = gzip.compress(updated_content.encode("utf-8"))
            build_log.log_content = compressed_content
            build_log.size_bytes = len(compressed_content)

            session.commit()
            logger.debug(
                f"Appended {len(chunk)} bytes to log {log_id} (total compressed: {len(compressed_content)} bytes)"
            )

        except Exception as exc:
            session.rollback()
            msg = f"Failed to append to build log: {exc}"
            logger.error(msg)
            raise DatabaseError(msg) from exc
        finally:
            session.close()

    def finalize_build_log(self, log_id: int, exit_code: int) -> None:
        """Finalize build log with exit code.

        Args:
            log_id: Log ID to finalize
            exit_code: Exit code of the build process

        Raises:
            DatabaseError: If finalization fails
        """
        session = self.Session()
        try:
            stmt = select(BuildLog).where(BuildLog.log_id == log_id)
            build_log = session.execute(stmt).scalar_one_or_none()

            if not build_log:
                raise DatabaseError(f"Build log {log_id} not found")

            build_log.exit_code = exit_code
            session.commit()
            logger.debug(f"Finalized log {log_id} with exit code {exit_code}")

        except Exception as exc:
            session.rollback()
            msg = f"Failed to finalize build log: {exc}"
            logger.error(msg)
            raise DatabaseError(msg) from exc
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
                select(BuildLog, Iteration, Host)
                .join(Iteration, BuildLog.iteration_id == Iteration.iteration_id)
                .outerjoin(Host, BuildLog.host_id == Host.host_id)
            )

            # Add filters
            if session_id is not None:
                stmt = stmt.where(Iteration.session_id == session_id)
            if log_type is not None:
                stmt = stmt.where(BuildLog.log_type == log_type)

            stmt = stmt.order_by(BuildLog.timestamp.desc())

            results = session.execute(stmt).all()

            logs = []
            for build_log, iteration, host in results:
                logs.append(
                    {
                        "log_id": build_log.log_id,
                        "iteration_id": build_log.iteration_id,
                        "iteration_num": iteration.iteration_num,
                        "commit_sha": iteration.commit_sha,
                        "log_type": build_log.log_type,
                        "timestamp": build_log.timestamp,
                        "size_bytes": build_log.size_bytes,
                        "exit_code": build_log.exit_code,
                        "hostname": host.hostname if host else None,
                        "status": (
                            "RUNNING"
                            if build_log.exit_code is None
                            else ("SUCCESS" if build_log.exit_code == 0 else "FAILED")
                        ),
                    }
                )

            return logs
        finally:
            session.close()

    def store_metadata(
        self,
        session_id: int,
        metadata_dict: Dict[str, Any],
        iteration_id: Optional[int] = None,
        host_id: Optional[int] = None,
    ) -> int:
        """Store metadata in database.

        Args:
            session_id: Session ID
            metadata_dict: Metadata dictionary
            iteration_id: Optional iteration ID
            host_id: Optional host ID to link metadata to specific machine

        Returns:
            Metadata ID

        Raises:
            DatabaseError: If metadata storage fails
        """
        session = self.Session()
        try:
            # Convert metadata to JSON
            data = json.dumps(metadata_dict, sort_keys=True)

            # Insert new metadata
            new_metadata = Metadata(
                session_id=session_id,
                iteration_id=iteration_id,
                host_id=host_id,
                collection_time=metadata_dict.get(
                    "collection_time", datetime.now(timezone.utc).isoformat()
                ),
                collection_type=metadata_dict.get("collection_type", "unknown"),
                data=data,
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

    def update_metadata(
        self,
        metadata_id: int,
        metadata_dict: Dict[str, Any],
    ) -> bool:
        """Update existing metadata record with new data.

        Args:
            metadata_id: Metadata ID to update
            metadata_dict: New metadata dictionary

        Returns:
            True if updated successfully, False otherwise

        Raises:
            DatabaseError: If metadata update fails
        """
        session = self.Session()
        try:
            # Get existing metadata record
            stmt = select(Metadata).where(Metadata.metadata_id == metadata_id)
            existing = session.execute(stmt).scalar_one_or_none()

            if not existing:
                logger.warning(f"Metadata record {metadata_id} not found for update")
                return False

            # Update metadata fields
            data = json.dumps(metadata_dict, sort_keys=True)

            existing.data = data
            existing.collection_time = metadata_dict.get(
                "collection_time", existing.collection_time
            )
            existing.collection_type = metadata_dict.get(
                "collection_type", existing.collection_type
            )

            session.commit()
            logger.debug(f"Updated metadata record {metadata_id}")
            return True

        except Exception as exc:
            session.rollback()
            msg = f"Failed to update metadata: {exc}"
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

            # Try to parse as JSON, otherwise return raw string
            try:
                metadata_content = json.loads(result.data)
            except (json.JSONDecodeError, ValueError):
                metadata_content = result.data

            return {
                "metadata_id": result.metadata_id,
                "session_id": result.session_id,
                "iteration_id": result.iteration_id,
                "collection_time": result.collection_time,
                "collection_type": result.collection_type,
                "metadata": metadata_content,
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
                select(Metadata, Host)
                .outerjoin(Host, Metadata.host_id == Host.host_id)
                .where(Metadata.session_id == session_id)
                .order_by(Metadata.collection_time)
            )

            if collection_type:
                stmt = stmt.where(Metadata.collection_type == collection_type)

            results = session.execute(stmt).all()

            metadata_list = []
            for meta, host in results:
                # Try to parse as JSON, otherwise use raw string
                try:
                    metadata_content = json.loads(meta.data)
                except (json.JSONDecodeError, ValueError):
                    metadata_content = meta.data

                metadata_list.append(
                    {
                        "metadata_id": meta.metadata_id,
                        "session_id": meta.session_id,
                        "iteration_id": meta.iteration_id,
                        "collection_time": meta.collection_time,
                        "collection_type": meta.collection_type,
                        "hostname": host.hostname if host else None,
                        "metadata": metadata_content,
                    }
                )

            return metadata_list
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

    def store_file_metadata(
        self,
        session_id: int,
        iteration_id: Optional[int],
        file_type: str,
        file_content: str,
        host_id: Optional[int] = None,
        **_extra_metadata: Any,
    ) -> int:
        """Store file as a metadata record with collection_type='file'.

        Args:
            session_id: Session ID
            iteration_id: Iteration ID (None for session-level files)
            file_type: Type of file (e.g., 'kernel_config')
            file_content: File content as text
            host_id: Optional host ID to link metadata to specific machine
            **extra_metadata: Additional metadata to include in JSON

        Returns:
            Metadata ID of the created file record

        Raises:
            DatabaseError: If file storage fails
        """
        db_session = self.Session()
        try:
            # Calculate size for logging
            file_size = len(file_content)

            # Create metadata record with file content as data
            new_metadata = Metadata(
                session_id=session_id,
                iteration_id=iteration_id,
                host_id=host_id,
                collection_time=datetime.now(timezone.utc).isoformat(),
                collection_type=file_type,  # Use file_type as collection_type (e.g., 'kernel_config')
                data=file_content,  # Store raw file content directly
            )

            db_session.add(new_metadata)
            db_session.commit()
            metadata_id = new_metadata.metadata_id

            logger.debug(
                f"Stored {file_type} file as metadata (metadata_id: {metadata_id}, "
                f"size: {file_size} bytes)"
            )
            return metadata_id

        except Exception as exc:
            db_session.rollback()
            msg = f"Failed to store file metadata: {exc}"
            logger.error(msg)
            raise DatabaseError(msg) from exc
        finally:
            db_session.close()

    def get_file_content(self, metadata_id: int) -> Optional[str]:
        """Get file content from a metadata record.

        Args:
            metadata_id: Metadata ID of the file record

        Returns:
            File content as text, or None if not found

        Raises:
            DatabaseError: If file retrieval fails
        """
        db_session = self.Session()
        try:
            stmt = select(Metadata).where(Metadata.metadata_id == metadata_id)
            metadata = db_session.execute(stmt).scalar_one_or_none()

            if not metadata:
                return None

            # Return raw data content directly
            return metadata.data

        except Exception as exc:
            msg = f"Failed to get file content: {exc}"
            logger.error(msg)
            raise DatabaseError(msg) from exc
        finally:
            db_session.close()

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

            # Show first bad commit prominently at the end if found
            if summary["result_commit"]:
                report.append("\n" + "=" * 70)
                report.append("FIRST BAD COMMIT:")
                report.append("=" * 70)

                # Find the iteration matching the first bad commit
                first_bad_iteration = None
                for it in summary["iterations"]:
                    if it["commit_sha"].startswith(summary["result_commit"][:7]):
                        first_bad_iteration = it
                        break

                if first_bad_iteration:
                    report.append(
                        f"# first bad commit: [{first_bad_iteration['commit_sha']}] "
                        f"{first_bad_iteration['commit_message']}"
                    )
                else:
                    report.append(f"# first bad commit: {summary['result_commit']}")

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
