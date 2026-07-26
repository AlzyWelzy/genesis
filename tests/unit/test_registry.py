"""Tests for feature-module discovery.

Discovery replaced four hand-maintained import lists. Three of those lists
failed *silently* when stale — a missing model import produces an empty
migration, a missing handler import produces an event system where nothing
listens — so the tests here focus on the failure modes rather than the happy
path.
"""

import sys
from pathlib import Path

import pytest
from fastapi import APIRouter

from app.core import discovery as registry


@pytest.fixture
def feature(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Build a throwaway feature package and point discovery at it.

    Creates a real package on disk rather than mocking ``pkgutil``: the thing
    under test *is* the filesystem walk, and mocking it would test the mock.
    """
    modules_dir = tmp_path / "modules"
    modules_dir.mkdir()
    (modules_dir / "__init__.py").write_text('"""Throwaway modules package."""\n')

    package_name = "throwaway_modules"
    sys.path.insert(0, str(tmp_path))
    monkeypatch.setattr(registry, "MODULES_PACKAGE", package_name)

    def make(name: str, files: dict[str, str]) -> None:
        feature_dir = modules_dir / name
        feature_dir.mkdir(exist_ok=True)
        (feature_dir / "__init__.py").write_text('"""Feature."""\n')
        for filename, body in files.items():
            (feature_dir / f"{filename}.py").write_text(body)

    # The package must be importable under the patched name.
    (tmp_path / package_name).mkdir()
    (tmp_path / package_name / "__init__.py").write_text('"""Throwaway."""\n')

    def make_in_package(name: str, files: dict[str, str]) -> None:
        feature_dir = tmp_path / package_name / name
        feature_dir.mkdir(exist_ok=True)
        (feature_dir / "__init__.py").write_text('"""Feature."""\n')
        for filename, body in files.items():
            (feature_dir / f"{filename}.py").write_text(body)

    yield make_in_package

    sys.path.remove(str(tmp_path))
    for module in [m for m in sys.modules if m.startswith(package_name)]:
        del sys.modules[module]


class TestDiscovery:
    def test_finds_feature_packages(self, feature) -> None:
        feature("billing", {})
        feature("invoicing", {})

        assert registry.discover_feature_names() == ("billing", "invoicing")

    def test_order_is_stable(self, feature) -> None:
        """The OpenAPI document must be reproducible across machines."""
        feature("zebra", {})
        feature("alpha", {})

        assert registry.discover_feature_names() == ("alpha", "zebra")

    def test_private_packages_are_skipped(self, feature) -> None:
        feature("_internal", {})
        feature("billing", {})

        assert registry.discover_feature_names() == ("billing",)

    def test_no_features_is_not_an_error(self, feature) -> None:
        assert registry.discover_feature_names() == ()


class TestSideEffectImports:
    def test_imports_models_handlers_and_tasks(self, feature) -> None:
        feature(
            "billing",
            {
                "models": "LOADED = True\n",
                "handlers": "LOADED = True\n",
                "tasks": "LOADED = True\n",
            },
        )

        loaded = registry.import_side_effect_modules()
        assert loaded["billing"] == ("models", "handlers", "tasks")

    def test_absent_submodules_are_skipped(self, feature) -> None:
        feature("billing", {"models": "LOADED = True\n"})

        assert registry.import_side_effect_modules()["billing"] == ("models",)

    def test_a_broken_module_raises_rather_than_being_skipped(self, feature) -> None:
        """A swallowed ImportError looks identical to "no such module".

        The consequence differs enormously: one means the feature defines no
        models, the other means its table is silently never created.
        """
        feature("billing", {"models": "import a_module_that_does_not_exist\n"})

        with pytest.raises(ModuleNotFoundError):
            registry.import_side_effect_modules()

    def test_a_syntax_error_raises(self, feature) -> None:
        feature("billing", {"models": "def broken(\n"})

        with pytest.raises(SyntaxError):
            registry.import_side_effect_modules()

    def test_repeated_calls_are_idempotent(self, feature) -> None:
        feature("billing", {"models": "LOADED = True\n"})

        first = registry.import_side_effect_modules()
        second = registry.import_side_effect_modules()
        assert first == second


class TestRouterCollection:
    def test_collects_exported_routers(self, feature) -> None:
        feature(
            "billing",
            {
                "router": (
                    "from fastapi import APIRouter\n"
                    "router = APIRouter(prefix='/billing', tags=['billing'])\n"
                    "@router.get('/')\n"
                    "async def index() -> dict:\n"
                    "    return {}\n"
                )
            },
        )

        collected = registry.collect_routers()
        assert len(collected) == 1
        assert collected[0][0] == "billing"
        assert isinstance(collected[0][1], APIRouter)

    def test_a_feature_without_a_router_is_skipped(self, feature) -> None:
        feature("billing", {"models": "LOADED = True\n"})

        assert registry.collect_routers() == []

    def test_a_router_module_with_no_router_raises(self, feature) -> None:
        """Better a startup crash than a feature whose endpoints are absent."""
        feature("billing", {"router": "not_a_router = 1\n"})

        with pytest.raises(TypeError, match="must export an APIRouter"):
            registry.collect_routers()

    def test_a_non_apirouter_export_raises(self, feature) -> None:
        feature("billing", {"router": "router = 'not a router'\n"})

        with pytest.raises(TypeError, match="must export an APIRouter"):
            registry.collect_routers()

    def test_tags_are_derived_from_routes(self, feature) -> None:
        feature(
            "billing",
            {
                "router": (
                    "from fastapi import APIRouter\n"
                    "router = APIRouter(tags=['billing'])\n"
                    "@router.get('/x')\n"
                    "async def x() -> dict:\n"
                    "    return {}\n"
                )
            },
        )

        assert registry.collect_router_tags() == ["billing"]


class TestRealPackage:
    def test_the_real_modules_package_is_empty_and_that_is_fine(self) -> None:
        """Stage 2 has not started; discovery must handle zero features."""
        assert registry.discover_feature_names() == ()

    def test_the_registry_module_is_not_a_feature(self) -> None:
        assert "registry" not in registry.discover_feature_names()
