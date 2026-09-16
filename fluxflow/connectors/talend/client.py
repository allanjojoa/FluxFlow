"""Low-level REST client for the Talend Management Console (TMC) API.

Handles authentication, request construction, retry logic, and response parsing.
All higher-level logic (diffing, building, deploying) lives in sibling modules.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import requests

from fluxflow.core.models import (
    EnvironmentConfig, 
    RemoteArtifact, 
    RemoteTask, 
    RemotePlan, 
    RemoteStep
)

logger = logging.getLogger(__name__)


class TalendAPIError(Exception):
    """Raised when a Talend API call fails after retries."""

    def __init__(self, message: str, status_code: int | None = None, response_body: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


class TalendClient:
    """Thin wrapper around the Talend Cloud Orchestration API.

    Parameters
    ----------
    env_config:
        Environment configuration containing base_url, token, and workspace_id.
    max_retries:
        Maximum number of retry attempts for transient failures (default 3).
    retry_backoff:
        Base delay in seconds between retries, doubled on each attempt.
    timeout:
        HTTP request timeout in seconds.
    """

    RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

    def __init__(
        self,
        env_config: EnvironmentConfig,
        *,
        max_retries: int = 3,
        retry_backoff: float = 1.0,
        timeout: int = 30,
    ) -> None:
        self._base_url = env_config.base_url.rstrip("/")
        self.environment_name = env_config.name
        self._workspace_name = env_config.workspace_name
        self._workspace_id = env_config.workspace_id
        self._timeout = timeout
        self._max_retries = max_retries
        self._retry_backoff = retry_backoff

        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {env_config.token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

    @property
    def workspace_id(self) -> str:
        """Get the workspace ID, resolving it from the name if necessary."""
        if getattr(self, "_workspace_id", None) is None:
            self._workspace_id = self._resolve_workspace_id(self._workspace_name)
        return self._workspace_id

    @property
    def environment_id(self) -> str:
        """Get the environment ID."""
        if getattr(self, "_environment_id", None) is None:
            self._workspace_id = self._resolve_workspace_id(self._workspace_name)
        return getattr(self, "_environment_id", "")

    def _resolve_workspace_id(self, name: str) -> str:
        """Fetch all workspaces and find the ID for the given name."""
        logger.info("Resolving workspace ID for name: '%s'", name)
        
        try:
            data = self._get("/orchestration/workspaces")
            items = data if isinstance(data, list) else data.get("items", [])
        except Exception:
            try:
                data = self._get("/workspaces")
                items = data if isinstance(data, list) else data.get("items", [])
            except Exception as e:
                raise TalendAPIError(f"Failed to fetch workspaces: {e}")

        # Check for exact matches on "EnvironmentName/WorkspaceName"
        for item in items:
            ws_name = item.get("name")
            env_name = item.get("environment", {}).get("name")
            
            if name == f"{env_name}/{ws_name}":
                ws_id = item.get("id")
                self._environment_id = item.get("environment", {}).get("id", "")
                logger.info("Resolved workspace '%s' (exact env/ws match) to ID: %s", name, ws_id)
                return ws_id

        # Check for matches on Environment Name (preferring custom workspaces)
        for item in items:
            env_name = item.get("environment", {}).get("name")
            if name == env_name and item.get("type") == "custom":
                ws_id = item.get("id")
                self._environment_id = item.get("environment", {}).get("id", "")
                logger.info("Resolved workspace via environment name '%s' to ID: %s", name, ws_id)
                return ws_id

        # Fallback to direct workspace name match
        for item in items:
            if item.get("name") == name:
                ws_id = item.get("id")
                self._environment_id = item.get("environment", {}).get("id", "")
                logger.warning(
                    "Resolved workspace by name '%s', but there might be multiple with this name across environments! "
                    "Consider using 'EnvironmentName/WorkspaceName' (e.g. 'OMEGA_DEV/OMEGA_AWS_MODE2_WS') for safety.", name
                )
                return ws_id

        raise TalendAPIError(f"Workspace with name or environment '{name}' not found.")
    # ------------------------------------------------------------------
    # Task endpoints
    # ------------------------------------------------------------------

    def list_tasks(self) -> list[RemoteTask]:
        """Fetch all tasks in the configured workspace.

        Returns
        -------
        list[RemoteTask]
            All tasks found in the workspace.
        """
        params = {"workspaceId": self._workspace_id}
        data = self._get("/orchestration/executables/tasks", params=params)

        # The API may return a dict with an "items" key or a raw list.
        items = data if isinstance(data, list) else data.get("items", [])

        return [self._parse_task(item) for item in items]

    def get_task(self, task_id: str) -> RemoteTask:
        """Fetch a single task by its ID.

        Parameters
        ----------
        task_id:
            The Talend task ID.

        Raises
        ------
        TalendAPIError
            If the task is not found or the request fails.
        """
        data = self._get(f"/orchestration/executables/tasks/{task_id}")
        return self._parse_task(data)

    def create_task(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Create a new task.

        Parameters
        ----------
        payload:
            Task creation payload matching the Talend API schema.

        Returns
        -------
        dict
            The created task response from the API.
        """
        return self._post("/orchestration/executables/tasks", json_body=payload)

    def update_task(self, task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Update an existing task.

        Parameters
        ----------
        task_id:
            The Talend task ID to update.
        payload:
            Fields to update, matching the Talend API schema.

        Returns
        -------
        dict
            The updated task response from the API.
        """
        return self._put(f"/orchestration/executables/tasks/{task_id}", json_body=payload)

    # ------------------------------------------------------------------
    # Plan endpoints
    # ------------------------------------------------------------------

    def list_plans(self) -> list[RemotePlan]:
        """Fetch all plans in the configured workspace."""
        params = {"workspaceId": self._workspace_id}
        data = self._get("/orchestration/executables/plans", params=params)
        items = data if isinstance(data, list) else data.get("items", [])
        return [self._parse_plan(item) for item in items]

    def get_plan(self, plan_id: str) -> RemotePlan:
        """Fetch a single plan by its ID."""
        data = self._get(f"/orchestration/executables/plans/{plan_id}")
        return self._parse_plan(data)

    def create_plan(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Create a new plan."""
        return self._post("/orchestration/executables/plans", json_body=payload)

    def update_plan(self, plan_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Update an existing plan."""
        return self._put(f"/orchestration/executables/plans/{plan_id}", json_body=payload)

    # ------------------------------------------------------------------
    # Artifact endpoints
    # ------------------------------------------------------------------

    def list_artifacts(self) -> list[RemoteArtifact]:
        """Fetch all artifacts in the configured workspace.

        Returns
        -------
        list[RemoteArtifact]
            All artifacts found in the workspace.
        """
        params = {"workspaceId": self._workspace_id}
        data = self._get("/orchestration/artifacts", params=params)

        items = data if isinstance(data, list) else data.get("items", [])

        artifacts: list[RemoteArtifact] = []
        for item in items:
            versions = item.get("versions", [])
            version = item.get("version", versions[0] if versions else "")
            artifacts.append(RemoteArtifact(
                id=item.get("id", ""),
                name=item.get("name", ""),
                version=version,
                versions=versions,
            ))
        return artifacts

    def get_artifact(self, artifact_id: str) -> dict[str, Any]:
        """Fetch a single artifact by ID."""
        return self._get(f"/orchestration/artifacts/{artifact_id}")

    def get_artifact_version(self, artifact_id: str, version: str) -> dict[str, Any]:
        """Fetch a specific version of an artifact."""
        return self._get(f"/orchestration/artifacts/{artifact_id}/versions/{version}")

    def resolve_artifact_version(self, artifact_name: str, requested_version: str) -> str | None:
        """Find the full matching version string (e.g. 0.1.4 -> 0.1.4.2026...)"""
        try:
            artifacts = self.list_artifacts()
            for art in artifacts:
                if art.name == artifact_name:
                    for v in art.versions:
                        if v == requested_version or v.startswith(f"{requested_version}."):
                            return v
            return None
        except TalendAPIError:
            return None

    def check_artifact_exists(self, artifact_name: str, version: str) -> bool:
        """Check whether an artifact with the given name and version exists.

        Returns
        -------
        bool
            ``True`` if the artifact version is available in the workspace.
        """
        return self.resolve_artifact_version(artifact_name, version) is not None

    def resolve_connection_id(self, connection_name: str) -> str | None:
        """Find the Cloud Connection ID for a given connection name."""
        try:
            params = {"workspaceId": self.workspace_id}
            data = self._get("/orchestration/connections", params=params)
            items = data if isinstance(data, list) else data.get("items", [])
            for item in items:
                if item.get("name") == connection_name:
                    return item.get("id")
            return None
        except TalendAPIError:
            return None

    def resolve_engine_id(self, engine_name: str) -> str | None:
        """Find the Remote Engine ID for a given engine name in the current environment."""
        try:
            data = self._get("/processing/runtimes/remote-engines")
            items = data if isinstance(data, list) else data.get("items", [])
            for item in items:
                if item.get("name") == engine_name:
                    env_name = item.get("workspace", {}).get("environment", {}).get("name")
                    if env_name == self.environment_name:
                        return item.get("id")
            return None
        except TalendAPIError:
            return None

    def publish_artifact(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Publish (promote) an artifact into the workspace.

        Parameters
        ----------
        payload:
            Artifact publish payload.  Typically includes the artifact
            binary reference, name, version, and target workspace ID.

        Returns
        -------
        dict
            The published artifact response from the API.
        """
        return self._post("/orchestration/artifacts", json_body=payload)

    # ------------------------------------------------------------------
    # HTTP helpers with retry logic
    # ------------------------------------------------------------------

    def _get(self, path: str, *, params: dict | None = None) -> Any:
        return self._request("GET", path, params=params)

    def _post(self, path: str, *, json_body: dict | None = None) -> Any:
        return self._request("POST", path, json_body=json_body)

    def _put(self, path: str, *, json_body: dict | None = None) -> Any:
        return self._request("PUT", path, json_body=json_body)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
    ) -> Any:
        """Execute an HTTP request with retry logic.

        Retries on transient HTTP errors (429, 5xx) with exponential backoff.
        """
        url = f"{self._base_url}{path}"

        last_exception: Exception | None = None

        for attempt in range(1, self._max_retries + 1):
            try:
                logger.debug(
                    "Talend API %s %s (attempt %d/%d)",
                    method, path, attempt, self._max_retries,
                )

                response = self._session.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    timeout=self._timeout,
                )

                if response.status_code < 300:
                    # Some endpoints return empty bodies on success (e.g., 204)
                    if response.status_code == 204 or not response.content:
                        return {}
                    return response.json()

                if response.status_code in self.RETRYABLE_STATUS_CODES and attempt < self._max_retries:
                    delay = self._retry_backoff * (2 ** (attempt - 1))
                    logger.warning(
                        "Talend API returned %d for %s %s — retrying in %.1fs",
                        response.status_code, method, path, delay,
                    )
                    time.sleep(delay)
                    continue

                # Non-retryable error or final attempt
                self._raise_api_error(method, path, response)

            except requests.RequestException as exc:
                last_exception = exc
                if attempt < self._max_retries:
                    delay = self._retry_backoff * (2 ** (attempt - 1))
                    logger.warning(
                        "Talend API connection error for %s %s — retrying in %.1fs: %s",
                        method, path, delay, exc,
                    )
                    time.sleep(delay)
                    continue

        # All retries exhausted
        raise TalendAPIError(
            f"Talend API request failed after {self._max_retries} attempts: "
            f"{method} {path} — {last_exception}"
        )

    def _raise_api_error(self, method: str, path: str, response: requests.Response) -> None:
        """Parse an error response and raise :class:`TalendAPIError`."""
        try:
            body = response.json()
        except (ValueError, requests.JSONDecodeError):
            body = response.text

        message = (
            f"Talend API error: {method} {path} returned {response.status_code}. "
            f"Response: {body}"
        )
        logger.error(message)
        raise TalendAPIError(message, status_code=response.status_code, response_body=body)

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_task(data: dict[str, Any]) -> RemoteTask:
        """Convert a raw API task response into a :class:`RemoteTask`."""
        artifact_data = data.get("artifact") or {}
        artifact = None
        if artifact_data:
            artifact = RemoteArtifact(
                id=artifact_data.get("id", ""),
                name=artifact_data.get("name", ""),
                version=artifact_data.get("version", ""),
            )

        # Parameters may be a list of dicts with "key"/"value" or a flat dict.
        raw_params = data.get("parameters", {})
        if isinstance(raw_params, list):
            parameters = {p.get("key", ""): str(p.get("value", "")) for p in raw_params}
        elif isinstance(raw_params, dict):
            parameters = {str(k): str(v) for k, v in raw_params.items()}
        else:
            parameters = {}

        return RemoteTask(
            id=data.get("id", data.get("executable", "")),
            name=data.get("name", ""),
            artifact=artifact,
            parameters=parameters,
            workspace_id=data.get("workspace", {}).get("id", data.get("workspaceId", data.get("workspace_id", ""))),
            raw=data,
        )

    @staticmethod
    def _parse_plan(data: dict[str, Any]) -> RemotePlan:
        """Convert a raw API plan response into a :class:`RemotePlan`."""
        steps: list[RemoteStep] = []
        
        # Walk the linked list of steps
        current_step_data = data.get("chart")
        while current_step_data:
            step_id = current_step_data.get("id", "")
            step_name = current_step_data.get("name", "")
            
            flows = current_step_data.get("flows", [])
            task_id = ""
            task_name = ""
            if flows:
                # Assuming one task per step for now
                task_id = flows[0].get("id", "")
                task_name = flows[0].get("name", "")
                
            if step_id or step_name:
                steps.append(RemoteStep(
                    id=step_id,
                    name=step_name,
                    task_id=task_id,
                    task_name=task_name
                ))
            
            current_step_data = current_step_data.get("nextStep")
            
        return RemotePlan(
            id=data.get("id", data.get("executable", "")),
            name=data.get("name", ""),
            steps=steps,
            workspace_id=data.get("workspace", {}).get("id", data.get("workspaceId", data.get("workspace_id", ""))),
            raw=data,
        )
