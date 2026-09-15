"""Tests for the deploy stage — manifest loading, deploy orchestration, and dry-run."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from fluxflow.core.deployer import find_latest_manifest, load_manifest, run_deploy
from fluxflow.core.models import (
    BuildManifest,
    ChangeType,
    BuildChange,
    DeployResult,
    DeployStatus,
    ArtifactConfig,
    ReleaseConfig,
    TaskConfig,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_manifest_data() -> dict:
    """Raw manifest data as would be written by the build stage."""
    return {
        "environment": "dev",
        "connector": "talend",
        "timestamp": "2026-09-15T12:00:00",
        "status": "SUCCESS",
        "changes": [
            {
                "task_name": "ETL_Customer_Load",
                "change_type": "NEW_TASK",
                "remote_task_id": None,
                "artifact_old_version": None,
                "artifact_new_version": "2.3.1",
                "artifact_name": "customer-pipeline",
                "param_diffs": [],
                "desired_parameters": {"batch_size": "5000"},
                "promotion_needed": False,
                "promotion_source_env": None,
            },
            {
                "task_name": "ETL_Order_Sync",
                "change_type": "UPDATE_ARTIFACT",
                "remote_task_id": "task-002",
                "artifact_old_version": "1.7.0",
                "artifact_new_version": "1.8.0",
                "artifact_name": "order-pipeline",
                "param_diffs": [],
                "desired_parameters": {},
                "promotion_needed": True,
                "promotion_source_env": "dev",
            },
            {
                "task_name": "ETL_Unchanged",
                "change_type": "NO_CHANGE",
                "remote_task_id": "task-003",
                "artifact_old_version": None,
                "artifact_new_version": None,
                "artifact_name": None,
                "param_diffs": [],
                "desired_parameters": {},
                "promotion_needed": False,
                "promotion_source_env": None,
            },
        ],
    }


@pytest.fixture
def manifest_file(tmp_path: Path, sample_manifest_data: dict) -> Path:
    """Write the sample manifest to disk and return the path."""
    path = tmp_path / "build_dev_20260915_120000.json"
    path.write_text(json.dumps(sample_manifest_data), encoding="utf-8")
    return path


@pytest.fixture
def release_config_file(tmp_path: Path) -> Path:
    """Create a matching release config for deploy tests."""
    content = """\
connector: talend

tasks:
  - name: "ETL_Customer_Load"
    artifact:
      name: "customer-pipeline"
      version: "2.3.1"
    parameters:
      batch_size: "5000"
    studio_connection:
      type: "CLOUD"
    processing:
      runtime: "Standard"

  - name: "ETL_Order_Sync"
    artifact:
      name: "order-pipeline"
      version: "1.8.0"
    parameters:
      sync_mode: "incremental"
