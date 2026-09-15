"""Abstract base class for FluxFlow connectors.

Every platform connector (Talend, Snowflake, Tableau) must implement this
interface so the core engine can orchestrate build and deploy stages
without knowing platform-specific details.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from fluxflow.core.models import (
    BuildChange,
    DeployResult,
    EnvironmentConfig,
    RemoteTask,
    TaskConfig,
)


class BaseConnector(ABC):
    """Contract that all platform connectors must fulfil."""

    def __init__(self, env_config: EnvironmentConfig) -> None:
        self.env_config = env_config

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    @abstractmethod
    def list_tasks(self) -> list[RemoteTask]:
        """Return all tasks visible in the configured workspace/project."""
        ...

    @abstractmethod
    def get_task_by_name(self, task_name: str) -> RemoteTask | None:
        """Return a single task by name, or ``None`` if it does not exist."""
        ...

    # ------------------------------------------------------------------
    # Diff / build operations
    # ------------------------------------------------------------------

    @abstractmethod
    def diff_task(self, desired: TaskConfig, remote: RemoteTask | None) -> BuildChange:
        """Compare *desired* state with *remote* state and return the diff.

        If *remote* is ``None`` the task is treated as new.
        """
        ...

    # ------------------------------------------------------------------
    # Artifact operations
    # ------------------------------------------------------------------

    @abstractmethod
    def check_artifact_exists(self, artifact_name: str, version: str) -> bool:
        """Check if a specific artifact version exists in the target environment."""
        ...

    @abstractmethod
    def promote_artifact(
        self,
        artifact_name: str,
        version: str,
        source_connector: BaseConnector,
    ) -> DeployResult:
        """Promote an artifact from a source environment to this environment.

        Parameters
        ----------
        artifact_name:
            Name of the artifact to promote.
        version:
            Version to promote.
        source_connector:
            A connector initialized against the source environment where
            the artifact currently exists.
        """
        ...

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    @abstractmethod
    def create_task(self, desired: TaskConfig) -> DeployResult:
        """Create a new task on the remote platform."""
        ...

    @abstractmethod
    def update_task(self, remote_task_id: str, desired: TaskConfig) -> DeployResult:
        """Update an existing task on the remote platform."""
        ...
