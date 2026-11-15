"""Autentique API v2 (GraphQL): the contract sent to be signed.

https://docs.autentique.com.br/api/ . One lazy HTTP client, like asaas.py.
Every failure is an AutentiqueError that names the call and the status, and
says whether the document may have been created anyway (`uncertain`); nothing
here logs or raises the key, the file, or who signs.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

import httpx

from core.config import settings

logger = logging.getLogger(__name__)

URL = "https://api.autentique.com.br/v2/graphql"
USER_AGENT = "converge-app"

_CREATE = """mutation CreateDocument($document: DocumentInput!, $signers: [SignerInput!]!,
  $file: Upload!) {
  createDocument(sandbox: %s, document: $document, signers: $signers, file: $file) {
    id
    signatures { public_id email action { name } }
  }
}"""
# The id goes in the query itself: the docs never name its GraphQL type.
_LINK = """mutation SigningLink {
  createLinkToSignature(public_id: "%s") { short_link }
}"""
_PUBLIC_ID = re.compile(r"[A-Za-z0-9-]{1,64}")
_CODE = re.compile(r"[a-z]{1,30}(?:_[a-z]{1,30}){1,5}")

_client: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=30.0)
    return _client


class AutentiqueError(Exception):
    def __init__(self, call: str, status: int | None, *, uncertain: bool = False,
                 codes: tuple[str, ...] = ()):
        self.call, self.status, self.uncertain, self.codes = call, status, uncertain, codes
        super().__init__(f"Autentique {call} failed (status {status}, codes {list(codes)}"
                         f"{', maybe done' if uncertain else ''})")


@dataclass(frozen=True)
class Signer:
    name: str
    email: str
    cpf: str = ""


@dataclass(frozen=True)
class Created:
    document_id: str
    # The signature id of each signer, by e-mail in lower case.
    signatures: dict[str, str]


def configured() -> bool:
    return bool(settings.autentique_api_key)


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.autentique_api_key}", "User-Agent": USER_AGENT}


def _codes(errors: object) -> tuple[str, ...]:
    """Autentique's error messages are codes ("invalid_email"); anything else
    is left out, since it may quote what was sent (an e-mail, a CPF)."""
    found = []
    for error in errors if isinstance(errors, list) else []:
        message = error.get("message") if isinstance(error, dict) else None
        if isinstance(message, str) and _CODE.fullmatch(message):
            found.append(message)
    return tuple(found)


def _validation_only(errors: object) -> bool:
    if not isinstance(errors, list) or not errors:
        return False
    return all(isinstance(e, dict) and isinstance(e.get("extensions"), dict)
               and e["extensions"].get("category") in ("validation", "graphql")
               for e in errors)


async def _call(call: str, *, created_if_sent: bool, **request) -> dict:
    """The `data` of a GraphQL answer. A lost answer counts as uncertain only
    for a call that creates something."""
    try:
        response = await _http().post(URL, headers=_headers(), **request)
    except httpx.ConnectError:
        raise AutentiqueError(call, None) from None  # never reached Autentique
    except httpx.HTTPError:
        # Timed out or broke mid-way: it may have been done.
        raise AutentiqueError(call, None, uncertain=created_if_sent) from None
    status = response.status_code
    try:
        body = response.json()
    except ValueError:
        body = None
    if status >= 500:
        raise AutentiqueError(call, status, uncertain=created_if_sent)
    if not isinstance(body, dict):
        raise AutentiqueError(call, status, uncertain=created_if_sent and status < 400)
    data = body.get("data")
    if status >= 400 or body.get("errors"):
        made = (data or {}).get("createDocument") if isinstance(data, dict) else None
        if (status < 400 and isinstance(data, dict) and isinstance(made, dict)
                and isinstance(made.get("id"), str)):
            # Created, with an error in some field of the answer: it exists.
            logger.warning("Autentique %s answered with errors %s", call,
                           list(_codes(body.get("errors"))))
            return data
        # A refusal is sure on HTTP 4xx or when every error is a validation
        # one (nothing ran); any other error may have come after the insert.
        sure = status >= 400 or _validation_only(body.get("errors"))
        raise AutentiqueError(call, status, uncertain=created_if_sent and not sure,
                              codes=_codes(body.get("errors")))
    if not isinstance(data, dict):
        raise AutentiqueError(call, status, uncertain=created_if_sent)
    return data


async def create_document(name: str, pdf: bytes, filename: str, signers: list[Signer], *,
                          message: str = "", sandbox: bool) -> Created:
    """The document on Autentique, which e-mails each signer their link.
    Sandbox documents cost nothing, have no legal value and vanish in days."""
    people = []
    for s in signers:
        person: dict = {"name": s.name, "email": s.email, "action": "SIGN"}
        if s.cpf:
            person["configs"] = {"cpf": s.cpf}
        people.append(person)
    document: dict = {"name": name}
    if message:
        document["message"] = message
    operations = {"query": _CREATE % ("true" if sandbox else "false"),
                  "variables": {"document": document, "signers": people, "file": None}}
    data = await _call(
        "createDocument", created_if_sent=True,
        data={"operations": json.dumps(operations, ensure_ascii=False),
              "map": json.dumps({"file": ["variables.file"]})},
        files={"file": (filename, pdf, "application/pdf")})
    made = data.get("createDocument")
    if not isinstance(made, dict) or not isinstance(made.get("id"), str):
        raise AutentiqueError("createDocument", 200, uncertain=True)
    signatures = {}
    for sig in made.get("signatures") or []:
        if (isinstance(sig, dict) and isinstance(sig.get("email"), str)
                and isinstance(sig.get("public_id"), str)):
            signatures[sig["email"].lower()] = sig["public_id"]
    return Created(document_id=made["id"], signatures=signatures)


async def signing_link(public_id: str) -> str:
    """The short link that opens this signer's signature (only theirs)."""
    if not _PUBLIC_ID.fullmatch(public_id):
        raise AutentiqueError("createLinkToSignature", None)
    data = await _call("createLinkToSignature", created_if_sent=False,
                       json={"query": _LINK % public_id})
    link = (data.get("createLinkToSignature") or {}).get("short_link")
    if not isinstance(link, str) or not link.startswith("https://"):
        raise AutentiqueError("createLinkToSignature", 200)
    return link