"""
    path = tmp_path / "release_config.yaml"
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Tests: load_manifest
# ---------------------------------------------------------------------------

class TestLoadManifest:
    """Tests for :func:`load_manifest`."""

    def test_loads_valid_manifest(self, manifest_file: Path):
        manifest = load_manifest(manifest_file)
        assert isinstance(manifest, BuildManifest)
        assert manifest.environment == "dev"
        assert manifest.connector == "talend"
        assert len(manifest.changes) == 3

    def test_parses_change_types(self, manifest_file: Path):
        manifest = load_manifest(manifest_file)
        types = [c.change_type for c in manifest.changes]
        assert types == [
            ChangeType.NEW_TASK,
            ChangeType.UPDATE_ARTIFACT,
            ChangeType.NO_CHANGE,
        ]

    def test_parses_promotion_fields(self, manifest_file: Path):
        manifest = load_manifest(manifest_file)
        # ETL_Order_Sync has promotion_needed=True, source=dev
        update_change = manifest.changes[1]
        assert update_change.promotion_needed is True
        assert update_change.promotion_source_env == "dev"

        # ETL_Customer_Load has no promotion
        new_change = manifest.changes[0]
        assert new_change.promotion_needed is False

    def test_actionable_changes_excludes_no_change(self, manifest_file: Path):
        manifest = load_manifest(manifest_file)
        assert len(manifest.actionable_changes) == 2

    def test_raises_on_missing_file(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            load_manifest(tmp_path / "nonexistent.json")


# ---------------------------------------------------------------------------
# Tests: find_latest_manifest
# ---------------------------------------------------------------------------

class TestFindLatestManifest:
    """Tests for :func:`find_latest_manifest`."""

    def test_finds_latest_by_name(self, tmp_path: Path):
        """Should return the manifest with the latest timestamp in filename."""
        (tmp_path / "build_dev_20260901_100000.json").write_text("{}", encoding="utf-8")
        (tmp_path / "build_dev_20260915_120000.json").write_text("{}", encoding="utf-8")
        (tmp_path / "build_dev_20260910_080000.json").write_text("{}", encoding="utf-8")

        latest = find_latest_manifest(tmp_path, "dev")
        assert latest.name == "build_dev_20260915_120000.json"

    def test_filters_by_environment(self, tmp_path: Path):
        """Should only find manifests for the requested environment."""
        (tmp_path / "build_dev_20260915_120000.json").write_text("{}", encoding="utf-8")
        (tmp_path / "build_prod_20260916_120000.json").write_text("{}", encoding="utf-8")

        latest = find_latest_manifest(tmp_path, "dev")
        assert "dev" in latest.name

    def test_raises_when_no_manifests(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError, match="staging"):
            find_latest_manifest(tmp_path, "staging")


# ---------------------------------------------------------------------------
# Tests: run_deploy
# ---------------------------------------------------------------------------

class TestRunDeploy:
    """Tests for the deploy orchestrator."""

    def test_deploys_actionable_changes(
        self, manifest_file: Path, release_config_file: Path
    ):
        manifest = load_manifest(manifest_file)

        connector = MagicMock()
        connector.create_task.return_value = DeployResult(
            task_name="ETL_Customer_Load",
            change_type=ChangeType.NEW_TASK,
            status=DeployStatus.SUCCESS,
            message="Created",
        )
        connector.update_task.return_value = DeployResult(
            task_name="ETL_Order_Sync",
            change_type=ChangeType.UPDATE_ARTIFACT,
            status=DeployStatus.SUCCESS,
            message="Updated",
        )
        connector.promote_artifact.return_value = DeployResult(
            task_name="promote:order-pipeline",
            change_type=ChangeType.PROMOTE_ARTIFACT,
            status=DeployStatus.SUCCESS,
            message="Promoted",
        )

        report = run_deploy(
            connector, manifest, release_config_file, "dev",
            env_configs={"dev": MagicMock()},
            connector_factory=lambda cfg: MagicMock(),
        )

        # Should have promotion + 2 task changes
        assert len(report.results) >= 2
        connector.create_task.assert_called_once()
        connector.update_task.assert_called_once()

    def test_reports_failure(self, manifest_file: Path, release_config_file: Path):
        manifest = load_manifest(manifest_file)

        connector = MagicMock()
        connector.create_task.return_value = DeployResult(
            task_name="ETL_Customer_Load",
            change_type=ChangeType.NEW_TASK,
            status=DeployStatus.FAILED,
            message="API error",
        )
        connector.update_task.return_value = DeployResult(
            task_name="ETL_Order_Sync",
            change_type=ChangeType.UPDATE_ARTIFACT,
            status=DeployStatus.SUCCESS,
            message="Updated",
        )
        connector.promote_artifact.return_value = DeployResult(
            task_name="promote:order-pipeline",
            change_type=ChangeType.PROMOTE_ARTIFACT,
            status=DeployStatus.SUCCESS,
            message="Promoted",
        )

        report = run_deploy(
            connector, manifest, release_config_file, "dev",
            env_configs={"dev": MagicMock()},
            connector_factory=lambda cfg: MagicMock(),
        )
        assert report.status == "PARTIAL"


class TestRunDeployDryRun:
    """Tests for dry-run mode in the deploy orchestrator."""

    def test_dry_run_makes_no_api_calls(
        self, manifest_file: Path, release_config_file: Path
    ):
        """In dry-run mode, connector create/update should NOT be called."""
        manifest = load_manifest(manifest_file)
        connector = MagicMock()

        report = run_deploy(
            connector, manifest, release_config_file, "dev",
            dry_run=True,
        )

        connector.create_task.assert_not_called()
        connector.update_task.assert_not_called()
        connector.promote_artifact.assert_not_called()

    def test_dry_run_returns_skipped_results(
        self, manifest_file: Path, release_config_file: Path
    ):
        """All results should be SKIPPED in dry-run mode."""
        manifest = load_manifest(manifest_file)
        connector = MagicMock()

        report = run_deploy(
            connector, manifest, release_config_file, "dev",
            dry_run=True,
        )

        for result in report.results:
            assert result.status == DeployStatus.SKIPPED
            assert "dry-run" in result.message.lower()

    def test_dry_run_reports_success_status(
        self, manifest_file: Path, release_config_file: Path
    ):
        """Dry-run should always report SUCCESS (nothing failed)."""
        manifest = load_manifest(manifest_file)
        connector = MagicMock()

        report = run_deploy(
            connector, manifest, release_config_file, "dev",
            dry_run=True,
        )
        assert report.status == "SUCCESS"
