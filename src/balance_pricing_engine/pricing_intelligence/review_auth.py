"""Authentication for the quarantine review CLI.

Every mutating review command (approve, reject, void) must be authorised. Authorisation is
secret-based and fails closed:

* **Configured credentials** live in ``PRICING_INTELLIGENCE_REVIEW_CREDENTIALS`` as
  comma-separated ``identity=<sha256 hex of that reviewer's token>`` entries. Only digests are
  stored in configuration, so the variable is not itself a usable credential.
* **The presented token** comes from ``PRICING_INTELLIGENCE_REVIEW_TOKEN`` or, interactively,
  a no-echo prompt. There is deliberately no command-line option for it: argv is visible to
  other local users and lands in shell history.
* The identity recorded in the review ledger is the one **that token authenticates** -- never a
  free-text name an operator types, which would prove nothing.

Neither the token nor any digest is ever logged, echoed or placed in an exception message.
Generate a digest with ``printf %s "$TOKEN" | shasum -a 256`` (see docs/pricing_intelligence.md).
"""

from __future__ import annotations

from dataclasses import dataclass
import getpass
import hashlib
import hmac
import re
import sys
from typing import Mapping, Optional

CREDENTIALS_ENV_VAR = "PRICING_INTELLIGENCE_REVIEW_CREDENTIALS"
TOKEN_ENV_VAR = "PRICING_INTELLIGENCE_REVIEW_TOKEN"
#: Shorter tokens are refused: the digest only protects a token that cannot be guessed.
MIN_TOKEN_LENGTH = 16

_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,63}$")
_DIGEST = re.compile(r"^[0-9a-fA-F]{64}$")


class ReviewAuthError(Exception):
    """Authorisation failed. The message is safe to show: it never contains a secret."""


@dataclass(frozen=True)
class AuthenticatedReviewer:
    """The identity a presented token authenticated."""

    identity: str


def parse_credentials(raw: Optional[str]) -> dict[str, str]:
    """``identity -> lowercase sha256 hex`` from the configured value, or raise ReviewAuthError."""
    if not raw or not raw.strip():
        raise ReviewAuthError(
            f"review authorisation is not configured: set {CREDENTIALS_ENV_VAR} "
            "(identity=sha256-of-token, comma separated)"
        )
    credentials: dict[str, str] = {}
    for entry in raw.split(","):
        identity, separator, digest = entry.strip().partition("=")
        if not separator or not _IDENTITY.match(identity) or not _DIGEST.match(digest):
            # Never echo the entry: it may be a token someone pasted by mistake.
            raise ReviewAuthError(
                f"{CREDENTIALS_ENV_VAR} is malformed: expected identity=<64 hex sha256>, ..."
            )
        credentials[identity] = digest.lower()
    return credentials


def _presented_token(env: Mapping[str, str], *, prompt: bool) -> str:
    token = (env.get(TOKEN_ENV_VAR) or "").strip()
    if not token and prompt and sys.stdin is not None and sys.stdin.isatty():
        token = getpass.getpass("Review token: ").strip()
    if not token:
        raise ReviewAuthError(f"authorisation failed: no review token (set {TOKEN_ENV_VAR})")
    return token


def authenticate(env: Mapping[str, str], *, prompt: bool = False) -> AuthenticatedReviewer:
    """Authenticate the presented token against the configured credentials.

    Compares against *every* configured digest in constant time, so neither timing nor early
    exit reveals which identity (if any) was close.
    """
    credentials = parse_credentials(env.get(CREDENTIALS_ENV_VAR))
    token = _presented_token(env, prompt=prompt)
    if len(token) < MIN_TOKEN_LENGTH:
        raise ReviewAuthError("authorisation failed")
    presented = hashlib.sha256(token.encode("utf-8")).hexdigest()
    matched: Optional[str] = None
    for identity, digest in credentials.items():
        if hmac.compare_digest(presented, digest):
            matched = identity
    if matched is None:
        raise ReviewAuthError("authorisation failed")
    return AuthenticatedReviewer(identity=matched)
