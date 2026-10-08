"""Check classification against the real exception classes of popular SDKs.

Each test is skipped when that SDK is not installed.
"""

import pytest

from retryhop import is_transient, retry_after_of, status_code_of

URL = "https://api.example.com/v1/chat"


def _response(httpx, status, headers=None):
    return httpx.Response(status, headers=headers or {}, request=httpx.Request("POST", URL))


def test_openai_exceptions():
    openai = pytest.importorskip("openai")
    httpx = pytest.importorskip("httpx")

    rate = openai.RateLimitError("slow down", response=_response(
        httpx, 429, {"retry-after": "4"}), body=None)
    assert is_transient(rate) and retry_after_of(rate) == 4.0 and status_code_of(rate) == 429

    auth = openai.AuthenticationError("bad key", response=_response(httpx, 401), body=None)
    assert not is_transient(auth)

    bad = openai.BadRequestError("bad", response=_response(httpx, 400), body=None)
    assert not is_transient(bad)

    server = openai.InternalServerError("boom", response=_response(httpx, 500), body=None)
    assert is_transient(server)

    conn = openai.APIConnectionError(request=httpx.Request("POST", URL))
    assert is_transient(conn)
    timeout = openai.APITimeoutError(request=httpx.Request("POST", URL))
    assert is_transient(timeout)


def test_anthropic_exceptions():
    anthropic = pytest.importorskip("anthropic")
    httpx = pytest.importorskip("httpx")

    overloaded = anthropic.InternalServerError(
        "overloaded", response=_response(httpx, 529), body=None)
    assert is_transient(overloaded) and status_code_of(overloaded) == 529

    rate = anthropic.RateLimitError("rate", response=_response(
        httpx, 429, {"retry-after": "12"}), body=None)
    assert is_transient(rate) and retry_after_of(rate) == 12.0

    perm = anthropic.PermissionDeniedError("no", response=_response(httpx, 403), body=None)
    assert not is_transient(perm)

    conn = anthropic.APIConnectionError(request=httpx.Request("POST", URL))
    assert is_transient(conn)


def test_httpx_exceptions():
    httpx = pytest.importorskip("httpx")
    req = httpx.Request("GET", URL)
    assert is_transient(httpx.ConnectTimeout("t", request=req))
    assert is_transient(httpx.ReadTimeout("t", request=req))
    assert is_transient(httpx.ConnectError("c", request=req))
    err = httpx.HTTPStatusError("x", request=req, response=_response(httpx, 503, {"Retry-After": "2"}))
    assert is_transient(err) and retry_after_of(err) == 2.0
    err404 = httpx.HTTPStatusError("x", request=req, response=_response(httpx, 404))
    assert not is_transient(err404)


def test_requests_exceptions():
    requests = pytest.importorskip("requests")
    assert is_transient(requests.ConnectionError("down"))
    assert is_transient(requests.Timeout("slow"))
    resp = requests.Response()
    resp.status_code = 429
    resp.headers["Retry-After"] = "5"
    err = requests.HTTPError(response=resp)
    assert is_transient(err) and retry_after_of(err) == 5.0
    resp400 = requests.Response()
    resp400.status_code = 400
    assert not is_transient(requests.HTTPError(response=resp400))
