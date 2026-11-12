"""Every one of the brief's 15 acceptance tests has at least one automated test (see conftest.acceptance_numbers).
Run the full regression with `pytest -m acceptance`."""

import importlib
import pkgutil

import tests

from .conftest import acceptance_numbers


def test_every_acceptance_test_is_covered():
    covered: dict[int, list[str]] = {}
    for mod in pkgutil.iter_modules(tests.__path__):
        m = importlib.import_module(f"tests.{mod.name}")
        for name in dir(m):
            if name.startswith("test_") and callable(getattr(m, name)) and getattr(m, name).__module__ == m.__name__:
                for n in acceptance_numbers(name):
                    covered.setdefault(n, []).append(f"{mod.name}::{name}")
    missing = [n for n in range(1, 16) if n not in covered]
    assert missing == [], f"acceptance tests without an automated test: {missing}"
