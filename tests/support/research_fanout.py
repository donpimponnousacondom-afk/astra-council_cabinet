"""Shared research fanout test helpers."""

from hortator.research_assistant import ID


def set_limit(kernel, value, *, bot=False):
    kind, entity_id = ("bots", "ada") if bot else ("plugins", ID)
    entity = kernel.store.get(kind, entity_id)
    target = entity.setdefault("plugin_config", {}).setdefault(ID, {}) if bot else entity["config"]
    target["max_parallel_jobs"] = value
    kernel.store.put(kind, entity)


def worker(kernel, run):
    # Keep real registry admission and grant/profile checks while replacing only
    # paid inference in lifecycle/admission tests.
    _, check = kernel.jobs.handlers[ID]
    kernel.jobs.handlers[ID] = (run, check)
