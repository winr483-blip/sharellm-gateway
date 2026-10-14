"""Provider adapters.

Houses the provider-specific translation the engine and the HTTP layer build
on: :mod:`gateway.adapters.sharellm` (ShareLLM error normalization and
prompt-cache telemetry) and :mod:`gateway.adapters.sanitizer` (the anti-422
request-parameter guard). Further wire-protocol adapters (``openai``) will be
added here as later tasks require them.
"""
