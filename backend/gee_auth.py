"""
gee_auth.py — Google Earth Engine Service Account Authentication
================================================================
Drop this file in the backend directory. It is imported by mainnew.py
and api_mean_prediction.py to replace the bare `ee.Initialize(...)` call.

HOW TO SET UP (one-time, takes ~10 minutes)
-------------------------------------------
1. Go to https://console.cloud.google.com → Select project "carboncredits-487906"
2. IAM & Admin → Service Accounts → Create Service Account
   - Name: carbon-mrv-backend
   - Role: Earth Engine Resource Viewer (or Earth Engine Resource Writer)
3. Click the new service account → Keys → Add Key → JSON
   - Download the JSON key file (e.g. gee-service-account.json)
4. Register the service account with Earth Engine:
   https://code.earthengine.google.com/register
   (use the service account email, e.g. carbon-mrv-backend@carboncredits-487906.iam.gserviceaccount.com)
5. Set the environment variable on Render (or any host):
   GEE_SERVICE_ACCOUNT_JSON = <paste the ENTIRE contents of the JSON key file>
   GEE_PROJECT              = carboncredits-487906

That's it — no local file needed on the server.
"""

import ee
import json
import os
import logging

logger = logging.getLogger(__name__)


def initialize_earth_engine() -> None:
    """
    Initialize the Earth Engine API using one of three methods, tried in order:

    Priority 1 — Service Account JSON in env var  (recommended for Render / cloud)
    Priority 2 — Service Account JSON file path in env var
    Priority 3 — Application Default Credentials   (works if you ran `gcloud auth`)
    """
    project = os.getenv("GEE_PROJECT", "carboncredits-487906")

    # ── Priority 1: Full JSON in environment variable ──────────────────────────
    sa_json_str = os.getenv("GEE_SERVICE_ACCOUNT_JSON", "").strip()
    if sa_json_str:
        try:
            sa_info = json.loads(sa_json_str)
            credentials = ee.ServiceAccountCredentials(
                email=sa_info["client_email"],
                key_data=sa_json_str,
            )
            ee.Initialize(credentials=credentials, project=project)
            logger.info("✅ Earth Engine initialized via GEE_SERVICE_ACCOUNT_JSON env var")
            return
        except Exception as exc:
            logger.error(f"GEE init via env JSON failed: {exc}")
            raise RuntimeError(
                "GEE_SERVICE_ACCOUNT_JSON is set but is invalid. "
                "Check that you pasted the full JSON from your service-account key file."
            ) from exc

    # ── Priority 2: Path to a JSON key file ────────────────────────────────────
    sa_key_path = os.getenv("GEE_SERVICE_ACCOUNT_KEY_PATH", "").strip()
    if sa_key_path and os.path.isfile(sa_key_path):
        try:
            with open(sa_key_path) as f:
                sa_info = json.load(f)
            credentials = ee.ServiceAccountCredentials(
                email=sa_info["client_email"],
                key_file=sa_key_path,
            )
            ee.Initialize(credentials=credentials, project=project)
            logger.info(f"✅ Earth Engine initialized via key file: {sa_key_path}")
            return
        except Exception as exc:
            logger.error(f"GEE init via key file failed: {exc}")
            raise RuntimeError(
                f"Failed to authenticate Earth Engine with key file at {sa_key_path}."
            ) from exc

    # ── Priority 3: Application Default Credentials (local dev / GCP) ──────────
    try:
        ee.Initialize(project=project)
        logger.info("✅ Earth Engine initialized via Application Default Credentials")
        return
    except Exception as exc:
        logger.error(f"GEE init via ADC failed: {exc}")

    # ── Nothing worked — raise a clear, actionable error ──────────────────────
    raise RuntimeError(
        "\n\n"
        "❌  Google Earth Engine could not be initialized.\n"
        "    Set ONE of the following environment variables:\n\n"
        "    Option A (recommended for Render/cloud):\n"
        "      GEE_SERVICE_ACCOUNT_JSON = <full contents of your service-account JSON key>\n\n"
        "    Option B (local or mounted secrets):\n"
        "      GEE_SERVICE_ACCOUNT_KEY_PATH = /path/to/your-key.json\n\n"
        "    Option C (local dev with gcloud CLI):\n"
        "      Run:  gcloud auth application-default login\n"
        "      Then: gcloud config set project carboncredits-487906\n\n"
        "    See backend/README_DEPLOYMENT.md for full setup instructions.\n"
    )
