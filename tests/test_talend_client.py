"""Tests for the Talend REST client.

All HTTP calls are mocked — no real network traffic.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from fluxflow.connectors.talend.client import TalendAPIError, TalendClient
from fluxflow.core.models import EnvironmentConfig


@pytest.fixture
def env_config() -> EnvironmentConfig:
    """A dummy environment config for testing."""
    return EnvironmentConfig(
        name="test",
        region="us",
        base_url="https://api.us.cloud.talend.com",
        token="test-token-123",
        workspace_name="ws-test-001",
    )


@pytest.fixture
def client(env_config: EnvironmentConfig) -> TalendClient:
    """A TalendClient configured for testing (no retries, no backoff)."""
    return TalendClient(env_config, max_retries=1, retry_backoff=0)


class TestTalendClientAuth:
    """Verify the client sets up authentication correctly."""

    def test_authorization_header(self, client: TalendClient):
        assert client._session.headers["Authorization"] == "Bearer test-token-123"

    def test_content_type_header(self, client: TalendClient):
        assert client._session.headers["Content-Type"] == "application/json"


class TestListTasks:
    """Tests for TalendClient.list_tasks()."""

    def test_returns_parsed_tasks(self, client: TalendClient):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b"data"
        mock_response.json.return_value = {
            "items": [
                {
                    "id": "task-001",
                    "name": "ETL_Customer_Load",
                    "artifact": {"id": "art-001", "name": "customer-pipeline", "version": "2.3.0"},
                    "parameters": {"batch_size": "1000"},
                    "workspaceId": "ws-test-001",
                },
                {
                    "id": "task-002",
                    "name": "ETL_Order_Sync",
                    "artifact": {"id": "art-002", "name": "order-pipeline", "version": "1.7.0"},
                    "parameters": [{"key": "sync_mode", "value": "full"}],
                    "workspaceId": "ws-test-001",
                },
            ]
        }

        with patch.object(client._session, "request", return_value=mock_response):
            tasks = client.list_tasks()

        assert len(tasks) == 2
        assert tasks[0].name == "ETL_Customer_Load"
        assert tasks[0].artifact is not None
        assert tasks[0].artifact.version == "2.3.0"
        assert tasks[0].parameters["batch_size"] == "1000"

        # List-style parameters
        assert tasks[1].parameters["sync_mode"] == "full"

    def test_handles_list_response_format(self, client: TalendClient):
        """Some Talend API versions return a raw list instead of {items: [...]}."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b"data"
        mock_response.json.return_value = [
            {"id": "t1", "name": "Task1", "parameters": {}},
        ]

        with patch.object(client._session, "request", return_value=mock_response):
            tasks = client.list_tasks()

        assert len(tasks) == 1
        assert tasks[0].name == "Task1"


class TestCreateTask:
    """Tests for TalendClient.create_task()."""

    def test_sends_post_request(self, client: TalendClient):
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.content = b'{"id": "new-task-001"}'
        mock_response.json.return_value = {"id": "new-task-001"}

        with patch.object(client._session, "request", return_value=mock_response) as mock_req:
            result = client.create_task({"name": "NewTask", "workspaceId": "ws-001"})

        assert result["id"] == "new-task-001"
        mock_req.assert_called_once()
        call_args = mock_req.call_args
        assert call_args[0][0] == "POST"  # method


class TestUpdateTask:
    """Tests for TalendClient.update_task()."""

    def test_sends_put_request(self, client: TalendClient):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = b'{"id": "task-001"}'
        mock_response.json.return_value = {"id": "task-001"}

        with patch.object(client._session, "request", return_value=mock_response) as mock_req:
            result = client.update_task("task-001", {"parameters": {"key": "val"}})

        assert result["id"] == "task-001"
        call_args = mock_req.call_args
        assert call_args[0][0] == "PUT"


class TestErrorHandling:
    """Tests for error handling and retries."""

    def test_raises_on_404(self, client: TalendClient):
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.content = b'{"error": "Not found"}'
        mock_response.json.return_value = {"error": "Not found"}
        mock_response.text = '{"error": "Not found"}'

        with patch.object(client._session, "request", return_value=mock_response):
            with pytest.raises(TalendAPIError) as exc_info:
                client.get_task("nonexistent")

        assert exc_info.value.status_code == 404

    def test_handles_empty_204_response(self, client: TalendClient):
        mock_response = MagicMock()
        mock_response.status_code = 204
        mock_response.content = b""

        with patch.object(client._session, "request", return_value=mock_response):
            result = client.update_task("task-001", {})

        assert result == {}


class TestParseTask:
    """Tests for the static _parse_task method."""

    def test_handles_missing_artifact(self):
        data = {"id": "t1", "name": "NoArtifact", "parameters": {}}
        task = TalendClient._parse_task(data)
        assert task.artifact is None

    def test_handles_dict_parameters(self):
        data = {"id": "t1", "name": "T", "parameters": {"a": "1", "b": "2"}}
        task = TalendClient._parse_task(data)
        assert task.parameters == {"a": "1", "b": "2"}

    def test_handles_list_parameters(self):
        data = {"id": "t1", "name": "T", "parameters": [
            {"key": "x", "value": "10"},
            {"key": "y", "value": "20"},
        ]}
        task = TalendClient._parse_task(data)
        assert task.parameters == {"x": "10", "y": "20"}
