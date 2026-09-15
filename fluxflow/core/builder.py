"""Build stage orchestrator.

Coordinates the connector to fetch remote state, diff against the release
config, and produce a build manifest.  Also handles artifact promotion
detection across the promotion chain.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.table import Table

from fluxflow.connectors.base import BaseConnector
from fluxflow.core.models import BuildManifest, ChangeType, ReleaseConfig

logger = logging.getLogger(__name__)
console = Console()


def run_build(
    connector: BaseConnector,
    release_config: ReleaseConfig,
    env_name: str,
    output_dir: str | Path = "builds",
    promotion_chain: list[str] | None = None,
    env_configs: dict | None = None,
    connector_factory: callable | None = None,
) -> BuildManifest:
    """Execute the build stage.

    1. Fetch all remote tasks via the connector.
    2. For each desired task in the release config, compute the diff.
    3. Check artifact availability; flag promotion if needed.
    4. Assemble a :class:`BuildManifest` with all changes.
    5. Write the manifest to ``output_dir/build_<timestamp>.json``.
    6. Print a summary table to the console.

    Parameters
    ----------
    connector:
        An initialized platform connector.
    release_config:
        Parsed release configuration.
    env_name:
        Target environment name.
    output_dir:
        Directory to write the build manifest to.
    promotion_chain:
        Ordered list of environment names (e.g. ``["dev", "qa", "prod"]``).
    env_configs:
        Dict of environment name → EnvironmentConfig for promotion lookups.
    connector_factory:
        Callable ``(env_config) -> BaseConnector`` for creating connectors
        against source environments during promotion checks.

    Returns
    -------
    BuildManifest
        The generated build manifest.
    """
    console.print(f"\n[bold cyan]⚙  FluxFlow Build — environment: {env_name}[/bold cyan]\n")

    # Step 1: Fetch remote state
    with console.status("[bold green]Fetching remote tasks…"):
        connector.list_tasks()

    # Step 2: Diff each task
    manifest = BuildManifest(
        environment=env_name,
        connector=release_config.connector,
    )

    for task_config in release_config.tasks:
        remote_task = connector.get_task_by_name(task_config.name)
        change = connector.diff_task(task_config, remote_task)

        # Step 3: Check artifact availability for changes that need it
        if change.change_type in (
            ChangeType.NEW_TASK,
            ChangeType.UPDATE_ARTIFACT,
            ChangeType.UPDATE_ARTIFACT_AND_PARAMS,
        ):
            _check_artifact_promotion(
                change=change,
                connector=connector,
                artifact_name=task_config.artifact.name,
                artifact_version=task_config.artifact.version,
                env_name=env_name,
                promotion_chain=promotion_chain or [],
                env_configs=env_configs or {},
                connector_factory=connector_factory,
            )

        manifest.changes.append(change)

    # Step 4: Write manifest to disk
    output_path = _write_manifest(manifest, output_dir)

    # Step 5: Print summary
    _print_summary(manifest, output_path)

    return manifest


def _check_artifact_promotion(
    change,
    connector: BaseConnector,
    artifact_name: str,
    artifact_version: str,
    env_name: str,
    promotion_chain: list[str],
    env_configs: dict,
    connector_factory: callable | None,
) -> None:
    """Check if the artifact version exists; flag for promotion if not.

    Walks backwards through the promotion chain to find which source
    environment has the artifact.
    """
    # Check if artifact exists in current environment
    exists = connector.check_artifact_exists(artifact_name, artifact_version)
    if exists:
        return

    logger.info(
        "Artifact '%s@%s' not found in '%s' — checking promotion chain",
        artifact_name, artifact_version, env_name,
    )

    # Find the source environment in the promotion chain
    if env_name not in promotion_chain:
        change.promotion_needed = True
        change.promotion_source_env = None
        logger.warning(
            "Environment '%s' is not in the promotion chain — "
            "cannot auto-detect source for '%s@%s'",
            env_name, artifact_name, artifact_version,
        )
        return

    current_idx = promotion_chain.index(env_name)
    if current_idx == 0:
        change.promotion_needed = True
        change.promotion_source_env = None
        logger.warning(
            "'%s' is the first environment in the chain — "
            "artifact '%s@%s' must be published directly",
            env_name, artifact_name, artifact_version,
        )
        return

    # Walk backwards through prior environments
    for i in range(current_idx - 1, -1, -1):
        source_env = promotion_chain[i]
        source_config = env_configs.get(source_env)

        if source_config is None or connector_factory is None:
            continue

        try:
            source_connector = connector_factory(source_config)
            if source_connector.check_artifact_exists(artifact_name, artifact_version):
                change.promotion_needed = True
                change.promotion_source_env = source_env
                logger.info(
                    "Artifact '%s@%s' found in '%s' — flagged for promotion",
                    artifact_name, artifact_version, source_env,
                )
                return
        except Exception as exc:
            logger.debug(
                "Could not check artifact in '%s': %s", source_env, exc,
            )

    # Artifact not found anywhere
    change.promotion_needed = True
    change.promotion_source_env = None
    logger.warning(
        "Artifact '%s@%s' not found in any environment in the promotion chain",
        artifact_name, artifact_version,
    )


def _write_manifest(manifest: BuildManifest, output_dir: str | Path) -> Path:
    """Serialize the manifest to a JSON file and return the file path."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"build_{manifest.environment}_{timestamp}.json"
    output_path = output_dir / filename

    # Convert dataclasses to dicts, handling enums
    manifest_dict = asdict(manifest)
    for change in manifest_dict.get("changes", []):
        if isinstance(change.get("change_type"), ChangeType):
            change["change_type"] = change["change_type"].value

    with open(output_path, "w", encoding="utf-8") as fh:
        json.dump(manifest_dict, fh, indent=2, default=str)

    logger.info("Build manifest written to %s", output_path)
    return output_path


