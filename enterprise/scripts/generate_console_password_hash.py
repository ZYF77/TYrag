"""Generate a Gateway WebUI scrypt password hash without echoing the password."""

from __future__ import annotations

from getpass import getpass

from enterprise.gateway.auth.console_session import hash_console_password


def main() -> None:
    password = getpass("Console password: ")
    confirmation = getpass("Confirm password: ")
    if not password or password != confirmation:
        raise SystemExit("Passwords do not match or are empty")
    print(hash_console_password(password))


if __name__ == "__main__":
    main()
