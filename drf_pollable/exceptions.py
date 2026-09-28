"""Exceptions raised by drf-pollable."""

from __future__ import annotations


class PollableConfigurationError(Exception):
    """Raised when a polling endpoint is declared with an invalid configuration.

    This is a programming error (bad ``source`` string, unknown model, duplicate
    endpoint name, missing field allow-list), so it deliberately is *not* a DRF
    ``APIException`` -- it should blow up loudly at registration or first use
    rather than quietly becoming a 500 for end users.
    """
