"""Generate RS256 key pair for JWT signing/verification."""

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from pathlib import Path


def generate_keys(output_dir: str = "keys") -> None:
    output = Path(output_dir)
    output.mkdir(exist_ok=True)

    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )

    # Write private key
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    (output / "private.pem").write_bytes(private_pem)

    # Write public key
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    (output / "public.pem").write_bytes(public_pem)

    print(f"Keys generated in {output.resolve()}/")
    print(f"  - private.pem (keep secret, used by Auth Center)")
    print(f"  - public.pem  (distribute to AI Apps)")


if __name__ == "__main__":
    generate_keys()
