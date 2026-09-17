# secrets/

Local-only credential material. Everything here is git-ignored except this
file and `.gitignore`; nothing in this directory is ever committed, and the
Dockerfile does not copy it into the image.

## Snowflake key-pair auth

Put the **private** key here as `snowflake_key_2.p8` (whatever name you use, keep
`.env` and the compose mount pointing at it):

```bash
cp ~/path/to/your_key_pkcs8.pem secrets/snowflake_key.p8
chmod 600 secrets/snowflake_key.p8
```

It must be unencrypted. PKCS#8 (`-----BEGIN PRIVATE KEY-----`) is what Snowflake
documents and what other clients (SnowSQL, the CLI, JDBC) require; this app also
accepts PKCS#1 (`-----BEGIN RSA PRIVATE KEY-----`) because the loader normalises it.
An encrypted key (`-----BEGIN ENCRYPTED PRIVATE KEY-----`) fails at connect
time, because the loader in `app/tools/snowflake_sql.py` passes
`password=None`. Decrypt one with:

```bash
openssl pkcs8 -in encrypted.p8 -out secrets/snowflake_key.p8 -nocrypt
```

Only the private key belongs here. The public half is registered on the
Snowflake user with `ALTER USER ... SET RSA_PUBLIC_KEY='...'` and does not
need to be on disk.

## How it is referenced

| Context | Path |
|---|---|
| Local (`uvicorn`) | `SNOWFLAKE_PRIVATE_KEY_PATH` in `.env` points here |
| Docker Compose | mounted read-only at `/run/secrets/snowflake_key.p8` |
| ECS / production | do **not** ship this file -- use Secrets Manager (see `infra/`) |

Verify it works:

```bash
python scripts/check_snowflake.py
```

## Rotation

Snowflake holds two public keys at once, so rotation needs no downtime:
set `RSA_PUBLIC_KEY_2` to the new key, swap the file here, then clear
`RSA_PUBLIC_KEY`.
