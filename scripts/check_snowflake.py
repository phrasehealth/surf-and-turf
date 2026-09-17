#!/usr/bin/env python
"""Connectivity check for the SNOWFLAKE_* settings in .env.

Run this after filling in credentials to find out whether the agent will be
able to query anything, and if not, which part is wrong:

    python scripts/check_snowflake.py

It goes through the same code path the agent uses (`SnowflakeBackend` in
app/tools/snowflake_sql.py), so a pass here means run_sql / list_tables /
describe_table work.  Nothing is written to Snowflake, and no secret is
printed -- only the public fingerprint of the private key, which is what
`DESC USER` shows as RSA_PUBLIC_KEY_FP.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

OK, BAD, WARN = "\033[32m✓\033[0m", "\033[31m✗\033[0m", "\033[33m!\033[0m"


def load_env_file(path: Path) -> int:
    """Minimal KEY=value loader, so the script works without python-dotenv.

    Variables already present in the environment win, which lets you override
    a single field inline: SNOWFLAKE_ROLE=OTHER python scripts/check_snowflake.py
    """
    if not path.exists():
        return 0
    n = 0
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.split("  #")[0].strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value
            n += 1
    return n


def fingerprint(key_path: Path) -> tuple[str | None, str]:
    """Return (SHA256 fingerprint, diagnostic) for a private key file."""
    from cryptography.hazmat.primitives import serialization

    header = key_path.read_text(errors="replace").splitlines()[0].strip()
    if "ENCRYPTED" in header:
        return None, (
            "key is passphrase-encrypted; the loader in snowflake_sql.py passes "
            "password=None. Decrypt it: openssl pkcs8 -in KEY -out KEY.plain -nocrypt"
        )
    with key_path.open("rb") as f:
        key = serialization.load_pem_private_key(f.read(), password=None)
    der = key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    digest = base64.b64encode(hashlib.sha256(der).digest()).decode()
    return f"SHA256:{digest}", header


def preflight(settings) -> bool:
    """Checks that need no network. Returns False if connecting is pointless."""
    print("config")
    for label, value in [
        ("account", settings.snowflake_account),
        ("user", settings.snowflake_user),
        ("role", settings.snowflake_role),
        ("warehouse", settings.snowflake_warehouse),
        ("database", settings.snowflake_database),
        ("schema", settings.snowflake_schema),
    ]:
        mark = OK if value else WARN
        print(f"  {mark} {label:<10} {value or '<empty>'}")

    ok = True
    if not settings.snowflake_account or not settings.snowflake_user:
        print(f"  {BAD} SNOWFLAKE_ACCOUNT and SNOWFLAKE_USER are both required")
        ok = False

    print("auth")
    key_path, password = settings.snowflake_private_key_path, settings.snowflake_password
    if key_path and password:
        print(f"  {WARN} both key path and password set; the key wins "
              f"(snowflake_sql.py prefers SNOWFLAKE_PRIVATE_KEY_PATH)")
    if key_path:
        path = Path(key_path).expanduser()
        if not path.exists():
            print(f"  {BAD} key-pair    {path} does not exist")
            return False
        try:
            fp, note = fingerprint(path)
        except Exception as exc:  # unreadable, wrong format, encrypted
            print(f"  {BAD} key-pair    cannot load {path}: {exc}")
            return False
        if fp is None:
            print(f"  {BAD} key-pair    {note}")
            return False
        print(f"  {OK} key-pair    {path}")
        print(f"              {note}")
        print(f"              {fp}")
        print(f"              ^ must match RSA_PUBLIC_KEY_FP from: "
              f"DESC USER {settings.snowflake_user or '<user>'};")
    elif password:
        print(f"  {OK} password    <set, {len(password)} chars>")
        print(f"  {WARN} password auth is blocked for service users under current "
              f"Snowflake MFA policy; key-pair is the supported path")
    else:
        print(f"  {BAD} neither SNOWFLAKE_PRIVATE_KEY_PATH nor SNOWFLAKE_PASSWORD is set")
        ok = False
    return ok


HINTS = [
    ("could not connect", "check SNOWFLAKE_ACCOUNT: Snowsight bottom-left menu -> "
                          "hover account -> Copy account identifier (ORG-ACCOUNT)"),
    ("jwt token is invalid", "the private key does not match the public key on the user. "
                             "Compare the fingerprint above with DESC USER."),
    ("incorrect username or password", "wrong SNOWFLAKE_USER, or the user has no password "
                                       "(expected for TYPE = SERVICE users -- use key-pair)"),
    ("does not exist or not authorized", "the role/warehouse/database/schema is missing or "
                                         "not granted to SNOWFLAKE_ROLE"),
    ("no active warehouse", "SNOWFLAKE_WAREHOUSE is unset, or the role lacks USAGE on it"),
]


async def probe(settings) -> bool:
    from app.tools.snowflake_sql import SnowflakeBackend

    backend = SnowflakeBackend()
    print("connection")
    try:
        rows = await backend.run(
            "SELECT CURRENT_ACCOUNT() AS ACCOUNT, CURRENT_USER() AS USER, "
            "CURRENT_ROLE() AS ROLE, CURRENT_WAREHOUSE() AS WAREHOUSE, "
            "CURRENT_DATABASE() AS DATABASE, CURRENT_SCHEMA() AS SCHEMA, "
            "CURRENT_VERSION() AS VERSION"
        )
    except Exception as exc:
        message = str(exc)
        print(f"  {BAD} connect failed\n      {message.strip().splitlines()[0]}")
        for needle, hint in HINTS:
            if needle in message.lower():
                print(f"      hint: {hint}")
        return False

    session = rows[0]
    print(f"  {OK} authenticated")
    for key in ("ACCOUNT", "USER", "ROLE", "WAREHOUSE", "DATABASE", "SCHEMA", "VERSION"):
        value = session.get(key)
        mark = WARN if value is None and key in {"WAREHOUSE", "DATABASE", "SCHEMA"} else " "
        print(f"    {mark} {key.lower():<10} {value if value is not None else '<none>'}")
    requested, actual = (settings.snowflake_role or "").upper(), (session.get("ROLE") or "")
    if requested and actual.upper() != requested:
        print(f"  {WARN} session role is {actual or '<none>'}, not {settings.snowflake_role}: "
              f"the role may not be granted to this user")
    if actual.upper() == "PUBLIC":
        print(f"  {WARN} PUBLIC is the catch-all role, not a read-only reporting role; "
              f"its grants are whatever every user in the account has")

    print("grants")
    try:
        tables = await backend.list_tables(None)
    except Exception as exc:
        print(f"  {BAD} list_tables failed\n      {str(exc).strip().splitlines()[0]}")
        return False
    if not tables:
        print(f"  {WARN} the role can log in but sees no tables in "
              f"{settings.snowflake_database or '<db>'}: check the SELECT grants")
        return False
    print(f"  {OK} list_tables  {len(tables)} object(s) visible, first few:")
    for t in tables[:5]:
        print(f"      {t['database']}.{t['schema']}.{t['name']}  ({t['kind']})")

    first = f"{tables[0]['database']}.{tables[0]['schema']}.{tables[0]['name']}"
    try:
        cols = await backend.describe(first)
        print(f"  {OK} describe_table  {first}: {len(cols)} column(s)")
        sample = await backend.run(f"SELECT * FROM {first} LIMIT 1")
        print(f"  {OK} run_sql  SELECT from {first} returned {len(sample)} row(s)")
    except Exception as exc:
        print(f"  {BAD} reading {first} failed\n      {str(exc).strip().splitlines()[0]}")
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env-file", default=str(ROOT / ".env"),
                       help="path to the .env to load (default: ./.env)")
    parser.add_argument("--no-env-file", action="store_true",
                       help="use the ambient environment only")
    parser.add_argument("--sql", help="run this query instead of the default probe")
    args = parser.parse_args()

    if not args.no_env_file:
        n = load_env_file(Path(args.env_file))
        print(f"loaded {n} variable(s) from {args.env_file}\n")

    sys.path.insert(0, str(ROOT))
    from app.config import settings  # imported after .env, defaults read os.getenv at import

    if settings.snowflake_mode != "real":
        print(f"{WARN} SNOWFLAKE_MODE={settings.snowflake_mode!r}: the app itself will serve "
              f"mock data.\n  Checking the real connection anyway.\n")

    if not preflight(settings):
        print(f"\n{BAD} fix the above before connecting")
        return 1
    print()

    if args.sql:
        from app.tools.snowflake_sql import SnowflakeBackend

        async def run_one():
            rows = await SnowflakeBackend().run(args.sql)
            for row in rows[:20]:
                print(f"  {row}")
            print(f"  ({len(rows)} row(s))")
        try:
            asyncio.run(run_one())
        except Exception as exc:
            print(f"  {BAD} {str(exc).strip().splitlines()[0]}")
            return 1
        return 0

    ok = asyncio.run(probe(settings))
    print()
    if ok:
        print(f"{OK} credentials work: the agent can query Snowflake")
        if settings.snowflake_mode != "real":
            print(f"  set SNOWFLAKE_MODE=real in .env to actually use them")
        return 0
    print(f"{BAD} not usable yet -- see the failure above")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
