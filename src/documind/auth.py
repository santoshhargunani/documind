"""
auth.py  --  Stub authentication for DocuMind (multi-tenant).

PLACE AT: src/documind/auth.py

PURPOSE
-------
Issues and verifies small signed tokens (JWTs) that carry two facts:
    * who the user is        (claim "sub")
    * which tenant they are  (claim "tenant_id")

The tenant identity ALWAYS comes from a verified token, never from the
request body or query string, because anything a client can type, a client
can forge. Row-Level Security in Postgres then uses this tenant_id.

THIS IS A STUB: we sign tokens ourselves with a shared secret (HS256).
Later you replace verify_token() with validation of an Identity Platform /
OIDC token (check signature against Google's public keys). Everything that
CALLS get_tenant_context() stays unchanged -- that is the point of keeping
auth behind one function.
"""

# `os` -- read the signing secret from an environment variable so the secret is
# never hard-coded in source control.
import os

# `re` -- regular expressions, used to strictly validate tenant ids.
import re

# `time` -- current Unix time, used for token issue/expiry timestamps.
import time

# `dataclass` -- a tiny, immutable container for the verified identity.
from dataclasses import dataclass

# `jwt` is the PyJWT library (pip install "PyJWT>=2.8"). It creates and verifies
# JSON Web Tokens: signed, tamper-evident strings carrying claims.
import jwt

# FastAPI pieces, used only by the get_tenant_context dependency below:
#   Header       -- tells FastAPI to read a value from an HTTP request header.
#   HTTPException-- lets us return a clean 401 response instead of crashing.
from fastapi import Header, HTTPException

# Name of the environment variable that holds the signing secret.
# Locally you set it from Secret Manager (see mint_token.py instructions);
# on Cloud Run / GKE you inject it from Secret Manager at deploy time.
_SECRET_ENV_VAR = "DOCUMIND_JWT_SECRET"

# Signing algorithm. HS256 = HMAC-SHA256 with ONE shared secret. Fine for a stub
# where the same service signs and verifies. A real IdP uses asymmetric keys.
_ALGORITHM = "HS256"

# "iss" (issuer) claim. We reject tokens that were not issued by us, so a token
# minted by some other system cannot be replayed here.
_ISSUER = "documind-stub-auth"

# Tenant ids become GCS folder names AND database values, so allow only a
# conservative character set: lowercase letters, digits, hyphens, 2-63 chars,
# starting with a letter or digit. This blocks path tricks like "../other".
_TENANT_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")


class AuthError(Exception):
    """Raised when a token is missing, malformed, expired, or untrusted."""


@dataclass(frozen=True)
class TenantContext:
    """
    The verified identity of the caller.

    frozen=True makes it immutable, so no code later in the request can
    quietly change which tenant a request belongs to.
    """

    user_id: str    # who is calling (from the "sub" claim)
    tenant_id: str  # which tenant they belong to (from the "tenant_id" claim)


def is_valid_tenant_id(tenant_id: str) -> bool:
    """Return True if tenant_id matches our strict naming rule.

    Also reusable by the ingestion worker to validate the tenant segment it
    parses out of a GCS object path.
    """
    # fullmatch -> the WHOLE string must match the pattern, not just a prefix.
    return bool(_TENANT_ID_PATTERN.fullmatch(tenant_id))


def _get_secret() -> str:
    """Read the signing secret from the environment, failing loudly if unset."""
    # os.environ.get returns None when the variable does not exist.
    secret = os.environ.get(_SECRET_ENV_VAR)
    if not secret:
        # Failing closed: without a secret we must not accept or issue tokens.
        raise AuthError(f"{_SECRET_ENV_VAR} is not set; cannot sign/verify tokens")
    return secret


def create_token(user_id: str, tenant_id: str, ttl_seconds: int = 3600) -> str:
    """
    Mint a signed token for a user in a tenant.

    Args:
        user_id:     identifier of the user (any non-empty string for the stub).
        tenant_id:   the tenant the user belongs to; validated against our rule.
        ttl_seconds: how long the token stays valid (default 1 hour). Short
                     lifetimes limit the damage if a token leaks.
    Returns:
        The encoded JWT as a string, to send as "Authorization: Bearer <token>".
    """
    # Refuse to mint a token for a malformed tenant id.
    if not is_valid_tenant_id(tenant_id):
        raise AuthError(f"invalid tenant_id: {tenant_id!r}")
    if not user_id:
        raise AuthError("user_id must be non-empty")

    # Current time as an integer Unix timestamp (seconds since 1970).
    now = int(time.time())

    # The token body ("claims"). iat = issued at, exp = expires at.
    claims = {
        "iss": _ISSUER,
        "sub": user_id,
        "tenant_id": tenant_id,
        "iat": now,
        "exp": now + ttl_seconds,
    }

    # jwt.encode signs the claims with our secret and returns the token string.
    return jwt.encode(claims, _get_secret(), algorithm=_ALGORITHM)


def verify_token(token: str) -> TenantContext:
    """
    Validate a token and return the caller's verified identity.

    Checks signature, expiry, issuer, and that required claims exist and the
    tenant id is well-formed. Any failure raises AuthError (never returns
    partial or guessed data).
    """
    try:
        # jwt.decode verifies the signature and the "exp" automatically.
        #   algorithms=[...]  -- ONLY accept our algorithm. Never allow the
        #                        token to choose its own (blocks the classic
        #                        "alg: none" downgrade attack).
        #   issuer=...        -- must match the "iss" claim.
        #   options.require   -- these claims must be present, else it fails.
        claims = jwt.decode(
            token,
            _get_secret(),
            algorithms=[_ALGORITHM],
            issuer=_ISSUER,
            options={"require": ["exp", "iat", "sub", "tenant_id", "iss"]},
        )
    except jwt.PyJWTError as exc:
        # PyJWTError is the base class for expired, bad signature, malformed...
        # We re-raise a single AuthError so callers handle one exception type.
        raise AuthError(f"invalid token: {exc}") from exc

    tenant_id = claims["tenant_id"]
    # Defense in depth: even a correctly signed token must carry a sane tenant.
    if not isinstance(tenant_id, str) or not is_valid_tenant_id(tenant_id):
        raise AuthError("token carries an invalid tenant_id")

    return TenantContext(user_id=str(claims["sub"]), tenant_id=tenant_id)


def get_tenant_context(authorization: str = Header(default="")) -> TenantContext:
    """
    FastAPI dependency: turn the Authorization header into a TenantContext.

    Use it in a route like:
        @app.post("/ask")
        def ask(req: AskRequest, ctx: TenantContext = Depends(get_tenant_context)):
            ...  # ctx.tenant_id is trustworthy here

    FastAPI maps the `authorization` parameter to the "Authorization" header.
    """
    # Expected format: "Bearer <token>". partition splits on the first space.
    scheme, _, token = authorization.partition(" ")
    # Reject anything that is not exactly the Bearer scheme with a token.
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="missing bearer token")
    try:
        return verify_token(token)
    except AuthError as exc:
        # 401 = "not authenticated". We return the reason text but never the
        # secret or internals.
        raise HTTPException(status_code=401, detail=str(exc)) from exc
