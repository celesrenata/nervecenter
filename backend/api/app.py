"""NerveCenter FastAPI application wiring.

Both the module-level :data:`app` instance and :func:`create_app` expose the
full spec §46 API surface: the legacy core collections (``/agents``,
``/goals``, ``/tasks``, ``/knowledge``), the canonical ``/api/*`` aliases for
those collections, the event/entity/evidence routers, all twelve read-only
collection routers, and the dependency-tolerant health endpoints.

T208: The backend honours X-Forwarded-Proto via uvicorn's proxy_headers flag.
When uvicorn is started with ``--proxy-headers --forwarded-allow-ips``, it
modifies the request scope ``scheme`` and ``client`` based on X-Forwarded-*
headers, preventing 307 redirects to http:// behind TLS-terminating ingresses.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

import structlog

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

log = structlog.get_logger(__name__)

from backend.api.auth import (
    approvals_router as auth_approvals_router,
    require_person,
    router as auth_router,
)
from backend.api.docs import custom_openapi, get_version_info
from backend.attention import get_attention_service
from backend.briefing import get_briefing_service
from backend.calendar import get_calendar_service
from backend.communications import get_communications_service
from backend.contacts import get_contacts_service
from backend.connectors import get_mail_connector_service
from backend.drafts import get_drafts_service
from backend.email import get_email_service
from backend.messages import get_messages_service
from backend.voicemail import get_voicemail_service
from backend.waiting_on import get_waiting_on_service
from backend.api.endpoints import (
    agent_runner,
    agents,
    omniroute_keys,
    auth_tokens,
    attention,
    briefing,
    approvals,
    bills,
    finance,
    research,
    projects,
    mcp_projects,
    handoffs,
    model_policy,
    calendar,
    calendar_deletions,
    communications,
    contact_photos,
    infra,
    conversations,
    connections,
    contacts,
    people,
    changes,
    claims,
    clients,
    compute,
    context,
    decisions,
    drafts,
    logs,
    entities,
    followups,
    events,
    events_ws,
    email,
    mail_connectors,
    mail_security,
    mail_security_confirmations,
    mail_security_expectedness,
    mail_security_feedback,
    evidence,
    voicemail,
    waiting_on,
    goals,
    health_samples,
    mental_health,
    incidents,
    inference,
    inference_durable,
    knowledge,
    messages,
    metrics,
    models,
    models_history,
    obligations,
    overview,
    relationships,
    relay,
    routines,
    routes,
    runs,
    search,
    systems,
    tasks,
    today,
    ask_sessions,
    voice,
    wellness,
)
from backend.api.health import router as health_router
from backend.api.observability import router as observability_router
from backend.api.wiring import install_dependency_wiring


ALL_ROUTERS = (
    agent_runner.router,
    agents.router,
    omniroute_keys.router,
    auth_tokens.router,
    attention.router,
    briefing.router,
    email.router,
    mail_connectors.router,
    mail_security.router,
    mail_security_feedback.router,
    mail_security_expectedness.router,
    mail_security_confirmations.router,
    messages.router,
    voicemail.router,
    ask_sessions.router,
    voice.router,
    waiting_on.router,
    calendar.router,
    calendar_deletions.router,
    contacts.router,
    contact_photos.router,
    infra.router,
    people.router,
    communications.router,
    conversations.router,
    drafts.router,
    followups.router,
    goals.router,
    tasks.router,
    evidence.router,
    knowledge.router,
    events.router,
    events_ws.router,
    entities.router,
    relationships.router,
    claims.router,
    search.router,
    context.router,
    runs.router,
    incidents.router,
    inference.router,
    inference_durable.router,
    models.router,
    compute.router,
    routines.router,
    routes.router,
    changes.router,
    approvals.router,
    clients.router,
    health_router,
    observability_router,
    auth_router,
    auth_approvals_router,
    overview.router,
    decisions.router,
    systems.router,
    models_history.router,
    metrics.router,
    logs.router,
    connections.router,
    relay.router,
    today.router,
    wellness.router,
    bills.router,
    finance.router,
    research.router,
    projects.router,
    mcp_projects.router,
    handoffs.router,
    model_policy.router,
    health_samples.router,
    mental_health.router,
    obligations.router,
)


def _install_connection_control_plane(app: FastAPI) -> None:
    """Bind the connector control plane onto ``app.state``.

    Reuses the *single* durable registry that :mod:`backend.api.wiring`
    already built — which is itself bound to the Fernet ``SecretCodec`` when
    ``NERVE_SECRET_KEY`` is set — so this plane, the mail worker, and the
    calendar sync all share one codec-bound registry. It is set to ``None``
    when no registry is present, so the ``/api/connections`` and
    ``/api/relay`` routers keep their honest degradation semantics.

    The loaded vault (``app.state.secret_vault``, set by the wiring) is the
    plane's secret gate: when the key is absent it is ``None`` and the plane
    stays available for credential-less config, with secret operations
    failing a stable ``vault_unavailable`` code instead of storing
    plaintext. No second registry is rebuilt here.
    """

    registry = getattr(app.state, "repository_registry", None)
    if registry is None:
        app.state.connection_control_plane = None
        return
    from backend.connectors.registry import ConnectionControlPlane
    from backend.connectors.wiring import install_connection_wiring

    vault = getattr(app.state, "secret_vault", None)
    # Relay message bodies go to the same encrypted object store as mail
    # (None when none is configured: bodies are then not durable, and the
    # control plane logs it).
    from backend.ingest.supervisor import _object_store_from_env

    app.state.connection_control_plane = ConnectionControlPlane(
        registry, vault=vault, object_store=_object_store_from_env()
    )
    # P1-103: register the production probes, the durable OAuth code→token
    # exchanger, and the mail-ingest sync triggers (the latter only when the
    # ingest supervisor is live). Best-effort — a registration failure leaves
    # the affected endpoint in its honest degraded state.
    try:
        install_connection_wiring(app)
    except Exception as exc:  # noqa: BLE001 - startup must not crash
        log.warning("api.connection_wiring_failed", error=str(exc))


def _install_control_plane_wiring(target: FastAPI) -> None:
    """Wrap the dependency-wiring lifespan to install the control plane.

    Runs after :func:`backend.api.wiring.wire_dependencies` (which installs
    ``repository_registry`` and starts/stops workstream C's mail-ingest
    supervisor) and before the first request is served.
    """

    original = target.router.lifespan_context

    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        async with original(app):
            # Anchor the in-service log capture. This runs *after* uvicorn's
            # dictConfig (which runs during server setup, before the lifespan)
            # so the stdlib handler survives uvicorn's reconfiguration and the
            # structlog pipeline is re-anchored for this app instance.
            try:
                from backend.observability.log_buffer import install_log_capture

                install_log_capture()
            except Exception as exc:  # noqa: BLE001 - capture must not crash startup
                log.warning("api.log_capture_install_failed", error=str(exc))
            try:
                _install_connection_control_plane(app)
            except Exception as exc:  # noqa: BLE001 - startup must not crash
                log.warning("api.control_plane_install_failed", error=str(exc))
                app.state.connection_control_plane = None
            # Restore recent relay messages into the in-memory Messages plane,
            # which is empty after every restart (bodies are durable, NS-ADR-091).
            try:
                from backend.ingest.supervisor import _object_store_from_env
                from backend.messages.rehydrate import rehydrate_messages

                counts = await rehydrate_messages(
                    getattr(app.state, "repository_registry", None),
                    _object_store_from_env(),
                    getattr(app.state, "messages_service", None),
                )
                if any(counts.values()):  # silent when there is nothing to restore
                    log.info("api.messages_rehydrated", **counts)
            except Exception as exc:  # noqa: BLE001 - startup must not crash
                log.warning("api.messages_rehydrate_failed", error=str(exc))
            # Contact names into Messages + the Contacts screen (NS-ADR-097).
            try:
                from backend.contacts.durable import hydrate_views

                if getattr(app.state, "contacts_service", None) is None:
                    app.state.contacts_service = get_contacts_service()
                counts = await hydrate_views(
                    getattr(app.state, "repository_registry", None),
                    getattr(app.state, "messages_service", None),
                    app.state.contacts_service,
                )
                if counts["people"]:
                    log.info("api.contacts_hydrated", **counts)
            except Exception as exc:  # noqa: BLE001 - startup must not crash
                log.warning("api.contacts_hydrate_failed", error=str(exc))
            # The Email screen reads an in-memory service; fill it from stored
            # mail (security verdicts respected, see backend.email.durable_feed).
            try:
                from backend.email.durable_feed import DurableEmailFeed
                from backend.ingest.supervisor import _object_store_from_env

                registry = getattr(app.state, "repository_registry", None)
                if registry is not None:
                    if getattr(app.state, "email_service", None) is None:
                        app.state.email_service = get_email_service()
                    feed = DurableEmailFeed(
                        registry, _object_store_from_env(), app.state.email_service
                    )
                    app.state.email_durable_feed = feed
                    counts = await feed.refresh(force=True)
                    if any(counts.values()):
                        log.info("api.email_rehydrated", **counts)
            except Exception as exc:  # noqa: BLE001 - startup must not crash
                log.warning("api.email_rehydrate_failed", error=str(exc))
            # Agent runner loop (NS-ADR-099): durable cadence + back-off.
            runner = None
            researcher = None
            try:
                from backend.agents.runner import get_runner, runner_disabled
                if not runner_disabled():
                    runner = get_runner(app)
                    if runner is not None:
                        key_store = getattr(app.state, "omniroute_key_store", None)
                        if key_store is not None:  # NS-ADR-117: pick up key changes made elsewhere
                            runner.add_tick_hook(key_store.refresh)
                        runner.start()
                        log.debug("api.agent_runner_started")
                        from backend.research import get_engine
                        researcher = get_engine(app)
                        try:
                            from backend.handoffs.central import attach as attach_handoffs

                            attach_handoffs(app)
                        except Exception as exc:  # noqa: BLE001 - startup must not crash
                            log.warning("api.handoff_executor_failed", error=type(exc).__name__)
            except Exception as exc:  # noqa: BLE001 - startup must not crash
                log.warning("api.agent_runner_start_failed", error=str(exc))
            # Infra MCP on its own port, the only thing opencode may reach (NS-ADR-121).
            infra_mcp = None
            try:
                from backend.infra import mcp_listener

                infra_mcp = await mcp_listener.start(app)
            except Exception as exc:  # noqa: BLE001 - startup must not crash
                log.warning("api.infra_mcp_listener_failed", error=type(exc).__name__)
            try:
                yield
            finally:
                if infra_mcp is not None:
                    from backend.infra import mcp_listener

                    await mcp_listener.stop(infra_mcp)
                if researcher is not None:
                    try:
                        await researcher.stop()
                    except Exception as exc:  # noqa: BLE001
                        log.warning("api.research_stop_failed", error=str(exc))
                if runner is not None:
                    try:
                        await runner.stop()
                    except Exception as exc:  # noqa: BLE001
                        log.warning("api.agent_runner_stop_failed", error=str(exc))

    target.router.lifespan_context = _lifespan


def canonical_aliases() -> APIRouter:
    """Build the canonical ``/api/agents`` read aliases.

    Goals and tasks now serve their canonical ``/api/goals`` / ``/api/tasks``
    read AND write routes directly from :mod:`backend.api.endpoints.goals`
    and :mod:`backend.api.endpoints.tasks` (see the spec §46 contract and T-H33
    write/mutation work). The alias router therefore only covers the legacy
    read routes that no endpoint module owns under the ``/api`` prefix.
    """
    alias = APIRouter(tags=["canonical-aliases"])

    @alias.get("/api/agents")
    async def api_list_agents(request: Request):
        """List agents under the canonical prefix."""
        return await agents.list_agents(request)

    @alias.get("/api/agents/{agent_id}")
    async def api_get_agent(agent_id: str, request: Request):
        """Get an agent by ID under the canonical prefix."""
        return await agents.get_agent(agent_id, request)

    @alias.get("/api/overview")
    async def api_get_overview(request: Request):
        """Get overview data under the canonical prefix."""
        return await overview.get_overview(request)

    @alias.get("/api/decisions")
    async def api_list_decisions():
        """List decisions under the canonical prefix."""
        return await decisions.list_decisions()

    @alias.get("/api/decisions/{decision_id}")
    async def api_get_decision(decision_id: str):
        """Get a decision by ID under the canonical prefix."""
        return await decisions.get_decision(decision_id)

    return alias


def _cors_origins() -> list[str]:
    """Resolve the explicit CORS origin allow-list from ``NC_CORS_ORIGINS``.

    The value is a comma-separated list of concrete origins (e.g.
    ``https://app.example.com,https://panel.example.com``). An unset or empty
    value yields ``[]`` — the API then serves no cross-origin requests at all.

    A wildcard (``*``) can never appear: origins are only ever taken verbatim
    from the explicit allow-list, so a wildcard can not be configured even by
    accident, and the wildcard + credentials combination is structurally
    impossible (see the middleware wiring in :func:`_configure`).
    """
    raw = os.environ.get("NC_CORS_ORIGINS", "")
    origins = [part.strip() for part in raw.split(",") if part.strip()]
    return [o for o in origins if o != "*"]


def _configure(target: FastAPI) -> None:
    """Attach CORS middleware, every router, and the version endpoint."""
    # Credentials are only ever enabled together with an explicit, non-empty
    # origin list. The wildcard origin (``*``) is never used, so the
    # "allow any origin AND allow credentials" combination — which browsers
    # must reject and which leaks credentials to arbitrary origins — can not
    # occur.
    origins = _cors_origins()
    target.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=bool(origins),
        allow_methods=["*"],
        allow_headers=["*"],
    )

    for router in ALL_ROUTERS:
        target.include_router(router)
    target.include_router(canonical_aliases())

    @target.get("/api/openapi.json", include_in_schema=False)
    async def openapi_schema():
        """The API schema (login required; Swagger UI is off)."""
        return target.openapi()

    @target.get("/version")
    async def version():
        """Get API version information"""
        return get_version_info()


def _install_ask_wiring(target: FastAPI) -> None:
    """Bind the grounded-ask service with the deployed OmniRoute transport.

    The ask pipeline (T003.AC9) needs a real transport to answer live. The
    lazy singleton (:func:`get_ask_service`) opens with *no* transport, so a
    live ``POST /api/context/ask`` degrades to the honest 202 "no transport
    configured" even when ``NERVE_OMNIROUTE_URL`` is set. We construct the
    service here from the deployed ``NERVE_OMNIROUTE_*`` environment so that:

    - ``NERVE_OMNIROUTE_URL`` set → live chat-completions routing.
    - ``NERVE_OMNIROUTE_FIXTURE`` set → deterministic offline fixture replay.
    - neither → the transport carries no base_url, so
      ``AskService.transport_available`` is False and asks stay in the honest
      202 shape.

    The default model/provider come from :func:`default_inference` (the
    catalog default, e.g. ``auto/best-chat``) rather than the constructor's
    virtual ``nerve-ask`` placeholder, so live asks route to a real catalog
    model. Best-effort: a wiring failure leaves the singleton in place rather
    than crashing startup.
    """
    try:
        from backend.context.ask import AskService
        from backend.inference import OmniRouteTransport
        from backend.inference.providers import default_inference
        from backend.inference.secure_client import secure_transport

        default = default_inference()
        target.state.ask_service = AskService(
            # NS-ADR-108: ask carries messages/mail/contacts context -> secure.
            transport=secure_transport("ask", OmniRouteTransport.from_env()),
            remote_provider_allowed=True,
            provider=default.get("provider", "omniroute"),
            model=default.get("model", "nerve-ask"),
        )
        log.info(
            "api.ask_wiring_ok",
            provider=default.get("provider"),
            model=default.get("model"),
        )
    except Exception as exc:  # noqa: BLE001 - startup must not crash
        log.warning("api.ask_wiring_failed", error=str(exc))


def create_app() -> FastAPI:
    """Factory function for creating and configuring the FastAPI application.

    Returns:
        FastAPI: Configured FastAPI application instance
    """
    target = FastAPI(
        title="NerveCenter API",
        description="HTTP/gRPC interface for the NerveCenter core",
        version="0.1.0",
        # Default-deny gate (NS-ADR-122): every route needs a signed-in person
        # unless it is on the allowlist in backend/api/public_routes.py. The
        # built-in docs/schema routes sit outside any dependency, so they are
        # off; the schema is served, gated, at GET /api/openapi.json.
        dependencies=[Depends(require_person)],
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    # D-10: structured 500 for unhandled exceptions. Starlette dispatches
    # ``HTTPException`` to its (more specific) ``StarletteHTTPException``
    # handler first — see starlette ``_lookup_exception_handler`` which walks
    # the exception's MRO — so only genuinely unhandled errors reach this
    # handler. The response body is stable and machine-readable; the
    # exception type name is included for triage, but no stack-trace text or
    # secret data is ever exposed.
    async def _unhandled_500(request: Request, exc: BaseException) -> JSONResponse:
        log.exception(
            "unhandled_exception",
            path=request.url.path,
            method=request.method,
            error_type=type(exc).__name__,
        )
        return JSONResponse(
            status_code=500,
            content={
                "detail": "Internal server error",
                "error_type": type(exc).__name__,
            },
        )

    target.add_exception_handler(Exception, _unhandled_500)

    _configure(target)
    install_dependency_wiring(target)
    _install_control_plane_wiring(target)
    _install_ask_wiring(target)

    # Attention engine (prompt2 P0): an in-process, engine-independent
    # store that is always available, so bind the singleton explicitly.
    # Dev mode (NERVE_SEED_ATTENTION=1) pre-populates a demo queue so the
    # Now screen has ranked content before email ingestion lands.
    attention_service = get_attention_service()
    if os.environ.get("NERVE_SEED_ATTENTION", "").lower() in {"1", "true", "yes"}:
        attention_service.seed_demo()
    target.state.attention_service = attention_service

    # Messages plane (prompt2 P1): in-process store that receives normalized
    # messages from the Personal Relay and (re)creates AttentionItems through
    # the shared P0 attention service. Dev mode (NERVE_SEED_MESSAGES=1)
    # pre-populates a demo thread so the Messages screen has content before
    # real relay traffic arrives.
    messages_service = get_messages_service()
    target.state.messages_service = messages_service
    if os.environ.get("NERVE_SEED_MESSAGES", "").lower() in {"1", "true", "yes"}:
        messages_service.seed_demo()

    # Email plane (prompt2 P0 — Personal Operations Plane): in-process store
    # that receives normalized email events from the Personal Relay and
    # (re)creates AttentionItems through the shared P0 attention service.
    # Dev mode (NERVE_SEED_EMAILS=1) pre-populates a demo thread so the Email
    # screen has content before real relay traffic arrives.
    email_service = get_email_service()
    target.state.email_service = email_service
    if os.environ.get("NERVE_SEED_EMAILS", "").lower() in {"1", "true", "yes"}:
        email_service.seed_demo()

    # Mail connector registry (prompt2 §46 — connector registry + liveness):
    # an in-process store of *described* mail connectors (where the relay reads
    # from + whether the mailbox answers). NerveCenter never holds a mailbox
    # socket — the Personal Relay does (§47); the registry only records the
    # connector's description and its last liveness probe. Dev mode
    # (NERVE_SEED_CONNECTORS=1) pre-populates a demo connector so the
    # Connectors screen has content before the user registers a real one.
    mail_connector_service = get_mail_connector_service()
    target.state.mail_connector_service = mail_connector_service
    if os.environ.get("NERVE_SEED_CONNECTORS", "").lower() in {"1", "true", "yes"}:
        mail_connector_service.seed_demo()

    # Voicemail plane (prompt2 P2 — Personal Operations Plane): in-process store
    # that receives normalized voicemail events from the Personal Relay (the
    # transcript and provenance are already computed by the relay — §46/§47
    # source-side deliverables, out of scope) and (re)creates AttentionItems
    # through the shared P0 attention service. Callback-request voicemails
    # surface a stronger, explainable factor. Dev mode
    # (NERVE_SEED_VOICEMAILS=1) pre-populates a demo thread so the Voicemail
    # screen has content before real relay traffic arrives.
    voicemail_service = get_voicemail_service()
    target.state.voicemail_service = voicemail_service
    if os.environ.get("NERVE_SEED_VOICEMAILS", "").lower() in {"1", "true", "yes"}:
        voicemail_service.seed_demo()

    # Waiting-On plane (prompt2 §19 — Waiting-On Detection / §31): in-process
    # store that tracks *outgoing requests that received no response*. The
    # relay / personal-ops layers detect the outgoing request and post a
    # normalized record here (channel-side detection is §46/§47 source-side,
    # out of scope); this plane only tracks it and never sends. Each OPEN
    # request produces exactly one live AttentionItem through the shared P0
    # attention service (factor: "waiting on {person} for {description}
    # ({n} days")). Dev mode (NERVE_SEED_WAITING_ON=1) pre-populates demo
    # requests so the Waiting-On screen has content before real traffic
    # arrives.
    waiting_on_service = get_waiting_on_service()
    target.state.waiting_on_service = waiting_on_service
    if os.environ.get("NERVE_SEED_WAITING_ON", "").lower() in {"1", "true", "yes"}:
        waiting_on_service.seed_demo()

    # Calendar plane (prompt2 P2): in-process read-only copy of the user's
    # schedule, used for availability + conflict reasoning (§42/§43). NerveCenter
    # never mutates the real calendar. Dev mode (NERVE_SEED_CALENDAR=1)
    # pre-populates a demo schedule so the Calendar screen has content.
    calendar_service = get_calendar_service()
    target.state.calendar_service = calendar_service
    if os.environ.get("NERVE_SEED_CALENDAR", "").lower() in {"1", "true", "yes"}:
        calendar_service.seed_demo()

    # Contacts plane (prompt2 P4 — cross-channel person resolution + VIP):
    # in-process store holding one resolved person per ``person_id``. It
    # receives normalized person records from the Personal Relay (the macOS
    # local-address-book bridge is source-side, §46/§47 — out of scope) and
    # only ever sees normalized events. The service stores/returns the VIP
    # flag (§12); the attention engine is what weighs VIP contacts heavier
    # (§22/§45) — that is a separate concern, so this store creates no
    # AttentionItems. Dev mode (NERVE_SEED_CONTACTS=1) pre-populates a demo
    # set of people (one VIP) so the Contacts screen has content before real
    # relay traffic arrives.
    contacts_service = get_contacts_service()
    target.state.contacts_service = contacts_service
    if os.environ.get("NERVE_SEED_CONTACTS", "").lower() in {"1", "true", "yes"}:
        contacts_service.seed_demo()

    # Unified communications plane (gaps4 R101 — prompt2 §5–§7, §32): the
    # canonical Person/ContactIdentity/Communication/Conversation graph the
    # existing VIP contacts and per-channel message threads delegate to.
    # In-process, always available; empty store returns empty listings.
    # There is no demo seed — the plane only gains content when real
    # identity/communication ingest arrives (R102 normalizes the existing
    # planes into it).
    communications_service = get_communications_service()
    target.state.communications_service = communications_service

    # Drafts plane (gaps1 G-1.4 — R105, prompt2 §40–§41): in-process store of
    # auditable, human-controlled drafts. The rule-based drafter is the
    # deterministic fallback; a model-assisted drafter (if injected) is
    # gated by the R107 inference policy before invocation. No endpoint in
    # this plane sends — DRAFT -> DISCARDED is the only API transition.
    target.state.drafts_service = get_drafts_service()

    # Briefing plane (prompt2 §30 — Briefings): a read-only, deterministic
    # aggregation over the personal-operations planes above. It owns no state
    # of its own and never ingests — it only *projects* already-committed
    # plane state (Now attention, open waiting-on requests, today's calendar,
    # and 24-hour activity counts) into the §30 brief at compose time. Like
    # the P0/P1/P2 stores above it is always available, so bind the singleton.
    target.state.briefing_service = get_briefing_service()

    return target


app = create_app()