def _print_summary(manifest: BuildManifest, output_path: Path) -> None:
    """Print a rich table summarizing the build results."""
    table = Table(
        title=f"Build Summary — {manifest.environment}",
        show_header=True,
        header_style="bold magenta",
    )
    table.add_column("Task", style="cyan", min_width=25)
    table.add_column("Change Type", min_width=20)
    table.add_column("Artifact", min_width=15)
    table.add_column("Version Change", min_width=20)
    table.add_column("Param Changes", min_width=12)
    table.add_column("Promotion", min_width=12)

    _STYLE_MAP = {
        ChangeType.NEW_TASK: "[bold green]",
        ChangeType.UPDATE_ARTIFACT: "[bold yellow]",
        ChangeType.UPDATE_PARAMS: "[bold yellow]",
        ChangeType.UPDATE_ARTIFACT_AND_PARAMS: "[bold yellow]",
        ChangeType.PROMOTE_ARTIFACT: "[bold magenta]",
        ChangeType.NO_CHANGE: "[dim]",
    }

    for change in manifest.changes:
        style = _STYLE_MAP.get(change.change_type, "")
        end_style = "[/]" if style else ""

        # Version change column
        if change.artifact_old_version and change.artifact_new_version:
            version_str = f"{change.artifact_old_version} → {change.artifact_new_version}"
        elif change.artifact_new_version:
            version_str = f"→ {change.artifact_new_version}"
        else:
            version_str = "—"

        # Param changes column
        param_count = len(change.param_diffs)
        param_str = f"{param_count} change(s)" if param_count else "—"

        # Promotion column
        if change.promotion_needed:
            if change.promotion_source_env:
                promo_str = f"[bold magenta]← {change.promotion_source_env}[/bold magenta]"
            else:
                promo_str = "[bold red]NOT FOUND[/bold red]"
        else:
            promo_str = "—"

        table.add_row(
            f"{style}{change.task_name}{end_style}",
            f"{style}{change.change_type.value}{end_style}",
            f"{style}{change.artifact_name or '—'}{end_style}",
            f"{style}{version_str}{end_style}",
            f"{style}{param_str}{end_style}",
            promo_str,
        )

    console.print(table)

    # Summary footer
    actionable = manifest.actionable_changes
    promotions = [c for c in manifest.changes if c.promotion_needed]

    if actionable:
        console.print(f"\n[bold green]✓[/bold green] Build complete: "
                       f"[bold]{len(actionable)}[/bold] task(s) to deploy.")
    else:
        console.print("\n[bold green]✓[/bold green] Build complete: "
                       "[dim]No changes detected.[/dim]")

    if promotions:
        console.print(f"[bold magenta]↑[/bold magenta] {len(promotions)} artifact(s) "
                       f"require promotion before deploy.")

    console.print(f"[dim]Manifest: {output_path}[/dim]\n")
