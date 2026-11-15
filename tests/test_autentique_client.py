"""The Autentique client: the multipart GraphQL request, how answers read back,
which failures may have created the document, and that the key never leaks."""
import json

import httpx
import pytest

from core.config import settings
from core.integrations import autentique

KEY = "fake-key"


class Fake:
    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.status = 200
        self.body: object = {}
        self.error: Exception | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        if isinstance(self.body, (dict, list)):
            return httpx.Response(self.status, json=self.body)
        return httpx.Response(self.status, text=str(self.body))


@pytest.fixture
def fake(monkeypatch):
    f = Fake()
    client = httpx.AsyncClient(transport=httpx.MockTransport(f.handler))
    monkeypatch.setattr(autentique, "_http", lambda: client)
    monkeypatch.setattr(settings, "autentique_api_key", KEY)
    return f


SIGNERS = [autentique.Signer("Ana Lima", "ana@cliente.com", "52998224725"),
           autentique.Signer("Paulo", "paulo@converge.me")]
CREATED = {"data": {"createDocument": {"id": "doc1", "signatures": [
    {"public_id": "sig-ana", "email": "Ana@Cliente.com", "action": {"name": "Sign"}},
    {"public_id": "sig-paulo", "email": "paulo@converge.me", "action": {"name": "Sign"}},
    {"public_id": "sig-owner", "email": None}]}}}


def _parts(request: httpx.Request) -> dict[str, bytes]:
    boundary = request.headers["content-type"].split("boundary=")[1].encode()
    parts = {}
    for chunk in request.content.split(b"--" + boundary):
        if b'name="' not in chunk:
            continue
        head, _, body = chunk.partition(b"\r\n\r\n")
        name = head.split(b'name="')[1].split(b'"')[0].decode()
        parts[name] = body.removesuffix(b"\r\n")
    return parts


async def test_create_sends_the_file_and_signers_as_multipart(fake):
    fake.body = CREATED

    made = await autentique.create_document("Contrato Ana", b"%PDF-1", "contrato.pdf", SIGNERS,
                                            message="Contrato", sandbox=True)

    request = fake.requests[0]
    assert str(request.url) == autentique.URL
    assert request.headers["Authorization"] == f"Bearer {KEY}"
    assert request.headers["User-Agent"] == "converge-app"
    parts = _parts(request)
    operations = json.loads(parts["operations"])
    assert "createDocument(sandbox: true," in operations["query"]
    assert operations["variables"]["document"] == {"name": "Contrato Ana", "message": "Contrato"}
    assert operations["variables"]["signers"] == [
        {"name": "Ana Lima", "email": "ana@cliente.com", "action": "SIGN",
         "configs": {"cpf": "52998224725"}},
        {"name": "Paulo", "email": "paulo@converge.me", "action": "SIGN"}]
    assert operations["variables"]["file"] is None
    assert json.loads(parts["map"]) == {"file": ["variables.file"]}
    assert parts["file"] == b"%PDF-1"
    assert made == autentique.Created("doc1", {"ana@cliente.com": "sig-ana",
                                               "paulo@converge.me": "sig-paulo"})


async def test_production_is_not_sandbox(fake):
    fake.body = CREATED
    await autentique.create_document("C", b"%PDF", "c.pdf", SIGNERS, sandbox=False)
    assert "createDocument(sandbox: false," in json.loads(_parts(fake.requests[0])["operations"])["query"]


@pytest.mark.parametrize(("status", "body", "error", "uncertain"), [
    (200, {"errors": [{"message": "invalid_email", "extensions": {"category": "validation"}}]},
     None, False),
    (200, {"errors": [{"message": "internal_error"}]}, None, True),
    (200, {"data": {"createDocument": None},
           "errors": [{"message": "internal_error", "path": ["createDocument", "signatures"]}]},
     None, True),
    (401, {"message": "Unauthenticated."}, None, False),
    (422, "not json", None, False),
    (500, {}, None, True),
    (200, {"data": {"createDocument": None}}, None, True),
    (200, {}, httpx.ReadTimeout("slow"), True),
    (200, {}, httpx.ConnectError("refused"), False),
])
async def test_failures_say_whether_it_may_exist(fake, status, body, error, uncertain):
    fake.status, fake.body, fake.error = status, body, error

    with pytest.raises(autentique.AutentiqueError) as raised:
        await autentique.create_document("C", b"%PDF", "c.pdf", SIGNERS, sandbox=True)

    assert raised.value.uncertain is uncertain
    assert KEY not in str(raised.value) and "ana@cliente.com" not in str(raised.value)


async def test_error_codes_keep_only_codes(fake):
    fake.body = {"errors": [{"message": "invalid_email"},
                            {"message": "O e-mail ana@cliente.com é inválido"},
                            {"message": "ana@cliente.com"}, {"message": "52998224725"},
                            {"message": "Ana"}]}
    with pytest.raises(autentique.AutentiqueError) as raised:
        await autentique.create_document("C", b"%PDF", "c.pdf", SIGNERS, sandbox=True)
    assert raised.value.codes == ("invalid_email",)
    assert "ana@" not in str(raised.value) and "52998" not in str(raised.value)


async def test_created_with_an_error_in_the_answer_still_counts(fake):
    fake.body = {"data": {"createDocument": {"id": "doc1", "signatures": None}},
                 "errors": [{"message": "internal_error", "path": ["createDocument", "signatures"]}]}

    made = await autentique.create_document("C", b"%PDF", "c.pdf", SIGNERS, sandbox=True)

    assert made == autentique.Created("doc1", {})


async def test_signing_link(fake):
    fake.body = {"data": {"createLinkToSignature": {"short_link": "https://assina.ae/A9"}}}

    assert await autentique.signing_link("sig-ana") == "https://assina.ae/A9"
    assert 'createLinkToSignature(public_id: "sig-ana")' in json.loads(
        fake.requests[0].content)["query"]


async def test_signing_link_refuses_an_odd_id_without_calling(fake):
    with pytest.raises(autentique.AutentiqueError):
        await autentique.signing_link('x") { id } #')
    assert fake.requests == []


async def test_a_lost_link_answer_is_not_uncertain(fake):
    fake.error = httpx.ReadTimeout("slow")
    with pytest.raises(autentique.AutentiqueError) as raised:
        await autentique.signing_link("sig-ana")
    assert raised.value.uncertain is False


def test_configured_only_with_the_key(monkeypatch):
    monkeypatch.setattr(settings, "autentique_api_key", "")
    assert not autentique.configured()
    monkeypatch.setattr(settings, "autentique_api_key", KEY)
    assert autentique.configured()
