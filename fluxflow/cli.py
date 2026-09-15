"""FluxFlow CLI — command-line interface for build and deploy stages.

Usage
-----
::

    # Build: detect changes against remote
    fluxflow build --env dev --config configs/release_config.yaml

    # Deploy: apply the latest build
    fluxflow deploy --env dev --latest

    # Deploy: preview what would change (no API calls)
    fluxflow deploy --env dev --latest --dry-run

    # Deploy: apply a specific build
    fluxflow deploy --env dev --build-file builds/build_dev_20260915_130000.json
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler

console = Console()

# Default paths (relative to CWD)
DEFAULT_ENV_CONFIG = "configs/env_config.yaml"
DEFAULT_RELEASE_CONFIG = "configs/release_config.yaml"
DEFAULT_BUILDS_DIR = "builds"


def main(argv: list[str] | None = None) -> int:
    """Main entry point for the FluxFlow CLI."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    # Setup logging
    _setup_logging(args.verbose)

    if not hasattr(args, "command") or args.command is None:
        parser.print_help()
        return 1

    try:
        if args.command == "build":
            return _handle_build(args)
        elif args.command == "deploy":
            return _handle_deploy(args)
        else:
            parser.print_help()
            return 1
    except Exception as exc:
        console.print(f"\n[bold red]Error:[/bold red] {exc}")
        logger = logging.getLogger(__name__)
        logger.debug("Full traceback:", exc_info=True)
        return 1


def _handle_build(args: argparse.Namespace) -> int:
    """Execute the build stage."""
    from fluxflow.core.config import (
        load_env_config,
        load_release_config,
        load_promotion_chain,
        load_all_env_configs,
    )
    from fluxflow.core.builder import run_build

    # Load configs
    env_config = load_env_config(args.env_config, args.env)
    release_config = load_release_config(args.config)

    # Load promotion chain and all env configs for cross-env checks
    promotion_chain = load_promotion_chain(args.env_config)
    env_configs = load_all_env_configs(args.env_config)

    # Initialize the connector
    connector = _create_connector(release_config.connector, env_config)

    # Run build
    manifest = run_build(
        connector=connector,
        release_config=release_config,
        env_name=args.env,
        output_dir=args.builds_dir,
        promotion_chain=promotion_chain,
        env_configs=env_configs,
        connector_factory=lambda cfg: _create_connector(release_config.connector, cfg),
    )

    return 0 if manifest.status == "SUCCESS" else 1


def _handle_deploy(args: argparse.Namespace) -> int:
    """Execute the deploy stage."""
    from fluxflow.core.config import (
        load_env_config,
        load_release_config,
        load_all_env_configs,
    )
    from fluxflow.core.deployer import run_deploy, load_manifest, find_latest_manifest

    # Load env config
    env_config = load_env_config(args.env_config, args.env)

    # Determine which manifest to deploy
    if args.build_file:
        manifest_path = Path(args.build_file)
    elif args.latest:
        manifest_path = find_latest_manifest(args.builds_dir, args.env)
        console.print(f"[dim]Using latest manifest: {manifest_path}[/dim]")
    else:
        console.print("[bold red]Error:[/bold red] Specify --build-file or --latest")
        return 1

    # Load manifest
    manifest = load_manifest(manifest_path)

    # Validate environment match
    if manifest.environment != args.env:
        console.print(
            f"[bold red]Error:[/bold red] Manifest was built for environment "
            f"'{manifest.environment}', but you specified '{args.env}'."
        )
        return 1

    # Initialize connector
    release_config = load_release_config(args.config)
    connector = _create_connector(release_config.connector, env_config)

    # Load all env configs for potential promotion during deploy
    env_configs = load_all_env_configs(args.env_config)

    # Run deploy
    report = run_deploy(
        connector=connector,
        manifest=manifest,
        release_config_path=args.config,
        env_name=args.env,
        dry_run=args.dry_run,
        env_configs=env_configs,
        connector_factory=lambda cfg: _create_connector(release_config.connector, cfg),
    )

    return 0 if report.status == "SUCCESS" else 1


def _create_connector(connector_name: str, env_config):
    """Factory function to create the appropriate connector."""
    if connector_name.lower() == "talend":
        from fluxflow.connectors.talend.builder import TalendConnector
        return TalendConnector(env_config)
    else:
        raise ValueError(
            f"Unknown connector: '{connector_name}'. "
            f"Supported connectors: talend"
        )


def _build_parser() -> argparse.ArgumentParser:
    """Build the argument parser with build/deploy sub-commands."""
    parser = argparse.ArgumentParser(
        prog="fluxflow",
        description="FluxFlow — Modular release management for Talend, Snowflake, and Tableau",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="count",
        default=0,
        help="Increase verbosity (-v for INFO, -vv for DEBUG)",
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # --- Build sub-command ---
    build_parser = subparsers.add_parser("build", help="Detect changes between config and remote state")
    build_parser.add_argument(
        "--env",
        required=True,
        help="Target environment name (e.g., dev, staging, prod)",
    )
    build_parser.add_argument(
        "--config",
        default=DEFAULT_RELEASE_CONFIG,
        help=f"Path to release_config.yaml (default: {DEFAULT_RELEASE_CONFIG})",
    )
    build_parser.add_argument(
        "--env-config",
        default=DEFAULT_ENV_CONFIG,
        help=f"Path to env_config.yaml (default: {DEFAULT_ENV_CONFIG})",
    )
    build_parser.add_argument(
        "--builds-dir",
        default=DEFAULT_BUILDS_DIR,
        help=f"Output directory for build manifests (default: {DEFAULT_BUILDS_DIR})",
    )

    # --- Deploy sub-command ---
    deploy_parser = subparsers.add_parser("deploy", help="Apply changes from a build manifest")
    deploy_parser.add_argument(
        "--env",
        required=True,
        help="Target environment name (e.g., dev, staging, prod)",
    )
    deploy_parser.add_argument(
        "--build-file",
        default=None,
        help="Path to a specific build manifest JSON file",
    )
    deploy_parser.add_argument(
        "--latest",
        action="store_true",
        help="Use the latest build manifest for the environment",
    )
    deploy_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes without making any API calls",
    )
    deploy_parser.add_argument(
        "--config",
        default=DEFAULT_RELEASE_CONFIG,
        help=f"Path to release_config.yaml (default: {DEFAULT_RELEASE_CONFIG})",
    )
    deploy_parser.add_argument(
        "--env-config",
        default=DEFAULT_ENV_CONFIG,
        help=f"Path to env_config.yaml (default: {DEFAULT_ENV_CONFIG})",
    )
    deploy_parser.add_argument(
        "--builds-dir",
        default=DEFAULT_BUILDS_DIR,
        help=f"Builds directory for --latest lookup (default: {DEFAULT_BUILDS_DIR})",
    )

    return parser


def _setup_logging(verbosity: int) -> None:
    """Configure logging with rich handler."""
    level = logging.WARNING
    if verbosity == 1:
        level = logging.INFO
    elif verbosity >= 2:
        level = logging.DEBUG

    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(rich_tracebacks=True, console=console)],
    )


if __name__ == "__main__":
    sys.exit(main())
