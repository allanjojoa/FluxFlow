"""Tests for the build stage — both the Talend connector diff logic and the
core build orchestrator.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fluxflow.connectors.talend.builder import TalendConnector
from fluxflow.core.builder import run_build
from fluxflow.core.models import (
    ArtifactConfig,
    BuildManifest,
    ChangeType,
    EnvironmentConfig,
    ReleaseConfig,
    RemoteArtifact,
    RemoteTask,
    TaskConfig,
)


@pytest.fixture
def env_config() -> EnvironmentConfig:
    return EnvironmentConfig(
        name="test",
        region="us",
        base_url="https://api.us.cloud.talend.com",
        token="test-token",
        workspace_name="ws-test",
    )


# ---------------------------------------------------------------------------
# Diff tests (TalendConnector.diff_task)
# ---------------------------------------------------------------------------

class TestDiffTask:
    """Tests for TalendConnector.diff_task()."""

    def _make_connector(self, env_config: EnvironmentConfig) -> TalendConnector:
        connector = TalendConnector(env_config)
        # Prevent real API calls
        connector.client = MagicMock()
        return connector

    def test_new_task_when_remote_is_none(self, env_config):
        connector = self._make_connector(env_config)
        desired = TaskConfig(
            name="NewTask",
            artifact=ArtifactConfig(name="art", version="1.0.0"),
            parameters={"key": "value"},
        )
        change = connector.diff_task(desired, None)
        assert change.change_type == ChangeType.NEW_TASK
        assert change.task_name == "NewTask"

    def test_no_change_when_identical(self, env_config):
        connector = self._make_connector(env_config)
        desired = TaskConfig(
            name="SameTask",
            artifact=ArtifactConfig(name="art", version="1.0.0"),
            parameters={"key": "value"},
        )
        remote = RemoteTask(
            id="t1",
            name="SameTask",
            artifact=RemoteArtifact(id="a1", name="art", version="1.0.0"),
            parameters={"key": "value"},
        )
        change = connector.diff_task(desired, remote)
        assert change.change_type == ChangeType.NO_CHANGE

    def test_update_artifact_version(self, env_config):
        connector = self._make_connector(env_config)
        desired = TaskConfig(
            name="T",
            artifact=ArtifactConfig(name="art", version="2.0.0"),
            parameters={"key": "value"},
        )
        remote = RemoteTask(
            id="t1",
            name="T",
            artifact=RemoteArtifact(id="a1", name="art", version="1.0.0"),
            parameters={"key": "value"},
        )
        change = connector.diff_task(desired, remote)
        assert change.change_type == ChangeType.UPDATE_ARTIFACT
        assert change.artifact_old_version == "1.0.0"
        assert change.artifact_new_version == "2.0.0"

    def test_update_params_only(self, env_config):
        connector = self._make_connector(env_config)
        desired = TaskConfig(
            name="T",
            artifact=ArtifactConfig(name="art", version="1.0.0"),
            parameters={"key": "new_value"},
        )
        remote = RemoteTask(
            id="t1",
            name="T",
            artifact=RemoteArtifact(id="a1", name="art", version="1.0.0"),
            parameters={"key": "old_value"},
        )
        change = connector.diff_task(desired, remote)
        assert change.change_type == ChangeType.UPDATE_PARAMS
        assert len(change.param_diffs) == 1
        assert change.param_diffs[0].key == "key"
        assert change.param_diffs[0].old_value == "old_value"
        assert change.param_diffs[0].new_value == "new_value"

    def test_update_artifact_and_params(self, env_config):
        connector = self._make_connector(env_config)
        desired = TaskConfig(
            name="T",
            artifact=ArtifactConfig(name="art", version="2.0.0"),
            parameters={"key": "new_value"},
        )
        remote = RemoteTask(
            id="t1",
            name="T",
            artifact=RemoteArtifact(id="a1", name="art", version="1.0.0"),
            parameters={"key": "old_value"},
        )
        change = connector.diff_task(desired, remote)
        assert change.change_type == ChangeType.UPDATE_ARTIFACT_AND_PARAMS

    def test_added_parameter_detected(self, env_config):
        connector = self._make_connector(env_config)
        desired = TaskConfig(
            name="T",
            artifact=ArtifactConfig(name="art", version="1.0.0"),
            parameters={"existing": "v1", "new_param": "v2"},
        )
        remote = RemoteTask(
            id="t1",
            name="T",
            artifact=RemoteArtifact(id="a1", name="art", version="1.0.0"),
            parameters={"existing": "v1"},
        )
        change = connector.diff_task(desired, remote)
        assert change.change_type == ChangeType.UPDATE_PARAMS
        added = [d for d in change.param_diffs if d.action == "added"]
        assert len(added) == 1
        assert added[0].key == "new_param"

    def test_ignored_unmanaged_parameter_detected(self, env_config):
        connector = self._make_connector(env_config)
        desired = TaskConfig(
            name="T",
            artifact=ArtifactConfig(name="art", version="1.0.0"),
            parameters={},
        )
        remote = RemoteTask(
            id="t1",
            name="T",
            artifact=RemoteArtifact(id="a1", name="art", version="1.0.0"),
            parameters={"old_param": "v1"},
        )
        change = connector.diff_task(desired, remote)
        assert change.change_type == ChangeType.NO_CHANGE
        assert len(change.param_diffs) == 0


# ---------------------------------------------------------------------------
# Build orchestrator tests
# ---------------------------------------------------------------------------

class TestRunBuild:
    """Tests for the build orchestrator."""

    def test_generates_manifest_with_changes(self, env_config, tmp_path):
        """Build should produce a manifest file with detected changes."""
        connector = MagicMock()
        connector.list_tasks.return_value = []
        connector.get_task_by_name.return_value = None

        from fluxflow.core.models import BuildChange
        connector.diff_task.return_value = BuildChange(
            task_name="NewTask",
            change_type=ChangeType.NEW_TASK,
            artifact_name="art",
            artifact_new_version="1.0.0",
        )

        release_config = ReleaseConfig(
            connector="talend",
            tasks=[TaskConfig(
                name="NewTask",
                artifact=ArtifactConfig(name="art", version="1.0.0"),
            )],
        )

        manifest = run_build(connector, release_config, "test", output_dir=tmp_path)

        assert isinstance(manifest, BuildManifest)
        assert manifest.has_changes
        assert len(manifest.changes) == 1
        assert manifest.changes[0].change_type == ChangeType.NEW_TASK

        # Manifest file should exist
        files = list(tmp_path.glob("build_test_*.json"))
        assert len(files) == 1

        # Verify JSON is valid
        data = json.loads(files[0].read_text())
        assert data["environment"] == "test"

    def test_no_changes_manifest(self, env_config, tmp_path):
        """When everything matches, has_changes should be False."""
        connector = MagicMock()
        connector.list_tasks.return_value = []
        connector.get_task_by_name.return_value = RemoteTask(
            id="t1", name="T", artifact=RemoteArtifact(id="a1", name="art", version="1.0.0"),
        )

        from fluxflow.core.models import BuildChange
        connector.diff_task.return_value = BuildChange(
            task_name="T",
            change_type=ChangeType.NO_CHANGE,
        )

        release_config = ReleaseConfig(
            connector="talend",
            tasks=[TaskConfig(
                name="T",
                artifact=ArtifactConfig(name="art", version="1.0.0"),
            )],
        )

        manifest = run_build(connector, release_config, "test", output_dir=tmp_path)
        assert not manifest.has_changes
