"""Offline tests for the two independent live-test gates."""

import pytest

from .bridge_support import (
    CONFIG,
    OPT_IN,
    RECIPIENT,
    SENDER,
    LiveSettings,
    LiveTestsDisabled,
    require_live_settings,
)


@pytest.fixture
def settings(tmp_path):
    return {
        OPT_IN: "1",
        CONFIG: str(tmp_path / "test.toml"),
        SENDER: "configured@example.com",
        RECIPIENT: "test-recipient@example.net",
    }


@pytest.mark.parametrize(
    ("argv", "expression"),
    [
        ([], ""),
        ([], "integration"),  # e.g. selected only by PYTEST_ADDOPTS
        (["--", "-m", "integration"], "integration"),
        (["-m", "not integration"], "not integration"),
        (["-m", "integration or smoke"], "integration or smoke"),
        (["-m", "integration", "-m", "smoke"], "smoke"),
        (["tests/integration/test_bridge.py"], ""),
    ],
)
def test_opt_in_without_explicit_marker_selection_is_blocked(
    argv, expression, settings
):
    with pytest.raises(LiveTestsDisabled, match="explicit -m integration"):
        require_live_settings(argv, expression, settings)


@pytest.mark.parametrize("opt_in", [None, "", "0", "yes"])
def test_marker_without_exact_opt_in_is_blocked(opt_in, settings):
    if opt_in is None:
        del settings[OPT_IN]
    else:
        settings[OPT_IN] = opt_in
    with pytest.raises(LiveTestsDisabled, match=f"{OPT_IN}=1"):
        require_live_settings(["-m", "integration"], "integration", settings)


@pytest.mark.parametrize("missing", [CONFIG, SENDER, RECIPIENT])
def test_selection_and_opt_in_still_require_explicit_settings(missing, settings):
    del settings[missing]
    with pytest.raises(ValueError, match=missing):
        require_live_settings(["-m", "integration"], "integration", settings)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        (CONFIG, "relative.toml"),
        (SENDER, "Display Name <configured@example.com>"),
        (RECIPIENT, "first@example.net, second@example.net"),
    ],
)
def test_invalid_live_settings_stop_without_echoing_values(field, value, settings):
    settings[field] = value
    with pytest.raises(ValueError, match=field) as exc:
        require_live_settings(["-m", "integration"], "integration", settings)
    assert value not in str(exc.value)


def test_both_gates_with_explicit_settings_return_only_test_identity(
    settings, tmp_path
):
    assert require_live_settings(["-m", "integration"], "integration", settings) == (
        LiveSettings(
            tmp_path / "test.toml",
            "configured@example.com",
            "test-recipient@example.net",
        )
    )
