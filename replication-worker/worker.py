from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
from pathlib import Path

import boto3
import requests
from boto3.s3.transfer import TransferConfig
from botocore.config import Config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("replication-worker")

CONTROLLER = os.environ["CONTROLLER_URL"].rstrip("/")
TOKEN = os.environ["REPLICATION_API_TOKEN"]
WORKER_ID = os.getenv("WORKER_ID", socket.gethostname())
DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
POLL_SECONDS = max(2, int(os.getenv("POLL_SECONDS", "10")))
CHUNK_MB = max(8, int(os.getenv("S3_MULTIPART_CHUNK_MB", "64")))
BACKENDS = {item["name"]: item for item in json.loads(os.environ["S3_BACKENDS_JSON"])}
TARGETS = [value.strip() for value in os.environ["TARGET_BACKENDS"].split(",") if value.strip()]
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
TRANSFER = TransferConfig(
    multipart_threshold=CHUNK_MB * 1024 * 1024,
    multipart_chunksize=CHUNK_MB * 1024 * 1024,
    max_concurrency=4,
    use_threads=True,
)


def client(name: str):
    item = BACKENDS[name]
    return boto3.client(
        "s3",
        endpoint_url=item["endpoint_url"],
        region_name=item.get("region", "auto"),
        aws_access_key_id=item["access_key_id"],
        aws_secret_access_key=item["secret_access_key"],
        config=Config(signature_version="s3v4", retries={"max_attempts": 8, "mode": "adaptive"}),
    )


def post(path: str, payload: dict, timeout: int = 30) -> requests.Response:
    response = requests.post(f"{CONTROLLER}{path}", headers=HEADERS, json=payload, timeout=timeout)
    response.raise_for_status()
    return response


def renew_loop(job_id: int, stop: threading.Event) -> None:
    while not stop.wait(240):
        try:
            post(f"/internal/replication/{job_id}/renew", {"worker_id": WORKER_ID})
        except Exception:
            logger.exception("Could not renew lease for job %s", job_id)


def resume_download(job: dict, destination: Path) -> None:
    source = BACKENDS[job["source_backend"]]
    source_client = client(job["source_backend"])
    downloaded = destination.stat().st_size if destination.exists() else 0
    expected = int(job["size"])
    if downloaded > expected:
        destination.unlink()
        downloaded = 0
    if downloaded == expected:
        return
    kwargs = {"Bucket": source["bucket"], "Key": job["source_object_key"]}
    if downloaded:
        kwargs["Range"] = f"bytes={downloaded}-"
    response = source_client.get_object(**kwargs)
    mode = "ab" if downloaded else "wb"
    with destination.open(mode) as output:
        while chunk := response["Body"].read(8 * 1024 * 1024):
            output.write(chunk)
    if destination.stat().st_size != expected:
        raise RuntimeError("Downloaded size does not match source metadata")


def process(job: dict) -> None:
    job_id = int(job["id"])
    destination = DATA_DIR / f"{job_id}.part"
    stop = threading.Event()
    renewer = threading.Thread(target=renew_loop, args=(job_id, stop), daemon=True)
    renewer.start()
    try:
        resume_download(job, destination)
        target = BACKENDS[job["target_backend"]]
        client(job["target_backend"]).upload_file(
            str(destination), target["bucket"], job["target_object_key"],
            ExtraArgs={"ContentType": job["mime_type"]}, Config=TRANSFER,
        )
        post(
            f"/internal/replication/{job_id}/complete",
            {"worker_id": WORKER_ID},
        )
        destination.unlink(missing_ok=True)
        logger.info("Job %s replicated to %s", job_id, job["target_backend"])
    except Exception as exc:
        logger.exception("Job %s failed", job_id)
        try:
            post(
                f"/internal/replication/{job_id}/fail",
                {"worker_id": WORKER_ID, "error": type(exc).__name__},
            )
        except Exception:
            logger.exception("Could not report failure for job %s", job_id)
    finally:
        stop.set()
        renewer.join(timeout=2)


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    unknown = [name for name in TARGETS if name not in BACKENDS]
    if unknown:
        raise RuntimeError("Unknown target backends: " + ", ".join(unknown))
    logger.info("Worker %s started for targets: %s", WORKER_ID, ", ".join(TARGETS))
    while True:
        try:
            response = post(
                "/internal/replication/claim",
                {"worker_id": WORKER_ID, "targets": TARGETS},
            )
            if response.status_code == 204:
                time.sleep(POLL_SECONDS)
                continue
            process(response.json())
        except Exception:
            logger.exception("Controller poll failed")
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
