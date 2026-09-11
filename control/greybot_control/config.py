"""Runtime configuration. Never load server configuration from the checkout."""

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


def secret(name):
    value = os.environ.get(name, "")
    parameter = os.environ.get(name + "_SSM", "")
    if value and parameter:
        raise ValueError(f"Set only one source for {name}")
    if parameter:
        import boto3
        session = boto3.Session(profile_name=os.environ.get("AWS_PROFILE") or None)
        value = session.client("ssm").get_parameter(
            Name=parameter, WithDecryption=True)["Parameter"]["Value"]
    return value


@dataclass(frozen=True)
class Config:
    guild_id: str
    client_id: str
    bot_token: str
    client_secret: str
    origin: str
    state_dir: Path
    archive_bucket: str = ""
    retention_days: int = 0
    capture_content: bool = False
    enforce: bool = False
    archive_dir: Path | None = None
    start_channel_id: str = ""
    public_channel_ids: tuple[str, ...] = ()

    @property
    def secure(self):
        return self.origin.startswith("https://")

    @property
    def callback(self):
        return self.origin + "/auth/callback"

    @classmethod
    def from_env(cls):
        origin = os.environ.get("GREYBOT_ORIGIN", "http://127.0.0.1:8080").rstrip("/")
        parsed = urlsplit(origin)
        if (parsed.path or parsed.query or parsed.fragment or parsed.username
                or parsed.scheme not in {"http", "https"} or not parsed.hostname):
            raise ValueError("GREYBOT_ORIGIN must be an origin without a path")
        if parsed.scheme != "https" and parsed.hostname not in {"127.0.0.1", "localhost"}:
            raise ValueError("Non-loopback deployments require HTTPS")
        state = Path(os.environ.get("GREYBOT_STATE_DIR", "~/.local/share/greybot-control")).expanduser().resolve()
        checkout = Path(__file__).resolve().parents[2]
        if state == checkout or checkout in state.parents:
            raise ValueError("Runtime data must be outside the repository")
        guild = os.environ["GREYBOT_GUILD_ID"]
        client = os.environ["GREYBOT_CLIENT_ID"]
        if not guild.isdecimal() or not client.isdecimal():
            raise ValueError("Guild and application IDs must be numeric")
        days = int(os.environ.get("GREYBOT_RETENTION_DAYS", "0"))
        enforce = os.environ.get("GREYBOT_ENFORCE") == "1"
        bucket = os.environ.get("GREYBOT_ARCHIVE_BUCKET", "")
        local = os.environ.get("GREYBOT_ARCHIVE_DIR", "")
        archive_dir = Path(local).expanduser().resolve() if local else None
        if archive_dir and (archive_dir == checkout or checkout in archive_dir.parents):
            raise ValueError("Archive must be outside the repository")
        if days < 0 or (bucket and days < 1) or (bucket and local) or (enforce and not (bucket or local)):
            raise ValueError("Configure one archive before enabling enforcement")
        start_channel = os.environ.get("GREYBOT_START_CHANNEL_ID", "").strip()
        public_channels = tuple(filter(None, (v.strip() for v in os.environ.get("GREYBOT_PUBLIC_CHANNEL_IDS", "").split(","))))
        if any(not value.isdecimal() for value in (*public_channels, *((start_channel,) if start_channel else ()))):
            raise ValueError("Configured channel IDs must be numeric")
        return cls(guild, client, secret("GREYBOT_BOT_TOKEN"),
                   secret("GREYBOT_OAUTH_SECRET"), origin, state, bucket, days,
                   os.environ.get("GREYBOT_CAPTURE_CONTENT") == "1", enforce, archive_dir, start_channel, public_channels)
