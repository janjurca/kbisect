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
    hosts: Mapped[List["Host"]] = relationship(
        "Host", back_populates="session", cascade="all, delete-orphan"
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
    iteration_results: Mapped[List["IterationResult"]] = relationship(
        "IterationResult", back_populates="iteration", cascade="all, delete-orphan"
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
    host_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("hosts.host_id"), nullable=True)
    log_type: Mapped[str] = mapped_column(String, nullable=False)
    timestamp: Mapped[str] = mapped_column(String, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)

    # Relationships
    iteration: Mapped["Iteration"] = relationship("Iteration", back_populates="logs")
    host: Mapped[Optional["Host"]] = relationship("Host")

    def __repr__(self) -> str:
        return f"<Log(id={self.log_id}, type={self.log_type}, iteration={self.iteration_id})>"


class BuildLog(Base):
    """Build log model with compression.

    Stores build/boot/test logs as compressed BLOBs.
    """

    __tablename__ = "build_logs"

    log_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    iteration_id: Mapped[int] = mapped_column(Integer, ForeignKey("iterations.iteration_id"), nullable=False)
    host_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("hosts.host_id"), nullable=True)
    log_type: Mapped[str] = mapped_column(String, nullable=False)
    timestamp: Mapped[str] = mapped_column(String, nullable=False)
    log_content: Mapped[bytes] = mapped_column(BLOB, nullable=False)
    compressed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    size_bytes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    exit_code: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Relationships
    iteration: Mapped["Iteration"] = relationship("Iteration", back_populates="build_logs")
    host: Mapped[Optional["Host"]] = relationship("Host")

    def __repr__(self) -> str:
        return (
            f"<BuildLog(id={self.log_id}, type={self.log_type}, "
            f"size={self.size_bytes}, iteration={self.iteration_id})>"
        )


class Metadata(Base):
    """Metadata collection model.

    Stores system metadata as text content. The collection_type field specifies
    the nature of the metadata (e.g., kernel_config, rpmqa, etc.).
    """

    __tablename__ = "metadata"

    metadata_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(Integer, ForeignKey("sessions.session_id"), nullable=False)
    iteration_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("iterations.iteration_id"), nullable=True
    )
    host_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("hosts.host_id"), nullable=True)
    collection_time: Mapped[str] = mapped_column(String, nullable=False)
    collection_type: Mapped[str] = mapped_column(String, nullable=False)
    data: Mapped[str] = mapped_column(Text, nullable=False)

    # Relationships
    session: Mapped["Session"] = relationship("Session", back_populates="metadata_records")
    host: Mapped[Optional["Host"]] = relationship("Host")

    def __repr__(self) -> str:
        return (
            f"<Metadata(id={self.metadata_id}, type={self.collection_type}, "
            f"session={self.session_id})>"
        )


class Host(Base):
    """Host configuration model for multi-host bisection.

    Represents a single host/machine participating in the bisection.
    Each host can have its own test script and configuration.
    """

    __tablename__ = "hosts"

    host_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(Integer, ForeignKey("sessions.session_id"), nullable=False)
    hostname: Mapped[str] = mapped_column(String, nullable=False)
    ssh_user: Mapped[str] = mapped_column(String, nullable=False, default="root")
    kernel_path: Mapped[str] = mapped_column(String, nullable=False)
    bisect_path: Mapped[str] = mapped_column(String, nullable=False)
    test_script: Mapped[str] = mapped_column(String, nullable=False)
    ipmi_host: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    ipmi_user: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    ipmi_password: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    # Relationships
    session: Mapped["Session"] = relationship("Session", back_populates="hosts")
    iteration_results: Mapped[List["IterationResult"]] = relationship(
        "IterationResult", back_populates="host", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return (
            f"<Host(id={self.host_id}, hostname={self.hostname}, "
            f"test_script={self.test_script})>"
        )


class IterationResult(Base):
    """Per-host test result for a single iteration.

    In multi-host mode, each iteration can have multiple results - one per host.
    All hosts must pass for the iteration to be marked as GOOD.
    """

    __tablename__ = "iteration_results"

    result_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    iteration_id: Mapped[int] = mapped_column(Integer, ForeignKey("iterations.iteration_id"), nullable=False)
    host_id: Mapped[int] = mapped_column(Integer, ForeignKey("hosts.host_id"), nullable=False)
    build_result: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    boot_result: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    test_result: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    final_result: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    timestamp: Mapped[str] = mapped_column(String, nullable=False)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    test_output: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Relationships
    iteration: Mapped["Iteration"] = relationship("Iteration", back_populates="iteration_results")
    host: Mapped["Host"] = relationship("Host", back_populates="iteration_results")

    def __repr__(self) -> str:
        return (
            f"<IterationResult(id={self.result_id}, iteration={self.iteration_id}, "
            f"host={self.host_id}, result={self.final_result})>"
        )
