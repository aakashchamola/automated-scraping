"""
web/setup_auth.py — Set the dashboard's username and password.

Writes a hash to .dashboard-auth.json (gitignored, mode 600). The password
itself is never stored and never reaches the repository, which is public.

    python -m web.setup_auth                    # prompts, input hidden
    python -m web.setup_auth --username bob     # prompts for the password only
"""

import argparse
import getpass
import os
import sys

from web import auth

MIN_LENGTH = 8


def main() -> None:
    ap = argparse.ArgumentParser(description="Set the dashboard login")
    ap.add_argument("--username")
    args = ap.parse_args()

    username = args.username or input("Username: ").strip()
    if not username:
        sys.exit("username cannot be empty")

    # Read from the terminal, never from argv — a password on the command line
    # lands in shell history and in `ps`.
    if not sys.stdin.isatty():
        password = sys.stdin.readline().rstrip("\n")
    else:
        password = getpass.getpass("Password: ")
        if password != getpass.getpass("Repeat password: "):
            sys.exit("passwords do not match")

    if len(password) < MIN_LENGTH:
        sys.exit(f"password must be at least {MIN_LENGTH} characters")

    path = auth.save_credentials(username, password)
    print(f"saved {os.path.relpath(path, auth.PROJECT_ROOT)} (mode 600, gitignored)")
    print(f"username: {username}")
    print("Start the dashboard with ./start_dashboard.sh and sign in.")


if __name__ == "__main__":
    main()
