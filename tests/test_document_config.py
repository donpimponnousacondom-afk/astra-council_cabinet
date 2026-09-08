import pytest

from hortator.models import ControlError


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "https://user:password@example.com",
        "https://example.com?q=1",
        "http://[broken",
        "http://localhost:invalid",
        "https://example.com/#section",
    ],
)
async def test_publication_url_validation_rejects_invalid_configuration(kernel, owner, url):
    before = kernel.store.get("plugins", "document_site")
    with pytest.raises(ControlError):
        await kernel.service.save(owner, "plugins", "document_site", {"config": {"public_base_url": url}})
    assert kernel.store.get("plugins", "document_site") == before


async def test_document_url_bot_overrides_and_budget_defaults(kernel, owner):
    public = kernel.service.public("bots", kernel.store.get("bots", "ada"))
    assert public["document_task_rounds"] == 20
    assert public["document_task_calls_per_round"] == 8
    assert public["document_task_seconds"] == 900
    with pytest.raises(ControlError, match="must be a string"):
        await kernel.service.save(
            owner, "bots", "ada", {"plugin_config": {"document_site": {"local_base_url": 17}}}
        )
    with pytest.raises(ControlError, match="must not be empty"):
        await kernel.service.save(
            owner, "bots", "ada", {"plugin_config": {"document_site": {"local_base_url": ""}}}
        )
    value = await kernel.service.save(
        owner,
        "bots",
        "ada",
        {"plugin_config": {"document_site": {"public_base_url": "https://example.com/council"}}},
    )
    assert value["plugin_config"]["document_site"]["public_base_url"] == "https://example.com/council"
