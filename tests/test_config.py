"""Tests for the configuration loader."""

import os
import tempfile
from pathlib import Path

import pytest

from fluxflow.core.config import (
    ConfigError,
    load_env_config,
    load_release_config,
    load_promotion_chain,
    load_all_env_configs,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def env_config_file(tmp_path: Path) -> Path:
    """Create a valid env_config.yaml in a temp directory."""
    content = """\
promotion_chain:
  - dev
  - qa
  - prod

environments:
  dev:
    region: "us"
    token: "static-token-dev"
    workspace_name: "ws-dev-001"
  qa:
    region: "us"
    token: "static-token-qa"
    workspace_name: "ws-qa-001"
  prod:
    region: "eu"
    base_url: "https://api.eu.cloud.talend.com"
    token: "static-token-prod"
    workspace_name: "ws-prod-001"
  custom:
    region: "us"
    token: "${TEST_TALEND_TOKEN}"
    workspace_name: "ws-custom-001"
"""
    path = tmp_path / "env_config.yaml"
    path.write_text(content, encoding="utf-8")
    return path


@pytest.fixture
def release_config_file(tmp_path: Path) -> Path:
    """Create a valid release_config.yaml in a temp directory."""
    content = """\
connector: talend

tasks:
  - name: "ETL_Customer_Load"
    artifact:
      name: "customer-pipeline"
      version: "2.3.1"
    parameters:
      source_database: "raw_db"
      batch_size: "5000"
    studio_connection:
      type: "CLOUD"
    processing:
      runtime: "Standard"
      log_level: "INFO"
      timeout_minutes: 60

  - name: "ETL_Order_Sync"
    artifact:
      name: "order-pipeline"
      version: "1.8.0"
    parameters:
      sync_mode: "incremental"
    studio_connection:
      type: "REMOTE_ENGINE"
      connection_id: "re-order-01"
    processing:
      runtime: "Standard"
      log_level: "WARN"
"""
    path = tmp_path / "release_config.yaml"
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Tests: load_env_config
# ---------------------------------------------------------------------------

class TestLoadEnvConfig:
    """Tests for :func:`load_env_config`."""

    def test_loads_valid_env_with_static_token(self, env_config_file: Path):
        """Static tokens (no ${VAR}) should be loaded as-is."""
        config = load_env_config(env_config_file, "prod")
        assert config.name == "prod"
        assert config.region == "eu"
        assert config.base_url == "https://api.eu.cloud.talend.com"
        assert config.token == "static-token-prod"
        assert config.workspace_name == "ws-prod-001"

    def test_auto_derives_base_url_from_region(self, env_config_file: Path):
        """When base_url is omitted, it should be derived from the region."""
        config = load_env_config(env_config_file, "dev")
        assert config.base_url == "https://api.us.cloud.talend.com"

    def test_resolves_env_var_token(self, env_config_file: Path, monkeypatch):
        """Token with ${VAR} should resolve from environment."""
        monkeypatch.setenv("TEST_TALEND_TOKEN", "my-secret-token")
        config = load_env_config(env_config_file, "custom")
        assert config.token == "my-secret-token"

    def test_raises_on_missing_env_var(self, env_config_file: Path, monkeypatch):
        """Should raise ConfigError if the referenced env var is not set."""
        monkeypatch.delenv("TEST_TALEND_TOKEN", raising=False)
        with pytest.raises(ConfigError, match="TEST_TALEND_TOKEN"):
            load_env_config(env_config_file, "custom")

    def test_raises_on_unknown_environment(self, env_config_file: Path):
        """Should raise ConfigError for non-existent environment names."""
        with pytest.raises(ConfigError, match="staging"):
            load_env_config(env_config_file, "staging")

    def test_raises_on_missing_file(self, tmp_path: Path):
        """Should raise ConfigError if the config file doesn't exist."""
        with pytest.raises(ConfigError, match="not found"):
            load_env_config(tmp_path / "nonexistent.yaml", "dev")

    def test_strips_trailing_slash_from_base_url(self, env_config_file: Path):
        """base_url should have trailing slashes stripped."""
        config = load_env_config(env_config_file, "prod")
        assert not config.base_url.endswith("/")


# ---------------------------------------------------------------------------
# Tests: load_promotion_chain
# ---------------------------------------------------------------------------

class TestLoadPromotionChain:
    """Tests for :func:`load_promotion_chain`."""

    def test_loads_chain(self, env_config_file: Path):
        chain = load_promotion_chain(env_config_file)
        assert chain == ["dev", "qa", "prod"]

    def test_returns_empty_when_missing(self, tmp_path: Path):
        path = tmp_path / "no_chain.yaml"
        path.write_text("environments:\n  dev:\n    region: us\n    token: x\n    workspace_name: y\n", encoding="utf-8")
        chain = load_promotion_chain(path)
        assert chain == []


# ---------------------------------------------------------------------------
# Tests: load_all_env_configs
# ---------------------------------------------------------------------------

class TestLoadAllEnvConfigs:
    """Tests for :func:`load_all_env_configs`."""

    def test_loads_all_resolvable_envs(self, env_config_file: Path):
        """Should load envs with static tokens, skip those with unset vars."""
        configs = load_all_env_configs(env_config_file)
        # dev, qa, prod have static tokens; custom has ${TEST_TALEND_TOKEN}
        assert "dev" in configs
        assert "qa" in configs
        assert "prod" in configs
        # custom should be skipped if env var not set
        assert "custom" not in configs


# ---------------------------------------------------------------------------
# Tests: load_release_config
# ---------------------------------------------------------------------------

class TestLoadReleaseConfig:
    """Tests for :func:`load_release_config`."""

    def test_loads_valid_release_config(self, release_config_file: Path):
        """Should parse all tasks with artifacts, parameters, and new fields."""
        config = load_release_config(release_config_file)
        assert config.connector == "talend"
        assert len(config.tasks) == 2

        task1 = config.tasks[0]
        assert task1.name == "ETL_Customer_Load"
        assert task1.artifact.name == "customer-pipeline"
        assert task1.artifact.version == "2.3.1"
        assert task1.parameters["source_database"] == "raw_db"
        assert task1.parameters["batch_size"] == "5000"

    def test_parses_studio_connection(self, release_config_file: Path):
        """studio_connection should be parsed as a dict."""
        config = load_release_config(release_config_file)
        task1 = config.tasks[0]
        assert task1.studio_connection["type"] == "CLOUD"

        task2 = config.tasks[1]
        assert task2.studio_connection["type"] == "REMOTE_ENGINE"
        assert task2.studio_connection["connection_id"] == "re-order-01"

    def test_parses_processing(self, release_config_file: Path):
        """processing should be parsed as a dict."""
        config = load_release_config(release_config_file)
        task1 = config.tasks[0]
        assert task1.processing["runtime"] == "Standard"
        assert task1.processing["log_level"] == "INFO"
        assert task1.processing["timeout_minutes"] == 60

    def test_parameters_are_strings(self, release_config_file: Path):
        """All parameter values should be coerced to strings."""
        config = load_release_config(release_config_file)
        for task in config.tasks:
            for v in task.parameters.values():
                assert isinstance(v, str)

    def test_raises_on_missing_connector(self, tmp_path: Path):
        """Should raise ConfigError if connector field is missing."""
        path = tmp_path / "bad.yaml"
        path.write_text("tasks:\n  - name: test\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="connector"):
            load_release_config(path)

    def test_raises_on_missing_tasks(self, tmp_path: Path):
        """Should raise ConfigError if tasks field is missing."""
        path = tmp_path / "bad.yaml"
        path.write_text("connector: talend\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="tasks"):
            load_release_config(path)

    def test_raises_on_missing_artifact_in_task(self, tmp_path: Path):
        """Should raise ConfigError if a task is missing the artifact field."""
        content = """\
connector: talend
tasks:
  - name: "test_task"
    parameters:
      key: "value"
"""
        path = tmp_path / "bad.yaml"
        path.write_text(content, encoding="utf-8")
        with pytest.raises(ConfigError, match="artifact"):
            load_release_config(path)

    def test_empty_optional_fields_default_to_dict(self, tmp_path: Path):
        """Tasks without parameters/studio_connection/processing get empty dicts."""
        content = """\
connector: talend
tasks:
  - name: "minimal_task"
    artifact:
      name: "some-artifact"
      version: "1.0.0"
"""
        path = tmp_path / "ok.yaml"
        path.write_text(content, encoding="utf-8")
        config = load_release_config(path)
        assert config.tasks[0].parameters == {}
        assert config.tasks[0].studio_connection == {}
        assert config.tasks[0].processing == {}
