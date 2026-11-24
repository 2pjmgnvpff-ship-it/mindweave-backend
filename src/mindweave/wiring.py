"""Composition-root runtime wiring for AppContainer ports (`gateway`, `safety`, `job_queue`, `push_provider`)."""

from __future__ import annotations

import inspect
from typing import Any

from mindweave.ai.gateway import ModelGateway
from mindweave.ai.ports import ModelGatewayPort, SafetyServicePort
from mindweave.core.dependencies import AppContainer
from mindweave.modules.identity.infra.email_sender import build_email_otp_sender
from mindweave.modules.memory.infra.search_cache import PersonSearchIndexCache
from mindweave.modules.reminders.infra.push import CompositePushDispatcher
from mindweave.modules.safety.application.service import SafetyService
from mindweave.workers.ports import JobQueuePort
from mindweave.workers.queue import PostgresJobQueue


def wire_container_services(container: AppContainer) -> AppContainer:
    """Populate `container.extras` with concrete adapter singletons while respecting test overrides."""
    existing_gateway: ModelGatewayPort | None = (
        container.extras.get("gateway")
        or container.extras.get("model_gateway")
        or container.extras.get("ai_gateway")
    )
    if existing_gateway is not None:
        gateway: ModelGatewayPort = existing_gateway
    else:
        gw_kwargs: dict[str, Any] = {"clock": container.clock}
        if "db" in inspect.signature(ModelGateway.__init__).parameters:
            gw_kwargs["db"] = container.db
        gateway = ModelGateway(container.settings, **gw_kwargs)
    container.extras["gateway"] = gateway
    container.extras["model_gateway"] = gateway

    existing_safety: SafetyServicePort | None = container.extras.get(
        "safety"
    ) or container.extras.get("safety_service")
    safety: SafetyServicePort = existing_safety or SafetyService(
        gateway,
        timeout_seconds=container.settings.safety_timeout_seconds,
    )
    container.extras["safety"] = safety
    container.extras["safety_service"] = safety

    existing_queue: JobQueuePort | None = container.extras.get("job_queue") or container.extras.get(
        "queue"
    )
    queue: JobQueuePort = existing_queue or PostgresJobQueue(clock=container.clock)
    container.extras["job_queue"] = queue

    if "push_provider" not in container.extras and "push" not in container.extras:
        container.extras["push_provider"] = CompositePushDispatcher.from_adapter_kinds(
            apns=container.settings.push_apns_adapter,
            web_push=container.settings.push_web_push_adapter,
        )
    elif "push_provider" not in container.extras and "push" in container.extras:
        container.extras["push_provider"] = container.extras["push"]

    if "otp_sender" not in container.extras:
        container.extras["otp_sender"] = build_email_otp_sender(container.settings)

    search_cache = container.extras.get("search_index_cache")
    if not isinstance(search_cache, PersonSearchIndexCache):
        search_cache = PersonSearchIndexCache()
        container.extras["search_index_cache"] = search_cache
    container.cipher.register_eviction_hook(search_cache.evict_user)

    return container
