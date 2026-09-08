import json

import httpx
import pytest
from pydantic import ValidationError

from conftest import configured
from hortator.models import Profile
from hortator.provider import ProviderError, normalized_usage


RATES = {
    "input_price_per_million": 3,
    "output_price_per_million": 15,
    "cache_hit_input_price_per_million": 0.014,
    "cache_miss_input_price_per_million": 3,
}


def test_cache_hit_discount_and_reasoning_are_not_double_counted():
    metrics = normalized_usage(
        {
            "prompt_tokens": 1_000_000,
            "completion_tokens": 100_000,
            "completion_tokens_details": {"reasoning_tokens": 90_000},
            "prompt_cache_hit_tokens": 900_000,
            "prompt_cache_miss_tokens": 100_000,
        },
        RATES,
    )
    assert metrics["cost"] == pytest.approx(1.8126)
    assert metrics["cost_source"] == "estimated"
    assert metrics["reasoning_tokens"] == 90_000
    assert metrics["pricing"]["basis"] == "cache_split"
    assert metrics["pricing"]["output_includes_reasoning"] is True


@pytest.mark.parametrize(
    "split",
    [
        {"prompt_tokens_details": {"cached_tokens": 90}},
        {"prompt_cache_hit_tokens": 90},
        {"prompt_cache_miss_tokens": 10},
    ],
)
def test_reported_total_plus_one_cache_part_determines_other_part(split):
    metrics = normalized_usage({"prompt_tokens": 100, "completion_tokens": 20, **split}, RATES)
    assert metrics["cached_tokens"] == 90
    assert metrics["pricing"]["cache_miss_tokens"] == 10
    assert metrics["cost"] == pytest.approx((90 * 0.014 + 10 * 3 + 20 * 15) / 1_000_000)


@pytest.mark.parametrize(
    "usage",
    [
        {"prompt_tokens": 100, "completion_tokens": 20},
        {"prompt_tokens": 100, "completion_tokens": 20, "prompt_cache_hit_tokens": 101},
        {"prompt_tokens": 100, "completion_tokens": 20, "prompt_cache_miss_tokens": 101},
        {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "prompt_cache_hit_tokens": 90,
            "prompt_cache_miss_tokens": 20,
        },
        {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "prompt_cache_hit_tokens": 90,
            "prompt_tokens_details": {"cached_tokens": 80},
        },
        {"prompt_tokens": 100, "prompt_cache_hit_tokens": 90},
        {"completion_tokens": 20, "prompt_cache_hit_tokens": 90, "prompt_cache_miss_tokens": 10},
    ],
)
def test_unavailable_or_inconsistent_split_is_not_flat_priced(usage):
    metrics = normalized_usage(usage, RATES)
    assert metrics["cost"] is None
    assert metrics["cost_source"] is None
    assert metrics["pricing"]["note"]


def test_explicit_cold_and_zero_cost_usage_are_supported():
    cold = normalized_usage(
        {"prompt_tokens": 100, "completion_tokens": 20, "prompt_cache_hit_tokens": 0}, RATES
    )
    assert cold["cost"] == pytest.approx(0.0006)
    free = normalized_usage(
        {"prompt_tokens": 0, "completion_tokens": 0, "prompt_cache_hit_tokens": 0}, {key: 0 for key in RATES}
    )
    assert free["cost"] == 0
    assert free["cost_source"] == "estimated"


def test_reported_cost_including_zero_wins_even_without_usage_or_rates():
    for cost in (0, 1.23):
        metrics = normalized_usage({"cost": cost}, {})
        assert metrics["cost"] == cost
        assert metrics["cost_source"] == "reported"
        assert metrics["pricing"]["basis"] == "provider_reported"


def test_legacy_flat_rates_and_partial_cache_configuration():
    usage = {"prompt_tokens": 100, "completion_tokens": 20, "prompt_cache_hit_tokens": 90}
    legacy = {"input_price_per_million": 3, "output_price_per_million": 15}
    assert normalized_usage(usage, legacy)["cost"] == pytest.approx(0.0006)
    incomplete = normalized_usage(usage, {**legacy, "cache_hit_input_price_per_million": 0.014})
    assert incomplete["cost"] is None
    assert "require" in incomplete["pricing"]["note"]


@pytest.mark.parametrize("value", [-1, float("inf"), float("nan")])
def test_invalid_cache_prices_are_rejected(value):
    with pytest.raises(ValidationError):
        Profile(
            id="pricing",
            name="Pricing",
            provider_id="provider",
            model="model",
            cache_hit_input_price_per_million=value,
        )


@pytest.mark.parametrize("failed", [False, True])
async def test_completed_and_failed_request_keep_immutable_pricing_evidence(kernel, failed):
    bot = configured(kernel)
    profile = kernel.store.get("profiles", "balanced")
    profile.update(RATES, stream=False)
    usage = {"prompt_tokens": 100, "completion_tokens": 20, "prompt_cache_hit_tokens": 90}

    def response(request):
        return httpx.Response(
            200,
            json={
                "usage": usage,
                "choices": [
                    {
                        "message": {
                            "content": "Answer",
                            "tool_calls": [{"id": "bad", "type": "invalid"}] if failed else [],
                        },
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    await kernel.pool.client.aclose()
    kernel.pool.client = httpx.AsyncClient(transport=httpx.MockTransport(response))
    kwargs = dict(
        bot=bot,
        profile=profile,
        messages=[{"role": "user", "content": "hello"}],
        tools=[],
        turn_id="pricing-test",
        context={"estimated_tokens": 1},
    )
    if failed:
        with pytest.raises(ProviderError):
            await kernel.pool.complete(**kwargs)
    else:
        await kernel.pool.complete(**kwargs)
    row = kernel.store.one("SELECT * FROM requests")
    evidence = json.loads(row["response"])["pricing"]
    assert row["status"] == ("failed" if failed else "completed")
    assert evidence["cost"] == row["cost"]
    assert evidence["rates_per_million"] == RATES
    assert evidence["rate_schedule"] == "manual_profile_snapshot"
    assert json.loads(row["usage"]) == usage
    stored = kernel.store.get("profiles", "balanced")
    stored["cache_hit_input_price_per_million"] = 99
    stored.pop("revision")
    kernel.store.put("profiles", stored)
    assert json.loads(kernel.store.one("SELECT response FROM requests")["response"])["pricing"] == evidence
