"""FastAPI application entry point for the Agent Runtime Workbench."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Callable

from fastapi import FastAPI

from ..database.runs import RunStore
from ..features.skills import scan_skills
from ..tooling.hooks import register_default_hooks
from .routes import router
from .run_manager import RunManager


RunManagerFactory = Callable[[], RunManager]
RunStoreFactory = Callable[[], RunStore]


def health() -> dict[str, str]:
    """Return a dependency-free liveness response for the React shell."""
    return {"status": "ok"}


def create_app(
    *,
    run_manager_factory: RunManagerFactory = RunManager,
    run_store_factory: RunStoreFactory = RunStore,
    initialize_agent_runtime: bool = True,
) -> FastAPI:
    """Build an app with injectable lifecycle dependencies for API tests."""

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        if initialize_agent_runtime:
            scan_skills()
            register_default_hooks()

        # S1 creates/migrates durable run storage before accepting HTTP work.
        # The MVP RunManager remains in-memory until S2 wires this store into
        # the event sink, so this initialization is intentionally non-invasive.
        run_store = run_store_factory()
        run_store.initialize()
        # A restarted process cannot revive Python threads or subprocesses from
        # its predecessor. Record that truth before exposing historical runs.
        run_store.recover_interrupted_runs()
        application.state.run_store = run_store

        manager = run_manager_factory()
        manager.attach_run_store(run_store)
        application.state.run_manager = manager
        try:
            yield
        finally:
            manager.shutdown(wait=True)

    application = FastAPI(
        title="Agent Runtime API",
        description="Local API for observing and controlling the teaching Agent.",
        version="0.1.0",
        lifespan=lifespan,
    )
    application.add_api_route(
        "/api/health",
        health,
        methods=["GET"],
        tags=["system"],
    )
    application.include_router(router)
    return application


app = create_app()
