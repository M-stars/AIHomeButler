import pytest

from helpers import make_registry, make_tool_registry

from butler.devices.simulated import JsonStateStore


@pytest.fixture
def store(tmp_path):
    return JsonStateStore(tmp_path / "state.json")


@pytest.fixture
def registry(store):
    return make_registry(store)


@pytest.fixture
def tool_registry(registry):
    return make_tool_registry(registry)
