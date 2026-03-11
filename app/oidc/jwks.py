"""JWKS utilities for OIDC — converts PEM public key to JWK Set format."""

import hashlib
import json
import base64

from cryptography.hazmat.primitives.serialization import load_pem_public_key
from jwt.algorithms import RSAAlgorithm


def _compute_kid(jwk_dict: dict) -> str:
    """Compute RFC 7638 JWK Thumbprint as the key ID."""
    thumbprint_input = json.dumps(
        {"e": jwk_dict["e"], "kty": jwk_dict["kty"], "n": jwk_dict["n"]},
        separators=(",", ":"),
        sort_keys=True,
    )
    digest = hashlib.sha256(thumbprint_input.encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def get_jwks(public_key_pem: str) -> dict:
    """Convert an RSA PEM public key to a JWKS (JSON Web Key Set) dict."""
    key = load_pem_public_key(public_key_pem.encode())
    jwk_dict = json.loads(RSAAlgorithm.to_jwk(key))
    jwk_dict["kid"] = _compute_kid(jwk_dict)
    jwk_dict["use"] = "sig"
    jwk_dict["alg"] = "RS256"
    return {"keys": [jwk_dict]}


def get_kid(public_key_pem: str) -> str:
    """Get the key ID (kid) for the current RSA public key."""
    jwks = get_jwks(public_key_pem)
    return jwks["keys"][0]["kid"]
