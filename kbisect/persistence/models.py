#!/usr/bin/env python3
"""SQLAlchemy ORM Models for Kernel Bisection Database.

Defines database schema using SQLAlchemy declarative models.
Compatible with existing sqlite3 schema for backward compatibility.
"""

from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import (
    BLOB,
    Boolean,
    Column,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Base class for all ORM models."""

    pass


class Session(Base):
    """Bisection session model.

    Tracks a complete bisection run from start to finish.
    """

    __tablename__ = "sessions"

    session_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    good_commit: Mapped[str] = mapped_column(String, nullable=False)
    bad_commit: Mapped[str] = mapped_column(String, nullable=False)
    start_time: Mapped[str] = mapped_column(String, nullable=False)
    end_time: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="running")
    result_commit: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    config: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON as TEXT
    session_state: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # Bisection state JSON

    # Relationships
    iterations: Mapped[List["Iteration"]] = relationship(
        "Iteration", back_populates="session", cascade="all, delete-orphan"
    )
    metadata_records: Mapped[List["Metadata"]] = relationship(
        "Metadata", back_populates="session", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return (
            f"<Session(id={self.session_id}, status={self.status}, "
            f"good={self.good_commit[:7]}, bad={self.bad_commit[:7]})>"
        )


class Iteration(Base):
    """Test iteration model.

    Represents a single build/boot/test cycle for a specific commit.
    """

    __tablename__ = "iterations"

    iteration_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(Integer, ForeignKey("sessions.session_id"), nullable=False)
    iteration_num: Mapped[int] = mapped_column(Integer, nullable=False)
    commit_sha: Mapped[str] = mapped_column(String, nullable=False)
    commit_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    build_result: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    boot_result: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    test_result: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    final_result: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    start_time: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    end_time: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    duration: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    kernel_version: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    # Relationships
    session: Mapped["Session"] = relationship("Session", back_populates="iterations")
    logs: Mapped[List["Log"]] = relationship(
        "Log", back_populates="iteration", cascade="all, delete-orphan"
    )
    build_logs: Mapped[List["BuildLog"]] = relationship(
        "BuildLog", back_populates="iteration", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return (
            f"<Iteration(id={self.iteration_id}, num={self.iteration_num}, "
            f"commit={self.commit_sha[:7]}, result={self.final_result})>"
        )


class Log(Base):
    """Simple log entry model.

    Stores lightweight log messages for iterations.
    """

    __tablename__ = "logs"

    log_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    iteration_id: Mapped[int] = mapped_column(Integer, ForeignKey("iterations.iteration_id"), nullable=False)
    log_type: Mapped[str] = mapped_column(String, nullable=False)
    timestamp: Mapped[str] = mapped_column(String, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)

    # Relationships
    iteration: Mapped["Iteration"] = relationship("Iteration", back_populates="logs")

    def __repr__(self) -> str:
        return f"<Log(id={self.log_id}, type={self.log_type}, iteration={self.iteration_id})>"


class BuildLog(Base):
    """Build log model with compression.

    Stores build/boot/test logs as compressed BLOBs.
    """

    __tablename__ = "build_logs"

    log_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    iteration_id: Mapped[int] = mapped_column(Integer, ForeignKey("iterations.iteration_id"), nullable=False)
    log_type: Mapped[str] = mapped_column(String, nullable=False)
    timestamp: Mapped[str] = mapped_column(String, nullable=False)
    log_content: Mapped[bytes] = mapped_column(BLOB, nullable=False)
    compressed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    size_bytes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    exit_code: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Relationships
    iteration: Mapped["Iteration"] = relationship("Iteration", back_populates="build_logs")

    def __repr__(self) -> str:
        return (
            f"<BuildLog(id={self.log_id}, type={self.log_type}, "
            f"size={self.size_bytes}, iteration={self.iteration_id})>"
        )


class Metadata(Base):
    """Metadata collection model.

    Stores system metadata with JSON content and hash-based deduplication.
    """

    __tablename__ = "metadata"

    metadata_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(Integer, ForeignKey("sessions.session_id"), nullable=False)
    iteration_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("iterations.iteration_id"), nullable=True
    )
    collection_time: Mapped[str] = mapped_column(String, nullable=False)
    collection_type: Mapped[str] = mapped_column(String, nullable=False)
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_hash: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    # Relationships
    session: Mapped["Session"] = relationship("Session", back_populates="metadata_records")
    files: Mapped[List["MetadataFile"]] = relationship(
        "MetadataFile", back_populates="metadata_record", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return (
            f"<Metadata(id={self.metadata_id}, type={self.collection_type}, "
            f"session={self.session_id})>"
        )


class MetadataFile(Base):
    """Metadata file model with optional embedded content.

    Stores file content directly in database as BLOB or references external files.
    """

    __tablename__ = "metadata_files"

    file_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    metadata_id: Mapped[int] = mapped_column(Integer, ForeignKey("metadata.metadata_id"), nullable=False)
    file_type: Mapped[str] = mapped_column(String, nullable=False)
    file_path: Mapped[Optional[str]] = mapped_column(String, nullable=True)  # Optional for DB-only storage
    file_content: Mapped[Optional[bytes]] = mapped_column(BLOB, nullable=True)  # File content as BLOB
    file_hash: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    file_size: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    compressed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Relationships
    metadata_record: Mapped["Metadata"] = relationship("Metadata", back_populates="files")

    def __repr__(self) -> str:
        storage = "DB" if self.file_content is not None else f"path={self.file_path}"
        return (
            f"<MetadataFile(id={self.file_id}, type={self.file_type}, "
            f"{storage})>"
        )
