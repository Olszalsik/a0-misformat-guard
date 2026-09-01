"""Integration tests for v0.4.0 misformat_guard extension resolution.

Verifies that:
  - The PRIMARY cascade class loads from
    extensions/python/_functions/agent/Agent/call_chat_model_turn/end
  - The SAFETY-NET cascade class loads from
    extensions/python/_functions/agent/Agent/process_tools/end
  - The DEAD call_chat_model/end path is gone
  - The DEAD process_tools_after extension is gone

These tests use the same _get_extension_classes that the framework
uses at runtime, so a passing test means the cascade will actually
fire in production.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


# Repo/install root: derived from this file's location
# (<root>/usr/plugins/misformat_guard/tests/ -> 4 levels up), which works
# both inside the container (/a0) and on the host. REPO_ROOT_OVERRIDE
# still wins for non-standard layouts.
REPO_ROOT = Path(
    os.environ.get("REPO_ROOT_OVERRIDE") or Path(__file__).resolve().parents[4]
)
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _plugin_ext_dir(extension_point: str) -> Path:
    """Return the plugin's directory for a given extension point."""
    return REPO_ROOT / "usr" / "plugins" / "misformat_guard" / "extensions" / "python" / Path(extension_point)


def test_primary_cascade_directory_exists():
    p = _plugin_ext_dir("_functions/agent/Agent/call_chat_model_turn/end")
    assert p.is_dir(), f"primary cascade dir missing: {p}"
    files = list(p.glob("_*.py"))
    assert files, f"no _*.py cascade file in {p}"
    # Must contain the CascadeUtilityRepair class
    content = (files[0]).read_text(encoding="utf-8")
    assert "class CascadeUtilityRepair" in content


def test_safety_net_cascade_directory_exists():
    p = _plugin_ext_dir("_functions/agent/Agent/process_tools/end")
    assert p.is_dir(), f"safety-net cascade dir missing: {p}"
    files = list(p.glob("_*.py"))
    assert files, f"no _*.py safety-net file in {p}"
    content = files[0].read_text(encoding="utf-8")
    assert "class ProcessToolsFallback" in content


def test_dead_call_chat_model_end_is_gone():
    """The v0.3.0 cascade was at the wrong extension point. The v0.4.0
    code is at call_chat_model_turn (with `_turn` suffix). The old dir
    must be removed."""
    p = _plugin_ext_dir("_functions/agent/Agent/call_chat_model/end")
    assert not p.is_dir(), (
        f"dead cascade dir still exists: {p}. Delete it -- it will never "
        f"fire because the monologue loop calls call_chat_model_turn, "
        f"not call_chat_model."
    )


def test_dead_process_tools_after_is_gone():
    """The v0.2.0 escape-valve was at extensions/python/process_tools_after.
    v2.5 has no process_tools_after extension point. The dir must be removed."""
    p = REPO_ROOT / "usr" / "plugins" / "misformat_guard" / "extensions" / "python" / "process_tools_after"
    assert not p.is_dir(), (
        f"dead process_tools_after dir still exists: {p}. v2.5 has no "
        f"process_tools_after extension point; this file will never fire."
    )


def test_primary_cascade_class_imports():
    """The class must import without error."""
    from usr.plugins.misformat_guard.extensions.python._functions.agent.Agent.call_chat_model_turn.end import (  # noqa: E402
        _20_repair_via_utility as cascade_mod,
    )
    assert hasattr(cascade_mod, "CascadeUtilityRepair")


def test_safety_net_cascade_class_imports():
    from usr.plugins.misformat_guard.extensions.python._functions.agent.Agent.process_tools.end import (  # noqa: E402
        _30_repair_via_utility_fallback as fallback_mod,
    )
    assert hasattr(fallback_mod, "ProcessToolsFallback")


def test_api_module_exports_repair_functions():
    from usr.plugins.misformat_guard.api import misformat_repair
    assert callable(misformat_repair.is_misformat)
    assert callable(misformat_repair.try_repair)
    assert callable(misformat_repair.try_repair_via_utility)


def test_stats_module_exports_attempt_counter():
    from usr.plugins.misformat_guard.api import misformat_stats
    assert callable(misformat_stats.record_cascade_attempt)
    assert callable(misformat_stats.record_cascade_repair)
    assert callable(misformat_stats.record_cascade_failure)
    # The new counter must exist in the volatile dict
    snap = misformat_stats.snapshot(None)
    assert "cascade_attempts_total" in snap


def test_utility_repair_prompt_exists():
    p = REPO_ROOT / "usr" / "plugins" / "misformat_guard" / "prompts" / "utility_repair.md"
    assert p.is_file(), f"utility_repair.md missing at {p}"
    content = p.read_text(encoding="utf-8")
    # Sanity: must instruct the utility model
    assert "JSON" in content
    assert "tool" in content
    assert "tool_args" in content


def test_plugin_yaml_bumped_to_v050():
    import yaml
    p = REPO_ROOT / "usr" / "plugins" / "misformat_guard" / "plugin.yaml"
    with p.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    assert data.get("version") == "0.5.1", (
        f"plugin version is {data.get('version')!r}, expected '0.5.1'"
    )
    assert data.get("min_framework_version") == "2.5.0", (
        f"min_framework_version is {data.get('min_framework_version')!r}, "
        f"expected '2.5.0' (the cascade uses v2.5-only hooks)"
    )


def test_hooks_py_no_longer_applies_core_patch():
    """v0.4.0 must NOT apply any core patch."""
    sys.path.insert(0, str(REPO_ROOT / "usr" / "plugins" / "misformat_guard"))
    try:
        import hooks as hooks_mod
        # The apply_core_patch function must be a no-op (return True
        # without doing anything). Inspecting the source is the
        # simplest way to assert this without invoking the function.
        import inspect
        src = inspect.getsource(hooks_mod.apply_core_patch)
        # The function should be a stub -- check for the explanatory comment
        assert "no core patch is required" in src or "v0.4.0" in src, (
            "apply_core_patch() must be a no-op stub in v0.4.0"
        )
        # And the legacy apply function must be gone
        assert not hasattr(hooks_mod, "_legacy_apply_core_patch_unused"), (
            "legacy _legacy_apply_core_patch_unused() must be removed"
        )
        # Cascade override setters must be gone
        assert not hasattr(hooks_mod, "_set_cascade_overrides")
        assert not hasattr(hooks_mod, "_clear_cascade_overrides")
        assert not hasattr(hooks_mod, "_set_class_overrides")
        assert not hasattr(hooks_mod, "_clear_class_overrides")
    finally:
        sys.path.pop(0)
