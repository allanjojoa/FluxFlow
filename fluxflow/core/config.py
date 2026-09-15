"""Configuration loader and validator for FluxFlow.

Loads env_config.yaml and release_config.yaml, resolves environment variable
references (${VAR_NAME}), and returns typed dataclass instances.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

from fluxflow.core.models import (
    ArtifactConfig,
    EnvironmentConfig,
    ReleaseConfig,
    TaskConfig,
)

# Pattern to match ${VAR_NAME} tokens in config values.
_ENV_VAR_PATTERN = re.compile(r"\$\{([^}]+)\}")

# Known Talend Cloud regions → base URLs.
_REGION_BASE_URLS: dict[str, str] = {
    "us": "https://api.us.cloud.talend.com",
    "eu": "https://api.eu.cloud.talend.com",
    "ap": "https://api.ap.cloud.talend.com",
    "us-west": "https://api.us-west.cloud.talend.com",
}


class ConfigError(Exception):
    """Raised when configuration loading or validation fails."""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_env_config(config_path: str | Path, env_name: str) -> EnvironmentConfig:
    """Load and return the :class:`EnvironmentConfig` for *env_name*.

    Parameters
    ----------
    config_path:
        Path to the ``env_config.yaml`` file.
    env_name:
        Name of the target environment (e.g. ``dev``, ``staging``, ``prod``).

    Raises
    ------
    ConfigError
        If the file is missing, malformed, or the environment is not defined.
    """
    raw = _load_yaml(config_path)

    environments = raw.get("environments")
    if not environments or not isinstance(environments, dict):
        raise ConfigError(
            f"env_config.yaml must contain an 'environments' mapping. "
            f"Got: {type(environments)}"
        )

    if env_name not in environments:
        available = ", ".join(environments.keys())
        raise ConfigError(
            f"Environment '{env_name}' not found in env_config.yaml. "
            f"Available: {available}"
        )

    env_data: dict = environments[env_name]
    _validate_keys(env_data, ["region", "token", "workspace_name"], context=f"environments.{env_name}")

    region = _resolve_env_vars(str(env_data["region"]))

    # base_url is optional — auto-derived from region if not provided.
    if "base_url" in env_data:
        base_url = _resolve_env_vars(str(env_data["base_url"])).rstrip("/")
    else:
        base_url = _REGION_BASE_URLS.get(region)
        if base_url is None:
            raise ConfigError(
                f"Cannot auto-derive base_url for unknown region '{region}'. "
                f"Known regions: {', '.join(_REGION_BASE_URLS.keys())}. "
                f"Provide an explicit 'base_url' in the config."
            )

    return EnvironmentConfig(
        name=env_name,
        region=region,
        base_url=base_url,
        token=_resolve_env_vars(str(env_data["token"])),
        workspace_name=_resolve_env_vars(str(env_data["workspace_name"])),
    )


def load_all_env_configs(config_path: str | Path) -> dict[str, EnvironmentConfig]:
    """Load :class:`EnvironmentConfig` for *every* environment in the file.

    Returns a dict keyed by environment name.  Useful for promotion logic
    which needs to resolve the source environment's config.
    """
    raw = _load_yaml(config_path)
    environments = raw.get("environments")
    if not environments or not isinstance(environments, dict):
        raise ConfigError(
            "env_config.yaml must contain an 'environments' mapping."
        )

    configs: dict[str, EnvironmentConfig] = {}
    for env_name in environments:
        try:
            configs[env_name] = load_env_config(config_path, env_name)
        except ConfigError:
            # Skip environments with unresolvable tokens (e.g. if the
            # user only has dev credentials set in their shell).
            pass
    return configs


def load_promotion_chain(config_path: str | Path) -> list[str]:
    """Load the promotion chain from env_config.yaml.

    The promotion chain defines the order in which artifacts flow between
    environments (e.g. ``[dev, qa, prod]``).  If not specified, returns
    an empty list.

    Parameters
    ----------
    config_path:
        Path to the ``env_config.yaml`` file.
    """
    raw = _load_yaml(config_path)
    chain = raw.get("promotion_chain", [])
    if not isinstance(chain, list):
        raise ConfigError(
            f"'promotion_chain' must be a list of environment names, got {type(chain).__name__}"
        )
    return [str(e) for e in chain]


def load_release_config(config_path: str | Path) -> ReleaseConfig:
    """Load and return the :class:`ReleaseConfig` from *config_path*.

    Parameters
    ----------
    config_path:
        Path to the ``release_config.yaml`` file.

    Raises
    ------
    ConfigError
        If the file is missing, malformed, or tasks are invalid.
    """
    raw = _load_yaml(config_path)

    connector = raw.get("connector")
    if not connector:
        raise ConfigError("release_config.yaml must specify a 'connector' field.")

    raw_tasks = raw.get("tasks")
    if not raw_tasks or not isinstance(raw_tasks, list):
        raise ConfigError("release_config.yaml must contain a 'tasks' list.")

    tasks: list[TaskConfig] = []
    for idx, task_data in enumerate(raw_tasks):
        tasks.append(_parse_task(task_data, idx))

    return ReleaseConfig(connector=connector, tasks=tasks)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _load_yaml(path: str | Path) -> dict:
    """Read and parse a YAML file, returning the top-level dict."""
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"Configuration file not found: {path}")

    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ConfigError(f"Expected a YAML mapping at top level in {path}, got {type(data).__name__}")

    return data


def _resolve_env_vars(value: str) -> str:
    """Replace ``${VAR_NAME}`` tokens with their environment variable values.

    Raises :class:`ConfigError` if a referenced variable is not set.
    """

    def _replacer(match: re.Match) -> str:
        var_name = match.group(1)
        env_value = os.environ.get(var_name)
        if env_value is None:
            raise ConfigError(
                f"Environment variable '{var_name}' is not set. "
                f"Referenced in config as ${{{var_name}}}."
            )
        return env_value

    return _ENV_VAR_PATTERN.sub(_replacer, value)


def _validate_keys(data: dict, required_keys: list[str], context: str) -> None:
    """Raise :class:`ConfigError` if any *required_keys* are missing from *data*."""
    missing = [k for k in required_keys if k not in data]
    if missing:
        raise ConfigError(f"Missing required keys in '{context}': {', '.join(missing)}")


def _parse_task(data: dict, index: int) -> TaskConfig:
    """Parse a single task entry from the release config."""
    context = f"tasks[{index}]"

    if not isinstance(data, dict):
        raise ConfigError(f"{context}: expected a mapping, got {type(data).__name__}")

    _validate_keys(data, ["name", "artifact"], context=context)

    artifact_data = data["artifact"]
    if not isinstance(artifact_data, dict):
        raise ConfigError(f"{context}.artifact: expected a mapping, got {type(artifact_data).__name__}")
    _validate_keys(artifact_data, ["name", "version"], context=f"{context}.artifact")

    parameters = data.get("parameters", {})
    if not isinstance(parameters, dict):
        raise ConfigError(f"{context}.parameters: expected a mapping, got {type(parameters).__name__}")

    # Ensure all parameter values are strings
    parameters = {str(k): str(v) for k, v in parameters.items()}

    # Optional: studio connection and processing configuration
    studio_connection = data.get("studio_connection", {})
    if not isinstance(studio_connection, dict):
        raise ConfigError(f"{context}.studio_connection: expected a mapping, got {type(studio_connection).__name__}")

    processing = data.get("processing", {})
    if not isinstance(processing, dict):
        raise ConfigError(f"{context}.processing: expected a mapping, got {type(processing).__name__}")

    return TaskConfig(
        name=str(data["name"]),
        artifact=ArtifactConfig(
            name=str(artifact_data["name"]),
            version=str(artifact_data["version"]),
        ),
        parameters=parameters,
        studio_connection=studio_connection,
        processing=processing,
    )

