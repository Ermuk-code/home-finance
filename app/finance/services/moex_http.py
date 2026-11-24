"""HTTP client configuration shared by the MOEX ISS integrations."""

import logging
import os
import ssl

import certifi
import httpx

logger = logging.getLogger(__name__)


def create_moex_client(timeout: httpx.Timeout) -> httpx.AsyncClient:
    ca_file = os.getenv("MOEX_CA_FILE")
    if not ca_file:
        return httpx.AsyncClient(timeout=timeout)

    try:
        context = ssl.create_default_context(cafile=certifi.where())
        context.load_verify_locations(cafile=ca_file)
    except OSError as exc:
        logger.error("MOEX ISS: не удалось загрузить MOEX_CA_FILE %s: %s", ca_file, exc)
        return httpx.AsyncClient(timeout=timeout)

    return httpx.AsyncClient(timeout=timeout, verify=context)
