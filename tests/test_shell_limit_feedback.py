"""Entry-limit receipts survive transport and explain rollback without schema churn."""

import json
from pathlib import Path

import pytest

from hortator import shell_worker
from support.agentic_runtime import grant
from test_filesystem import require_runner


def test_entry_monitor_counts_directories_and_returns_a_bounded_lower_estimate(tmp_path):
    (tmp_path / "tree").mkdir()
    (tmp_path / "tree" / "empty").touch()
    assert shell_worker.entry_limit_exceeded(tmp_path, 2) is None
    for index in range(8):
        (tmp_path / str(index)).touch()
    assert shell_worker.entry_limit_exceeded(tmp_path, 2) == {
        "resource": "entries",
        "path": str(tmp_path),
        "limit": 2,
        "observed_at_least": 3,
    }


@pytest.mark.parametrize("storage", ["filesystem", "workspace", "disposable"])
@pytest.mark.parametrize("path", ["/workspace", "/tmp", "/packages"])
async def test_real_entry_limit_names_tree_count_and_preserves_saved_files(kernel, storage, path):
    runner = await require_runner(kernel)
    context = grant(kernel, "shell", *([] if storage == "disposable" else [storage]))
    scope = {"task": "limits"} if storage == "workspace" else {}
    if storage == "workspace":
        await kernel.registry.call("workspace", {"operation": "start", **scope}, context, "start")
    if storage != "disposable":
        await kernel.registry.call(
            storage, {"operation": "write", **scope, "path": "keep", "content": "saved"}, context, "write"
        )
    plugin = kernel.store.get("plugins", "shell")
    kernel.store.put("plugins", {**plugin, "config": {**plugin["config"], "package_entries": 1000}})
    schemas = kernel.registry.schemas(context)
    result = await kernel.registry.call(
        "shell",
        {
            "operation": "run",
            **scope,
            "command": "rm -f /workspace/keep; python3 - <<'PY'\n"
            "from pathlib import Path\nimport time\n"
            f"for i in range(2000): Path('{path}/entry'+str(i)).touch()\n"
            "time.sleep(3)\nPY",
        },
        context,
        "limited",
    )
    expected = {"resource": "entries", "path": path, "limit": 1000, "observed_at_least": 1001}
    assert result["status"] == "resource_limited" and result["exit_code"] == -9, result
    assert not result["workspace_committed"]
    assert result["resource_limit"] == expected
    assert path in result["error"] and "at least 1001" in result["error"] and "limit 1000" in result["error"]
    assert "entry count" in result["error"] and "longer timeout" in result["error"]
    assert result["limits"]["max_files"] == 1000
    capacity = (256 if storage == "filesystem" else 128) * 1024 * 1024
    assert result["limits"]["quota_bytes"] == capacity
    assert result["limits"]["max_file_bytes"] == (128 if storage == "filesystem" else 32) * 1024 * 1024
    assert runner.status(context, result["job_id"])["resource_limit"] == expected
    event = kernel.store.one("SELECT data FROM events WHERE kind='job.failed' ORDER BY seq DESC LIMIT 1")
    assert json.loads(event["data"])["resource_limit"] == expected
    if storage != "disposable":
        kept = await kernel.registry.call(
            storage, {"operation": "read", **scope, "path": "keep"}, context, "read"
        )
        assert kept["content"] == "saved"
        assert "previously saved files are unchanged" in result["error"]
    else:
        assert "disposable" in result["error"] and "previously saved" not in result["error"]
    assert kernel.registry.schemas(context) == schemas


@pytest.mark.parametrize(
    "bad_detail",
    [
        None,
        {},
        {"path": "/host"},
        {
            "resource": "entries",
            "path": "/workspace",
            "limit": 1000,
            "observed_at_least": True,
        },
        {
            "resource": "entries",
            "path": "/workspace",
            "limit": 999,
            "observed_at_least": 1000,
        },
    ],
)
async def test_real_transport_rejects_malformed_limit_receipt(kernel, tmp_path, monkeypatch, bad_detail):
    runner = await require_runner(kernel)
    context = grant(kernel, "filesystem", "shell")
    frame = {
        "type": "result",
        "status": "resource_limited",
        "exit_code": -9,
        "elapsed_seconds": 0,
        "cwd": ".",
        "resource_limit": bad_detail,
    }
    worker = tmp_path / "invalid-worker.py"
    worker.write_text(
        "print(" + repr(json.dumps(frame) + "\n" + json.dumps({"type": "complete"})) + ", flush=True)\n"
    )
    original = runner._command

    def command(*args):
        values = original(*args)
        values[values.index(str(Path(shell_worker.__file__)))] = str(worker)
        return values

    monkeypatch.setattr(runner, "_command", command)
    result = await kernel.registry.call("shell", {"operation": "run", "command": "true"}, context, "bad")
    assert result["status"] == "failed" and not result["workspace_committed"], result
    assert "Invalid worker resource-limit detail" in result["error"]
    assert "resource_limit" not in result


async def test_saved_package_and_execute_guidance_is_static_across_usage(kernel):
    context = grant(kernel, "filesystem", "shell")
    before = kernel.registry.schemas(context)
    shell = next(item["function"] for item in before if item["function"]["name"] == "shell")
    description = shell["description"]
    assert "--target /workspace/pylibs" in description and "PYTHONPATH=/workspace/pylibs" in description
    assert "preserve owner-execute" in description and "are not preserved" not in description
    assert "file/directory entry limits" in description
    await kernel.registry.call(
        "filesystem", {"operation": "write", "path": "note", "content": "changed"}, context, "write"
    )
    assert kernel.registry.schemas(context) == before
    filesystem = await kernel.registry.call("filesystem", {}, context, "help")
    assert "Owner-execute permission" in filesystem["usage"]["description"]
    assert "are not preserved" not in filesystem["usage"]["description"]
    assert "current usage" in filesystem["usage"]["description"]
