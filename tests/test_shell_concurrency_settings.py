import pytest
from pydantic import ValidationError

from hortator.models import ControlError, Settings


def test_old_settings_use_shell_concurrency_default_without_mutating_input():
    old_settings = {"id": "global", "name": "Existing council", "max_concurrent_turns": 12}

    settings = Settings.model_validate(old_settings)

    assert settings.max_concurrent_shell_jobs == 4
    assert settings.max_concurrent_turns == 12
    assert "max_concurrent_shell_jobs" not in old_settings


@pytest.mark.parametrize("limit", [1, 4, 32])
def test_shell_concurrency_accepts_integer_limits(limit):
    assert Settings(max_concurrent_shell_jobs=limit).max_concurrent_shell_jobs == limit


@pytest.mark.parametrize("limit", [0, 33, 1.5, True, "4", None])
def test_shell_concurrency_rejects_invalid_limits(limit):
    with pytest.raises(ValidationError, match="max_concurrent_shell_jobs"):
        Settings(max_concurrent_shell_jobs=limit)


async def test_shell_concurrency_save_preserves_other_settings(kernel, owner):
    original = kernel.store.get("settings", "global")

    saved = await kernel.service.save(
        owner,
        "settings",
        "global",
        {"revision": original["revision"], "max_concurrent_shell_jobs": 6},
    )

    assert saved["max_concurrent_shell_jobs"] == 6
    assert saved["revision"] == original["revision"] + 1
    for key, value in original.items():
        if key not in {"max_concurrent_shell_jobs", "revision"}:
            assert saved[key] == value
    assert kernel.store.get("settings", "global") == saved


async def test_rejected_shell_concurrency_save_leaves_record_unchanged(kernel, owner):
    original = kernel.store.get("settings", "global")

    with pytest.raises(ControlError, match="max_concurrent_shell_jobs"):
        await kernel.service.save(owner, "settings", "global", {"max_concurrent_shell_jobs": 33})

    assert kernel.store.get("settings", "global") == original
