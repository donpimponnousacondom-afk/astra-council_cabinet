"""Example tool; the host Hortator environment supplies the registry API."""

from time import time

from hortator.plugins import PluginSpec, schema
from hortator.timekeeping import council_timezone, local_timestamp


def register(registry):
    async def clock(arguments, context, configuration, api_key):
        return {"now": local_timestamp(time(), council_timezone(registry.store))}

    registry.register(
        PluginSpec(
            id="clock",
            name="Clock",
            description='Read the current council-local time with its UTC offset: {"operation":"now"}.',
            parameters=schema({"operation": {"type": "string", "enum": ["now"]}}, ("operation",)),
            handler=clock,
            defaults={},
            keyless=True,
            installed_description=True,
        )
    )
