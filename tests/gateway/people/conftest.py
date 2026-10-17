import pytest

from .resolver_fixtures import DirectoryCase


@pytest.fixture
def directory_case(tmp_path):
    case = DirectoryCase(tmp_path)
    try:
        yield case
    finally:
        case.close()


@pytest.fixture
def gold_directory(tmp_path):
    from .resolver_fixtures import gold_directory as build

    paths = build(tmp_path)
    try:
        yield paths
    finally:
        paths.case.close()


@pytest.fixture
def eval_module():
    import importlib.util
    import sys
    from pathlib import Path

    script = Path(__file__).resolve().parents[3] / "scripts" / "eval_person_resolver.py"
    spec = importlib.util.spec_from_file_location("eval_person_resolver", script)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module
