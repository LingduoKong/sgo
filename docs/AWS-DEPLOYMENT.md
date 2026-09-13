# Private AWS deployment

Run one Linux host with Docker Compose. Domain names, email addresses, account IDs and IPs in examples must be replaced with your own. Never commit the resulting instance configuration.

## Configuration

Copy `.env.example` to `.env`, restrict permissions to 600, and configure:

- `SGO_DOMAIN`: the site's hostname. Compose derives the HTTPS origin from this value.
- `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL_NAME`, `LLM_FAST_MODEL`: server-only model configuration.
- `AUTH_SECRET`: at least 32 random characters; generate with Python's `secrets.token_urlsafe(48)`.
- `AWS_REGION`, `SES_FROM_EMAIL`, and optional `SES_REPLY_TO_EMAIL`.
- The standard AWS credentials for a least-privilege SES sender. Do not use account administrator credentials in the application.

The private authentication database and persona datasets live in the `sgo_app_data` volume. Authentication is always enabled and fails closed when configuration is missing.

## SES

Verify the sending domain in SES and publish its DKIM records with your DNS provider. DKIM CNAME records must be DNS-only. Configure and validate SPF/DMARC as appropriate to your domain's mail configuration.

Sandbox accounts can send only to verified recipients or the AWS mailbox simulator. Each allowed recipient must complete AWS verification while the account remains in the sandbox. After production access is approved, recipient verification is no longer required; the application's allowlist remains mandatory.

Use `deploy/ses-policy.example.json` as a template. Replace all placeholders, restrict the From address and server source IP, and include the relevant SES identities. In sandbox testing SES may evaluate verified recipient identity ARNs as well as the sender's identity. Do not replace the policy with unrestricted administrator access to work around an authorization failure.

[SES sandbox documentation](https://docs.aws.amazon.com/ses/latest/dg/request-production-access.html)

## Start and authorize users

Point the desired hostname at the host's static address. Open 80/443; restrict SSH to management sources. Do not expose the application's port directly.

Run `docker compose up -d --build` from the project directory. Caddy obtains and renews the HTTPS certificate.

Initialize users explicitly:

```sh
SGO_ADMIN_EMAIL=admin@example.com SGO_MEMBER_EMAIL=member@example.com sh deploy/bootstrap-access.sh
```

The script is for explicit initialization, not every release. Running it again reauthorizes its specified users. Manage access afterward with:

```sh
docker compose exec app python -m web.auth allow someone@example.com
docker compose exec app python -m web.auth allow admin@example.com --admin
docker compose exec app python -m web.auth revoke someone@example.com
docker compose exec app python -m web.auth list
```

Administrators can load datasets but do not gain access to other users' reviews.

## Limits and security

- Login expires 24 hours after issuance without sliding renewal. Logout or allowlist removal revokes subsequent access.
- Login codes expire after 10 minutes and are consumed once, with at most 5 failed guesses. Resend cooldown is 60 seconds. Failed delivery refunds email delivery quotas while retaining request/IP limits.
- Email limits: 5 per address/hour, 20 requests per source/hour, 50 deliveries globally/day. The response includes retry timing when applicable.
- Panels default to 5 and support up to 50 people, with 2 concurrent model calls. Generated profiles are produced in batches of at most 2 to fit the output budget.
- Per-user rolling 24-hour limits: 20 chargeable endpoints, 6 heavy analysis runs, 100 model calls. Global limits: 60 chargeable endpoints and 300 model calls. A second 50-person review can exhaust the model quota because preparation also consumes calls.
- Model output is capped at 2,048 tokens and automatic SDK retries are disabled. Provider spend controls should also be configured; application call counts are not dollar guarantees.
- Requests are limited to 64 KiB and individual input strings to 6,000 characters. Server-generated panels are saved directly to the owner's review so large profiles do not need re-uploading.
- Only one instance and one worker are supported. Review results and concurrency coordination are in memory. Download important reports; application restarts do not preserve review results. Authentication and quotas are persistent.
- The application runs as a non-root container with a read-only root filesystem, resource limits and dropped capabilities. Only Caddy is exposed publicly.
- Compose trusts forwarded client addresses only from its fixed private Caddy address. If that network conflicts with the host, update both the network and trusted-proxy address together; never use a wildcard trust setting.
- Dataset demographic sampling does not guarantee professional-role matching. Unsupported audiences may require purpose-built data or explicitly selected generated personas.

## Releases, backups and key rotation

Follow [release consistency checks](../deploy/README.md). Publish committed code only and record the same Git revision in the deployed image.

Back up the private data volume and environment configuration with restricted, encrypted storage. Stop the application or use SQLite's online backup API when backing up its database. After restoring an older authentication database, rotate `AUTH_SECRET` to invalidate old sessions and review the allowlist.

After changing a model or SES key in `.env`, recreate the application with `docker compose up -d --force-recreate app`; a simple restart does not reload Compose environment settings. Verify the replacement before revoking an old key used elsewhere.

Do not run `docker compose down -v` during routine releases because it deletes persistent volumes. Verify provider credit eligibility and expiration separately from deployment.

## Validation

Run the Python test suite and the three JavaScript runtime checks in `tests/`. Also verify HTTPS, unauthenticated API rejection, authorized email login, owner isolation, review creation and report download in the target environment. Keep live test inputs and evaluation reports out of the public repository.
