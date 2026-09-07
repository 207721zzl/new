import hashlib
from functools import cached_property
from pathlib import Path
from packages.platform.config import settings


class ObjectStore:
    @cached_property
    def client(self):
        import boto3
        from botocore.config import Config
        config = settings()
        if not config.object_access_key or not config.object_secret_key:
            raise ValueError("object storage credentials are required")
        return boto3.client("s3", endpoint_url=config.object_endpoint,
            aws_access_key_id=config.object_access_key.get_secret_value(),
            aws_secret_access_key=config.object_secret_key.get_secret_value(),
            region_name="us-east-1", config=Config(connect_timeout=5, read_timeout=60,
            retries={"max_attempts": 2}, s3={"addressing_style": "path"}))

    def upload(self, path: Path, key):
        self.client.upload_file(str(path), settings().object_bucket, key)
        return key

    def download(self, key, path: Path, expected_hash=None):
        self.client.download_file(settings().object_bucket, key, str(path))
        if expected_hash and hashlib.sha256(path.read_bytes()).hexdigest() != expected_hash:
            raise ValueError("uploaded file checksum mismatch")

    def delete(self, key):
        self.client.delete_object(Bucket=settings().object_bucket, Key=key)
