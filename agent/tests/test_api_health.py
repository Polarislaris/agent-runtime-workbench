"""Step 0 smoke tests for the FastAPI application shell."""

from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from agent.api.app import app, create_app, health
from agent.api.run_manager import RunManager
from agent.database.runs import RunStore
from agent.runtime.events import AgentLoopResult


class ApiHealthTests(unittest.TestCase):
    def test_health_returns_ok(self) -> None:
        self.assertEqual(health(), {"status": "ok"})

    def test_health_route_is_registered(self) -> None:
        paths = {route.path for route in app.routes}
        self.assertIn("/api/health", paths)

    def test_lifespan_initializes_a_durable_run_store(self) -> None:
        from tempfile import TemporaryDirectory
        from pathlib import Path

        with TemporaryDirectory() as directory:
            db_path = Path(directory) / "runs.sqlite3"
            store = RunStore(db_path)
            manager = RunManager(
                agent_runner=lambda _messages, _runtime: AgentLoopResult.completed(),
                hook_collector=lambda *_args: [],
            )
            application = create_app(
                run_manager_factory=lambda: manager,
                run_store_factory=lambda: store,
                initialize_agent_runtime=False,
            )
            with TestClient(application):
                self.assertIs(application.state.run_store, store)
                self.assertTrue(db_path.is_file())


if __name__ == "__main__":
    unittest.main()
