"""
mint_token.py  --  Command-line helper to create stub JWTs for test tenants.

PLACE AT: scripts/mint_token.py

USAGE (PowerShell, from C:\\Source\\documind with the venv active):

  # 1) One-time: install the JWT library
  pip install "PyJWT>=2.8"

  # 2) One-time: create the signing secret in Secret Manager.
  #    We write it to a file with Python (no BOM / no stray newline, which
  #    PowerShell piping can introduce), upload it, then delete the file.
  python -c "import secrets; open('jwt_secret.txt','w').write(secrets.token_urlsafe(48))"
  gcloud secrets create documind-jwt-secret --data-file=jwt_secret.txt
  Remove-Item jwt_secret.txt

  # 3) Each terminal session: load the secret into the environment variable
  $env:DOCUMIND_JWT_SECRET = gcloud secrets versions access latest --secret=documind-jwt-secret

  # 4) Mint a token
  python scripts/mint_token.py --tenant acme --user alice
  python scripts/mint_token.py --tenant globex --user bob --ttl 7200
"""

# argparse -- standard library tool for reading command-line options.
import argparse

# sys -- used to adjust the import path and to exit with an error code.
import sys

# pathlib.Path -- convenient, OS-independent file path handling.
from pathlib import Path

# Make "src" importable so `from documind.auth import ...` works when this
# script is run directly (python scripts/mint_token.py) without installing the
# package. Path(__file__) = this file; .parent.parent = the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# Import our own auth helpers. AuthError is raised for bad input / missing secret.
from documind.auth import AuthError, create_token  # noqa: E402  (import after sys.path edit)


def main() -> int:
    """Parse arguments, mint a token, print it. Returns a process exit code."""
    # Build the argument parser and describe each option.
    parser = argparse.ArgumentParser(description="Mint a stub DocuMind JWT.")
    parser.add_argument("--tenant", required=True, help="tenant id, e.g. acme")
    parser.add_argument("--user", required=True, help="user id, e.g. alice")
    parser.add_argument(
        "--ttl", type=int, default=3600, help="lifetime in seconds (default 3600)"
    )
    # Reads sys.argv and validates; exits with a usage message on bad input.
    args = parser.parse_args()

    try:
        # Create and print ONLY the token so you can copy/paste or capture it.
        print(create_token(args.user, args.tenant, args.ttl))
        return 0
    except AuthError as exc:
        # Print the error to stderr and return a non-zero exit code.
        print(f"error: {exc}", file=sys.stderr)
        return 1


# Standard Python entry-point guard: run main() only when executed directly.
if __name__ == "__main__":
    sys.exit(main())
