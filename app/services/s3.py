import io, boto3
from PIL import Image, ImageOps
from pillow_heif import register_heif_opener
from ..config.settings import settings

register_heif_opener()


class S3Service:
    def __init__(self):
        self.client = boto3.client(
            's3',
            aws_access_key_id=settings.aws_access_key_id,
            aws_secret_access_key=settings.aws_secret_access_key,
            region_name=settings.aws_region,
        )
        self.bucket = settings.s3_bucket_name
  
    def _url_to_key(self, url: str) -> str:
        prefix = f"{self.bucket}.s3.{settings.aws_region}.amazonaws.com/"
        return url.split(prefix)[-1]

    def delete_object(self, url: str):
        """Delete a single object given its full S3 URL."""
        if not url:
            return
        self.client.delete_object(Bucket=self.bucket, Key=self._url_to_key(url))

    def delete_objects(self, urls: list[str]):
        """Batch delete — S3 allows up to 1000 keys per delete_objects call."""
        urls = [u for u in urls if u]
        if not urls:
            return
        keys = [{"Key": self._url_to_key(u)} for u in urls]
        for i in range(0, len(keys), 1000):
            batch = keys[i:i + 1000]
            self.client.delete_objects(Bucket=self.bucket, Delete={"Objects": batch})


s3_service = S3Service()