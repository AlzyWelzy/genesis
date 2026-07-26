"""Generate the Ed25519 key pair used to sign JWTs.

Why this file exists
--------------------
Every developer and every environment needs its own signing key pair, and the
one thing that must never happen is a shared key committed to the repository —
anyone with the private key can mint a valid token for any user.

This script makes generating a fresh pair a single command, so there is no
excuse to copy one between machines.

Usage::

    uv run python scripts/generate_keys.py
    uv run python scripts/generate_keys.py --out-dir keys --force

The private key is written with mode 0600. Point ``JWT__PRIVATE_KEY_PATH`` and
``JWT__PUBLIC_KEY_PATH`` at the results.

Production keys must come from a secret manager, not from this script run on a
laptop.
"""

import argparse
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519


def generate_key_pair(out_dir: Path, *, force: bool = False) -> tuple[Path, Path]:
    """Write a new Ed25519 key pair to ``out_dir``.

    Args:
        out_dir: Directory to write ``private.pem`` and ``public.pem`` into.
        force: Overwrite existing files. Refusing by default is deliberate —
            silently replacing a signing key invalidates every issued token.

    Returns:
        The ``(private_path, public_path)`` pair.

    Raises:
        FileExistsError: When a key already exists and ``force`` is False.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    private_path = out_dir / "private.pem"
    public_path = out_dir / "public.pem"

    if not force and (private_path.exists() or public_path.exists()):
        raise FileExistsError(
            f"Keys already exist in {out_dir}. Pass --force to overwrite "
            "(this invalidates every token signed with the old key)."
        )

    private_key = ed25519.Ed25519PrivateKey.generate()

    private_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    # Readable only by the owner: a world-readable signing key on a shared host
    # is equivalent to no authentication at all.
    private_path.chmod(0o600)

    public_path.write_bytes(
        private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )

    return private_path, public_path


def main() -> None:
    """Parse arguments and generate the key pair."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("keys"),
        help="Directory to write the key pair into (default: keys).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing key pair.",
    )
    args = parser.parse_args()

    private_path, public_path = generate_key_pair(args.out_dir, force=args.force)
    print(f"private key: {private_path} (mode 0600 — never commit this)")
    print(f"public key:  {public_path}")


if __name__ == "__main__":
    main()
