"""Talend-specific build logic.

Implements the :class:`BaseConnector` interface for build/diff operations
against the Talend Management Console API.
"""

from __future__ import annotations

import logging
from typing import Any

from fluxflow.connectors.base import BaseConnector
from fluxflow.connectors.talend.client import TalendClient
from fluxflow.core.models import (
    BuildChange,
    ChangeType,
    DeployResult,
    DeployStatus,
    EnvironmentConfig,
    ParamDiff,
    RemoteTask,
    TaskConfig,
    PlanConfig,
    RemotePlan,
)

logger = logging.getLogger(__name__)


class TalendConnector(BaseConnector):
    """Talend Cloud connector implementing the FluxFlow interface.

    Wraps :class:`TalendClient` to provide high-level build (diff) and
    deploy (create/update) operations.
    """

    def __init__(self, env_config: EnvironmentConfig) -> None:
        super().__init__(env_config)
        self.client = TalendClient(env_config)
        self._task_cache: dict[str, RemoteTask] | None = None
        self._plan_cache: dict[str, RemotePlan] | None = None

    def refresh_task_cache(self) -> None:
        """Force a refresh of the task cache."""
        self._task_cache = None
        self.list_tasks()

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    def list_tasks(self) -> list[RemoteTask]:
        """Fetch all tasks and populate the local cache."""
        tasks = self.client.list_tasks()
        self._task_cache = {t.name: t for t in tasks}
        logger.info("Fetched %d tasks from Talend workspace", len(tasks))
        return tasks

    def get_task_by_name(self, task_name: str) -> RemoteTask | None:
        """Look up a task by name, using cache if available.

        This fetches the full task details (parameters, artifact version) 
        from the API since the list endpoint only returns shallow data.
        """
        if self._task_cache is None:
            self.list_tasks()

        shallow_task = self._task_cache.get(task_name)
        if shallow_task is None:
            return None
            
        # The list endpoint returns shallow tasks (no parameters or artifact version).
        # We must fetch the full task by its ID to perform an accurate diff.
        if getattr(shallow_task, "_is_full", False):
            return shallow_task
            
        try:
            full_task = self.client.get_task(shallow_task.id)
            setattr(full_task, "_is_full", True)
            self._task_cache[task_name] = full_task
            return full_task
        except Exception as exc:
            logger.warning(
                "Could not fetch full details for task '%s' (id=%s): %s", 
                task_name, shallow_task.id, exc
            )
            return shallow_task

    def list_plans(self) -> list[RemotePlan]:
        """Fetch all plans and populate the local cache."""
        plans = self.client.list_plans()
        self._plan_cache = {p.name: p for p in plans}
        logger.info("Fetched %d plans from Talend workspace", len(plans))
        return plans

    def get_plan_by_name(self, plan_name: str) -> RemotePlan | None:
        """Look up a plan by name, using cache if available."""
        if self._plan_cache is None:
            self.list_plans()
        
        shallow_plan = self._plan_cache.get(plan_name)
        if shallow_plan is None:
            return None
            
        try:
            full_plan = self.client.get_plan(shallow_plan.id)
            self._plan_cache[plan_name] = full_plan
            return full_plan
        except Exception as exc:
            logger.warning(
                "Could not fetch full details for plan '%s' (id=%s): %s", 
                plan_name, shallow_plan.id, exc
            )
            return shallow_plan

    # ------------------------------------------------------------------
    # Diff / build
    # ------------------------------------------------------------------

    def diff_task(self, desired: TaskConfig, remote: RemoteTask | None) -> BuildChange:
        """Compare desired state with remote state and return the diff.

        Decision logic by task name:
        - If no remote task matches the name → ``NEW_TASK``
        - If a remote task matches → compare artifact version and params
        """

        # --- Task doesn't exist remotely → NEW_TASK ---
        if remote is None:
            logger.info("Task '%s' not found remotely — flagged as NEW_TASK", desired.name)
            return BuildChange(
                task_name=desired.name,
                change_type=ChangeType.NEW_TASK,
                artifact_name=desired.artifact.name,
                artifact_new_version=desired.artifact.version,
                desired_parameters=desired.parameters,
            )

        # --- Compare artifact version ---
        artifact_changed = False
        old_version = None
        if remote.artifact:
            old_version = remote.artifact.version
            if old_version == desired.artifact.version or old_version.startswith(f"{desired.artifact.version}."):
                artifact_changed = False
            else:
                artifact_changed = True
        else:
            artifact_changed = True

        # --- Compare parameters ---
        param_diffs = self._diff_parameters(remote.parameters, desired.parameters)
        params_changed = len(param_diffs) > 0

        # --- Determine change type ---
        if artifact_changed and params_changed:
            change_type = ChangeType.UPDATE_ARTIFACT_AND_PARAMS
        elif artifact_changed:
            change_type = ChangeType.UPDATE_ARTIFACT
        elif params_changed:
            change_type = ChangeType.UPDATE_PARAMS
        else:
            change_type = ChangeType.NO_CHANGE

        if change_type != ChangeType.NO_CHANGE:
            logger.info("Task '%s' has changes: %s", desired.name, change_type.value)
        else:
            logger.debug("Task '%s' — no changes detected", desired.name)

        return BuildChange(
            task_name=desired.name,
            change_type=change_type,
            remote_task_id=remote.id,
            artifact_name=desired.artifact.name,
            artifact_old_version=old_version,
            artifact_new_version=desired.artifact.version if artifact_changed else None,
            param_diffs=param_diffs,
            desired_parameters=desired.parameters if params_changed else {},
        )

    def diff_plan(self, desired: PlanConfig, remote: RemotePlan | None) -> BuildChange:
        """Compare desired state with remote state for a Plan and return the diff."""
        if remote is None:
            logger.info("Plan '%s' not found remotely — flagged as NEW_PLAN", desired.name)
            return BuildChange(
                task_name=f"plan:{desired.name}",
                change_type=ChangeType.NEW_PLAN,
                plan_name=desired.name,
            )
            
        # Compare steps
        desired_steps = [(s.name, s.task) for s in desired.steps]
        remote_steps = [(s.name, s.task_name) for s in remote.steps]
        
        if desired_steps != remote_steps:
            logger.info("Plan '%s' has step changes — flagged as UPDATE_PLAN", desired.name)
            
            # Simple list of differences for reporting
            step_diffs = []
            max_len = max(len(desired_steps), len(remote_steps))
            for i in range(max_len):
                d_val = desired_steps[i] if i < len(desired_steps) else None
                r_val = remote_steps[i] if i < len(remote_steps) else None
                if d_val != r_val:
                    step_diffs.append(f"Step {i+1}: {r_val} -> {d_val}")
                    
            return BuildChange(
                task_name=f"plan:{desired.name}",
                change_type=ChangeType.UPDATE_PLAN,
                remote_plan_id=remote.id,
                plan_name=desired.name,
                step_diffs=step_diffs,
            )
            
        logger.debug("Plan '%s' — no changes detected", desired.name)
        return BuildChange(
            task_name=f"plan:{desired.name}",
            change_type=ChangeType.NO_CHANGE,
            remote_plan_id=remote.id,
            plan_name=desired.name,
        )

    # ------------------------------------------------------------------
    # Artifact operations
    # ------------------------------------------------------------------

    def check_artifact_exists(self, artifact_name: str, version: str) -> bool:
        """Check if an artifact version exists in this environment."""
        return self.client.check_artifact_exists(artifact_name, version)

    def promote_artifact(
        self,
        artifact_name: str,
        version: str,
        source_connector: BaseConnector,
    ) -> DeployResult:
        """Promote an artifact from a source environment.

        Fetches artifact metadata from the source environment and publishes
        it into the target workspace.
        """
        logger.info(
            "Promoting artifact '%s@%s' from '%s' → '%s'",
            artifact_name, version,
            source_connector.env_config.name,
            self.env_config.name,
        )

        try:
            # Resolve artifact ID in source
            if not isinstance(source_connector, TalendConnector):
                return DeployResult(
                    task_name=f"promote:{artifact_name}",
                    change_type=ChangeType.PROMOTE_ARTIFACT,
                    status=DeployStatus.FAILED,
                    message="Source connector is not a TalendConnector",
                )

            source_client: TalendClient = source_connector.client
            source_artifacts = source_client.list_artifacts()
            source_art = None
            for art in source_artifacts:
                if art.name == artifact_name and art.version == version:
                    source_art = art
                    break

            if source_art is None:
                return DeployResult(
                    task_name=f"promote:{artifact_name}",
                    change_type=ChangeType.PROMOTE_ARTIFACT,
                    status=DeployStatus.FAILED,
                    message=(
                        f"Artifact '{artifact_name}@{version}' not found in "
                        f"source environment '{source_connector.env_config.name}'"
                    ),
                )

            # Fetch full artifact details from source
            source_details = source_client.get_artifact(source_art.id)

            # Publish to target workspace
            publish_payload: dict[str, Any] = {
                "workspaceId": self.client.workspace_id,
                "name": artifact_name,
                "version": version,
                "type": source_details.get("type", ""),
                "sourceId": source_art.id,
            }

            result = self.client.publish_artifact(publish_payload)
            new_id = result.get("id", "unknown")
            logger.info(
                "Artifact '%s@%s' promoted successfully (new id=%s)",
                artifact_name, version, new_id,
            )

            return DeployResult(
                task_name=f"promote:{artifact_name}",
                change_type=ChangeType.PROMOTE_ARTIFACT,
                status=DeployStatus.SUCCESS,
                message=f"Promoted artifact (source_id={source_art.id}, target_id={new_id})",
            )

        except Exception as exc:
            logger.error("Failed to promote artifact '%s@%s': %s", artifact_name, version, exc)
            return DeployResult(
                task_name=f"promote:{artifact_name}",
                change_type=ChangeType.PROMOTE_ARTIFACT,
                status=DeployStatus.FAILED,
                message=str(exc),
            )

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    def create_task(self, desired: TaskConfig) -> DeployResult:
        """Create a new task in Talend Cloud.

        The task is matched by name — if ``get_task_by_name`` returned
        ``None`` during the build stage, this method is called to create
        a brand-new task.
        """
        logger.info("Creating task '%s' with artifact %s@%s",
                     desired.name, desired.artifact.name, desired.artifact.version)

        artifact_id = self._resolve_artifact_id(desired.artifact.name)

        payload = self._build_task_payload(
            desired, artifact_id, include_workspace=True
        )

        try:
            result = self.client.create_task(payload)
            task_id = result.get("id", "unknown")
            logger.info("Task '%s' created successfully (id=%s)", desired.name, task_id)
            return DeployResult(
                task_name=desired.name,
                change_type=ChangeType.NEW_TASK,
                status=DeployStatus.SUCCESS,
                message=f"Created task with id={task_id}",
                remote_task_id=task_id,
            )
        except Exception as exc:
            logger.error("Failed to create task '%s': %s", desired.name, exc)
            return DeployResult(
                task_name=desired.name,
                change_type=ChangeType.NEW_TASK,
                status=DeployStatus.FAILED,
                message=str(exc),
            )

    def update_task(self, remote_task_id: str, desired: TaskConfig) -> DeployResult:
        """Update an existing task in Talend Cloud.

        Called when the build stage detected a matching task by name but
        found differences in artifact version, parameters, or both.
        """
        logger.info("Updating task '%s' (id=%s)", desired.name, remote_task_id)

        try:
            # 1. Fetch the existing task to preserve connections, tags, and runtime
            remote_task = self.client.get_task(remote_task_id)
            payload = remote_task.raw.copy()
            
            # 2. Resolve artifact and version
            artifact_id = self._resolve_artifact_id(desired.artifact.name)
            resolved_version = self.client.resolve_artifact_version(desired.artifact.name, desired.artifact.version)
            if not resolved_version:
                resolved_version = desired.artifact.version

            # 3. Apply mutations
            payload["artifact"] = {
                "id": artifact_id,
                "version": resolved_version,
            }
            # Merge desired parameters into existing parameters so unmanaged defaults are preserved
            payload["parameters"] = {**remote_task.parameters, **desired.parameters}
            
            # Map studio connections
            if desired.studio_connection:
                if "connections" not in payload:
                    payload["connections"] = {}
                for studio_name, cloud_name in desired.studio_connection.items():
                    conn_id = self.client.resolve_connection_id(cloud_name)
                    if conn_id:
                        payload["connections"][studio_name] = conn_id
                    else:
                        logger.warning("Could not resolve connection '%s', using as is", cloud_name)
                        payload["connections"][studio_name] = cloud_name

            # Map runtime/engine
            if desired.processing and "runtime" in desired.processing:
                runtime_name = desired.processing["runtime"]
                runtime_id = self.client.resolve_engine_id(runtime_name)
                if "runtime" not in payload:
                    payload["runtime"] = {"type": "REMOTE_ENGINE"}
                
                if runtime_id:
                    payload["runtime"]["id"] = runtime_id
                    payload["runtime"]["type"] = "REMOTE_ENGINE"
                else:
                    logger.warning("Could not resolve engine '%s', using as is", runtime_name)
            
            # Always ensure workspaceId is preserved
            payload["workspaceId"] = self.client.workspace_id
            payload["environmentId"] = self.client.environment_id
            
            # 4. Update the task
            self.client.update_task(remote_task_id, payload)
            logger.info("Task '%s' updated successfully", desired.name)
            return DeployResult(
                task_name=desired.name,
                change_type=ChangeType.UPDATE_ARTIFACT,
                status=DeployStatus.SUCCESS,
                message=f"Updated task id={remote_task_id}",
                remote_task_id=remote_task_id,
            )
        except Exception as exc:
            logger.error("Failed to update task '%s': %s", desired.name, exc)
            return DeployResult(
                task_name=desired.name,
                change_type=ChangeType.UPDATE_ARTIFACT,
                status=DeployStatus.FAILED,
                message=str(exc),
            )

    def create_plan(self, desired: PlanConfig) -> DeployResult:
        """Create a new plan in Talend Cloud."""
        logger.info("Creating plan '%s'", desired.name)
        payload = self._build_plan_payload(desired, include_workspace=True)
        try:
            result = self.client.create_plan(payload)
            plan_id = result.get("id", "unknown")
            logger.info("Plan '%s' created successfully (id=%s)", desired.name, plan_id)
            return DeployResult(
                task_name=f"plan:{desired.name}",
                change_type=ChangeType.NEW_PLAN,
                status=DeployStatus.SUCCESS,
                message=f"Created plan with id={plan_id}",
                remote_task_id=plan_id,
            )
        except Exception as exc:
            logger.error("Failed to create plan '%s': %s", desired.name, exc)
            return DeployResult(
                task_name=f"plan:{desired.name}",
                change_type=ChangeType.NEW_PLAN,
                status=DeployStatus.FAILED,
                message=str(exc),
            )

    def update_plan(self, remote_plan_id: str, desired: PlanConfig) -> DeployResult:
        """Update an existing plan in Talend Cloud."""
        logger.info("Updating plan '%s' (id=%s)", desired.name, remote_plan_id)
        try:
            remote_plan = self.client.get_plan(remote_plan_id)
            payload = remote_plan.raw.copy()
            
            # Merge in the new steps
            new_payload = self._build_plan_payload(desired, include_workspace=False)
            payload.pop("chart", None)
            payload.pop("steps", None)
            if "steps" in new_payload:
                payload["steps"] = new_payload["steps"]
            payload["workspaceId"] = self.client.workspace_id
            payload["environmentId"] = self.client.environment_id
            
            self.client.update_plan(remote_plan_id, payload)
            logger.info("Plan '%s' updated successfully", desired.name)
            return DeployResult(
                task_name=f"plan:{desired.name}",
                change_type=ChangeType.UPDATE_PLAN,
                status=DeployStatus.SUCCESS,
                message=f"Updated plan id={remote_plan_id}",
                remote_task_id=remote_plan_id,
            )
        except Exception as exc:
            logger.error("Failed to update plan '%s': %s", desired.name, exc)
            return DeployResult(
                task_name=f"plan:{desired.name}",
                change_type=ChangeType.UPDATE_PLAN,
                status=DeployStatus.FAILED,
                message=str(exc),
            )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_task_payload(
        self,
        desired: TaskConfig,
        artifact_id: str,
        *,
        include_workspace: bool = False,
    ) -> dict[str, Any]:
        """Build the API payload for creating or updating a task.

        Includes artifact, parameters, studio_connection, and processing.
        """
        resolved_version = self.client.resolve_artifact_version(desired.artifact.name, desired.artifact.version)
        if not resolved_version:
            resolved_version = desired.artifact.version

        payload: dict[str, Any] = {
            "name": desired.name,
            "artifact": {
                "id": artifact_id,
                "version": resolved_version,
            },
        }

        if include_workspace:
            payload["workspaceId"] = self.client.workspace_id
            payload["environmentId"] = self.client.environment_id

        if desired.parameters:
            payload["parameters"] = desired.parameters

        # Studio connections
        if desired.studio_connection:
            payload["connections"] = {}
            for studio_name, cloud_name in desired.studio_connection.items():
                conn_id = self.client.resolve_connection_id(cloud_name)
                payload["connections"][studio_name] = conn_id or cloud_name

        # Processing / runtime configuration
        if desired.processing:
            if "runtime" in desired.processing:
                runtime_name = desired.processing["runtime"]
                runtime_id = self.client.resolve_engine_id(runtime_name)
                payload["runtime"] = {
                    "type": "REMOTE_ENGINE" if runtime_id else "CLOUD",
                    "id": runtime_id or runtime_name
                }
            
            # Other processing fields
            proc_keys = {k: v for k, v in desired.processing.items() if k != "runtime"}
            if proc_keys:
                payload["processing"] = proc_keys

        return payload

    def _build_plan_payload(
        self,
        desired: PlanConfig,
        *,
        include_workspace: bool = False,
    ) -> dict[str, Any]:
        """Build the API payload for creating or updating a plan.
        
        Converts a flat list of steps into Talend's nested 'chart -> nextStep' structure.
        """
        payload: dict[str, Any] = {
            "name": desired.name,
        }
        
        if include_workspace:
            payload["workspaceId"] = self.client.workspace_id
            payload["environmentId"] = self.client.environment_id

        if not desired.steps:
            return payload

        # Resolve all tasks first
        # Resolve all tasks/plans first
        resolved_items = []
        for step in desired.steps:
            item = self.get_task_by_name(step.task)
            if not item:
                item = self.get_plan_by_name(step.task)
            if not item:
                raise ValueError(f"Task or Plan '{step.task}' not found in Talend workspace. Make sure it is created first.")
            resolved_items.append((step, item))

        # Build the payload using the flat steps array expected by Talend's POST/PUT endpoints
        steps_payload = []
        for step, item in resolved_items:
            steps_payload.append({
                "name": step.name,
                "taskIds": [item.id]
            })

        payload["steps"] = steps_payload
        return payload

    def _resolve_artifact_id(self, artifact_name: str) -> str:
        """Find the artifact ID for a given artifact name.

        Fetches the artifact list and matches by name.
        Returns the artifact ID string, or the name itself as a fallback
        (some Talend API versions accept name-based references).
        """
        try:
            artifacts = self.client.list_artifacts()
            for art in artifacts:
                if art.name == artifact_name:
                    return art.id
            logger.warning(
                "Artifact '%s' not found in workspace — using name as ID fallback",
                artifact_name,
            )
            return artifact_name
        except Exception as exc:
            logger.warning("Could not resolve artifact '%s': %s — using name", artifact_name, exc)
            return artifact_name

    @staticmethod
    def _diff_parameters(
        remote_params: dict[str, str],
        desired_params: dict[str, str],
    ) -> list[ParamDiff]:
        """Compute a list of parameter-level diffs.
        
        Only compares parameters explicitly defined in the desired state.
        Unmanaged parameters (e.g. artifact defaults) in remote_params are ignored.
        """
        diffs: list[ParamDiff] = []

        for key, desired_val in desired_params.items():
            remote_val = remote_params.get(key)

            if remote_val is None:
                diffs.append(ParamDiff(key=key, old_value=None, new_value=desired_val, action="added"))
            elif remote_val != desired_val:
                diffs.append(ParamDiff(key=key, old_value=remote_val, new_value=desired_val, action="changed"))

        return diffs
