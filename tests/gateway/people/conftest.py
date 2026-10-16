import pytest

from .resolver_fixtures import DirectoryCase


@pytest.fixture
def directory_case(tmp_path):
    case = DirectoryCase(tmp_path)
    try:
        yield case
    finally:
        case.close()
