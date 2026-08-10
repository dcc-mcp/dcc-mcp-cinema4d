import pytest

from dcc_mcp_cinema4d import cinema4d_driver


def test_driver_validates_vectors():
    assert cinema4d_driver._vector([1, 2, 3], "value") == [1.0, 2.0, 3.0]
    with pytest.raises(ValueError, match="exactly three"):
        cinema4d_driver._vector([1, 2], "value")
    with pytest.raises(ValueError, match="positive"):
        cinema4d_driver._vector([1, 0, 3], "value", positive=True)


def test_driver_dispatch_is_allowlisted():
    with pytest.raises(ValueError, match="Unknown Cinema 4D method"):
        cinema4d_driver.dispatch("python.eval", {})


def test_driver_normalizes_api_version_tuples():
    assert cinema4d_driver._version_string((2026, 0, 0)) == "2026.0.0"
    assert cinema4d_driver._version_string(2026000) == "2026000"
