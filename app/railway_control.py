"""Writes a freshly captured Quotex session back into this service's own
Railway environment variables (and optionally redeploys it).

The point is to close the loop on the Cloudflare problem: a password
login is the expensive, rate-limited step, so when one finally succeeds
the resulting SSID token + cookies should outlive this process. Railway
variables are the only storage this app has that survives a redeploy, so
the app pushes the token into its own service variables through
Railway's public GraphQL API.

Nothing here is required for the app to run — with RAILWAY_API_TOKEN
unset every call is a no-op that reports why it was skipped.
"""

import logging
import time
from typing import Any

import httpx

from app import config

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT_SECONDS = 20

_VARIABLE_UPSERT_MUTATION = """
mutation variableCollectionUpsert($input: VariableCollectionUpsertInput!) {
  variableCollectionUpsert(input: $input)
}
"""

_REDEPLOY_MUTATION = """
mutation serviceInstanceRedeploy($environmentId: String!, $serviceId: String!) {
  serviceInstanceRedeploy(environmentId: $environmentId, serviceId: $serviceId)
}
"""

# Last outcome, surfaced on /api/status so a deploy can be diagnosed
# without digging through logs. Never holds token material.
_last_result: dict[str, Any] = {"attempted_at": None, "ok": None, "detail": "not attempted yet"}

# The token value most recently pushed successfully — used only to skip
# redundant writes (and the redeploy they trigger) when a reconnect
# re-captures the session we already stored.
_last_pushed_token: str | None = None


def missing_settings() -> list[str]:
    """Which required settings are absent, in the order a human would fix them."""
    required = {
        "RAILWAY_API_TOKEN": config.RAILWAY_API_TOKEN,
        "RAILWAY_PROJECT_ID": config.RAILWAY_PROJECT_ID,
        "RAILWAY_ENVIRONMENT_ID": config.RAILWAY_ENVIRONMENT_ID,
        "RAILWAY_SERVICE_ID": config.RAILWAY_SERVICE_ID,
    }
    return [name for name, value in required.items() if not value]


def is_configured() -> bool:
    return not missing_settings()


def status() -> dict[str, Any]:
    return {
        "configured": is_configured(),
        "missing_settings": missing_settings(),
        "redeploy_after_login": config.RAILWAY_REDEPLOY_AFTER_LOGIN,
        **_last_result,
    }


def _record(ok: bool | None, detail: str) -> None:
    global _last_result
    _last_result = {"attempted_at": int(time.time()), "ok": ok, "detail": detail}


def _headers() -> dict[str, str]:
    # Project-scoped tokens authenticate with their own header; personal
    # and team tokens use a normal bearer.
    if config.RAILWAY_TOKEN_TYPE == "project":
        return {"Project-Access-Token": config.RAILWAY_API_TOKEN}
    return {"Authorization": f"Bearer {config.RAILWAY_API_TOKEN}"}


async def _graphql(client: httpx.AsyncClient, query: str, variables: dict[str, Any]) -> Any:
    response = await client.post(
        config.RAILWAY_API_URL,
        json={"query": query, "variables": variables},
        headers=_headers(),
    )
    if response.status_code >= 400:
        # Deliberately not echoing the request body — it carries the session token.
        raise RuntimeError(f"Railway API returned HTTP {response.status_code}: {response.text[:300]}")

    body = response.json()
    if body.get("errors"):
        messages = "; ".join(str(e.get("message", e)) for e in body["errors"])
        raise RuntimeError(f"Railway API error: {messages}")
    return body.get("data")


async def persist_session(token: str, cookies: str, user_agent: str) -> bool:
    """Upserts QUOTEX_SESSION_* into this service's Railway variables and
    (by default) redeploys so a clean boot verifies the stored token.

    Returns True when the variables were written. Never raises — a
    persistence failure must not take down a working connection.
    """
    global _last_pushed_token

    if not token:
        _record(None, "skipped: no session token captured")
        return False

    missing = missing_settings()
    if missing:
        detail = f"skipped: {', '.join(missing)} not set"
        logger.info("Not persisting session to Railway — %s", detail)
        _record(None, detail)
        return False

    if token == _last_pushed_token:
        _record(None, "skipped: this token is already stored in Railway")
        return False

    variables = {
        "QUOTEX_SESSION_TOKEN": token,
        "QUOTEX_SESSION_COOKIES": cookies or "",
        "QUOTEX_USER_AGENT": user_agent or "",
    }

    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            await _graphql(
                client,
                _VARIABLE_UPSERT_MUTATION,
                {
                    "input": {
                        "projectId": config.RAILWAY_PROJECT_ID,
                        "environmentId": config.RAILWAY_ENVIRONMENT_ID,
                        "serviceId": config.RAILWAY_SERVICE_ID,
                        "variables": variables,
                    }
                },
            )
            _last_pushed_token = token
            logger.info(
                "Persisted Quotex session to Railway variables (token %d chars, cookies %d chars)",
                len(token),
                len(cookies or ""),
            )

            if not config.RAILWAY_REDEPLOY_AFTER_LOGIN:
                _record(True, "variables updated (redeploy disabled)")
                return True

            try:
                await _graphql(
                    client,
                    _REDEPLOY_MUTATION,
                    {
                        "environmentId": config.RAILWAY_ENVIRONMENT_ID,
                        "serviceId": config.RAILWAY_SERVICE_ID,
                    },
                )
            except Exception as e:
                # The token is stored, which is the part that matters;
                # the next natural restart will pick it up anyway.
                logger.warning("Persisted session but redeploy failed: %s", e)
                _record(True, f"variables updated, redeploy failed: {e}")
                return True

            logger.info("Requested Railway redeploy so the persisted session is verified on a clean boot")
            _record(True, "variables updated, redeploy requested")
            return True
    except Exception as e:
        logger.warning("Failed to persist session to Railway: %s", e)
        _record(False, str(e))
        return False
