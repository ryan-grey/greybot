"""Version-addressed S3 archive; no deletion or retention-edit API."""

from datetime import datetime, timezone

from .store import canonical, digest


class Archive:
    def __init__(self, client, bucket, retention_days):
        self.client, self.bucket, self.days = client, bucket, retention_days

    def check(self):
        cfg = self.client.get_object_lock_configuration(Bucket=self.bucket)["ObjectLockConfiguration"]
        rule = cfg.get("Rule", {}).get("DefaultRetention", {})
        if (cfg.get("ObjectLockEnabled") != "Enabled" or rule.get("Mode") != "COMPLIANCE"
                or rule.get("Days") != self.days):
            raise RuntimeError("Archive must have the configured COMPLIANCE retention in days")
        if self.client.get_bucket_versioning(Bucket=self.bucket).get("Status") != "Enabled":
            raise RuntimeError("Archive versioning must be enabled")

    def flush(self, store):
        self.check()
        for row in store.pending():
            body = canonical(row).encode()
            key = f"events/{row['guild']}/{row['seq']:020d}-{digest(body.decode())}.json"
            try:
                result = self.client.put_object(Bucket=self.bucket, Key=key, Body=body,
                    ContentType="application/json", ServerSideEncryption="AES256", IfNoneMatch="*")
                version = result["VersionId"]
            except self.client.exceptions.ClientError as exc:
                if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 412:
                    raise
                existing = self.client.get_object(Bucket=self.bucket, Key=key)
                if existing["Body"].read() != body:
                    raise RuntimeError("Archive object conflicts with journal") from None
                version = existing["VersionId"]
            # Verify the locked version itself, not just a successful PutObject.
            saved = self.client.get_object(Bucket=self.bucket, Key=key, VersionId=version)
            retain_until = saved.get("ObjectLockRetainUntilDate")
            if (saved["Body"].read() != body or saved.get("ObjectLockMode") != "COMPLIANCE"
                    or not retain_until or retain_until <= datetime.now(timezone.utc)):
                raise RuntimeError("Archive readback failed")
            store.receipt(row["seq"], key, version)
