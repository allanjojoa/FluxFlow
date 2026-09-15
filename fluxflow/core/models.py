"""Shared data models used across FluxFlow."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# Configuration models
# ---------------------------------------------------------------------------

@dataclass
class EnvironmentConfig:
    """Connection details for a single target environment."""

    name: str
    region: str
    base_url: str
    token: str
    workspace_name: str
    workspace_id: str | None = None


@dataclass
class ArtifactConfig:
    """Desired artifact reference inside a task definition."""

    name: str
    version: str


@dataclass
class TaskConfig:
    """Desired state of a single task as declared in release_config.yaml."""

    name: str
    artifact: ArtifactConfig
    parameters: dict[str, str] = field(default_factory=dict)
    studio_connection: dict[str, Any] = field(default_factory=dict)
    processing: dict[str, Any] = field(default_factory=dict)


@dataclass
class ReleaseConfig:
    """Parsed contents of release_config.yaml."""

    connector: str
    tasks: list[TaskConfig] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Remote state models (fetched from the connector)
# ---------------------------------------------------------------------------

@dataclass
class RemoteArtifact:
    """Artifact as it exists on the remote platform."""

    id: str
    name: str
    version: str
    versions: list[str] = field(default_factory=list)


@dataclass
class RemoteTask:
    """Task as it exists on the remote platform."""

    id: str
    name: str
    artifact: RemoteArtifact | None = None
    parameters: dict[str, str] = field(default_factory=dict)
    workspace_id: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Build models
# ---------------------------------------------------------------------------

class ChangeType(str, Enum):
    """Type of change detected between desired and remote state."""

    NEW_TASK = "NEW_TASK"
    UPDATE_ARTIFACT = "UPDATE_ARTIFACT"
    UPDATE_PARAMS = "UPDATE_PARAMS"
    UPDATE_ARTIFACT_AND_PARAMS = "UPDATE_ARTIFACT_AND_PARAMS"
    PROMOTE_ARTIFACT = "PROMOTE_ARTIFACT"
    NO_CHANGE = "NO_CHANGE"


@dataclass
class ParamDiff:
    """A single parameter change."""

    key: str
    old_value: str | None
    new_value: str | None
    action: str  # "added", "removed", "changed"


@dataclass
class BuildChange:
    """Represents a single change detected during the build stage."""

    task_name: str
    change_type: ChangeType
    remote_task_id: str | None = None
    artifact_old_version: str | None = None
    artifact_new_version: str | None = None
    artifact_name: str | None = None
    param_diffs: list[ParamDiff] = field(default_factory=list)
    desired_parameters: dict[str, str] = field(default_factory=dict)
    promotion_needed: bool = False
    promotion_source_env: str | None = None


@dataclass
class BuildManifest:
    """Complete build output — collection of changes + metadata."""

    environment: str
    connector: str
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    changes: list[BuildChange] = field(default_factory=list)
    status: str = "SUCCESS"

    @property
    def has_changes(self) -> bool:
        return any(c.change_type != ChangeType.NO_CHANGE for c in self.changes)

    @property
    def actionable_changes(self) -> list[BuildChange]:
        return [c for c in self.changes if c.change_type != ChangeType.NO_CHANGE]


# ---------------------------------------------------------------------------
# Deploy models
# ---------------------------------------------------------------------------

class DeployStatus(str, Enum):
    """Status of a single deploy action."""

    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


@dataclass
class DeployResult:
    """Result of deploying a single change."""

    task_name: str
    change_type: ChangeType
    status: DeployStatus
    message: str = ""
    remote_task_id: str | None = None


@dataclass
class DeployReport:
    """Complete deploy output."""

    environment: str
    connector: str
    build_timestamp: str
    deploy_timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    results: list[DeployResult] = field(default_factory=list)
    status: str = "SUCCESS"
