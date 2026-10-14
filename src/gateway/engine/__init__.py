"""Execution engine for the ShareLLM Smart Gateway.

Two modules make up the engine:

* :mod:`gateway.engine.proxy` — the transparent forwarding core
  (:func:`~gateway.engine.proxy.forward_request`): it strips hop-by-hop headers
  before forwarding, and relays one payload either as a fully buffered JSON
  response or as a raw byte stream whose first chunk is inspected (TTFT) before
  the caller's stream commits.
* :mod:`gateway.engine.router` — the resilient failover router
  (:func:`~gateway.engine.router.dispatch`): it walks a route's ordered targets,
  treats retryable statuses (429/5xx) and transport errors as failures, and
  skips recently failed targets through a per-application
  :class:`~gateway.engine.router.CooldownRegistry`.
"""
