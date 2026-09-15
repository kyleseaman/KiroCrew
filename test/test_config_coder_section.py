"""``session.coder`` survives the upstream config-dataclass extraction.

Upstream moved every config dataclass out of ``config/loader.py`` into
``config/sections.py``. The branch's Coder section had to move with them, so these
pin that the section is still constructed, still validated, and still TRISTATE.
"""

from __future__ import annotations

from kiro_crew.config.loader import _build_session_config
from kiro_crew.config.sections import CoderSessionConfig, SessionConfig


def test_session_config_carries_a_coder_section_by_default() -> None:
    cfg = SessionConfig()
    assert isinstance(cfg.coder, CoderSessionConfig)
    assert cfg.coder.enabled is None


def test_enabled_stays_tristate() -> None:
    """Only a real JSON boolean is authoritative.

    ``None`` is the compatibility sentinel that lets the legacy environment
    variables still apply, so coercing a non-boolean to ``False`` would silently
    turn "never configured" into "explicitly off".
    """
    assert _build_session_config({}).coder.enabled is None
    assert _build_session_config({"coder": {}}).coder.enabled is None
    assert _build_session_config({"coder": {"enabled": None}}).coder.enabled is None
    assert _build_session_config({"coder": {"enabled": "yes"}}).coder.enabled is None
    assert _build_session_config({"coder": {"enabled": 1}}).coder.enabled is None
    assert _build_session_config({"coder": {"enabled": True}}).coder.enabled is True
    assert _build_session_config({"coder": {"enabled": False}}).coder.enabled is False


def test_a_non_dict_coder_section_falls_back_to_defaults() -> None:
    for bad in ("nope", 3, [], None):
        coder = _build_session_config({"coder": bad}).coder
        assert coder.enabled is None
        assert coder.remote_cwd == CoderSessionConfig.remote_cwd


def test_numeric_fields_are_coerced_not_trusted() -> None:
    coder = _build_session_config(
        {
            "coder": {
                "max_running": "abc",
                "runtime_warm_minutes": None,
                "stop_after_minutes": 45,
            }
        }
    ).coder
    assert coder.max_running == CoderSessionConfig.max_running
    assert coder.runtime_warm_minutes == CoderSessionConfig.runtime_warm_minutes
    assert coder.stop_after_minutes == 45


def test_profiles_are_validated_and_resolvable() -> None:
    coder = _build_session_config(
        {
            "coder": {
                "template": "base",
                "preset": "small",
                "profiles": {
                    "big": {"template": "t-big", "preset": "p-big"},
                    "bad name!": {"template": "x"},
                    "wrongtype": "nope",
                },
            }
        }
    ).coder
    assert set(coder.profiles) == {"big"}
    assert coder.resolve_profile("big") == ("t-big", "p-big")
    # An empty selection falls back to the section defaults, a named-but-unknown
    # one raises rather than silently running on the wrong template.
    assert coder.resolve_profile("") == ("base", "small")
    try:
        coder.resolve_profile("missing")
    except ValueError:
        pass
    else:  # pragma: no cover - the raise is the contract
        raise AssertionError("an unknown profile must not silently fall back")
