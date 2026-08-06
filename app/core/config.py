"""
Runtime configuration.

Everything the backend needs to talk to Postgres and S3, plus the soft-delete
policy knobs, in one place. Values come from the environment (see .env.example).
"""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # ── App ──────────────────────────────────────────────────
    app_name: str = "Bimi Admin API"
    environment: str = Field(default="development")
    # The Next.js origin(s) allowed to call this API with cookies.
    # Accepts three forms so you never have to fight JSON-in-env-vars:
    #   CORS_ORIGINS=https://a.com                     (single)
    #   CORS_ORIGINS=https://a.com,https://b.com       (comma-separated)
    #   CORS_ORIGINS=["https://a.com","https://b.com"] (JSON array)
    # A bare host (no scheme) gets https:// prepended.
    cors_origins: list[str] = Field(default=["http://localhost:3000"])

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _parse_cors_origins(cls, v):
        import json

        if v is None or v == "":
            return ["http://localhost:3000"]
        if isinstance(v, str):
            s = v.strip()
            # JSON array form
            if s.startswith("["):
                try:
                    parsed = json.loads(s)
                    if isinstance(parsed, list):
                        v = parsed
                    else:
                        v = [s]
                except json.JSONDecodeError:
                    # Not valid JSON — treat the inside as a comma list, tolerating
                    # unquoted hosts like [bimi-admin-api.example.com]
                    v = [p.strip().strip('"').strip("'") for p in s.strip("[]").split(",")]
            else:
                # comma-separated or single value
                v = [p.strip() for p in s.split(",")]
        # Normalise: drop blanks, add scheme to bare hosts.
        out = []
        for item in v:
            item = str(item).strip().strip('"').strip("'")
            if not item:
                continue
            if "://" not in item:
                item = "https://" + item
            out.append(item)
        return out or ["http://localhost:3000"]

    # ── Database ─────────────────────────────────────────────
    # The SAME budgit_ai database the assistant reads from. The admin API only
    # ever touches the 35 fact/reference tables plus its own admin_* tables.
    database_url: str = Field(
        default="postgresql://budgit:budgit@localhost:5432/budgit_ai"
    )
    db_pool_min: int = 2
    db_pool_max: int = 10
    db_schema: str = "public"

    # ── Auth ─────────────────────────────────────────────────
    session_secret: str = Field(default="change-me-in-production")
    session_cookie_name: str = "bimi_admin_session"
    session_ttl_hours: int = 24
    # Cookie cross-site behaviour. Defaults adapt to ENVIRONMENT, but can be set
    # explicitly per deployment:
    #   • Same site (shared parent domain, e.g. app + api subdomains):
    #       COOKIE_SAMESITE=lax   COOKIE_DOMAIN=.yourdomain.com
    #   • Different sites (frontend and API on unrelated domains):
    #       COOKIE_SAMESITE=none  (forces Secure; requires HTTPS on both)
    # None = auto: 'lax' + Secure in production, 'lax' + insecure in development.
    cookie_samesite: str | None = Field(default=None)   # 'lax' | 'strict' | 'none'
    cookie_secure: bool | None = Field(default=None)    # None ⇒ derive from env / samesite
    cookie_domain: str | None = Field(default=None)     # e.g. '.kenechidukor.com'

    @property
    def resolved_samesite(self) -> str:
        return (self.cookie_samesite or "lax").lower()

    @property
    def resolved_secure(self) -> bool:
        # SameSite=None is invalid without Secure, so force it on.
        if self.resolved_samesite == "none":
            return True
        if self.cookie_secure is not None:
            return self.cookie_secure
        return self.environment != "development"
    # bcrypt work factor for password hashing.
    bcrypt_rounds: int = 12

    # ── S3 soft-delete recycle bin ───────────────────────────
    # Deleted rows are never DELETEd from Postgres blindly — they are exported
    # to CSV in this bucket first, indexed by a manifest, then removed. Restore
    # re-inserts from the CSV. See services/recycle_bin.py.
    s3_bucket: str = Field(default="bimi-admin-recycle-bin")
    s3_region: str = Field(default="eu-west-1")
    s3_prefix: str = Field(default="deletions")
    # Optional custom endpoint (MinIO / LocalStack in dev). None ⇒ real AWS.
    s3_endpoint_url: str | None = Field(default=None)
    aws_access_key_id: str | None = Field(default=None)
    aws_secret_access_key: str | None = Field(default=None)

    # How long a soft-deleted batch stays recoverable before the purge job may
    # remove its CSV. Surfaced to the UI as "purged permanently after 30 days".
    recycle_retention_days: int = 30


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
