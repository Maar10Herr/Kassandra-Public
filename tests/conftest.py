"""Shared test isolation for filesystem-backed application state."""

import pytest


@pytest.fixture(scope="session", autouse=True)
def isolate_project_root(tmp_path_factory):
    """Keep test evidence and other default state outside the checkout.

    Production configuration still resolves ``paths.project_root`` exactly as
    configured. Tests use an explicit temporary project root so calls to the
    real evidence store cannot create ignored artifacts in the repository.
    """
    import kassandra.config as config_module

    original_config = config_module._config
    isolated_root = tmp_path_factory.mktemp("kassandra-test-project")
    test_config = config_module.Config()
    test_config._data.setdefault("paths", {})["project_root"] = str(isolated_root)
    config_module._config = test_config
    try:
        yield
    finally:
        config_module._config = original_config
