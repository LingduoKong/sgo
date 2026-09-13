# Email allowlist access and test deployment security

Approved in conversation: Lightsail, application email OTP using SES, SQLite authentication state, server-managed allowlist, no self-registration. Follow-up: conservative test quotas and absolute 24-hour login expiry.

Only exact normalized allowlisted addresses may receive codes or access APIs. OTPs expire after 10 minutes, allow five attempts, and are atomically consumed once. Sessions expire 24 hours after creation without renewal. Removal revokes sessions and pending codes. SQLite stores keyed code digests and hashed high-entropy session tokens; secret material never enters logs.

All business routes require authentication. Evaluation resources are private to their owner. Unsafe requests and chargeable GET streams require same-origin custom headers. Fixed server LLM configuration eliminates user-controlled outbound URLs. Dataset management is admin-only and fixed-path. Request and workload limits protect a small test server. Production uses HTTPS, secure cookies, trusted Host, restricted proxy trust, non-root containers, persistent private state and data volumes.

Defaults: 10 personas (UI 5), 3 changes, 2 parallel model calls, 6,000 characters per string, 64 KiB request body, 20 chargeable requests/user/day, 6 heavy runs/user/day, 60 chargeable requests globally/day, one active chargeable request globally. Quotas count accepted attempts, not successful outputs; they are not a dollar guarantee. SES: 60-second resend wait, 5 messages/email/hour, 20/IP/hour, 50 globally/day; verification 20/IP/10 minutes in addition to 5/code.

Real SES delivery and public TLS require deployment-specific sender/domain, region, approved email list, and AWS credentials. Unit/integration tests use a fake delivery adapter, never real emails. No AWS resources are created in this change.
