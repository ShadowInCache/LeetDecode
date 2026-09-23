"""A bad environment variable must not make the service unreachable.

Settings are built while `app.main` is imported, so an unguarded validation
error means uvicorn never starts: the platform serves 502s, /health is
unreachable, and the only evidence is a traceback in a deploy log. That shape
of failure cost several deploy cycles on this project - the service looked dead
when it was merely misconfigured.

These tests pin the recovery behaviour: start anyway, refuse every request with
503, and say exactly which variable is wrong.
"""

import importlib
import sys

import pytest
from fastapi.testclient import TestClient

# xAI's "Grok" versus GroqCloud's "Groq" - one letter apart, and the typo that
# actually took this service down. Assembled from fragments on purpose: a
# blanket find-and-replace of the misspelling has twice turned these cases into
# the *valid* value, which silently guts the tests rather than failing loudly.
INVALID_PROVIDER = "gro" + "k"
VALID_PROVIDER = "gro" + "q"


def _reload_app(monkeypatch, **env: str):
    """Re-import app.main with a given environment."""
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("SENTRY_DSN", "")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")

    from app import config

    config.get_settings.cache_clear()
    for module in [m for m in sys.modules if m == "app.main"]:
        del sys.modules[module]
    return importlib.import_module("app.main")


@pytest.fixture(autouse=True)
def _restore_app_module():
    """Leave app.main in a clean state for other test modules."""
    yield
    from app import config

    config.get_settings.cache_clear()
    sys.modules.pop("app.main", None)
    importlib.import_module("app.main")


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        # Must stay INVALID - see INVALID_PROVIDER above.
        ("LLM_PROVIDER", INVALID_PROVIDER),
        ("LLM_FALLBACK_PROVIDER", "google"),
        ("DAILY_JOB_HOUR", "3am"),
        ("LLM_DAILY_CAP", "1,000"),
        ("FREE_CALL_LIMIT", "five"),
        ("SENTRY_TRACES_SAMPLE_RATE", "10%"),
    ],
)
def test_bad_config_starts_anyway_and_explains_itself(
    monkeypatch, variable: str, value: str
) -> None:
    main = _reload_app(monkeypatch, **{variable: value})
    client = TestClient(main.app)

    response = client.get("/health")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "misconfigured"
    assert body["error"] == "INVALID_CONFIGURATION"
    # The message must name the variable, or it is no better than a 502.
    assert variable.lower() in body["detail"].lower()


def test_every_other_route_also_refuses_clearly(monkeypatch) -> None:
    main = _reload_app(monkeypatch, LLM_PROVIDER=INVALID_PROVIDER)
    client = TestClient(main.app)

    for method, path in [("post", "/translate"), ("get", "/usage/abc"), ("get", "/admin")]:
        response = getattr(client, method)(path)
        assert response.status_code == 503
        assert response.json()["error"] == "INVALID_CONFIGURATION"


def test_a_valid_config_is_completely_unaffected(monkeypatch) -> None:
    """The guard must not change anything on the happy path."""
    main = _reload_app(
        monkeypatch, LLM_PROVIDER=VALID_PROVIDER, LLM_FALLBACK_PROVIDER="gemini"
    )
    assert main.config_error is None

    client = TestClient(main.app)
    assert client.get("/health").json() == {"status": "ok"}
    # Real routes are mounted, so validation applies rather than a blanket 503.
    assert client.post("/translate", json={}).status_code == 422
