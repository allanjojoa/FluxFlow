"""Deploy stage orchestrator.

Reads a build manifest and applies changes via the platform connector.
Supports dry-run mode and artifact promotion across environments.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from fluxflow.connectors.base import BaseConnector
from fluxflow.core.config import load_release_config
from fluxflow.core.models import (
    BuildChange,
    BuildManifest,
    ChangeType,
    DeployReport,
    DeployResult,
    DeployStatus,
    TaskConfig,
)

logger = logging.getLogger(__name__)
console = Console()


def run_deploy(
    connector: BaseConnector,
    manifest: BuildManifest,
    release_config_path: str | Path,
    env_name: str,
    *,
    dry_run: bool = False,
    env_configs: dict | None = None,
    connector_factory: callable | None = None,
) -> DeployReport:
    """Execute the deploy stage.

    1. Load the release config (for full task definitions).
    2. Promote artifacts if needed.
    3. Iterate over actionable changes in the manifest.
    4. Create or update tasks via the connector (or print what would happen
       if ``dry_run=True``).
    5. Print a deploy report.

    Parameters
    ----------
    connector:
        An initialized platform connector.
    manifest:
        The build manifest to deploy.
    release_config_path:
        Path to the release_config.yaml (needed for full task payloads).
    env_name:
        Target environment name.
    dry_run:
        If ``True``, print what would happen without making API calls.
    env_configs:
        Dict of environment name → EnvironmentConfig for promotion.
    connector_factory:
        Callable ``(env_config) -> BaseConnector`` for source connectors.

    Returns
    -------
    DeployReport
        The deploy report with per-task results.
    """
    mode_label = "[bold yellow]DRY RUN[/bold yellow] " if dry_run else ""
    console.print(f"\n[bold cyan]🚀 FluxFlow Deploy {mode_label}— environment: {env_name}[/bold cyan]\n")

    if dry_run:
        console.print(Panel(
            "[yellow]Dry-run mode is active. No changes will be applied.[/yellow]",
            title="ℹ  Dry Run",
            border_style="yellow",
        ))
        console.print()

    # Load the full release config for task details
    release_config = load_release_config(release_config_path)
    task_lookup: dict[str, TaskConfig] = {t.name: t for t in release_config.tasks}

    report = DeployReport(
        environment=env_name,
        connector=manifest.connector,
        build_timestamp=manifest.timestamp,
    )

    actionable = manifest.actionable_changes
    if not actionable:
        console.print("[dim]No changes to deploy.[/dim]\n")
        return report

    # --- Step 1: Handle artifact promotions ---
    promotions = [c for c in actionable if c.promotion_needed and c.promotion_source_env]
    if promotions:
        console.print(f"[bold magenta]↑[/bold magenta] Promoting "
                       f"[bold]{len(promotions)}[/bold] artifact(s)…\n")

        for change in promotions:
            result = _promote_artifact(
                connector=connector,
                change=change,
                dry_run=dry_run,
                env_configs=env_configs or {},
                connector_factory=connector_factory,
            )
            report.results.append(result)

    # --- Step 2: Deploy task changes ---
    task_changes = [c for c in actionable if c.change_type not in (ChangeType.NEW_PLAN, ChangeType.UPDATE_PLAN)]
    plan_changes = [c for c in actionable if c.change_type in (ChangeType.NEW_PLAN, ChangeType.UPDATE_PLAN)]
    
    if task_changes:
        console.print(f"Deploying [bold]{len(task_changes)}[/bold] task change(s)…\n")
        for change in task_changes:
            desired = task_lookup.get(change.task_name)
            if desired is None:
                result = DeployResult(
                    task_name=change.task_name,
                    change_type=change.change_type,
                    status=DeployStatus.FAILED,
                    message=f"Task '{change.task_name}' not found in release config",
                )
                report.results.append(result)
                continue

            if change.promotion_needed and not change.promotion_source_env:
                result = DeployResult(
                    task_name=change.task_name,
                    change_type=change.change_type,
                    status=DeployStatus.FAILED,
                    message=(
                        f"Cannot deploy: Artifact version '{change.artifact_new_version}' is missing "
                        "in the target workspace and cannot be promoted (source not found or target is first in chain)."
                    ),
                )
                report.results.append(result)
                continue

            result = _deploy_change(connector, change, desired, dry_run=dry_run)
            report.results.append(result)
            
    # --- Step 3: Deploy plan changes ---
    if plan_changes:
        # Before deploying plans, refresh the task cache in case tasks were just created/updated
        if not dry_run and hasattr(connector, "refresh_task_cache"):
            connector.refresh_task_cache()
            
        console.print(f"\nDeploying [bold]{len(plan_changes)}[/bold] plan change(s)…\n")
        
        # Load plans from config
        plan_lookup = {p.name: p for p in getattr(release_config, "plans", [])}
        
        for change in plan_changes:
            desired_plan = plan_lookup.get(change.plan_name)
            if desired_plan is None:
                result = DeployResult(
                    task_name=change.task_name,
                    change_type=change.change_type,
                    status=DeployStatus.FAILED,
                    message=f"Plan '{change.plan_name}' not found in release config",
                )
                report.results.append(result)
                continue
                
            result = _deploy_change(connector, change, desired_plan, dry_run=dry_run)
            report.results.append(result)

    # Check overall status
    failures = [r for r in report.results if r.status == DeployStatus.FAILED]
    if failures:
        report.status = "PARTIAL" if len(failures) < len(report.results) else "FAILED"

    _print_deploy_report(report, dry_run=dry_run)

    return report


def _promote_artifact(
    connector: BaseConnector,
    change: BuildChange,
    *,
    dry_run: bool,
    env_configs: dict,
    connector_factory: callable | None,
) -> DeployResult:
    """Promote an artifact from the source environment."""
    artifact_name = change.artifact_name or "unknown"
    version = change.artifact_new_version or "unknown"
    source_env = change.promotion_source_env or "unknown"

    label = f"  ↑ {artifact_name}@{version} from {source_env}"

    if dry_run:
        console.print(f"{label} [yellow]PROMOTE (dry-run)[/yellow]")
        return DeployResult(
            task_name=f"promote:{artifact_name}",
            change_type=ChangeType.PROMOTE_ARTIFACT,
            status=DeployStatus.SKIPPED,
            message=f"Would promote {artifact_name}@{version} from {source_env} (dry-run)",
        )

    console.print(f"{label} [magenta]PROMOTE[/magenta]")

    source_config = env_configs.get(source_env)
    if source_config is None or connector_factory is None:
        return DeployResult(
            task_name=f"promote:{artifact_name}",
            change_type=ChangeType.PROMOTE_ARTIFACT,
            status=DeployStatus.FAILED,
            message=f"Source environment '{source_env}' config not available",
        )

    source_connector = connector_factory(source_config)
    return connector.promote_artifact(artifact_name, version, source_connector)


def _deploy_change(
    connector: BaseConnector,
    change: BuildChange,
    desired: Any,
    *,
    dry_run: bool = False,
) -> DeployResult:
    """Deploy a single change — create or update based on change type."""
    task_label = f"  → {change.task_name}"

    if change.change_type == ChangeType.NEW_TASK:
        if dry_run:
            console.print(f"{task_label} [green]CREATE (dry-run)[/green]")
            return DeployResult(
                task_name=change.task_name,
                change_type=change.change_type,
                status=DeployStatus.SKIPPED,
                message=f"Would create task '{change.task_name}' (dry-run)",
            )
        console.print(f"{task_label} [green]CREATE[/green]")
        return connector.create_task(desired)

    elif change.change_type in (
        ChangeType.UPDATE_ARTIFACT,
        ChangeType.UPDATE_PARAMS,
        ChangeType.UPDATE_ARTIFACT_AND_PARAMS,
    ):
        if dry_run:
            console.print(f"{task_label} [yellow]UPDATE (dry-run)[/yellow] ({change.change_type.value})")
            return DeployResult(
                task_name=change.task_name,
                change_type=change.change_type,
                status=DeployStatus.SKIPPED,
                message=f"Would update task '{change.task_name}' — {change.change_type.value} (dry-run)",
            )

        console.print(f"{task_label} [yellow]UPDATE[/yellow] ({change.change_type.value})")
        if not change.remote_task_id:
            return DeployResult(
                task_name=change.task_name,
                change_type=change.change_type,
                status=DeployStatus.FAILED,
                message="No remote task ID available — cannot update",
            )
        return connector.update_task(change.remote_task_id, desired)

    elif change.change_type == ChangeType.NEW_PLAN:
        if dry_run:
            console.print(f"{task_label} [green]CREATE PLAN (dry-run)[/green]")
            return DeployResult(
                task_name=change.task_name,
                change_type=change.change_type,
                status=DeployStatus.SKIPPED,
                message=f"Would create plan '{change.plan_name}' (dry-run)",
            )
        console.print(f"{task_label} [green]CREATE PLAN[/green]")
        return connector.create_plan(desired)

    elif change.change_type == ChangeType.UPDATE_PLAN:
        if dry_run:
            console.print(f"{task_label} [yellow]UPDATE PLAN (dry-run)[/yellow]")
            return DeployResult(
                task_name=change.task_name,
                change_type=change.change_type,
                status=DeployStatus.SKIPPED,
                message=f"Would update plan '{change.plan_name}' (dry-run)",
            )

        console.print(f"{task_label} [yellow]UPDATE PLAN[/yellow]")
        if not change.remote_plan_id:
            return DeployResult(
                task_name=change.task_name,
                change_type=change.change_type,
                status=DeployStatus.FAILED,
                message="No remote plan ID available — cannot update",
            )
        return connector.update_plan(change.remote_plan_id, desired)
    else:
        console.print(f"{task_label} [dim]SKIP[/dim]")
        return DeployResult(
            task_name=change.task_name,
            change_type=change.change_type,
            status=DeployStatus.SKIPPED,
            message="No changes to apply",
        )


def load_manifest(manifest_path: str | Path) -> BuildManifest:
    """Load a build manifest from a JSON file.

    Parameters
    ----------
    manifest_path:
        Path to the build manifest JSON file.

    Returns
    -------
    BuildManifest
        The parsed build manifest.
    """
    path = Path(manifest_path)
    if not path.exists():
        raise FileNotFoundError(f"Build manifest not found: {path}")

    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)

    from fluxflow.core.models import ParamDiff

    changes: list[BuildChange] = []
    for c in data.get("changes", []):
        param_diffs = [
            ParamDiff(
                key=pd.get("key", ""),
                old_value=pd.get("old_value"),
                new_value=pd.get("new_value"),
                action=pd.get("action", ""),
            )
            for pd in c.get("param_diffs", [])
        ]
        changes.append(BuildChange(
            task_name=c.get("task_name", ""),
            change_type=ChangeType(c.get("change_type", "NO_CHANGE")),
            remote_task_id=c.get("remote_task_id"),
            artifact_old_version=c.get("artifact_old_version"),
            artifact_new_version=c.get("artifact_new_version"),
            artifact_name=c.get("artifact_name"),
            param_diffs=param_diffs,
            desired_parameters=c.get("desired_parameters", {}),
            promotion_needed=c.get("promotion_needed", False),
            promotion_source_env=c.get("promotion_source_env"),
            plan_name=c.get("plan_name"),
            remote_plan_id=c.get("remote_plan_id"),
            step_diffs=c.get("step_diffs", []),
        ))

    return BuildManifest(
        environment=data.get("environment", ""),
        connector=data.get("connector", ""),
        timestamp=data.get("timestamp", ""),
        changes=changes,
        status=data.get("status", "SUCCESS"),
    )


def find_latest_manifest(builds_dir: str | Path, env_name: str) -> Path:
    """Find the most recent build manifest for an environment.

    Parameters
    ----------
    builds_dir:
        Directory containing build manifest files.
    env_name:
        Environment name to filter by.

    Returns
    -------
    Path
        Path to the latest manifest file.

    Raises
    ------
    FileNotFoundError
        If no matching manifest is found.
    """
    builds_dir = Path(builds_dir)
    if not builds_dir.exists():
        raise FileNotFoundError(f"Builds directory not found: {builds_dir}")

    pattern = f"build_{env_name}_*.json"
    manifests = sorted(builds_dir.glob(pattern), reverse=True)

    if not manifests:
        raise FileNotFoundError(
            f"No build manifests found for environment '{env_name}' in {builds_dir}"
        )

    return manifests[0]


def _print_deploy_report(report: DeployReport, *, dry_run: bool = False) -> None:
    """Print a rich table summarizing the deploy results."""
    title_suffix = " (DRY RUN)" if dry_run else ""
    table = Table(
        title=f"Deploy Report — {report.environment}{title_suffix}",
        show_header=True,
        header_style="bold magenta",
    )
    table.add_column("Task / Artifact", style="cyan", min_width=25)
    table.add_column("Action", min_width=20)
    table.add_column("Status", min_width=10)
    table.add_column("Message", min_width=30)

    for result in report.results:
        if result.status == DeployStatus.SUCCESS:
            status_str = "[bold green]SUCCESS[/bold green]"
        elif result.status == DeployStatus.FAILED:
            status_str = "[bold red]FAILED[/bold red]"
        else:
            status_str = "[dim]SKIPPED[/dim]" if not dry_run else "[yellow]DRY RUN[/yellow]"

        table.add_row(
            result.task_name,
            result.change_type.value,
            status_str,
            result.message or "—",
        )

    console.print(table)

    # Summary footer
    successes = sum(1 for r in report.results if r.status == DeployStatus.SUCCESS)
    failures = sum(1 for r in report.results if r.status == DeployStatus.FAILED)
    skipped = sum(1 for r in report.results if r.status == DeployStatus.SKIPPED)

    if dry_run:
        console.print(f"\n[bold yellow]ℹ[/bold yellow] Dry-run complete: "
                       f"{skipped} action(s) would be performed.\n")
    elif failures:
        console.print(f"\n[bold red]✗[/bold red] Deploy finished with errors: "
                       f"{successes} succeeded, {failures} failed.\n")
    else:
        console.print(f"\n[bold green]✓[/bold green] Deploy complete: "
                       f"{successes} task(s) deployed successfully.\n")
