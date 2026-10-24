from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from vmlm.config import Settings
from vmlm.db.session import init_db, make_engine, make_session_factory
from vmlm.domain.scheduler import Scheduler
from vmlm.domain.service import LifecycleService
from vmlm.main import create_app
from vmlm.providers.simulated import SimulatedProvider


@pytest.fixture
def db_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'test.db'}"


@pytest.fixture
def provider() -> SimulatedProvider:
    return SimulatedProvider()


@pytest.fixture
def make_app(db_url: str, provider: SimulatedProvider) -> Iterator[Callable[..., FastAPI]]:
    apps: list[FastAPI] = []

    def _make(
        vcpus: int = 8,
        memory_mib: int = 16384,
        api_key: str | None = None,
        prov: SimulatedProvider | None = None,
    ) -> FastAPI:
        settings = Settings(
            database_url=db_url, host_vcpus=vcpus, host_memory_mib=memory_mib, api_key=api_key
        )
        app = create_app(settings, prov or provider)
        apps.append(app)
        return app

    yield _make
    for app in apps:
        app.state.engine.dispose()


@pytest.fixture
def client(make_app: Callable[..., FastAPI]) -> Iterator[TestClient]:
    with TestClient(make_app()) as c:
        yield c


class ServiceKit:
    def __init__(self, db_url: str, provider: SimulatedProvider, vcpus: int, memory_mib: int):
        self.engine: Engine = make_engine(db_url)
        init_db(self.engine)
        self.session_factory: sessionmaker[Session] = make_session_factory(self.engine)
        self.scheduler = Scheduler(vcpus, memory_mib)
        self.service = LifecycleService(self.session_factory, provider, self.scheduler)


@pytest.fixture
def make_service(db_url: str, provider: SimulatedProvider) -> Iterator[Callable[..., ServiceKit]]:
    kits: list[ServiceKit] = []

    def _make(vcpus: int = 8, memory_mib: int = 16384) -> ServiceKit:
        kit = ServiceKit(db_url, provider, vcpus, memory_mib)
        kits.append(kit)
        return kit

    yield _make
    for kit in kits:
        kit.engine.dispose()
