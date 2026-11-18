"""opencode web proxy: SSO token required, directory limit, password hidden."""
import asyncio

import httpx
from starlette.testclient import TestClient

from backend.workbench import oc_proxy as p

HOST = "opencode.example.test"


def test_directory_allowed():
    slugs = {"alpha"}
    assert p.directory_allowed("", slugs)
    assert p.directory_allowed("/projects/alpha", slugs)
    assert p.directory_allowed("/projects/alpha/src", slugs)
    assert p.directory_allowed("%2Fprojects%2Falpha", slugs)
    for bad in ("/projects/beta", "/projects/alpha/../beta", "/workspaces/x", "/", "projects/alpha",
                "/projects", "/projects/alpha\x00", "/etc"):
        assert not p.directory_allowed(bad, slugs), bad


def test_requested_directories_sources():
    got = p.requested_directories(
        [("directory", "/a"), ("x", "y")], {"x-opencode-directory": "/b"}, b'{"directory": "/c"}'
    )
    assert got == ["/a", "/b", "/c"]


def _app(seen):
    async def verify(tok):
        return {"sub": "owner"} if tok == "good" else None

    async def slugs():
        return {"alpha"}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        async def body():
            yield b"ok"

        return httpx.Response(200, content=body(), headers={"set-cookie": "x=1"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return p.create_oc_app(host=HOST, upstream="http://oc:4096", password="s3cret-pw",
                           verify_token=verify, allowed_slugs=slugs, http_client=client)


def test_requires_valid_token():
    seen = []
    c = TestClient(_app(seen))
    assert c.get("/").status_code == 401
    assert c.get("/", headers={"Authorization": "Bearer bad"}).status_code == 401
    assert seen == []


def test_forwards_with_basic_and_hides_credentials():
    seen = []
    c = TestClient(_app(seen))
    r = c.get("/session?directory=/projects/alpha",
              headers={"Authorization": "Bearer good", "Cookie": "__Secure-x=1"})
    assert r.status_code == 200 and "set-cookie" not in r.headers
    up = seen[0]
    assert up.headers["authorization"] == p.basic_auth("s3cret-pw")
    assert "cookie" not in up.headers
    assert "s3cret-pw" not in r.text


def test_refuses_other_directories():
    seen = []
    c = TestClient(_app(seen))
    h = {"Authorization": "Bearer good"}
    assert c.get("/session?directory=/workspaces/x", headers=h).status_code == 403
    other = {**h, "x-opencode-directory": "/projects/beta"}
    assert c.get("/session", headers=other).status_code == 403
    assert c.post("/project", headers=h, json={"directory": "/etc"}).status_code == 403
    assert seen == []


def test_host_dispatch():
    async def main_app(scope, receive, send):
        await httpx.Response(200)  # pragma: no cover

    calls = []

    async def other(scope, receive, send):
        calls.append(scope["path"])
        from starlette.responses import Response
        await Response("other")(scope, receive, send)

    d = p.HostDispatch(main_app, HOST, lambda: other)
    c = TestClient(d, base_url=f"http://{HOST}")
    assert c.get("/x").text == "other" and calls == ["/x"]
    asyncio.run(asyncio.sleep(0))


def test_keycloak_id_token_is_accepted(monkeypatch):
    """A verified Keycloak ID token (typ "ID", no NerveCenter typ) is a person."""
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = int(__import__("time").time())
    tok = jwt.encode({"iss": "https://sso.test/realms/r", "aud": "nervecenter", "exp": now + 60,
                      "sub": "u1", "typ": "ID", "email": "o@x"}, key, algorithm="RS256")

    class _Key:
        def __init__(self):
            self.key = key.public_key()

    class _Jwks:
        def __init__(self, *a, **k):
            pass

        def get_signing_key_from_jwt(self, _t):
            return _Key()

    monkeypatch.setattr(jwt, "PyJWKClient", _Jwks)
    verify = p.make_keycloak_verifier("https://sso.test/realms/r", "nervecenter")
    assert asyncio.run(verify(tok)) is not None
    assert asyncio.run(verify(tok + "x")) is None
    other_aud = jwt.encode({"iss": "https://sso.test/realms/r", "aud": "other", "exp": now + 60,
                            "sub": "u1"}, key, algorithm="RS256")
    assert asyncio.run(verify(other_aud)) is None
