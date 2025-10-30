#!/usr/bin/env python3
"""
State Manager - Persistent state storage using SQLite
Tracks bisection progress, test results, and generates reports
"""

import sqlite3
import json
import logging
from pathlib import Path
from typing import Optional, List, Dict
from datetime import datetime, timezone
from dataclasses import dataclass, asdict

logger = logging.getLogger(__name__)


@dataclass
class BisectSession:
    """Bisection session"""
    session_id: int
    good_commit: str
    bad_commit: str
    start_time: str
    end_time: Optional[str] = None
    status: str = "running"
    result_commit: Optional[str] = None


@dataclass
class TestIteration:
    """Test iteration record"""
    iteration_id: int
    session_id: int
    iteration_num: int
    commit_sha: str
    commit_message: str
    build_result: Optional[str] = None
    boot_result: Optional[str] = None
    test_result: Optional[str] = None
    final_result: Optional[str] = None  # good, bad, skip
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    duration: Optional[int] = None
    error_message: Optional[str] = None
    kernel_version: Optional[str] = None


class StateManager:
    """Manage bisection state in SQLite database"""

    def __init__(self, db_path: str = "bisect.db"):
        self.db_path = db_path
        self.conn: Optional[sqlite3.Connection] = None

        # Ensure directory exists (only needed if db_path contains subdirectories)
        db_parent = Path(db_path).parent
        if db_parent != Path("."):
            db_parent.mkdir(parents=True, exist_ok=True)

        # Initialize database
        self._init_database()

    def _init_database(self):
        """Initialize database schema"""
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row

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

    def create_session(self, good_commit: str, bad_commit: str, config: Optional[Dict] = None) -> int:
        """Create new bisection session"""
        cursor = self.conn.cursor()

        cursor.execute("""
            INSERT INTO sessions (good_commit, bad_commit, start_time, config)
            VALUES (?, ?, ?, ?)
        """, (
            good_commit,
            bad_commit,
            datetime.now(timezone.utc).isoformat(),
            json.dumps(config) if config else None
        ))

        self.conn.commit()
        session_id = cursor.lastrowid

        logger.info(f"Created bisection session {session_id}")
        return session_id

    def get_session(self, session_id: int) -> Optional[BisectSession]:
        """Get session by ID"""
        cursor = self.conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?",
            (session_id,)
        )

        row = cursor.fetchone()
        if not row:
            return None

        return BisectSession(
            session_id=row['session_id'],
            good_commit=row['good_commit'],
            bad_commit=row['bad_commit'],
            start_time=row['start_time'],
            end_time=row['end_time'],
            status=row['status'],
            result_commit=row['result_commit']
        )

    def get_latest_session(self) -> Optional[BisectSession]:
        """Get most recent session"""
        cursor = self.conn.execute(
            "SELECT * FROM sessions ORDER BY session_id DESC LIMIT 1"
        )

        row = cursor.fetchone()
        if not row:
            return None

        return BisectSession(
            session_id=row['session_id'],
            good_commit=row['good_commit'],
            bad_commit=row['bad_commit'],
            start_time=row['start_time'],
            end_time=row['end_time'],
            status=row['status'],
            result_commit=row['result_commit']
        )

    def update_session(self, session_id: int, **kwargs):
        """Update session fields"""
        valid_fields = ['end_time', 'status', 'result_commit']
        updates = {k: v for k, v in kwargs.items() if k in valid_fields}

        if not updates:
            return

        set_clause = ', '.join([f"{k} = ?" for k in updates.keys()])
        values = list(updates.values()) + [session_id]

        self.conn.execute(
            f"UPDATE sessions SET {set_clause} WHERE session_id = ?",
            values
        )
        self.conn.commit()

    def create_iteration(self, session_id: int, iteration_num: int,
                        commit_sha: str, commit_message: str) -> int:
        """Create new iteration"""
        cursor = self.conn.cursor()

        cursor.execute("""
            INSERT INTO iterations (
                session_id, iteration_num, commit_sha, commit_message, start_time
            ) VALUES (?, ?, ?, ?, ?)
        """, (
            session_id,
            iteration_num,
            commit_sha,
            commit_message,
            datetime.now(timezone.utc).isoformat()
        ))

        self.conn.commit()
        iteration_id = cursor.lastrowid

        logger.debug(f"Created iteration {iteration_id}")
        return iteration_id

    def update_iteration(self, iteration_id: int, **kwargs):
        """Update iteration fields"""
        valid_fields = [
            'build_result', 'boot_result', 'test_result', 'final_result',
            'end_time', 'duration', 'error_message', 'kernel_version'
        ]
        updates = {k: v for k, v in kwargs.items() if k in valid_fields}

        if not updates:
            return

        set_clause = ', '.join([f"{k} = ?" for k in updates.keys()])
        values = list(updates.values()) + [iteration_id]

        self.conn.execute(
            f"UPDATE iterations SET {set_clause} WHERE iteration_id = ?",
            values
        )
        self.conn.commit()

    def get_iterations(self, session_id: int) -> List[TestIteration]:
        """Get all iterations for a session"""
        cursor = self.conn.execute(
            """
            SELECT * FROM iterations
            WHERE session_id = ?
            ORDER BY iteration_num
            """,
            (session_id,)
        )

        iterations = []
        for row in cursor.fetchall():
            iterations.append(TestIteration(
                iteration_id=row['iteration_id'],
                session_id=row['session_id'],
                iteration_num=row['iteration_num'],
                commit_sha=row['commit_sha'],
                commit_message=row['commit_message'],
                build_result=row['build_result'],
                boot_result=row['boot_result'],
                test_result=row['test_result'],
                final_result=row['final_result'],
                start_time=row['start_time'],
                end_time=row['end_time'],
                duration=row['duration'],
                error_message=row['error_message'],
                kernel_version=row['kernel_version']
            ))

        return iterations

    def add_log(self, iteration_id: int, log_type: str, message: str):
        """Add log entry for an iteration"""
        self.conn.execute("""
            INSERT INTO logs (iteration_id, log_type, timestamp, message)
            VALUES (?, ?, ?, ?)
        """, (
            iteration_id,
            log_type,
            datetime.now(timezone.utc).isoformat(),
            message
        ))
        self.conn.commit()

    def get_logs(self, iteration_id: int) -> List[Dict]:
        """Get logs for an iteration"""
        cursor = self.conn.execute(
            """
            SELECT * FROM logs
            WHERE iteration_id = ?
            ORDER BY timestamp
            """,
            (iteration_id,)
        )

        return [dict(row) for row in cursor.fetchall()]

    def generate_summary(self, session_id: int) -> Dict:
        """Generate summary of bisection session"""
        session = self.get_session(session_id)
        if not session:
            return {}

        iterations = self.get_iterations(session_id)

        # Count results
        results = {
            'good': 0,
            'bad': 0,
            'skip': 0,
            'unknown': 0
        }

        for it in iterations:
            if it.final_result:
                results[it.final_result] = results.get(it.final_result, 0) + 1
            else:
                results['unknown'] += 1

        # Calculate total time
        total_duration = sum(it.duration for it in iterations if it.duration)

        summary = {
            'session_id': session_id,
            'good_commit': session.good_commit,
            'bad_commit': session.bad_commit,
            'start_time': session.start_time,
            'end_time': session.end_time,
            'status': session.status,
            'result_commit': session.result_commit,
            'total_iterations': len(iterations),
            'results': results,
            'total_duration_seconds': total_duration,
            'iterations': [asdict(it) for it in iterations]
        }

        return summary

    def export_report(self, session_id: int, format: str = "json") -> str:
        """Export bisection report"""
        summary = self.generate_summary(session_id)

        if format == "json":
            return json.dumps(summary, indent=2)

        elif format == "text":
            report = []
            report.append("=" * 70)
            report.append("KERNEL BISECTION REPORT")
            report.append("=" * 70)
            report.append(f"\nSession ID: {summary['session_id']}")
            report.append(f"Good commit: {summary['good_commit']}")
            report.append(f"Bad commit:  {summary['bad_commit']}")
            report.append(f"Status: {summary['status']}")

            if summary['result_commit']:
                report.append(f"\nFirst bad commit: {summary['result_commit']}")

            report.append(f"\nTotal iterations: {summary['total_iterations']}")
            report.append(f"Total time: {summary['total_duration_seconds']}s")

            report.append("\nResults breakdown:")
            for result, count in summary['results'].items():
                report.append(f"  {result}: {count}")

            report.append("\n" + "-" * 70)
            report.append("Iteration Details:")
            report.append("-" * 70)

            for it in summary['iterations']:
                report.append(f"\n{it['iteration_num']:3d}. {it['commit_sha'][:7]} | "
                            f"{it['final_result'] or 'unknown':7s} | "
                            f"{it['duration'] or 0:4d}s")
                report.append(f"     {it['commit_message']}")

                if it['error_message']:
                    report.append(f"     Error: {it['error_message']}")

            report.append("\n" + "=" * 70)

            return '\n'.join(report)

        return ""

    def store_metadata(self, session_id: int, metadata_dict: Dict,
                      iteration_id: Optional[int] = None) -> int:
        """Store metadata in database"""
        import hashlib

        # Convert metadata to JSON
        metadata_json = json.dumps(metadata_dict, sort_keys=True)

        # Calculate hash for deduplication
        metadata_hash = hashlib.sha256(metadata_json.encode()).hexdigest()

        # Check if identical metadata already exists
        cursor = self.conn.execute(
            "SELECT metadata_id FROM metadata WHERE metadata_hash = ?",
            (metadata_hash,)
        )
        existing = cursor.fetchone()

        if existing:
            logger.debug(f"Metadata already exists with hash {metadata_hash[:8]}")
            return existing['metadata_id']

        # Insert new metadata
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT INTO metadata (
                session_id, iteration_id, collection_time,
                collection_type, metadata_json, metadata_hash
            ) VALUES (?, ?, ?, ?, ?, ?)
        """, (
            session_id,
            iteration_id,
            metadata_dict.get('collection_time', datetime.now(timezone.utc).isoformat()),
            metadata_dict.get('collection_type', 'unknown'),
            metadata_json,
            metadata_hash
        ))

        self.conn.commit()
        metadata_id = cursor.lastrowid

        logger.info(f"Stored metadata {metadata_id} for session {session_id}")
        return metadata_id

    def get_metadata(self, metadata_id: int) -> Optional[Dict]:
        """Get metadata by ID"""
        cursor = self.conn.execute(
            "SELECT * FROM metadata WHERE metadata_id = ?",
            (metadata_id,)
        )

        row = cursor.fetchone()
        if not row:
            return None

        return {
            'metadata_id': row['metadata_id'],
            'session_id': row['session_id'],
            'iteration_id': row['iteration_id'],
            'collection_time': row['collection_time'],
            'collection_type': row['collection_type'],
            'metadata': json.loads(row['metadata_json']),
            'metadata_hash': row['metadata_hash']
        }

    def get_session_metadata(self, session_id: int,
                            collection_type: Optional[str] = None) -> List[Dict]:
        """Get all metadata for a session"""
        if collection_type:
            cursor = self.conn.execute("""
                SELECT * FROM metadata
                WHERE session_id = ? AND collection_type = ?
                ORDER BY collection_time
            """, (session_id, collection_type))
        else:
            cursor = self.conn.execute("""
                SELECT * FROM metadata
                WHERE session_id = ?
                ORDER BY collection_time
            """, (session_id,))

        results = []
        for row in cursor.fetchall():
            results.append({
                'metadata_id': row['metadata_id'],
                'session_id': row['session_id'],
                'iteration_id': row['iteration_id'],
                'collection_time': row['collection_time'],
                'collection_type': row['collection_type'],
                'metadata': json.loads(row['metadata_json']),
                'metadata_hash': row['metadata_hash']
            })

        return results

    def get_baseline_metadata(self, session_id: int) -> Optional[Dict]:
        """Get baseline metadata for a session"""
        metadata_list = self.get_session_metadata(session_id, 'baseline')
        return metadata_list[0] if metadata_list else None

    def store_metadata_file(self, metadata_id: int, file_type: str,
                           file_path: str, compressed: bool = False) -> int:
        """Store reference to a metadata file"""
        import hashlib

        # Calculate file hash and size
        file_hash = None
        file_size = 0

        if Path(file_path).exists():
            with open(file_path, 'rb') as f:
                content = f.read()
                file_hash = hashlib.sha256(content).hexdigest()
                file_size = len(content)

        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT INTO metadata_files (
                metadata_id, file_type, file_path,
                file_hash, file_size, compressed
            ) VALUES (?, ?, ?, ?, ?, ?)
        """, (
            metadata_id,
            file_type,
            file_path,
            file_hash,
            file_size,
            compressed
        ))

        self.conn.commit()
        return cursor.lastrowid

    def get_metadata_files(self, metadata_id: int) -> List[Dict]:
        """Get all files associated with metadata"""
        cursor = self.conn.execute("""
            SELECT * FROM metadata_files
            WHERE metadata_id = ?
        """, (metadata_id,))

        return [dict(row) for row in cursor.fetchall()]

    def close(self):
        """Close database connection"""
        if self.conn:
            self.conn.close()


def main():
    """Test state manager"""
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
