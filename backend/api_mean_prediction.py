import asyncio
import json
import logging
import os
import time
import secrets
import re
from collections import defaultdict
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta

import ee
import joblib
import pandas as pd
from fastapi import FastAPI, HTTPException, Request, Security, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from sqlalchemy import inspect, text

from auth_service import (
    hash_password, verify_password, create_access_token,
    get_current_user, generate_api_key,
    LoginRequest, RegisterRequest, VerifyUserRequest,
    require_auditor_or_admin, require_admin
)
from blockchain_service import mint_tokens, verify_deposit_transaction
from blockchain_listener import start_listener          # CCT auto-payment listener
from database import SessionLocal, engine
from document_service import save_document, delete_document, get_doc_types_for_role
from email_service import send_credits_minted_email, send_welcome_email, send_carbon_loss_alert
import models
from pdf_service import generate_certificate
from retirement_service import generate_retirement_certificate, generate_retirement_id

# ── Logging ───────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("carbon_mrv.log"), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# ── Earth Engine ──────────────────────────────────────────
try:
    from gee_auth import initialize_earth_engine
    initialize_earth_engine()
    logger.info("Earth Engine initialized")
except Exception as e:
    logger.error(f"Earth Engine failed: {e}")
    raise

# ── ML Model ──────────────────────────────────────────────
try:
    model = joblib.load("biomass_model.pkl")
    logger.info("Model loaded")
except Exception as e:
    logger.error(f"Model load failed: {e}")
    raise

# ── Lifespan (startup / shutdown) ─────────────────────────
@asynccontextmanager
async def lifespan(app_instance):
    """
    Start the CCT blockchain listener in a non-blocking background task.
    The listener auto-detects Transfer events → platform wallet and
    creates marketplace listings minus the 2% platform fee.
    """
    listener_task = asyncio.create_task(start_listener())
    logger.info("CCT blockchain listener task started.")
    try:
        yield
    finally:
        listener_task.cancel()
        try:
            await listener_task
        except asyncio.CancelledError:
            pass
        logger.info("CCT blockchain listener task stopped.")


# ── App ───────────────────────────────────────────────────
app = FastAPI(
    title="Carbon MRV Registry API",
    description="AI + Satellite + Blockchain Carbon Credit System",
    version="3.0.0",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"]
)

BUFFER_RATE      = 0.20
PLATFORM_WALLET  = os.getenv("PLATFORM_WALLET_ADDRESS", "")
if not PLATFORM_WALLET:
    raise EnvironmentError("PLATFORM_WALLET_ADDRESS not set in .env")

_rate_store: dict = defaultdict(list)
RATE_LIMIT_REQUESTS = 60
RATE_LIMIT_WINDOW   = 60
COORDS_PATTERN = re.compile(
    r"^-?\d+(?:\.\d+)?,-?\d+(?:\.\d+)?(?:\|-?\d+(?:\.\d+)?,-?\d+(?:\.\d+)?)+$"
)
BANK_ACCOUNT_PATTERN = re.compile(r"^\d{9,18}$")
IFSC_PATTERN = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")


def ensure_schema_columns():
    """
    Lightweight runtime migration for additive columns.
    Keeps existing installations working without manual DDL.
    """
    try:
        inspector = inspect(engine)
        user_cols = {c["name"] for c in inspector.get_columns("users")}
        project_cols = {c["name"] for c in inspector.get_columns("projects")}
        document_cols = {c["name"] for c in inspector.get_columns("documents")}

        with engine.begin() as conn:
            if "project_name" not in project_cols:
                conn.execute(text("ALTER TABLE projects ADD COLUMN project_name VARCHAR"))
                logger.info("DB migration: added projects.project_name")
            if "coordinates_key" not in project_cols:
                conn.execute(text("ALTER TABLE projects ADD COLUMN coordinates_key VARCHAR"))
                logger.info("DB migration: added projects.coordinates_key")
            if "is_minted" not in project_cols:
                conn.execute(text("ALTER TABLE projects ADD COLUMN is_minted BOOLEAN DEFAULT FALSE"))
                logger.info("DB migration: added projects.is_minted")
            if "mint_tx_hash" not in project_cols:
                conn.execute(text("ALTER TABLE projects ADD COLUMN mint_tx_hash VARCHAR"))
                logger.info("DB migration: added projects.mint_tx_hash")
            if "minted_at" not in project_cols:
                conn.execute(text("ALTER TABLE projects ADD COLUMN minted_at TIMESTAMP"))
                logger.info("DB migration: added projects.minted_at")
            if "approved_by" not in project_cols:
                conn.execute(text("ALTER TABLE projects ADD COLUMN approved_by INTEGER"))
                conn.execute(
                    text(
                        "ALTER TABLE projects "
                        "ADD CONSTRAINT fk_projects_approved_by "
                        "FOREIGN KEY (approved_by) REFERENCES users(id)"
                    )
                )
                logger.info("DB migration: added projects.approved_by")
            if "approved_at" not in project_cols:
                conn.execute(text("ALTER TABLE projects ADD COLUMN approved_at TIMESTAMP"))
                logger.info("DB migration: added projects.approved_at")
            if "project_id" not in document_cols:
                conn.execute(text("ALTER TABLE documents ADD COLUMN project_id INTEGER"))
                conn.execute(
                    text(
                        "ALTER TABLE documents "
                        "ADD CONSTRAINT fk_documents_project_id "
                        "FOREIGN KEY (project_id) REFERENCES projects(id)"
                    )
                )
                logger.info("DB migration: added documents.project_id")

            # Landowner payout bank account fields on users
            if "payout_account_holder" not in user_cols:
                conn.execute(text("ALTER TABLE users ADD COLUMN payout_account_holder VARCHAR"))
                logger.info("DB migration: added users.payout_account_holder")
            if "payout_bank_name" not in user_cols:
                conn.execute(text("ALTER TABLE users ADD COLUMN payout_bank_name VARCHAR"))
                logger.info("DB migration: added users.payout_bank_name")
            if "payout_account_number" not in user_cols:
                conn.execute(text("ALTER TABLE users ADD COLUMN payout_account_number VARCHAR"))
                logger.info("DB migration: added users.payout_account_number")
            if "payout_ifsc_code" not in user_cols:
                conn.execute(text("ALTER TABLE users ADD COLUMN payout_ifsc_code VARCHAR"))
                logger.info("DB migration: added users.payout_ifsc_code")
            if "payout_branch_name" not in user_cols:
                conn.execute(text("ALTER TABLE users ADD COLUMN payout_branch_name VARCHAR"))
                logger.info("DB migration: added users.payout_branch_name")
            if "payout_updated_at" not in user_cols:
                conn.execute(text("ALTER TABLE users ADD COLUMN payout_updated_at TIMESTAMP"))
                logger.info("DB migration: added users.payout_updated_at")

            # Backfill minted flag for legacy verified projects.
            conn.execute(
                text(
                    "UPDATE projects SET is_minted = TRUE "
                    "WHERE (status = 'verified' OR status = 'approved') AND COALESCE(is_minted, FALSE) = FALSE"
                )
            )

            # Best-effort DB uniqueness check for normalized coordinates.
            try:
                conn.execute(
                    text(
                        "CREATE UNIQUE INDEX IF NOT EXISTS ux_projects_coordinates_key "
                        "ON projects (coordinates_key) WHERE coordinates_key IS NOT NULL"
                    )
                )
            except Exception as e:
                logger.warning(f"Could not create unique index for coordinates_key: {e}")

            # ── CCT Auto-Payment Migrations ───────────────────────────────────
            # 1. Add platform_fee column to marketplace_listings
            listing_cols = {c["name"] for c in inspector.get_columns("marketplace_listings")}
            if "platform_fee" not in listing_cols:
                conn.execute(text(
                    "ALTER TABLE marketplace_listings ADD COLUMN platform_fee FLOAT"
                ))
                logger.info("DB migration: added marketplace_listings.platform_fee")

            # 2. Add source column to marketplace_listings
            if "source" not in listing_cols:
                conn.execute(text(
                    "ALTER TABLE marketplace_listings "
                    "ADD COLUMN source VARCHAR DEFAULT 'manual'"
                ))
                logger.info("DB migration: added marketplace_listings.source")

            # 3. Create processed_transactions table if it does not exist
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS processed_transactions (
                    id            SERIAL PRIMARY KEY,
                    tx_hash       VARCHAR UNIQUE NOT NULL,
                    sender_wallet VARCHAR NOT NULL,
                    amount        FLOAT NOT NULL,
                    fee           FLOAT NOT NULL,
                    listing_id    INTEGER REFERENCES marketplace_listings(id),
                    note          VARCHAR,
                    created_at    TIMESTAMP DEFAULT NOW()
                )
            """))
            conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_processed_tx_hash "
                "ON processed_transactions (tx_hash)"
            ))
            logger.info("DB migration: ensured processed_transactions table exists")

            # 4. Seed default_listing_price system setting if absent
            existing_price = conn.execute(
                text("SELECT 1 FROM system_settings WHERE key='default_listing_price'")
            ).fetchone()
            if not existing_price:
                conn.execute(text(
                    "INSERT INTO system_settings (key, value, description) "
                    "VALUES ('default_listing_price', '1.0', "
                    "'Default price per CCT credit for auto-listed marketplace entries')"
                ))
                logger.info("DB migration: seeded system_settings.default_listing_price")

            # ── MRV blacklist / flag columns ─────────────────────────────────
            # Re-inspect to get the freshest column list (handles hot-reloads)
            live_project_cols = {c["name"] for c in inspect(engine).get_columns("projects")}
            if "is_blacklisted" not in live_project_cols:
                conn.execute(text(
                    "ALTER TABLE projects ADD COLUMN is_blacklisted BOOLEAN DEFAULT FALSE"
                ))
                logger.info("DB migration: added projects.is_blacklisted")
            if "is_flagged" not in live_project_cols:
                conn.execute(text(
                    "ALTER TABLE projects ADD COLUMN is_flagged BOOLEAN DEFAULT FALSE"
                ))
                logger.info("DB migration: added projects.is_flagged")
            if "flag_reason" not in live_project_cols:
                conn.execute(text(
                    "ALTER TABLE projects ADD COLUMN flag_reason VARCHAR"
                ))
                logger.info("DB migration: added projects.flag_reason")

    except Exception as e:
        logger.warning(f"Schema migration skipped/failed: {e}")


ensure_schema_columns()

# ── Helpers ───────────────────────────────────────────────
def check_rate_limit(request: Request):
    ip  = request.client.host
    now = time.time()
    _rate_store[ip] = [t for t in _rate_store[ip] if now - t < RATE_LIMIT_WINDOW]
    if len(_rate_store[ip]) >= RATE_LIMIT_REQUESTS:
        raise HTTPException(status_code=429, detail="Too many requests. Try again in 60 seconds.")
    _rate_store[ip].append(now)


@contextmanager
def get_db():
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def log_action(db, user_id, action: str, details: dict, ip: str = None):
    db.add(models.AuditLog(
        user_id=user_id, action=action,
        details=json.dumps(details), ip_address=ip
    ))


def get_setting(db, key: str, default: str = None) -> str:
    s = db.query(models.SystemSetting).filter_by(key=key).first()
    return s.value if s else default


def validate_email(email: str):
    if "@" not in email or "." not in email:
        raise HTTPException(status_code=400, detail="Invalid email format.")


def validate_wallet(wallet: str):
    if not wallet.startswith("0x") or len(wallet) != 42:
        raise HTTPException(status_code=400, detail="Invalid wallet address.")


def mask_bank_account_number(account_number: str) -> str:
    if not account_number:
        return ""
    if len(account_number) <= 4:
        return account_number
    return ("*" * (len(account_number) - 4)) + account_number[-4:]


def validate_payout_bank_account(
    account_holder_name: str,
    bank_name: str,
    account_number: str,
    ifsc_code: str,
):
    if not account_holder_name:
        raise HTTPException(status_code=400, detail="Account holder name is required.")
    if len(account_holder_name) > 120:
        raise HTTPException(status_code=400, detail="Account holder name is too long.")
    if not bank_name:
        raise HTTPException(status_code=400, detail="Bank name is required.")
    if len(bank_name) > 120:
        raise HTTPException(status_code=400, detail="Bank name is too long.")
    if not BANK_ACCOUNT_PATTERN.fullmatch(account_number or ""):
        raise HTTPException(
            status_code=400,
            detail="Account number must contain 9 to 18 digits."
        )
    if not IFSC_PATTERN.fullmatch((ifsc_code or "").upper()):
        raise HTTPException(
            status_code=400,
            detail="Invalid IFSC code format. Example: HDFC0001234."
        )


def validate_coords(coords):
    if not coords or not isinstance(coords, list):
        raise HTTPException(status_code=400, detail="'coordinates' must be a non-empty list.")
    if len(coords) < 3:
        raise HTTPException(status_code=400, detail="At least 3 coordinate points required.")
    for i, pt in enumerate(coords):
        if "lat" not in pt or "lon" not in pt:
            raise HTTPException(status_code=400, detail=f"Coordinate {i} missing 'lat' or 'lon'.")
        if not (-90 <= pt["lat"] <= 90):
            raise HTTPException(status_code=400, detail=f"Coordinate {i} lat must be -90 to 90.")
        if not (-180 <= pt["lon"] <= 180):
            raise HTTPException(status_code=400, detail=f"Coordinate {i} lon must be -180 to 180.")


def parse_coordinates_string(coordinates_raw: str):
    """
    Parse coordinates from strict format:
    lat,lon|lat,lon|lat,lon
    """
    if not coordinates_raw or not isinstance(coordinates_raw, str):
        raise HTTPException(status_code=400, detail="'coordinates' is required.")

    coordinates_raw = re.sub(r"\s+", "", coordinates_raw.strip())
    if not COORDS_PATTERN.fullmatch(coordinates_raw):
        raise HTTPException(
            status_code=400,
            detail=(
                "Invalid coordinates format. Use: "
                "28.385,77.170|28.390,77.180|28.380,77.185|28.375,77.175"
            ),
        )

    coords = []
    for pair in coordinates_raw.split("|"):
        lat_s, lon_s = pair.split(",", 1)
        coords.append({"lat": float(lat_s), "lon": float(lon_s)})

    validate_coords(coords)
    return normalize_coords(coords)


def coords_to_string(coords: list[dict]):
    if isinstance(coords, str):
        return coords
    if not isinstance(coords, list):
        return None
    try:
        return "|".join(f"{pt['lat']:.6f},{pt['lon']:.6f}" for pt in coords)
    except Exception:
        return None


def normalize_coords(coords: list[dict]):
    validate_coords(coords)
    normalized = []
    for pt in coords:
        normalized.append({
            "lat": round(float(pt["lat"]), 6),
            "lon": round(float(pt["lon"]), 6),
        })
    return normalized


def coordinates_key(coords: list[dict]):
    normalized = normalize_coords(coords)
    return "|".join(f"{pt['lat']:.6f},{pt['lon']:.6f}" for pt in normalized)


def find_duplicate_project_by_coordinates(db, coords_key: str):
    # Fast path with normalized key column.
    existing = db.query(models.Project).filter_by(coordinates_key=coords_key).first()
    if existing:
        return existing

    # Backward-compatible check for older rows without coordinates_key.
    rows = db.query(models.Project).filter(models.Project.coordinates_key.is_(None)).all()
    for p in rows:
        if not p.coordinates:
            continue
        try:
            legacy_key = coords_to_string(normalize_coords(p.coordinates))
        except Exception:
            continue
        if legacy_key == coords_key:
            return p
    return None


def require_roles(current_user: dict, allowed_roles: list):
    if current_user.get("auth_type") == "api_key":
        return
    if current_user.get("role") not in allowed_roles:
        raise HTTPException(
            status_code=403,
            detail=f"Access denied. Required roles: {allowed_roles}"
        )


def require_approved(db, user_id: int):
    user = db.query(models.User).filter_by(id=user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")
    if not user.is_approved:
        raise HTTPException(status_code=403, detail="Account pending approval.")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account suspended.")


def require_identity_document(db, user: "models.User"):
    """
    Ensure the user has at least one approved identity document before verification.
    - land_owner / auditor: PAN individual (or legacy aadhaar/auditor_id)
    - organization: PAN organization (or legacy gst/cin/incorporation)
    """
    from sqlalchemy import or_
    identity_types = []
    if user.role in ["land_owner", "auditor"]:
        identity_types = ["pan_individual", "aadhaar", "auditor_id"]
    elif user.role == "organization":
        identity_types = ["pan_organization", "gst", "cin", "incorporation"]
    else:
        # Admin or unknown roles: skip identity requirement
        return

    doc = db.query(models.Document).filter(
        models.Document.user_id == user.id,
        models.Document.status == "approved",
        models.Document.doc_type.in_(identity_types)
    ).first()
    if not doc:
        raise HTTPException(
            status_code=400,
            detail=(
                "Identity document not found or not approved. "
                "Please ensure a PAN (or equivalent) document is uploaded and approved before verification."
            ),
        )


def create_polygon(coords):
    ring = [[c["lon"], c["lat"]] for c in coords]
    ring.append(ring[0])
    return ee.Geometry.Polygon([ring])


def calculate_area_hectares(polygon) -> float:
    try:
        return float(polygon.area(maxError=1).getInfo()) / 10_000
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Area calculation failed: {e}")


def extract_features(polygon) -> dict:
    try:
        end_dt   = datetime.utcnow()
        start_dt = end_dt - timedelta(days=30)
        sentinel = (
            ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
            .filterBounds(polygon).filterDate(start_dt.strftime("%Y-%m-%d"), end_dt.strftime("%Y-%m-%d"))
            .filter(ee.Filter.lt('CLOUDY_PIXEL_PERCENTAGE', 20))
            .median().divide(10000)
        )
        ndvi  = sentinel.normalizedDifference(['B8', 'B4'])
        evi   = sentinel.expression(
            '2.5*((NIR-RED)/(NIR+6*RED-7.5*BLUE+1))',
            {'NIR': sentinel.select('B8'), 'RED': sentinel.select('B4'), 'BLUE': sentinel.select('B2')}
        )
        dem   = ee.ImageCollection("COPERNICUS/DEM/GLO30").filterBounds(polygon).select('DEM').mosaic()
        slope = ee.Terrain.slope(dem)

        def mean_val(image):
            result = image.reduceRegion(
                reducer=ee.Reducer.mean(), geometry=polygon, scale=30, maxPixels=1e9
            ).getInfo()
            vals = list(result.values())
            return vals[0] if vals else None

        return {
            "ndvi": mean_val(ndvi), "evi": mean_val(evi),
            "elevation": mean_val(dem), "slope": mean_val(slope)
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Feature extraction failed: {e}")


def predict_carbon(features: dict, area_ha: float) -> dict:
    if features["ndvi"] is None or features["evi"] is None:
        raise HTTPException(status_code=400, detail="Optical data unavailable.")
    ndvi      = float(features["ndvi"])
    evi       = float(features["evi"])
    elevation = float(features["elevation"]) if features["elevation"] is not None else 0.0
    slope     = float(features["slope"])     if features["slope"]     is not None else 0.0
    df             = pd.DataFrame({"NDVI": [ndvi], "EVI": [evi], "Elevation": [elevation], "Slope": [slope]})
    biomass_per_ha = float(model.predict(df)[0])
    carbon_per_ha  = biomass_per_ha * 0.47
    credits_per_ha = carbon_per_ha  * 3.67
    return {
        "clean_features":  {"NDVI": ndvi, "EVI": evi, "Elevation": elevation, "Slope": slope},
        "biomass_per_ha":  biomass_per_ha,
        "carbon_per_ha":   carbon_per_ha,
        "credits_per_ha":  credits_per_ha,
        "total_biomass":   biomass_per_ha * area_ha,
        "total_carbon":    carbon_per_ha  * area_ha,
        "total_credits":   credits_per_ha * area_ha,
    }


def create_project_pending(db, request: Request, user: "models.User", coords: list[dict], project_name: str):
    user_id = user.id
    normalized_coords = normalize_coords(coords)
    coords_key = coordinates_key(normalized_coords)

    duplicate = find_duplicate_project_by_coordinates(db, coords_key)
    if duplicate:
        raise HTTPException(
            status_code=409,
            detail=f"Duplicate coordinates detected. Project #{duplicate.id} already uses the same coordinates.",
        )

    polygon = create_polygon(normalized_coords)
    area_ha = calculate_area_hectares(polygon)
    features = extract_features(polygon)
    prediction = predict_carbon(features, area_ha)
    total_credits = prediction["total_credits"]

    project = models.Project(
        user_id=user_id,
        project_name=project_name,
        coordinates_key=coords_key,
        area_hectares=area_ha,
        baseline_carbon=total_credits,
        coordinates=normalized_coords,
        status="pending",
        is_minted=False,
    )
    db.add(project)
    db.flush()
    db.refresh(project)

    db.add(models.CarbonRecord(
        project_id=project.id,
        carbon_stock=total_credits,
        carbon_credits_generated=0.0,
        buffer_credits_added=0.0
    ))

    log_action(
        db,
        user_id,
        "register_project_pending",
        {
            "project_id": project.id,
            "project_name": project_name,
            "coordinates_key": coords_key,
            "predicted_total_credits": total_credits,
        },
        request.client.host,
    )

    logger.info(f"Project {project.id} ({project_name}) created as pending.")
    return {
        "project_id": project.id,
        "project_name": project_name,
        "coordinates": coords_to_string(normalized_coords),
        "area_hectares": area_ha,
        "current_carbon_stock": total_credits,
        "credits_issued": 0.0,
        "buffer_held": 0.0,
        "wallet_total": None,
        "wallet_available": None,
        "blockchain_tx": None,
        "mint_error": None,
        "certificate_generated": False,
        "status": "pending",
        "message": "Project submitted and awaiting auditor approval.",
        "per_hectare": {
            "biomass_tons": prediction["biomass_per_ha"],
            "carbon_tons": prediction["carbon_per_ha"],
            "credits": prediction["credits_per_ha"],
        },
    }


def mint_project_tokens_on_approval(db, request: Request, project: "models.Project", approver: dict):
    if project.is_minted:
        raise HTTPException(status_code=409, detail="Tokens already minted for this project.")

    user = db.query(models.User).filter_by(id=project.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Project owner not found.")

    wallet = db.query(models.Wallet).filter_by(user_id=project.user_id).with_for_update().first()
    if not wallet:
        raise HTTPException(status_code=404, detail=f"Wallet for user {project.user_id} not found.")

    buffer = db.query(models.BufferPool).first()
    if not buffer:
        buffer = models.BufferPool(total_buffer_credits=0.0)
        db.add(buffer)
        db.flush()

    total_credits = float(project.baseline_carbon or 0.0)
    if total_credits <= 0:
        polygon = create_polygon(project.coordinates)
        area_ha = calculate_area_hectares(polygon)
        features = extract_features(polygon)
        prediction = predict_carbon(features, area_ha)
        total_credits = prediction["total_credits"]
        project.area_hectares = area_ha
        project.baseline_carbon = total_credits
    else:
        area_ha = float(project.area_hectares or 0.0)
        polygon = create_polygon(project.coordinates)
        features = extract_features(polygon)
        prediction = predict_carbon(features, area_ha)

    buffer_amount = total_credits * BUFFER_RATE
    user_amount = total_credits - buffer_amount

    tx_hash = mint_tokens(user.wallet_address, user_amount)

    wallet.total_credits += user_amount
    wallet.available_credits += user_amount
    wallet.buffer_contributed += buffer_amount
    buffer.total_buffer_credits += buffer_amount

    project.status = "approved"
    project.land_verified = True
    project.is_minted = True
    project.mint_tx_hash = tx_hash
    project.minted_at = datetime.utcnow()
    project.approved_by = approver["user_id"]
    project.approved_at = datetime.utcnow()

    pdf_path = None
    try:
        pdf_path = generate_certificate(
            project_id=project.id,
            user_id=user.id,
            farmer_name=user.name,
            farmer_email=user.email,
            wallet_address=user.wallet_address,
            area_ha=project.area_hectares,
            carbon_stock=total_credits,
            credits_issued=user_amount,
            buffer_held=buffer_amount,
            blockchain_tx=tx_hash or "",
            biomass_per_ha=prediction["biomass_per_ha"],
            carbon_per_ha=prediction["carbon_per_ha"],
            credits_per_ha=prediction["credits_per_ha"],
        )
        db.add(models.Certificate(
            project_id=project.id,
            user_id=user.id,
            credits_amount=user_amount,
            blockchain_tx=tx_hash,
            pdf_path=pdf_path
        ))
    except Exception as e:
        logger.error(f"PDF generation failed for project {project.id}: {e}")

    try:
        send_credits_minted_email(
            to_email=user.email,
            farmer_name=user.name,
            credits_issued=user_amount,
            blockchain_tx=tx_hash or "",
            project_id=project.id,
            area_ha=project.area_hectares,
            carbon_stock=total_credits,
            attachment_path=pdf_path
        )
    except Exception:
        pass

    log_action(
        db,
        approver["user_id"],
        "project_approved_and_minted",
        {
            "project_id": project.id,
            "owner_id": user.id,
            "credits_issued": user_amount,
            "buffer_held": buffer_amount,
            "tx_hash": tx_hash,
        },
        request.client.host,
    )

    logger.info(f"Project {project.id} approved by {approver['role']} {approver['user_id']} and minted.")
    return {
        "project_id": project.id,
        "project_name": project.project_name or f"Project #{project.id}",
        "status": project.status,
        "credits_issued": user_amount,
        "buffer_held": buffer_amount,
        "wallet_total": wallet.total_credits,
        "wallet_available": wallet.available_credits,
        "blockchain_tx": tx_hash,
    }


# ════════════════════════════════════════════════════════
#  GENERAL
# ════════════════════════════════════════════════════════

@app.get("/")
def home():
    return {"status": "Carbon MRV Registry running", "version": "3.0.0"}


# ════════════════════════════════════════════════════════
#  AUTH
# ════════════════════════════════════════════════════════

@app.post("/auth/register")
def register(data: RegisterRequest, request: Request):
    check_rate_limit(request)
    name           = data.name
    email          = data.email
    password       = data.password
    wallet_address = data.wallet_address or f"audit_{secrets.token_hex(4)}"  # ✅ Auto-generate for auditors
    role           = data.role

    validate_email(email)
    
    # ✅ Only validate wallet for non-auditor roles
    if role not in ['auditor', 'admin']:
        validate_wallet(wallet_address)

    with get_db() as db:
        if db.query(models.User).filter_by(email=email).first():
            raise HTTPException(status_code=409, detail="Email already registered.")
        if db.query(models.User).filter_by(wallet_address=wallet_address).first():
            raise HTTPException(status_code=409, detail="Wallet address already registered.")

        # ✅ Check auto-approval settings - Auditors never auto-approved
        setting_key = f"auto_approve_{role}s"
        auto_approve = get_setting(db, setting_key, "false").lower() == "true"
        if role == 'auditor':
            auto_approve = False  # ✅ Auditors require admin approval

        user = models.User(
            name=name, email=email,
            password_hash=hash_password(password),
            wallet_address=wallet_address,
            role=role,
            is_approved=auto_approve,
            is_verified=False  # ✅ NEW: User not verified initially
        )
        db.add(user)
        db.flush()
        db.refresh(user)
        new_id   = user.id
        new_addr = user.wallet_address
        db.add(models.Wallet(user_id=new_id))
        log_action(db, new_id, "register", {"email": email, "role": role}, request.client.host)

    try:
        send_welcome_email(email, name)
    except Exception:
        pass

    logger.info(f"New user: {email} ({role}) approved={auto_approve}, verified=False")
    return {
        "user_id":        new_id,
        "wallet_address": new_addr,
        "role":           role,
        "is_approved":    auto_approve,
        "is_verified":    False,  # ✅ NEW
        "message":        "Registration successful." if auto_approve else "Registration submitted. Awaiting approval."
    }


@app.post("/auth/register_with_document")
async def register_with_document(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    role: str = Form("land_owner"),
    wallet_address: str | None = Form(None),
    identity_doc: UploadFile = File(...),
):
    """
    Combined registration + identity document upload.

    - Land Owner / Auditor: PAN card (doc_type="pan_individual")
    - Organization: PAN card (doc_type="pan_organization")

    User is created with is_verified=False and is_approved per system settings.
    Identity document is stored in the documents table with status="pending".
    """
    check_rate_limit(request)

    # Normalize inputs through existing Pydantic model to reuse validation
    data = RegisterRequest(
        name=name,
        email=email,
        password=password,
        role=role,
        wallet_address=wallet_address,
    )

    # Map role → identity document type
    if data.role in ["land_owner", "auditor"]:
        identity_doc_type = "pan_individual"
    elif data.role == "organization":
        identity_doc_type = "pan_organization"
    else:
        raise HTTPException(status_code=400, detail="Invalid role for registration.")

    # Run same core logic as /auth/register but within this coroutine
    name           = data.name
    email          = data.email
    password       = data.password
    wallet_address = data.wallet_address or (f"audit_{secrets.token_hex(4)}" if data.role in ["auditor", "admin"] else None)
    role           = data.role

    validate_email(email)

    # Only validate wallet for non-auditor roles
    if role not in ["auditor", "admin"]:
        validate_wallet(wallet_address)

    with get_db() as db:
        if db.query(models.User).filter_by(email=email).first():
            raise HTTPException(status_code=409, detail="Email already registered.")
        if wallet_address and db.query(models.User).filter_by(wallet_address=wallet_address).first():
            raise HTTPException(status_code=409, detail="Wallet address already registered.")

        setting_key = f"auto_approve_{role}s"
        auto_approve = get_setting(db, setting_key, "false").lower() == "true"
        if role == "auditor":
            auto_approve = False

        user = models.User(
            name=name,
            email=email,
            password_hash=hash_password(password),
            wallet_address=wallet_address or f"audit_{secrets.token_hex(4)}" if role == "auditor" else wallet_address,
            role=role,
            is_approved=auto_approve,
            is_verified=False,
        )
        db.add(user)
        db.flush()
        db.refresh(user)
        new_id   = user.id
        new_addr = user.wallet_address
        db.add(models.Wallet(user_id=new_id))

        # Save identity document as pending
        saved = await save_document(identity_doc, new_id, identity_doc_type)
        doc   = models.Document(
            user_id   = new_id,
            file_path = saved["file_path"],
            file_name = saved["file_name"],
            file_type = saved["file_type"],
            doc_type  = identity_doc_type,
            status    = "pending",
        )
        db.add(doc)

        log_action(
            db,
            new_id,
            "register",
            {"email": email, "role": role, "identity_doc_type": identity_doc_type},
            request.client.host,
        )

    try:
        send_welcome_email(email, name)
    except Exception:
        pass

    logger.info(f"New user with document: {email} ({role}) approved={auto_approve}, verified=False")
    return {
        "user_id":        new_id,
        "wallet_address": new_addr,
        "role":           role,
        "is_approved":    auto_approve,
        "is_verified":    False,
        "identity_doc_type": identity_doc_type,
        "message":        "Registration submitted. Identity document pending verification."
    }


@app.post("/auth/login")
def login(data: LoginRequest, request: Request):
    check_rate_limit(request)
    email    = data.email
    password = data.password

    with get_db() as db:
        user = db.query(models.User).filter_by(email=email).first()
        if not user or not verify_password(password, user.password_hash):
            raise HTTPException(status_code=401, detail="Invalid email or password.")
        if not user.is_active:
            raise HTTPException(status_code=403, detail="Account suspended.")
        if not user.is_approved:
            raise HTTPException(status_code=403, detail="Account pending approval.")
        token   = create_access_token(user.id, user.role)
        user_id = user.id
        role    = user.role
        name    = user.name
        is_verified = user.is_verified  # ✅ Include verification status
        log_action(db, user.id, "login", {"email": email}, request.client.host)

    logger.info(f"Login: {email}")
    return {
        "access_token": token,
        "token_type":   "bearer",
        "user_id":      user_id,
        "role":         role,
        "name":         name,
        "is_verified":  is_verified,  # ✅ NEW
        "expires_in":   "24 hours"
    }


# ════════════════════════════════════════════════════════
# ✅ USER VERIFICATION ENDPOINTS (NEW)
# ════════════════════════════════════════════════════════

@app.post("/auth/verify_user/{user_id}")
def verify_user(
    user_id: int, 
    data: VerifyUserRequest,
    request: Request, 
    current_user: dict = Security(require_auditor_or_admin)
):
    """
    ✅ NEW: Verify a user - can be done by admin or auditor
    This allows user to mint credits after being verified
    """
    check_rate_limit(request)

    with get_db() as db:
        user = db.query(models.User).filter_by(id=user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found.")

        # Role-based verification rules
        verifier_role = current_user["role"]
        target_role   = user.role

        # 1) Only admin can verify/reject auditors
        if target_role == "auditor" and verifier_role != "admin":
            raise HTTPException(
                status_code=403,
                detail="Only admin can verify or reject auditor accounts.",
            )

        # 2) Land owners & organizations can be verified by admin or auditor
        if target_role in ["land_owner", "organization"] and verifier_role not in ["admin", "auditor"]:
            raise HTTPException(
                status_code=403,
                detail="Only admin or auditor can verify land owner / organization accounts.",
            )

        # 3) When marking verified=True, require approved identity document
        if data.verified:
            require_identity_document(db, user)

        # Update verification status
        user.is_verified = data.verified
        db.add(user)
        
        # Log the action
        log_action(
            db, 
            current_user["user_id"], 
            "verify_user", 
            {
                "target_user_id": user_id,
                "verified": data.verified,
                "reason": data.reason or "Not specified",
                "verifier_role": current_user["role"]
            }, 
            request.client.host
        )

        # Capture scalar values inside the session — avoids DetachedInstanceError
        # after the with-block closes and the session expires the ORM instance.
        is_verified_val = bool(user.is_verified)

    logger.info(f"User {user_id} {'verified' if data.verified else 'unverified'} by {current_user['role']} {current_user['user_id']}")
    return {
        "user_id": user_id,
        "is_verified": is_verified_val,
        "message": f"User {'verified' if is_verified_val else 'unverified'} successfully"
    }


@app.get("/auth/user/{user_id}/verification-status")
def check_verification_status(
    user_id: int,
    request: Request,
    current_user: dict = Security(get_current_user)
):
    """✅ NEW: Check if a user is verified"""
    with get_db() as db:
        user = db.query(models.User).filter_by(id=user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found.")
        
        return {
            "user_id": user_id,
            "name": user.name,
            "email": user.email,
            "role": user.role,
            "is_verified": user.is_verified,
            "is_approved": user.is_approved,
            "is_active": user.is_active
        }


# ════════════════════════════════════════════════════════
#  DOCUMENT UPLOAD
# ════════════════════════════════════════════════════════

@app.get("/landowner/payout-bank-account")
def get_landowner_payout_bank_account(
    request: Request,
    current_user: dict = Security(get_current_user),
):
    check_rate_limit(request)
    require_roles(current_user, ["land_owner"])
    user_id = current_user["user_id"]

    with get_db() as db:
        user = db.query(models.User).filter_by(id=user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found.")

        is_configured = all([
            bool(user.payout_account_holder),
            bool(user.payout_bank_name),
            bool(user.payout_account_number),
            bool(user.payout_ifsc_code),
        ])

        return {
            "is_configured": is_configured,
            "account_holder_name": user.payout_account_holder or "",
            "bank_name": user.payout_bank_name or "",
            "account_number": user.payout_account_number or "",
            "account_number_masked": mask_bank_account_number(user.payout_account_number or ""),
            "ifsc_code": user.payout_ifsc_code or "",
            "branch_name": user.payout_branch_name or "",
            "updated_at": user.payout_updated_at,
            "note": "This bank account will receive platform payouts when your listed credits are purchased.",
        }


@app.post("/landowner/payout-bank-account")
def update_landowner_payout_bank_account(
    data: dict,
    request: Request,
    current_user: dict = Security(get_current_user),
):
    check_rate_limit(request)
    require_roles(current_user, ["land_owner"])
    user_id = current_user["user_id"]

    account_holder_name = str(data.get("account_holder_name", "")).strip()
    bank_name = str(data.get("bank_name", "")).strip()
    account_number = str(data.get("account_number", "")).strip().replace(" ", "")
    ifsc_code = str(data.get("ifsc_code", "")).strip().upper()
    branch_name = str(data.get("branch_name", "")).strip()

    validate_payout_bank_account(
        account_holder_name=account_holder_name,
        bank_name=bank_name,
        account_number=account_number,
        ifsc_code=ifsc_code,
    )
    if len(branch_name) > 120:
        raise HTTPException(status_code=400, detail="Branch name is too long.")

    with get_db() as db:
        user = db.query(models.User).filter_by(id=user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found.")

        user.payout_account_holder = account_holder_name
        user.payout_bank_name = bank_name
        user.payout_account_number = account_number
        user.payout_ifsc_code = ifsc_code
        user.payout_branch_name = branch_name or None
        user.payout_updated_at = datetime.utcnow()

        log_action(
            db,
            user_id,
            "update_payout_bank_account",
            {
                "bank_name": bank_name,
                "ifsc_code": ifsc_code,
                "account_number_masked": mask_bank_account_number(account_number),
            },
            request.client.host,
        )

        return {
            "success": True,
            "message": "Payout bank account updated successfully.",
            "is_configured": True,
            "account_number_masked": mask_bank_account_number(account_number),
            "updated_at": user.payout_updated_at,
        }


@app.post("/upload_document")
async def upload_document(
    request:  Request,
    file:     UploadFile = File(...),
    doc_type: str        = Form(...),
    project_id: int | None = Form(None),
    current_user: dict   = Security(get_current_user)
):
    check_rate_limit(request)
    user_id = current_user["user_id"]

    with get_db() as db:
        require_approved(db, user_id)
        user          = db.query(models.User).filter_by(id=user_id).first()
        allowed_types = get_doc_types_for_role(user.role)
        if doc_type not in allowed_types:
            raise HTTPException(
                status_code=400,
                detail=f"Doc type '{doc_type}' not allowed for role '{user.role}'. Allowed: {allowed_types}"
            )

        if project_id is not None:
            project = db.query(models.Project).filter_by(id=project_id).first()
            if not project:
                raise HTTPException(status_code=404, detail=f"Project {project_id} not found.")
            if current_user.get("role") not in ["admin", "auditor"] and project.user_id != user_id:
                raise HTTPException(status_code=403, detail="Cannot attach document to another user's project.")

        saved = await save_document(file, user_id, doc_type)
        doc   = models.Document(
            user_id   = user_id,
            project_id = project_id,
            file_path = saved["file_path"],
            file_name = saved["file_name"],
            file_type = saved["file_type"],
            doc_type  = doc_type,
            status    = "pending"
        )
        db.add(doc)
        db.flush()
        db.refresh(doc)
        doc_id = doc.id
        log_action(db, user_id, "upload_document", {"doc_type": doc_type}, request.client.host)

    return {
        "document_id": doc_id,
        "doc_type":    doc_type,
        "file_name":   saved["file_name"],
        "status":      "pending",
        "message":     "Document uploaded successfully. Awaiting review."
    }


@app.get("/documents/{user_id}")
def get_documents(user_id: int, request: Request, current_user: dict = Security(get_current_user)):
    check_rate_limit(request)
    # Own docs OR auditor/admin
    if current_user.get("role") not in ["auditor", "admin"]:
        if current_user.get("user_id") != user_id:
            raise HTTPException(status_code=403, detail="Access denied.")

    with get_db() as db:
        docs = db.query(models.Document).filter_by(user_id=user_id).all()
        return [{
            "document_id": d.id,
            "project_id": d.project_id,
            "project_name": (
                db.query(models.Project).get(d.project_id).project_name
                if d.project_id and db.query(models.Project).get(d.project_id)
                else None
            ),
            "doc_type":    d.doc_type,
            "file_name":   d.file_name,
            "file_type":   d.file_type,
            "status":      d.status,
            "review_note": d.review_note,
            "uploaded_at": d.uploaded_at,
            "reviewed_at": d.reviewed_at,
        } for d in docs]


@app.get("/documents/{document_id}/download")
def download_document(
    document_id: int,
    request: Request,
    current_user: dict = Security(get_current_user),
):
    """
    Secure document download/preview.

    Access rules:
    - admin: can access all documents
    - auditor: can access only land_owner + organization user documents (NOT auditor docs)
    - other users: can access only their own documents
    """
    check_rate_limit(request)

    with get_db() as db:
        doc = db.query(models.Document).filter_by(id=document_id).first()
        if not doc:
            raise HTTPException(status_code=404, detail="Document not found.")

        owner = db.query(models.User).filter_by(id=doc.user_id).first()
        if not owner:
            raise HTTPException(status_code=404, detail="Document owner not found.")

        role = current_user.get("role")
        requester_id = current_user.get("user_id")

        if role == "admin":
            pass
        elif role == "auditor":
            if owner.role not in ["land_owner", "organization"]:
                raise HTTPException(status_code=403, detail="Auditors cannot access auditor/admin documents.")
        else:
            if requester_id != doc.user_id:
                raise HTTPException(status_code=403, detail="Access denied.")

        if not doc.file_path or not os.path.exists(doc.file_path):
            raise HTTPException(status_code=404, detail="Document file missing.")

        # Let browser preview PDFs/images inline when possible
        return FileResponse(
            doc.file_path,
            filename=doc.file_name,
        )


# ════════════════════════════════════════════════════════
#  API KEYS
# ════════════════════════════════════════════════════════

@app.post("/api_keys/create")
def create_api_key(data: dict, request: Request, current_user: dict = Security(get_current_user)):
    check_rate_limit(request)
    if current_user["auth_type"] != "jwt":
        raise HTTPException(status_code=401, detail="Login with JWT to create API keys.")
    name = data.get("name", "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="'name' is required.")
    raw_key, hashed = generate_api_key()
    with get_db() as db:
        db.add(models.ApiKey(user_id=current_user["user_id"], key_hash=hashed, name=name))
        log_action(db, current_user["user_id"], "create_api_key", {"name": name}, request.client.host)
    return {"api_key": raw_key, "name": name, "warning": "Copy this key now — it will NOT be shown again."}


@app.get("/api_keys")
def list_api_keys(request: Request, current_user: dict = Security(get_current_user)):
    check_rate_limit(request)
    with get_db() as db:
        keys = db.query(models.ApiKey).filter_by(user_id=current_user["user_id"]).all()
        return [{"id": k.id, "name": k.name, "is_active": k.is_active, "created_at": k.created_at} for k in keys]


@app.delete("/api_keys/{key_id}")
def revoke_api_key(key_id: int, request: Request, current_user: dict = Security(get_current_user)):
    check_rate_limit(request)
    with get_db() as db:
        key = db.query(models.ApiKey).filter_by(id=key_id, user_id=current_user["user_id"]).first()
        if not key:
            raise HTTPException(status_code=404, detail="API key not found.")
        key.is_active = False
    return {"message": f"API key #{key_id} revoked."}


# ════════════════════════════════════════════════════════
#  PROJECTS  (land_owner only)
# ════════════════════════════════════════════════════════

@app.post("/register_project")
def register_project(data: dict, request: Request, current_user: dict = Security(get_current_user)):
    check_rate_limit(request)
    require_roles(current_user, ["land_owner", "admin"])

    user_id = current_user["user_id"]
    project_name = (data.get("project_name") or "").strip()
    if not project_name:
        project_name = f"Project-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"

    coords_input = data.get("coordinates")
    if isinstance(coords_input, str):
        coords = parse_coordinates_string(coords_input)
    else:
        coords = coords_input
        validate_coords(coords)

    with get_db() as db:
        require_approved(db, user_id)
        user = db.query(models.User).filter_by(id=user_id).first()

        # Ensure land ownership / lease documents exist and are approved
        land_doc = db.query(models.Document).filter(
            models.Document.user_id == user_id,
            models.Document.status == "approved",
            models.Document.doc_type.in_(["land_deed", "lease_agreement"])
        ).first()
        if not land_doc:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Land ownership or lease document not found or not approved. "
                    "Please upload and get approved a land_deed or lease_agreement document before registering a project."
                ),
            )

        result = create_project_pending(db, request, user, coords, project_name)

    return result


@app.post("/register_project_with_documents")
async def register_project_with_documents(
    request: Request,
    project_name: str = Form(""),
    coordinates: str = Form(""),
    project_doc_type: str = Form(""),
    project_documents: list[UploadFile] | None = File(None),
    current_user: dict = Security(get_current_user),
):
    check_rate_limit(request)
    require_roles(current_user, ["land_owner", "admin"])

    cleaned_name = (project_name or "").strip()
    if not cleaned_name:
        raise HTTPException(status_code=400, detail="'project_name' is required.")

    coords = parse_coordinates_string(coordinates)

    valid_files = [f for f in (project_documents or []) if getattr(f, "filename", "").strip()]
    if not valid_files:
        raise HTTPException(status_code=400, detail="At least one project document is required.")

    if not project_doc_type:
        raise HTTPException(status_code=400, detail="'project_doc_type' is required.")

    required_project_doc_types = ["land_deed", "lease_agreement"]
    if project_doc_type not in required_project_doc_types:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid project_doc_type '{project_doc_type}'. Allowed: {required_project_doc_types}",
        )

    user_id = current_user["user_id"]
    saved_files = []
    with get_db() as db:
        require_approved(db, user_id)
        user = db.query(models.User).filter_by(id=user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found.")

        # Save files first so file validation errors happen before any minting logic.
        for file in valid_files:
            saved_files.append(await save_document(file, user_id, project_doc_type))

        try:
            result = create_project_pending(db, request, user, coords, cleaned_name)
            new_project_id = result["project_id"]

            saved_doc_ids = []
            for saved in saved_files:
                doc = models.Document(
                    user_id=user_id,
                    project_id=new_project_id,
                    file_path=saved["file_path"],
                    file_name=saved["file_name"],
                    file_type=saved["file_type"],
                    doc_type=project_doc_type,
                    status="pending",
                )
                db.add(doc)
                db.flush()
                saved_doc_ids.append(doc.id)
        except Exception:
            for saved in saved_files:
                delete_document(saved.get("file_path"))
            raise

        log_action(
            db,
            user_id,
            "upload_project_documents",
            {
                "project_id": new_project_id,
                "doc_type": project_doc_type,
                "document_ids": saved_doc_ids,
            },
            request.client.host,
        )

    result["project_documents"] = {
        "count": len(saved_doc_ids),
        "doc_type": project_doc_type,
        "document_ids": saved_doc_ids,
    }
    return result


@app.post("/monitor_project")
def monitor_project(data: dict, request: Request, current_user: dict = Security(get_current_user)):
    check_rate_limit(request)
    require_roles(current_user, ["auditor", "admin"])

    project_id = data.get("project_id")
    if not project_id:
        raise HTTPException(status_code=400, detail="'project_id' is required.")

    with get_db() as db:
        project = db.query(models.Project).filter_by(id=project_id).first()
        if not project:
            raise HTTPException(status_code=404, detail=f"Project {project_id} not found.")
        if project.status not in ["approved", "verified"] or not project.is_minted:
            raise HTTPException(
                status_code=400,
                detail="Project must be approved by auditor and minted before monitoring can issue new tokens."
            )

        wallet = db.query(models.Wallet).filter_by(user_id=project.user_id).with_for_update().first()
        buffer = db.query(models.BufferPool).first()
        if not buffer:
            buffer = models.BufferPool(total_buffer_credits=0.0)
            db.add(buffer)
            db.flush()

        last_record = db.query(models.CarbonRecord).filter_by(project_id=project_id)\
            .order_by(models.CarbonRecord.id.desc()).first()
        if not last_record:
            raise HTTPException(status_code=404, detail="No baseline found.")

        polygon          = create_polygon(project.coordinates)
        area_ha          = calculate_area_hectares(polygon)
        features         = extract_features(polygon)
        prediction       = predict_carbon(features, area_ha)
        current_credits  = prediction["total_credits"]
        previous_credits = float(last_record.carbon_stock)
        new_credits      = current_credits - previous_credits

        user    = db.query(models.User).filter_by(id=project.user_id).first()
        u_email  = user.email          if user else None
        u_name   = user.name           if user else None
        u_wallet = user.wallet_address if user else None

        if new_credits < 0:
            penalty = abs(new_credits) * BUFFER_RATE
            buffer.total_buffer_credits = max(0.0, buffer.total_buffer_credits - penalty)
            db.add(models.CarbonRecord(
                project_id=project_id, carbon_stock=current_credits,
                carbon_credits_generated=0.0, buffer_credits_added=-penalty
            ))
            try:
                if u_email:
                    send_carbon_loss_alert(u_email, u_name, project_id, abs(new_credits), penalty)
            except Exception:
                pass
            result = {"grew": False, "current_credits": current_credits,
                      "previous_credits": previous_credits, "carbon_loss": abs(new_credits),
                      "buffer_penalty": penalty}
        elif new_credits == 0:
            db.add(models.CarbonRecord(
                project_id=project_id, carbon_stock=current_credits,
                carbon_credits_generated=0.0, buffer_credits_added=0.0
            ))
            result = {"grew": False, "current_credits": current_credits,
                      "previous_credits": previous_credits, "carbon_loss": 0, "buffer_penalty": 0}
        else:
            buffer_amount = new_credits * BUFFER_RATE
            user_amount   = new_credits - buffer_amount
            wallet.total_credits        += user_amount
            wallet.available_credits    += user_amount
            wallet.buffer_contributed   += buffer_amount
            buffer.total_buffer_credits += buffer_amount
            db.add(models.CarbonRecord(
                project_id=project_id, carbon_stock=current_credits,
                carbon_credits_generated=user_amount, buffer_credits_added=buffer_amount
            ))
            tx_hash = mint_error = None
            try:
                tx_hash = mint_tokens(u_wallet, user_amount)
            except Exception as e:
                mint_error = str(e)

            pdf_path = None
            try:
                pdf_path = generate_certificate(
                    project_id=project_id, user_id=project.user_id,
                    farmer_name=u_name, farmer_email=u_email,
                    wallet_address=u_wallet, area_ha=area_ha,
                    carbon_stock=current_credits, credits_issued=user_amount,
                    buffer_held=buffer_amount, blockchain_tx=tx_hash or "",
                    biomass_per_ha=prediction["biomass_per_ha"],
                    carbon_per_ha=prediction["carbon_per_ha"],
                    credits_per_ha=prediction["credits_per_ha"],
                )
                db.add(models.Certificate(
                    project_id=project_id, user_id=project.user_id,
                    credits_amount=user_amount, blockchain_tx=tx_hash, pdf_path=pdf_path
                ))
            except Exception as e:
                logger.error(f"PDF failed: {e}")

            try:
                if u_email:
                    send_credits_minted_email(
                        to_email=u_email, farmer_name=u_name,
                        credits_issued=user_amount, blockchain_tx=tx_hash or "",
                        project_id=project_id, area_ha=area_ha,
                        carbon_stock=current_credits, attachment_path=pdf_path
                    )
            except Exception:
                pass

            result = {
                "grew": True, "new_credits_issued": user_amount,
                "buffer_contributed": buffer_amount,
                "wallet_total": wallet.total_credits,
                "wallet_available": wallet.available_credits,
                "current_carbon_stock": current_credits,
                "blockchain_tx": tx_hash, "mint_error": mint_error,
                "certificate_generated": pdf_path is not None
            }

        log_action(db, current_user["user_id"], "monitor_project",
                   {"project_id": project_id, "new_credits": new_credits}, request.client.host)

    logger.info(f"Monitor project {project_id}: new_credits={new_credits:.2f}")
    if not result["grew"]:
        return {
            "message":        "No new credits — carbon stock unchanged or decreased.",
            "current_credits":  result["current_credits"],
            "previous_credits": result["previous_credits"],
            "carbon_loss":    result.get("carbon_loss", 0),
            "buffer_penalty": result.get("buffer_penalty", 0),
        }
    return result


# ════════════════════════════════════════════════════════
#  LAND VERIFICATION  (auditor / admin)
# ════════════════════════════════════════════════════════

@app.post("/verify_land/{project_id}")
def verify_land(project_id: int, data: dict, request: Request, current_user: dict = Security(get_current_user)):
    check_rate_limit(request)
    require_roles(current_user, ["auditor", "admin"])
    land_doc_url = data.get("land_doc_url", "").strip()
    verified     = data.get("verified", False)

    with get_db() as db:
        project = db.query(models.Project).filter_by(id=project_id).first()
        if not project:
            raise HTTPException(status_code=404, detail=f"Project {project_id} not found.")
        project.land_doc_url = land_doc_url

    action = "approve" if verified else "reject"
    return review_project(project_id, {"action": action, "review_note": "Reviewed via verify_land endpoint"}, request, current_user)


@app.post("/projects/{project_id}/review")
def review_project(
    project_id: int,
    data: dict,
    request: Request,
    current_user: dict = Security(get_current_user),
):
    check_rate_limit(request)
    require_roles(current_user, ["auditor", "admin"])

    action = (data.get("action") or "").strip().lower()
    review_note = (data.get("review_note") or "").strip()
    if action not in ["approve", "reject"]:
        raise HTTPException(status_code=400, detail="Action must be 'approve' or 'reject'.")

    with get_db() as db:
        project = db.query(models.Project).filter_by(id=project_id).first()
        if not project:
            raise HTTPException(status_code=404, detail=f"Project {project_id} not found.")

        if action == "reject":
            if project.is_minted:
                raise HTTPException(status_code=409, detail="Cannot reject an already minted project.")

            project.status = "rejected"
            project.land_verified = False
            project.approved_by = current_user["user_id"]
            project.approved_at = datetime.utcnow()

            log_action(
                db,
                current_user["user_id"],
                "project_rejected",
                {"project_id": project_id, "review_note": review_note},
                request.client.host,
            )
            return {
                "project_id": project_id,
                "status": "rejected",
                "message": "Project rejected. No tokens minted.",
            }

        # approve path
        if current_user.get("role") != "auditor":
            raise HTTPException(status_code=403, detail="Only auditors can approve projects for minting.")

        if project.is_minted:
            raise HTTPException(status_code=409, detail="Project already approved and minted.")

        try:
            mint_result = mint_project_tokens_on_approval(db, request, project, current_user)
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Blockchain RPC error during minting — please retry. ({exc})"
            )
        mint_result["message"] = "Project approved and tokens minted successfully."
        return mint_result


# ════════════════════════════════════════════════════════
#  MARKETPLACE
# ════════════════════════════════════════════════════════

@app.post("/marketplace/list")
def list_for_sale(data: dict, request: Request, current_user: dict = Security(get_current_user)):
    check_rate_limit(request)
    require_roles(current_user, ["land_owner", "organization", "admin"])

    seller_id        = current_user["user_id"]
    project_id       = data.get("project_id")
    credits_amount   = data.get("credits_amount")
    price_per_credit = data.get("price_per_credit")

    if not all([project_id, credits_amount, price_per_credit]):
        raise HTTPException(
            status_code=400,
            detail="'project_id', 'credits_amount', 'price_per_credit' required."
        )
    
    # Convert to float
    try:
        credits_amount = float(credits_amount)
        price_per_credit = float(price_per_credit)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Credits and price must be numbers.")
    
    if credits_amount <= 0 or price_per_credit <= 0:
        raise HTTPException(status_code=400, detail="Amount and price must be > 0.")

    with get_db() as db:
        require_approved(db, seller_id)
        
        # ✅ NEW: Check if user is verified
        user = db.query(models.User).filter_by(id=seller_id).first()
        if not user.is_verified:
            raise HTTPException(
                status_code=403, 
                detail="You must be verified by an admin or auditor to list credits."
            )

        # Verify seller owns this project
        project = db.query(models.Project).filter_by(
            id=project_id, user_id=seller_id
        ).first()
        if not project:
            raise HTTPException(status_code=404, detail="Project not found or not yours.")
        if project.status not in ["approved", "verified"] or not project.is_minted:
            raise HTTPException(
                status_code=400,
                detail="Project is not auditor-approved for minting yet."
            )

        # ✅ IMPROVED: Better error message for insufficient credits
        wallet = db.query(models.Wallet).filter_by(user_id=seller_id).first()
        if not wallet or wallet.available_credits < credits_amount:
            available = wallet.available_credits if wallet else 0
            shortage = max(0, credits_amount - available)
            raise HTTPException(
                status_code=400,
                detail=f"Insufficient credits. You have {available:.2f} CCT available, but trying to list {credits_amount:.2f} CCT. Please enter an amount equal to or less than your available credits. You need {shortage:.2f} more CCT."
            )

        # Create listing — status pending_deposit, no credits deducted
        listing = models.MarketplaceListing(
            seller_id        = seller_id,
            project_id       = project_id,
            credits_amount   = credits_amount,
            price_per_credit = price_per_credit,
            status           = "pending_deposit",
        )
        db.add(listing)
        db.flush()
        db.refresh(listing)
        listing_id  = listing.id
        total_value = credits_amount * price_per_credit

        log_action(db, seller_id, "marketplace_list_initiated",
                   {"listing_id": listing_id, "credits": credits_amount,
                    "status": "pending_deposit", "user_verified": user.is_verified}, request.client.host)

    return {
        "listing_id":            listing_id,
        "status":                "pending_deposit",
        "credits_amount":        credits_amount,
        "price_per_credit":      price_per_credit,
        "total_value_usd":       total_value,
        "escrow_wallet_address": PLATFORM_WALLET,
        "next_step": (
            f"Transfer exactly {credits_amount} CCT tokens to "
            f"{PLATFORM_WALLET} from your MetaMask wallet, "
            f"then call POST /marketplace/submit_deposit with "
            f"your listing_id and the transaction hash."
        )
    }


@app.post("/marketplace/submit_deposit")
def submit_deposit(data: dict, request: Request, current_user: dict = Security(get_current_user)):
    """
    Seller submits blockchain tx_hash after transferring CCT tokens
    to the platform escrow wallet.
    Status: pending_deposit → pending_approval
    """
    check_rate_limit(request)
    require_roles(current_user, ["land_owner", "organization", "admin"])

    seller_id  = current_user["user_id"]
    listing_id = data.get("listing_id")
    tx_hash    = data.get("tx_hash", "").strip()

    if not listing_id:
        raise HTTPException(status_code=400, detail="'listing_id' is required.")
    if not tx_hash or not tx_hash.startswith("0x"):
        raise HTTPException(
            status_code=400,
            detail="Valid 'tx_hash' starting with '0x' is required."
        )

    with get_db() as db:
        require_approved(db, seller_id)

        listing = db.query(models.MarketplaceListing).filter_by(
            id        = listing_id,
            seller_id = seller_id,
            status    = "pending_deposit"
        ).first()

        if not listing:
            raise HTTPException(
                status_code=404,
                detail="Listing not found, not yours, or not in 'pending_deposit' state."
            )

        # Prevent duplicate tx_hash
        duplicate = db.query(models.MarketplaceListing).filter_by(payment_tx=tx_hash).first()
        if duplicate:
            raise HTTPException(
                status_code=409,
                detail="This transaction hash has already been submitted."
            )

        # Lock credits in seller wallet — deduct available, keep total
        wallet = db.query(models.Wallet).filter_by(
            user_id=seller_id
        ).with_for_update().first()

        if not wallet or wallet.available_credits < listing.credits_amount:
            raise HTTPException(
                status_code=400,
                detail=f"Insufficient credits. Available: {wallet.available_credits:.2f if wallet else 0}"
            )

        wallet.available_credits -= listing.credits_amount
        listing.status            = "pending_approval"
        listing.payment_tx        = tx_hash

        log_action(db, seller_id, "marketplace_deposit_submitted",
                   {"listing_id": listing_id, "tx_hash": tx_hash,
                    "status": "pending_approval"}, request.client.host)

    return {
        "listing_id": listing_id,
        "status":     "pending_approval",
        "tx_hash":    tx_hash,
        "message":    (
            "Deposit submitted successfully. "
            "An auditor will verify your transaction and activate the listing."
        )
    }


@app.post("/marketplace/approve_listing/{listing_id}")
def approve_listing(
    listing_id: int,
    data:         dict,
    request:      Request,
    current_user: dict = Security(get_current_user)
):
    """
    Auditor verifies the on-chain deposit transaction before activating listing.

    Flow:
        approved=False → manual reject    → credits refunded immediately
        approved=True  → verify on-chain
                             PASSED → listing.status = "active"
                             FAILED → listing.status = "rejected" + credits refunded

    Status transitions:
        pending_approval → active    (on-chain verified, approved)
        pending_approval → rejected  (invalid tx OR manually rejected)
    """
    check_rate_limit(request)
    require_roles(current_user, ["auditor", "admin"])

    approved    = data.get("approved", True)
    reject_note = data.get("reject_note", "").strip()

    with get_db() as db:

        # ── Fetch listing ─────────────────────────────────────
        listing = db.query(models.MarketplaceListing).filter_by(
            id=listing_id, status="pending_approval"
        ).first()

        if not listing:
            raise HTTPException(
                status_code=404,
                detail="Listing not found or not in 'pending_approval' state."
            )

        # ── Fetch seller for wallet address ───────────────────
        seller = db.query(models.User).filter_by(id=listing.seller_id).first()
        if not seller:
            raise HTTPException(status_code=404, detail="Seller not found.")

        seller_wallet = db.query(models.Wallet).filter_by(
            user_id=listing.seller_id
        ).with_for_update().first()

        # ── MANUAL REJECTION (approved=False) ─────────────────
        if not approved:
            listing.status = "rejected"
            if seller_wallet:
                seller_wallet.available_credits += listing.credits_amount

            message = (
                f"Listing manually rejected by auditor. "
                f"Credits refunded to seller. "
                f"Reason: {reject_note or 'No reason provided.'}"
            )
            log_action(
                db, current_user["user_id"],
                "marketplace_listing_manually_rejected",
                {
                    "listing_id":       listing_id,
                    "reject_note":      reject_note,
                    "credits_refunded": listing.credits_amount
                },
                request.client.host
            )
            return {
                "listing_id":  listing_id,
                "status":      "rejected",
                "approved_by": current_user["user_id"],
                "message":     message
            }

        # ── ON-CHAIN VERIFICATION (approved=True) ─────────────
        tx_hash = listing.payment_tx

        if not tx_hash or not tx_hash.startswith("0x"):
            listing.status = "rejected"
            if seller_wallet:
                seller_wallet.available_credits += listing.credits_amount
            log_action(
                db, current_user["user_id"],
                "marketplace_listing_rejected_no_tx",
                {"listing_id": listing_id, "reason": "No valid tx_hash on record"},
                request.client.host
            )
            raise HTTPException(
                status_code=400,
                detail=(
                    "Listing rejected: No valid transaction hash on record. "
                    "Seller must re-list and resubmit deposit."
                )
            )

        # Run all 7 on-chain checks
        verification_error = None
        try:
            verify_deposit_transaction(
                tx_hash         = tx_hash,
                expected_from   = seller.wallet_address,
                expected_to     = PLATFORM_WALLET,
                expected_amount = listing.credits_amount
            )
        except Exception as e:
            verification_error = str(e)

        # ── VERIFICATION FAILED → auto-reject ─────────────────
        if verification_error:
            listing.status = "rejected"
            if seller_wallet:
                seller_wallet.available_credits += listing.credits_amount

            log_action(
                db, current_user["user_id"],
                "marketplace_listing_rejected_invalid_tx",
                {
                    "listing_id":         listing_id,
                    "tx_hash":            tx_hash,
                    "verification_error": verification_error,
                    "credits_refunded":   listing.credits_amount
                },
                request.client.host
            )
            raise HTTPException(
                status_code=422,
                detail={
                    "listing_id":       listing_id,
                    "status":           "rejected",
                    "verification":     "FAILED",
                    "error":            verification_error,
                    "credits_refunded": listing.credits_amount,
                    "message": (
                        "On-chain verification failed. Listing auto-rejected. "
                        "Seller credits refunded. "
                        "Seller must re-list and resubmit a valid deposit."
                    )
                }
            )

        # ── VERIFICATION PASSED → activate listing ────────────
        listing.status = "active"
        log_action(
            db, current_user["user_id"],
            "marketplace_listing_approved_verified",
            {
                "listing_id":     listing_id,
                "tx_hash":        tx_hash,
                "credits_amount": listing.credits_amount,
                "seller_wallet":  seller.wallet_address,
                "verified":       True
            },
            request.client.host
        )

    return {
        "listing_id":     listing_id,
        "status":         "active",
        "approved_by":    current_user["user_id"],
        "tx_hash":        tx_hash,
        "verification":   "PASSED",
        "credits_amount": listing.credits_amount,
        "message": (
            "On-chain deposit verified successfully. "
            "Listing is now active on the marketplace."
        )
    }


@app.get("/marketplace")
def view_marketplace(request: Request):
    check_rate_limit(request)
    with get_db() as db:
        listings = db.query(models.MarketplaceListing)\
            .filter_by(status="active")\
            .order_by(models.MarketplaceListing.price_per_credit.asc()).all()
        return [{
            "listing_id":       l.id,
            "seller_id":        l.seller_id,
            "project_id":       l.project_id,
            "credits_amount":   l.credits_amount,
            "price_per_credit": l.price_per_credit,
            "total_value_usd":  l.credits_amount * l.price_per_credit,
            "platform_fee":     l.platform_fee,
            "source":           getattr(l, "source", "manual"),
            "listed_at":        l.created_at,
        } for l in listings]


# ════════════════════════════════════════════════════════
#  CCT AUTO-PAYMENT — Shared listing views (all dashboards)
# ════════════════════════════════════════════════════════

@app.post("/marketplace/{listing_id}/update_price")
def update_listing_price(
    listing_id: int,
    data: dict,
    request: Request,
    current_user: dict = Security(get_current_user),
):
    """Allow a land_owner to update the price_per_credit on their auto-listing."""
    check_rate_limit(request)
    price = data.get("price_per_credit")
    if price is None or float(price) <= 0:
        raise HTTPException(status_code=400, detail="price_per_credit must be positive.")

    with get_db() as db:
        listing = db.query(models.MarketplaceListing).filter_by(id=listing_id).first()
        if not listing:
            raise HTTPException(status_code=404, detail="Listing not found.")
        # Only seller or admin can update
        if current_user["role"] == "land_owner" and listing.seller_id != current_user["user_id"]:
            raise HTTPException(status_code=403, detail="Not your listing.")
        listing.price_per_credit = float(price)
        db.commit()
        return {"success": True, "listing_id": listing_id, "price_per_credit": float(price)}


@app.get("/marketplace/auto_listings")
def view_auto_listings(
    request: Request,
    current_user: dict = Security(get_current_user),
):
    """
    Returns all marketplace listings created automatically via CCT transfer detection.
    Visible to: land_owner (own listings), auditor, admin.
    """
    check_rate_limit(request)
    role      = current_user["role"]
    caller_id = current_user["user_id"]

    with get_db() as db:
        query = db.query(models.MarketplaceListing).filter(
            models.MarketplaceListing.source == "auto_cct_payment"
        )

        # Land owners only see their own auto-listings
        if role == "land_owner":
            query = query.filter(models.MarketplaceListing.seller_id == caller_id)
        elif role not in ("auditor", "admin"):
            raise HTTPException(status_code=403, detail="Access denied.")

        listings = query.order_by(models.MarketplaceListing.created_at.desc()).all()

        result = []
        for l in listings:
            seller = db.query(models.User).filter_by(id=l.seller_id).first()
            result.append({
                "listing_id":       l.id,
                "seller_id":        l.seller_id,
                "seller_name":      seller.name if seller else None,
                "seller_wallet":    seller.wallet_address if seller else None,
                "project_id":       l.project_id,
                "credits_amount":   l.credits_amount,
                "price_per_credit": l.price_per_credit,
                "total_value_usd":  l.credits_amount * l.price_per_credit,
                "platform_fee":     l.platform_fee,
                "source":           l.source,
                "payment_tx":       l.payment_tx,
                "status":           l.status,
                "listed_at":        l.created_at,
            })
        return result


@app.get("/marketplace/processed_transactions")
def view_processed_transactions(
    request: Request,
    current_user: dict = Security(require_auditor_or_admin),
):
    """
    Returns the processed_transactions ledger for CCT auto-payment detection.
    Accessible by auditor and admin only.
    """
    check_rate_limit(request)
    with get_db() as db:
        rows = (
            db.query(models.ProcessedTransaction)
            .order_by(models.ProcessedTransaction.created_at.desc())
            .limit(500)
            .all()
        )
        return [{
            "id":            r.id,
            "tx_hash":       r.tx_hash,
            "sender_wallet": r.sender_wallet,
            "amount":        r.amount,
            "fee":           r.fee,
            "listing_id":    r.listing_id,
            "note":          r.note,
            "processed_at":  r.created_at,
        } for r in rows]



@app.post("/buy_credits")
def buy_credits(data: dict, request: Request, current_user: dict = Security(get_current_user)):
    check_rate_limit(request)
    require_roles(current_user, ["organization", "admin"])

    buyer_id       = current_user["user_id"]
    listing_id     = data.get("listing_id")
    payment_method = data.get("payment_method", "metamask")
    payment_tx     = data.get("payment_tx", "")

    if not listing_id:
        raise HTTPException(status_code=400, detail="'listing_id' required.")

    with get_db() as db:
        require_approved(db, buyer_id)

        # Only ACTIVE (auditor-approved) listings can be purchased
        listing = db.query(models.MarketplaceListing).filter_by(
            id=listing_id, status="active"
        ).with_for_update().first()

        if not listing:
            raise HTTPException(
                status_code=404,
                detail="Listing not found, not active, or not yet approved by auditor."
            )
        if listing.seller_id == buyer_id:
            raise HTTPException(status_code=400, detail="Cannot buy your own listing.")

        buyer_wallet  = db.query(models.Wallet).filter_by(user_id=buyer_id).with_for_update().first()
        seller_wallet = db.query(models.Wallet).filter_by(user_id=listing.seller_id).with_for_update().first()
        buyer_user    = db.query(models.User).filter_by(id=buyer_id).first()

        if not buyer_wallet:
            raise HTTPException(status_code=404, detail="Buyer wallet not found.")

        # ✅ BLOCKCHAIN FIRST — transfer from escrow wallet to buyer
        # Tokens already deposited by seller into platform escrow wallet
        tx_hash = None
        try:
            from blockchain_service import transfer_tokens
            tx_hash = transfer_tokens(
                from_address = PLATFORM_WALLET,
                to_address   = buyer_user.wallet_address,
                amount       = listing.credits_amount
            )
        except Exception as e:
            raise HTTPException(
                status_code=500,
                detail=f"Blockchain transfer from escrow failed: {str(e)}"
            )

        # ✅ DB UPDATE only after successful blockchain transfer
        # Seller: deduct total_credits (available already deducted at submit_deposit)
        seller_wallet.total_credits    -= listing.credits_amount

        # Buyer: credit their wallet
        buyer_wallet.total_credits     += listing.credits_amount
        buyer_wallet.available_credits += listing.credits_amount

        listing.status         = "sold"
        listing.buyer_id       = buyer_id
        listing.sold_at        = datetime.utcnow()
        listing.payment_method = payment_method
        listing.payment_tx     = payment_tx

        db.add(models.CreditTransfer(
            from_user_id = listing.seller_id,
            to_user_id   = buyer_id,
            amount       = listing.credits_amount,
            blockchain_tx= tx_hash,
            note         = f"Marketplace escrow purchase #{listing_id}"
        ))

        credits_bought   = listing.credits_amount
        price_per_credit = listing.price_per_credit
        total_cost       = credits_bought * price_per_credit

        log_action(db, buyer_id, "marketplace_buy_escrow",
                   {"listing_id": listing_id, "tx_hash": tx_hash,
                    "credits": credits_bought}, request.client.host)

    return {
        "listing_id":       listing_id,
        "credits_bought":   credits_bought,
        "price_per_credit": price_per_credit,
        "total_cost_usd":   total_cost,
        "payment_method":   payment_method,
        "blockchain_tx":    tx_hash,
        "message":          "Credits transferred from escrow to your wallet successfully."
    }


# ════════════════════════════════════════════════════════
#  CREDIT RETIREMENT  (organization only)
# ════════════════════════════════════════════════════════

@app.post("/retire_credits")
def retire_credits(data: dict, request: Request, current_user: dict = Security(get_current_user)):
    check_rate_limit(request)
    require_roles(current_user, ["organization", "admin"])
    user_id = current_user["user_id"]
    amount  = data.get("amount")
    reason  = data.get("reason", "Carbon offset / ESG compliance")

    if not amount or amount <= 0:
        raise HTTPException(status_code=400, detail="'amount' must be > 0.")

    with get_db() as db:
        require_approved(db, user_id)
        user   = db.query(models.User).filter_by(id=user_id).first()
        wallet = db.query(models.Wallet).filter_by(user_id=user_id).with_for_update().first()
        if not wallet:
            raise HTTPException(status_code=404, detail="Wallet not found.")
        if wallet.available_credits < amount:
            raise HTTPException(
                status_code=400,
                detail=f"Insufficient credits. Available: {wallet.available_credits:.2f}"
            )

        # Deduct from wallet
        wallet.available_credits -= amount
        wallet.total_credits     -= amount
        wallet.retired_credits   += amount

        # Create retirement record first to get ID
        retirement = models.CreditRetirement(
            user_id       = user_id,
            amount        = amount,
            retirement_id = "PENDING",
            reason        = reason,
        )
        db.add(retirement)
        db.flush()
        db.refresh(retirement)

        # Generate proper retirement ID
        ret_id             = generate_retirement_id(retirement.id)
        retirement.retirement_id = ret_id

        # Blockchain burn (mint to zero address as burn simulation)
        tx_hash = mint_error = None
        try:
            BURN_ADDRESS = "0x000000000000000000000000000000000000dEaD"
            tx_hash = mint_tokens(BURN_ADDRESS, amount)
            retirement.blockchain_tx = tx_hash
        except Exception as e:
            mint_error = str(e)

        # Generate PDF certificate
        pdf_path = None
        try:
            pdf_path = generate_retirement_certificate(
                retirement_id    = ret_id,
                retirement_db_id = retirement.id,
                company_name     = user.name,
                company_email    = user.email,
                wallet_address   = user.wallet_address,
                amount_retired   = amount,
                reason           = reason,
                blockchain_tx    = tx_hash or "",
                retired_at       = retirement.retired_at,
            )
            retirement.pdf_path = pdf_path
        except Exception as e:
            logger.error(f"Retirement PDF failed: {e}")

        log_action(db, user_id, "retire_credits",
                   {"amount": amount, "retirement_id": ret_id}, request.client.host)

    logger.info(f"Retirement {ret_id}: {amount:.2f} CCT by user {user_id}")
    return {
        "retirement_id":         ret_id,
        "amount_retired":        amount,
        "co2_equivalent_tons":   amount / 3.67,
        "reason":                reason,
        "blockchain_tx":         tx_hash,
        "mint_error":            mint_error,
        "certificate_generated": pdf_path is not None,
        "message":               "Credits permanently retired. Certificate generated for compliance."
    }


@app.get("/retirements/{user_id}")
def get_retirements(user_id: int, request: Request, current_user: dict = Security(get_current_user)):
    check_rate_limit(request)
    if current_user.get("role") not in ["auditor", "admin"]:
        if current_user.get("user_id") != user_id:
            raise HTTPException(status_code=403, detail="Access denied.")

    with get_db() as db:
        retirements = db.query(models.CreditRetirement).filter_by(user_id=user_id)\
            .order_by(models.CreditRetirement.retired_at.desc()).all()
        return [{
            "retirement_id":       r.retirement_id,
            "amount":              r.amount,
            "co2_equivalent_tons": r.amount / 3.67,
            "reason":              r.reason,
            "blockchain_tx":       r.blockchain_tx,
            "certificate":         r.pdf_path is not None,
            "retired_at":          r.retired_at,
        } for r in retirements]


@app.get("/retirement_certificate/{retirement_id}")
def download_retirement_certificate(retirement_id: str, request: Request, current_user: dict = Security(get_current_user)):
    check_rate_limit(request)
    with get_db() as db:
        ret = db.query(models.CreditRetirement).filter_by(retirement_id=retirement_id).first()
        if not ret:
            raise HTTPException(status_code=404, detail="Retirement not found.")
        if current_user.get("role") not in ["auditor", "admin"]:
            if current_user.get("user_id") != ret.user_id:
                raise HTTPException(status_code=403, detail="Access denied.")
        if not ret.pdf_path or not os.path.exists(ret.pdf_path):
            raise HTTPException(status_code=404, detail="Certificate file missing.")
        pdf_path = ret.pdf_path

    return FileResponse(pdf_path, media_type="application/pdf",
                        filename=os.path.basename(pdf_path))


# ════════════════════════════════════════════════════════
#  TRANSFERS
# ════════════════════════════════════════════════════════

@app.post("/withdraw_to_wallet")
def withdraw_to_wallet(data: dict, request: Request, current_user: dict = Security(get_current_user)):
    """
    Withdraw CCT credits from the user's virtual vault to their registered
    blockchain wallet address.  This is a vault-debit + on-chain mint operation,
    NOT a peer-to-peer credit transfer (hence no self-transfer restriction).
    """
    check_rate_limit(request)
    require_roles(current_user, ["organization", "land_owner", "admin"])
    user_id = current_user["user_id"]
    amount  = data.get("amount")
    note    = data.get("note", "Vault withdrawal to registered wallet")

    if not amount or amount <= 0:
        raise HTTPException(status_code=400, detail="'amount' must be a positive number.")

    with get_db() as db:
        require_approved(db, user_id)
        user   = db.query(models.User).filter_by(id=user_id).first()
        wallet = db.query(models.Wallet).filter_by(user_id=user_id).with_for_update().first()

        if not wallet:
            raise HTTPException(status_code=404, detail="Wallet not found.")
        if not user or not user.wallet_address:
            raise HTTPException(status_code=400, detail="No registered blockchain wallet address on your account.")
        if wallet.available_credits < amount:
            raise HTTPException(
                status_code=400,
                detail=f"Insufficient credits. Available: {wallet.available_credits:.2f}"
            )

        # Deduct from virtual vault
        wallet.available_credits -= amount
        wallet.total_credits     -= amount

        # Send on-chain to the user's registered wallet address
        tx_hash = mint_error = None
        try:
            tx_hash = mint_tokens(user.wallet_address, amount)
        except Exception as e:
            mint_error = str(e)

        # Record the transaction (self-transfer: from == to for audit trail)
        db.add(models.CreditTransfer(
            from_user_id=user_id,
            to_user_id=user_id,
            amount=amount,
            blockchain_tx=tx_hash,
            note=note,
        ))
        log_action(db, user_id, "withdraw_to_wallet",
                   {"amount": amount, "to_address": user.wallet_address, "tx_hash": tx_hash},
                   request.client.host)

        available_now = wallet.available_credits

    return {
        "withdrawn":       amount,
        "to_address":      user.wallet_address,
        "available_now":   available_now,
        "blockchain_tx":   tx_hash,
        "mint_error":      mint_error,
        "note":            note,
    }


@app.post("/transfer_credits")
def transfer_credits(data: dict, request: Request, current_user: dict = Security(get_current_user)):
    check_rate_limit(request)
    require_roles(current_user, ["land_owner", "organization", "admin"])
    from_user_id = current_user["user_id"]
    to_user_id   = data.get("to_user_id")
    amount       = data.get("amount")
    note         = data.get("note", "")

    if not to_user_id or not amount:
        raise HTTPException(status_code=400, detail="'to_user_id' and 'amount' are required.")
    if amount <= 0:
        raise HTTPException(status_code=400, detail="'amount' must be > 0.")
    if from_user_id == to_user_id:
        raise HTTPException(status_code=400, detail="Cannot transfer to yourself.")

    with get_db() as db:
        require_approved(db, from_user_id)
        from_wallet = db.query(models.Wallet).filter_by(user_id=from_user_id).with_for_update().first()
        to_wallet   = db.query(models.Wallet).filter_by(user_id=to_user_id).with_for_update().first()
        to_user     = db.query(models.User).filter_by(id=to_user_id).first()
        if not from_wallet:
            raise HTTPException(status_code=404, detail="Sender wallet not found.")
        if not to_wallet or not to_user:
            raise HTTPException(status_code=404, detail="Receiver not found.")
        if from_wallet.available_credits < amount:
            raise HTTPException(status_code=400, detail=f"Insufficient credits. Available: {from_wallet.available_credits:.2f}")

        from_wallet.total_credits     -= amount
        from_wallet.available_credits -= amount
        to_wallet.total_credits       += amount
        to_wallet.available_credits   += amount

        tx_hash = mint_error = None
        try:
            tx_hash = mint_tokens(to_user.wallet_address, amount)
        except Exception as e:
            mint_error = str(e)

        db.add(models.CreditTransfer(
            from_user_id=from_user_id, to_user_id=to_user_id,
            amount=amount, blockchain_tx=tx_hash, note=note
        ))
        from_avail = from_wallet.available_credits
        to_avail   = to_wallet.available_credits
        log_action(db, from_user_id, "transfer_credits",
                   {"to": to_user_id, "amount": amount}, request.client.host)

    return {
        "transferred":       amount,
        "from_user_id":      from_user_id,
        "to_user_id":        to_user_id,
        "from_available_now": from_avail,
        "to_available_now":  to_avail,
        "blockchain_tx":     tx_hash,
        "mint_error":        mint_error,
        "note":              note
    }


# ════════════════════════════════════════════════════════
#  WALLET & HISTORY
# ════════════════════════════════════════════════════════

@app.get("/wallet/{user_id}")
def wallet_status(user_id: int, request: Request, current_user: dict = Security(get_current_user)):
    check_rate_limit(request)
    if current_user.get("role") not in ["auditor", "admin"]:
        if current_user.get("user_id") != user_id:
            raise HTTPException(status_code=403, detail="Access denied.")

    with get_db() as db:
        wallet = db.query(models.Wallet).filter_by(user_id=user_id).first()
        if not wallet:
            raise HTTPException(status_code=404, detail=f"Wallet for user {user_id} not found.")
        return {
            "user_id":           user_id,
            "total_credits":     wallet.total_credits,
            "available_credits": wallet.available_credits,
            "retired_credits":   wallet.retired_credits,
            "buffer_contributed": wallet.buffer_contributed,
            "status":            wallet.status
        }


@app.get("/projects/{user_id}")
def list_projects(user_id: int, request: Request, current_user: dict = Security(get_current_user)):
    check_rate_limit(request)
    if current_user.get("role") not in ["auditor", "admin"]:
        if current_user.get("user_id") != user_id:
            raise HTTPException(status_code=403, detail="Access denied.")

    with get_db() as db:
        if not db.query(models.User).filter_by(id=user_id).first():
            raise HTTPException(status_code=404, detail=f"User {user_id} not found.")
        projects = db.query(models.Project).filter_by(user_id=user_id)\
            .order_by(models.Project.id.asc()).all()
        return [{
            "id":              p.id,
            "project_id":      p.id,
            "project_name":    p.project_name or f"Project #{p.id}",
            "area_hectares":   p.area_hectares,
            "baseline_carbon": p.baseline_carbon,
            "coordinates":     p.coordinates,
            "coordinates_text": coords_to_string(p.coordinates) if p.coordinates else None,
            "land_verified":   p.land_verified,
            "is_minted":       bool(p.is_minted),
            "status":          p.status,
            "created_at":      p.created_at
        } for p in projects]


@app.get("/project_history/{project_id}")
def project_history(project_id: int, request: Request, current_user: dict = Security(get_current_user)):
    check_rate_limit(request)
    with get_db() as db:
        project = db.query(models.Project).filter_by(id=project_id).first()
        if not project:
            raise HTTPException(status_code=404, detail=f"Project {project_id} not found.")
        if current_user.get("role") not in ["auditor", "admin"]:
            if current_user.get("user_id") != project.user_id:
                raise HTTPException(status_code=403, detail="Access denied.")
        records = db.query(models.CarbonRecord).filter_by(project_id=project_id)\
            .order_by(models.CarbonRecord.measured_at.asc()).all()
        history = [{
            "record_number":    i + 1,
            "carbon_stock":     r.carbon_stock,
            "credits_generated": r.carbon_credits_generated,
            "buffer_added":     r.buffer_credits_added,
            "measured_at":      r.measured_at,
            "is_baseline":      i == 0
        } for i, r in enumerate(records)]
        return {"project_id": project_id, "total_records": len(history), "history": history}


@app.get("/transfers/{user_id}")
def transfer_history(user_id: int, request: Request, current_user: dict = Security(get_current_user)):
    check_rate_limit(request)
    if current_user.get("role") not in ["auditor", "admin"]:
        if current_user.get("user_id") != user_id:
            raise HTTPException(status_code=403, detail="Access denied.")

    with get_db() as db:
        transfers = db.query(models.CreditTransfer).filter(
            (models.CreditTransfer.from_user_id == user_id) |
            (models.CreditTransfer.to_user_id == user_id)
        ).order_by(models.CreditTransfer.transferred_at.desc()).all()
        return [{
            "transfer_id":   t.id,
            "from_user_id":  t.from_user_id,
            "to_user_id":    t.to_user_id,
            "amount":        t.amount,
            "direction":     "sent" if t.from_user_id == user_id else "received",
            "blockchain_tx": t.blockchain_tx,
            "note":          t.note,
            "transferred_at": t.transferred_at
        } for t in transfers]


# ════════════════════════════════════════════════════════
#  CERTIFICATES
# ════════════════════════════════════════════════════════

@app.get("/certificate/{project_id}")
def download_certificate(project_id: int, request: Request, current_user: dict = Security(get_current_user)):
    check_rate_limit(request)
    with get_db() as db:
        project = db.query(models.Project).filter_by(id=project_id).first()
        if not project:
            raise HTTPException(status_code=404, detail="Project not found.")
        if current_user.get("role") not in ["auditor", "admin"]:
            if current_user.get("user_id") != project.user_id:
                raise HTTPException(status_code=403, detail="Access denied.")
        cert = db.query(models.Certificate).filter_by(project_id=project_id)\
            .order_by(models.Certificate.issued_at.desc()).first()
        if not cert or not cert.pdf_path or not os.path.exists(cert.pdf_path):
            raise HTTPException(status_code=404, detail="Certificate not found.")
        pdf_path = cert.pdf_path

    return FileResponse(pdf_path, media_type="application/pdf",
                        filename=os.path.basename(pdf_path))


# ════════════════════════════════════════════════════════
#  BUFFER POOL
# ════════════════════════════════════════════════════════

@app.get("/buffer_pool")
def buffer_status(request: Request):
    check_rate_limit(request)
    with get_db() as db:
        buffer = db.query(models.BufferPool).first()
        total  = buffer.total_buffer_credits if buffer else 0.0
        return {
            "total_buffer_credits": total,
            "buffer_rate_percent":  BUFFER_RATE * 100,
            "note": "Buffer credits held as insurance against carbon reversals."
        }


# ════════════════════════════════════════════════════════
#  LANDOWNER DASHBOARD  (single endpoint — all sections)
# ════════════════════════════════════════════════════════

@app.get("/dashboard/landowner")
def landowner_dashboard(
    request: Request,
    current_user: dict = Security(get_current_user),
):
    """
    Unified Landowner Dashboard.

    Returns everything the land-owner needs in a single call:
      - profile & verification status
      - wallet / credit balances
      - projects list
      - marketplace listings (manual + auto CCT-payment)
      - CCT payment history (incoming transfers that created auto-listings)
      - credit retirements
      - recent credit transfers

    Auditors and admins may also call this endpoint for any user by passing
    ?user_id=<id> as a query parameter.
    """
    check_rate_limit(request)

    caller_id   = current_user["user_id"]
    caller_role = current_user["role"]

    # Support auditor/admin inspecting another user's dashboard via ?user_id=
    from fastapi import Query
    target_id_param = request.query_params.get("user_id")
    if target_id_param:
        if caller_role not in ("auditor", "admin"):
            raise HTTPException(status_code=403, detail="Only auditors/admins can view other users' dashboards.")
        try:
            target_user_id = int(target_id_param)
        except ValueError:
            raise HTTPException(status_code=400, detail="user_id must be an integer.")
    else:
        if caller_role not in ("land_owner", "auditor", "admin"):
            raise HTTPException(status_code=403, detail="Access denied.")
        target_user_id = caller_id

    with get_db() as db:

        # ── 1. Profile ────────────────────────────────────────
        user = db.query(models.User).filter_by(id=target_user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found.")

        profile = {
            "user_id":        user.id,
            "name":           user.name,
            "email":          user.email,
            "role":           user.role,
            "wallet_address": user.wallet_address,
            "is_verified":    user.is_verified,
            "is_approved":    user.is_approved,
            "payout_bank_account_configured": bool(
                user.payout_account_holder and
                user.payout_bank_name and
                user.payout_account_number and
                user.payout_ifsc_code
            ),
            "payout_account_last4": (user.payout_account_number[-4:] if user.payout_account_number else None),
            "payout_bank_updated_at": user.payout_updated_at,
            "joined_at":      user.created_at,
        }

        # ── 2. Wallet / Credit Balances ───────────────────────
        wallet = db.query(models.Wallet).filter_by(user_id=target_user_id).first()
        wallet_summary = {
            "total_credits":      wallet.total_credits     if wallet else 0.0,
            "available_credits":  wallet.available_credits if wallet else 0.0,
            "retired_credits":    wallet.retired_credits   if wallet else 0.0,
            "buffer_contributed": wallet.buffer_contributed if wallet else 0.0,
        } if wallet else None

        # ── 3. Projects ───────────────────────────────────────
        projects = db.query(models.Project).filter_by(user_id=target_user_id)\
            .order_by(models.Project.created_at.desc()).all()

        project_list = [{
            "project_id":      p.id,
            "project_name":    p.project_name or f"Project #{p.id}",
            "area_hectares":   p.area_hectares,
            "baseline_carbon": p.baseline_carbon,
            "status":          p.status,
            "land_verified":   p.land_verified,
            "is_minted":       bool(p.is_minted),
            "mint_tx_hash":    p.mint_tx_hash,
            "created_at":      p.created_at,
        } for p in projects]

        # ── 4. Marketplace Listings ───────────────────────────
        all_listings = db.query(models.MarketplaceListing)\
            .filter_by(seller_id=target_user_id)\
            .order_by(models.MarketplaceListing.created_at.desc()).all()

        manual_listings = []
        auto_listings   = []

        for l in all_listings:
            entry = {
                "listing_id":       l.id,
                "project_id":       l.project_id,
                "credits_amount":   l.credits_amount,
                "price_per_credit": l.price_per_credit,
                "total_value_usd":  l.credits_amount * l.price_per_credit,
                "status":           l.status,
                "payment_tx":       l.payment_tx,
                "created_at":       l.created_at,
                "sold_at":          l.sold_at,
            }
            source = getattr(l, "source", "manual") or "manual"
            if source == "auto_cct_payment":
                entry["platform_fee_deducted"] = l.platform_fee
                entry["original_transfer_tx"]  = l.payment_tx
                auto_listings.append(entry)
            else:
                manual_listings.append(entry)

        listings_summary = {
            "total":           len(all_listings),
            "active":          sum(1 for l in all_listings if l.status == "active"),
            "sold":            sum(1 for l in all_listings if l.status == "sold"),
            "pending":         sum(1 for l in all_listings if l.status in ("pending_deposit", "pending_approval")),
            "manual_listings": manual_listings,
            "auto_cct_listings": {
                "count":    len(auto_listings),
                "note":     "These were auto-created when you transferred CCT tokens to the platform wallet.",
                "listings": auto_listings,
            },
        }

        # ── 5. CCT Payment History ────────────────────────────
        # Show every detected incoming transfer that belongs to this user's wallet
        cct_payments = []
        if user.wallet_address:
            wallet_lower = user.wallet_address.lower()
            cct_txns = db.query(models.ProcessedTransaction)\
                .filter(models.ProcessedTransaction.sender_wallet == wallet_lower)\
                .order_by(models.ProcessedTransaction.created_at.desc())\
                .all()
            for t in cct_txns:
                cct_payments.append({
                    "tx_hash":        t.tx_hash,
                    "amount_received": t.amount,
                    "platform_fee":   t.fee,
                    "amount_listed":  round(t.amount - t.fee, 8),
                    "fee_percent":    "2%",
                    "listing_id":     t.listing_id,
                    "status":         t.note,
                    "detected_at":    t.created_at,
                })

        # ── 6. Credit Retirements ─────────────────────────────
        retirements = db.query(models.CreditRetirement)\
            .filter_by(user_id=target_user_id)\
            .order_by(models.CreditRetirement.retired_at.desc()).all()

        retirement_list = [{
            "retirement_id":       r.retirement_id,
            "amount":              r.amount,
            "co2_equivalent_tons": round(r.amount / 3.67, 4),
            "reason":              r.reason,
            "blockchain_tx":       r.blockchain_tx,
            "has_certificate":     r.pdf_path is not None,
            "retired_at":          r.retired_at,
        } for r in retirements]

        # ── 7. Recent Credit Transfers ────────────────────────
        transfers = db.query(models.CreditTransfer).filter(
            (models.CreditTransfer.from_user_id == target_user_id) |
            (models.CreditTransfer.to_user_id   == target_user_id)
        ).order_by(models.CreditTransfer.transferred_at.desc()).limit(20).all()

        transfer_list = [{
            "transfer_id":    t.id,
            "direction":      "sent"     if t.from_user_id == target_user_id else "received",
            "counterpart_id": t.to_user_id if t.from_user_id == target_user_id else t.from_user_id,
            "amount":         t.amount,
            "blockchain_tx":  t.blockchain_tx,
            "note":           t.note,
            "transferred_at": t.transferred_at,
        } for t in transfers]

        # ── Assemble full dashboard ───────────────────────────
        return {
            "profile":          profile,
            "wallet":           wallet_summary,
            "projects":         project_list,
            "marketplace":      listings_summary,
            "cct_payments": {
                "count": len(cct_payments),
                "note":  (
                    "These are CCT token transfers the platform automatically detected "
                    "from your wallet. A 2% platform fee was deducted and the remainder "
                    "was listed on the marketplace instantly."
                ),
                "history": cct_payments,
            },
            "retirements":      retirement_list,
            "recent_transfers": transfer_list,
        }


# ════════════════════════════════════════════════════════
#  ADMIN ENDPOINTS
# ════════════════════════════════════════════════════════

@app.get("/admin/users")
def admin_list_users(request: Request, current_user: dict = Security(get_current_user)):
    check_rate_limit(request)
    # Allow both admin and auditor to list users for verification workflows
    require_roles(current_user, ["admin", "auditor"])
    with get_db() as db:
        users = db.query(models.User).order_by(models.User.created_at.desc()).all()
        result = []
        for u in users:
            identity_doc = (
                db.query(models.Document)
                .filter(models.Document.user_id == u.id)
                .filter(models.Document.doc_type.in_(["pan_individual", "pan_organization"]))
                .order_by(models.Document.uploaded_at.desc())
                .first()
            )
            result.append({
            "id":             u.id,
            "name":           u.name,
            "email":          u.email,
            "role":           u.role,
            "is_approved":    u.is_approved,
            "is_verified":    u.is_verified,  # ✅ NEW: Added verification status
            "is_active":      u.is_active,
            "wallet_address": u.wallet_address,
            "created_at":     u.created_at,
            "identity_document": (
                {
                    "document_id": identity_doc.id,
                    "doc_type": identity_doc.doc_type,
                    "status": identity_doc.status,
                    "file_name": identity_doc.file_name,
                    "uploaded_at": identity_doc.uploaded_at,
                }
                if identity_doc
                else None
            ),
            })

        return result


@app.post("/admin/approve_user/{user_id}")
def approve_user(user_id: int, request: Request, current_user: dict = Security(get_current_user)):
    check_rate_limit(request)
    require_roles(current_user, ["admin", "auditor"])
    with get_db() as db:
        user = db.query(models.User).filter_by(id=user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found.")
        user.is_approved = True
        log_action(db, current_user["user_id"], "approve_user", {"user_id": user_id}, request.client.host)
    return {"user_id": user_id, "is_approved": True, "message": "User approved."}


@app.post("/admin/suspend_user/{user_id}")
def suspend_user(user_id: int, request: Request, current_user: dict = Security(get_current_user)):
    check_rate_limit(request)
    require_roles(current_user, ["admin"])
    with get_db() as db:
        user = db.query(models.User).filter_by(id=user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail="User not found.")
        user.is_active = False
        log_action(db, current_user["user_id"], "suspend_user", {"user_id": user_id}, request.client.host)
    return {"user_id": user_id, "is_active": False, "message": "User suspended."}


@app.get("/admin/stats")
def admin_stats(request: Request, current_user: dict = Security(get_current_user)):
    check_rate_limit(request)
    require_roles(current_user, ["admin", "auditor"])
    with get_db() as db:
        total_users        = db.query(models.User).count()
        total_land_owners  = db.query(models.User).filter_by(role="land_owner").count()
        total_orgs         = db.query(models.User).filter_by(role="organization").count()
        total_auditors     = db.query(models.User).filter_by(role="auditor").count()
        pending_users      = db.query(models.User).filter_by(is_approved=False).count()
        total_projects     = db.query(models.Project).count()
        pending_docs       = db.query(models.Document).filter_by(status="pending").count()
        buffer             = db.query(models.BufferPool).first()
        total_retired      = db.query(models.CreditRetirement).count()
        return {
            "users": {
                "total":        total_users,
                "land_owners":  total_land_owners,
                "organizations": total_orgs,
                "auditors":     total_auditors,
                "pending_approval": pending_users,
            },
            "projects":        {"total": total_projects},
            "documents":       {"pending_review": pending_docs},
            "buffer_pool":     {"total_credits": buffer.total_buffer_credits if buffer else 0.0},
            "retirements":     {"total": total_retired},
        }


@app.get("/admin/projects")
def admin_list_projects(
    request: Request,
    limit: int = 200,
    current_user: dict = Security(get_current_user),
):
    check_rate_limit(request)
    require_roles(current_user, ["admin", "auditor"])

    with get_db() as db:
        rows = (
            db.query(models.Project, models.User)
            .join(models.User, models.Project.user_id == models.User.id)
            .order_by(models.Project.created_at.desc())
            .limit(limit)
            .all()
        )

        return [{
            "id": p.id,
            "project_id": p.id,
            "project_name": p.project_name or f"Project #{p.id}",
            "user_id": p.user_id,
            "owner_name": u.name,
            "owner_email": u.email,
            "area_hectares": p.area_hectares,
            "baseline_carbon": p.baseline_carbon,
            "status": p.status,
            "land_verified": p.land_verified,
            "is_minted": bool(p.is_minted),
            "coordinates_text": coords_to_string(p.coordinates) if p.coordinates else None,
            "created_at": p.created_at,
        } for p, u in rows]


@app.get("/admin/settings")
def get_settings(request: Request, current_user: dict = Security(get_current_user)):
    check_rate_limit(request)
    require_roles(current_user, ["admin"])
    with get_db() as db:
        settings = db.query(models.SystemSetting).all()
        return [{
            "key":         s.key,
            "value":       s.value,
            "description": s.description,
            "updated_at":  s.updated_at
        } for s in settings]


@app.post("/admin/settings")
def update_setting(data: dict, request: Request, current_user: dict = Security(get_current_user)):
    check_rate_limit(request)
    require_roles(current_user, ["admin"])
    key   = data.get("key", "").strip()
    value = data.get("value", "").strip()
    if not key or not value:
        raise HTTPException(status_code=400, detail="'key' and 'value' are required.")
    with get_db() as db:
        setting = db.query(models.SystemSetting).filter_by(key=key).first()
        if not setting:
            raise HTTPException(status_code=404, detail=f"Setting '{key}' not found.")
        setting.value      = value
        setting.updated_by = current_user["user_id"]
        log_action(db, current_user["user_id"], "update_setting",
                   {"key": key, "value": value}, request.client.host)
    return {"key": key, "value": value, "message": "Setting updated."}


# ════════════════════════════════════════════════════════
#  AUDIT LOGS
# ════════════════════════════════════════════════════════

@app.get("/audit_logs")
def get_audit_logs(request: Request, limit: int = 100, current_user: dict = Security(get_current_user)):
    check_rate_limit(request)
    require_roles(current_user, ["auditor", "admin"])
    with get_db() as db:
        logs = db.query(models.AuditLog)\
            .order_by(models.AuditLog.created_at.desc()).limit(limit).all()
        return [{
            "id":         l.id,
            "user_id":    l.user_id,
            "action":     l.action,
            "details":    l.details,
            "ip_address": l.ip_address,
            "created_at": l.created_at
        } for l in logs]











# ════════════════════════════════════════════════════════════
# ✅ NEW: GET all documents (for admin/auditor)
# ════════════════════════════════════════════════════════════

@app.get("/admin/documents")
def admin_list_documents(
    status: str = None,
    doc_type: str = None,
    request: Request = None,
    current_user: dict = Security(require_auditor_or_admin)
):
    """Get all documents for review - Admin/Auditor only"""
    check_rate_limit(request)
    
    with get_db() as db:
        query = db.query(models.Document).order_by(models.Document.uploaded_at.desc())
        
        # Filter by status if provided
        if status:
            query = query.filter_by(status=status)
        
        # Filter by document type if provided
        if doc_type:
            query = query.filter_by(doc_type=doc_type)
        
        documents = query.all()
        
        return [{
            "id": d.id,
            "user_id": d.user_id,
            "project_id": d.project_id,
            "user_name": db.query(models.User).get(d.user_id).name,
            "user_email": db.query(models.User).get(d.user_id).email,
            "project_name": (
                db.query(models.Project).get(d.project_id).project_name
                if d.project_id and db.query(models.Project).get(d.project_id)
                else None
            ),
            "file_name": d.file_name,
            "file_path": d.file_path,
            "file_type": d.file_type,
            "doc_type": d.doc_type,
            "status": d.status,
            "review_note": d.review_note,
            "reviewed_by": d.reviewed_by,
            "reviewed_at": d.reviewed_at,
            "uploaded_at": d.uploaded_at,
        } for d in documents]


# ════════════════════════════════════════════════════════════
# ✅ NEW: Review/approve/reject document
# ════════════════════════════════════════════════════════════

@app.post("/admin/review_document/{document_id}")
def review_document(
    document_id: int,
    data: dict,
    request: Request,
    current_user: dict = Security(require_auditor_or_admin)
):
    """Review a document - approve or reject"""
    check_rate_limit(request)
    
    action = data.get("action")  # "approve" or "reject"
    review_note = data.get("review_note", "")
    
    if action not in ["approve", "reject"]:
        raise HTTPException(status_code=400, detail="Action must be 'approve' or 'reject'")
    
    with get_db() as db:
        doc = db.query(models.Document).filter_by(id=document_id).first()
        if not doc:
            raise HTTPException(status_code=404, detail="Document not found")
        
        # Update document
        doc.status = "approved" if action == "approve" else "rejected"
        doc.review_note = review_note
        doc.reviewed_by = current_user["user_id"]
        doc.reviewed_at = datetime.utcnow()
        db.add(doc)
        
        # If approving, check if we should auto-verify user
        if action == "approve":
            # Check if user has all documents approved
            user = db.query(models.User).filter_by(id=doc.user_id).first()
            all_approved = all(
                d.status == "approved" 
                for d in db.query(models.Document).filter_by(user_id=doc.user_id).all()
            )
            
            # If all documents approved, consider auto-verifying (optional)
            # You can enable this if desired:
            # if all_approved:
            #     user.is_verified = True
        
        # Log action
        log_action(
            db,
            current_user["user_id"],
            "review_document",
            {
                "document_id": document_id,
                "action": action,
                "review_note": review_note,
                "user_id": doc.user_id
            },
            request.client.host
        )
        doc_status = doc.status   # capture before session closes
    
    return {
        "document_id": document_id,
        "status": doc_status,
        "message": f"Document {action}d successfully"
    }

# ════════════════════════════════════════════════════════
#  MRV  ──  MONITOR ALL PROJECTS  (admin / auditor)
# ════════════════════════════════════════════════════════

@app.post("/admin/monitor_all")
def monitor_all_projects(request: Request, current_user: dict = Security(get_current_user)):
    """
    Trigger monitoring for every eligible project (approved/verified + minted).
    Returns per-project results without aborting on individual failures.
    """
    check_rate_limit(request)
    require_roles(current_user, ["admin", "auditor"])

    # Fetch just the IDs in a short-lived session, so the session is
    # cleanly closed before we open per-project sessions below.
    try:
        with get_db() as db:
            rows = db.query(models.Project.id).filter(
                models.Project.status.in_(["approved", "verified"]),
                models.Project.is_minted == True,
            ).all()
        eligible_ids = [r[0] for r in rows]
    except Exception as exc:
        logger.error(f"monitor_all: failed to fetch eligible projects: {exc}")
        raise HTTPException(status_code=500, detail=f"Could not fetch eligible projects: {exc}")

    if not eligible_ids:
        return {"monitored": 0, "results": [], "message": "No eligible projects found (must be approved/verified and minted)."}

    results = []
    for project_id_val in eligible_ids:
        try:
            with get_db() as db:
                p = db.query(models.Project).filter_by(id=project_id_val).first()
                wallet = db.query(models.Wallet).filter_by(user_id=p.user_id).with_for_update().first()
                buffer = db.query(models.BufferPool).first()
                if not buffer:
                    buffer = models.BufferPool(total_buffer_credits=0.0)
                    db.add(buffer)
                    db.flush()

                last_record = db.query(models.CarbonRecord).filter_by(project_id=p.id)\
                    .order_by(models.CarbonRecord.id.desc()).first()
                if not last_record:
                    results.append({"project_id": p.id, "project_name": p.project_name,
                                    "status": "skipped", "reason": "No baseline record"})
                    continue

                polygon         = create_polygon(p.coordinates)
                area_ha         = calculate_area_hectares(polygon)
                features        = extract_features(polygon)
                prediction      = predict_carbon(features, area_ha)
                current_credits = prediction["total_credits"]
                previous_credits= float(last_record.carbon_stock)
                new_credits     = current_credits - previous_credits

                user      = db.query(models.User).filter_by(id=p.user_id).first()
                u_wallet  = user.wallet_address if user else None

                if new_credits < 0:
                    penalty = abs(new_credits) * BUFFER_RATE
                    buffer.total_buffer_credits = max(0.0, buffer.total_buffer_credits - penalty)
                    db.add(models.CarbonRecord(
                        project_id=p.id, carbon_stock=current_credits,
                        carbon_credits_generated=0.0, buffer_credits_added=-penalty
                    ))
                    # Auto-flag if buffer drops to zero
                    if buffer.total_buffer_credits <= 0:
                        p.is_flagged = True
                        p.flag_reason = "Buffer pool exhausted after carbon loss"
                    log_action(db, current_user["user_id"], "monitor_project",
                               {"project_id": p.id, "new_credits": new_credits,
                                "triggered_by": "monitor_all"}, request.client.host)
                    results.append({
                        "project_id": p.id, "project_name": p.project_name or f"Project #{p.id}",
                        "previous_credits": previous_credits, "current_credits": current_credits,
                        "change": new_credits, "status": "decrease",
                        "buffer_penalty": penalty,
                        "flagged": bool(p.is_flagged),
                    })

                elif new_credits == 0:
                    db.add(models.CarbonRecord(
                        project_id=p.id, carbon_stock=current_credits,
                        carbon_credits_generated=0.0, buffer_credits_added=0.0
                    ))
                    log_action(db, current_user["user_id"], "monitor_project",
                               {"project_id": p.id, "new_credits": 0,
                                "triggered_by": "monitor_all"}, request.client.host)
                    results.append({
                        "project_id": p.id, "project_name": p.project_name or f"Project #{p.id}",
                        "previous_credits": previous_credits, "current_credits": current_credits,
                        "change": 0, "status": "unchanged",
                    })

                else:
                    buffer_amount = new_credits * BUFFER_RATE
                    user_amount   = new_credits - buffer_amount
                    if wallet:
                        wallet.total_credits        += user_amount
                        wallet.available_credits    += user_amount
                        wallet.buffer_contributed   += buffer_amount
                    buffer.total_buffer_credits += buffer_amount
                    db.add(models.CarbonRecord(
                        project_id=p.id, carbon_stock=current_credits,
                        carbon_credits_generated=user_amount, buffer_credits_added=buffer_amount
                    ))
                    tx_hash = mint_error = None
                    try:
                        tx_hash = mint_tokens(u_wallet, user_amount)
                    except Exception as e:
                        mint_error = str(e)
                    log_action(db, current_user["user_id"], "monitor_project",
                               {"project_id": p.id, "new_credits": new_credits,
                                "triggered_by": "monitor_all", "tx_hash": tx_hash}, request.client.host)
                    results.append({
                        "project_id": p.id, "project_name": p.project_name or f"Project #{p.id}",
                        "previous_credits": previous_credits, "current_credits": current_credits,
                        "change": new_credits, "status": "increase",
                        "credits_issued": user_amount, "buffer_added": buffer_amount,
                        "blockchain_tx": tx_hash, "mint_error": mint_error,
                    })

        except Exception as exc:
            logger.error(f"monitor_all: project {project_id_val} failed: {exc}")
            results.append({"project_id": project_id_val,
                            "project_name": f"Project #{project_id_val}",
                            "status": "error", "reason": str(exc)})

    return {"monitored": len(results), "results": results}


# ════════════════════════════════════════════════════════
#  MRV  ──  BLACKLIST / UNBLACKLIST / FLAG / UNFLAG / DELETE
# ════════════════════════════════════════════════════════

@app.post("/admin/projects/{project_id}/blacklist")
def blacklist_project(project_id: int, request: Request,
                      current_user: dict = Security(get_current_user)):
    check_rate_limit(request)
    require_roles(current_user, ["admin"])
    with get_db() as db:
        p = db.query(models.Project).filter_by(id=project_id).first()
        if not p:
            raise HTTPException(status_code=404, detail="Project not found.")
        p.is_blacklisted = True
        log_action(db, current_user["user_id"], "blacklist_project",
                   {"project_id": project_id}, request.client.host)
    return {"project_id": project_id, "is_blacklisted": True}


@app.post("/admin/projects/{project_id}/unblacklist")
def unblacklist_project(project_id: int, request: Request,
                        current_user: dict = Security(get_current_user)):
    check_rate_limit(request)
    require_roles(current_user, ["admin"])
    with get_db() as db:
        p = db.query(models.Project).filter_by(id=project_id).first()
        if not p:
            raise HTTPException(status_code=404, detail="Project not found.")
        p.is_blacklisted = False
        log_action(db, current_user["user_id"], "unblacklist_project",
                   {"project_id": project_id}, request.client.host)
    return {"project_id": project_id, "is_blacklisted": False}


@app.post("/admin/projects/{project_id}/flag")
def flag_project(project_id: int, data: dict, request: Request,
                 current_user: dict = Security(get_current_user)):
    check_rate_limit(request)
    require_roles(current_user, ["admin", "auditor"])
    reason = data.get("reason", "Flagged for manual review")
    with get_db() as db:
        p = db.query(models.Project).filter_by(id=project_id).first()
        if not p:
            raise HTTPException(status_code=404, detail="Project not found.")
        p.is_flagged  = True
        p.flag_reason = reason
        log_action(db, current_user["user_id"], "flag_project",
                   {"project_id": project_id, "reason": reason}, request.client.host)
    return {"project_id": project_id, "is_flagged": True, "reason": reason}


@app.post("/admin/projects/{project_id}/unflag")
def unflag_project(project_id: int, request: Request,
                   current_user: dict = Security(get_current_user)):
    check_rate_limit(request)
    require_roles(current_user, ["admin", "auditor"])
    with get_db() as db:
        p = db.query(models.Project).filter_by(id=project_id).first()
        if not p:
            raise HTTPException(status_code=404, detail="Project not found.")
        p.is_flagged  = False
        p.flag_reason = None
        log_action(db, current_user["user_id"], "unflag_project",
                   {"project_id": project_id}, request.client.host)
    return {"project_id": project_id, "is_flagged": False}


@app.delete("/admin/projects/{project_id}")
def delete_project(project_id: int, request: Request,
                   current_user: dict = Security(get_current_user)):
    check_rate_limit(request)
    require_roles(current_user, ["admin"])
    with get_db() as db:
        p = db.query(models.Project).filter_by(id=project_id).first()
        if not p:
            raise HTTPException(status_code=404, detail="Project not found.")
        name = p.project_name or f"Project #{p.id}"
        # Remove related carbon records first to avoid FK errors
        db.query(models.CarbonRecord).filter_by(project_id=project_id).delete()
        db.delete(p)
        log_action(db, current_user["user_id"], "delete_project",
                   {"project_id": project_id, "name": name}, request.client.host)
    return {"deleted": True, "project_id": project_id, "name": name}


# ════════════════════════════════════════════════════════
#  MRV  ──  MONITORING LOGS  (carbon record history, all projects)
# ════════════════════════════════════════════════════════

@app.get("/admin/monitoring_logs")
def monitoring_logs(request: Request, limit: int = 200,
                    current_user: dict = Security(get_current_user)):
    """
    Return the latest carbon records across all projects,
    joined with project name and owner details.
    """
    check_rate_limit(request)
    require_roles(current_user, ["admin", "auditor"])
    with get_db() as db:
        rows = (
            db.query(models.CarbonRecord, models.Project, models.User)
            .join(models.Project, models.CarbonRecord.project_id == models.Project.id)
            .join(models.User,    models.Project.user_id == models.User.id)
            .order_by(models.CarbonRecord.measured_at.desc())
            .limit(limit)
            .all()
        )
        return [{
            "record_id":     r.id,
            "project_id":    p.id,
            "project_name":  p.project_name or f"Project #{p.id}",
            "owner_name":    u.name,
            "owner_email":   u.email,
            "carbon_stock":  r.carbon_stock,
            "credits_generated": r.carbon_credits_generated,
            "buffer_added":  r.buffer_credits_added,
            "measured_at":   r.measured_at,
            "is_blacklisted": bool(getattr(p, "is_blacklisted", False)),
            "is_flagged":     bool(getattr(p, "is_flagged", False)),
        } for r, p, u in rows]


# ════════════════════════════════════════════════════════
#  MRV  ──  EXTENDED ADMIN STATS (CCT totals)
# ════════════════════════════════════════════════════════

@app.get("/admin/mrv_stats")
def mrv_stats(request: Request, current_user: dict = Security(get_current_user)):
    """
    Returns extended MRV statistics:
      - total_cct_issued    (sum of all credits generated across all carbon records)
      - total_cct_retired   (sum of all credit retirements)
      - total_marketplace   (sum of active marketplace listing credits)
      - buffer_pool         (current buffer pool total)
      - project_breakdown   (per-project latest carbon stock + total issued)
    """
    check_rate_limit(request)
    require_roles(current_user, ["admin", "auditor"])
    with get_db() as db:
        from sqlalchemy import func

        # Total CCT ever issued = sum of every wallet's current holdings PLUS
        # whatever has already been retired (retired_credits is deducted from total_credits
        # at retirement time, so we must add it back to get the lifetime total).
        issued_row = db.query(
            func.coalesce(func.sum(models.Wallet.total_credits), 0.0),
            func.coalesce(func.sum(models.Wallet.retired_credits), 0.0),
        ).first()
        total_issued  = float((issued_row[0] or 0.0) + (issued_row[1] or 0.0))

        # Total CCT retired = sum of all retirement records (authoritative source)
        total_retired = float(db.query(
            func.coalesce(func.sum(models.CreditRetirement.amount), 0.0)
        ).scalar() or 0.0)

        # Fallback: if CreditRetirement table is empty but wallets show retired_credits, use wallet sum
        if total_retired == 0.0:
            wallet_retired = db.query(
                func.coalesce(func.sum(models.Wallet.retired_credits), 0.0)
            ).scalar()
            total_retired = float(wallet_retired or 0.0)

        total_marketplace = float(db.query(
            func.coalesce(func.sum(models.MarketplaceListing.credits_amount), 0.0)
        ).filter_by(status="active").scalar() or 0.0)

        buffer = db.query(models.BufferPool).first()
        buffer_pool_credits = float(buffer.total_buffer_credits if buffer else 0)

        # Per-project summary: latest carbon stock
        projects = db.query(models.Project).all()
        breakdown = []
        for p in projects:
            last = db.query(models.CarbonRecord).filter_by(project_id=p.id)\
                .order_by(models.CarbonRecord.id.desc()).first()
            prev = db.query(models.CarbonRecord).filter_by(project_id=p.id)\
                .order_by(models.CarbonRecord.id.desc()).offset(1).first()
            owner = db.query(models.User).filter_by(id=p.user_id).first()
            breakdown.append({
                "project_id":      p.id,
                "project_name":    p.project_name or f"Project #{p.id}",
                "owner_name":      owner.name if owner else "—",
                "area_ha":         p.area_hectares,
                "status":          p.status,
                "is_blacklisted":  bool(getattr(p, "is_blacklisted", False)),
                "is_flagged":      bool(getattr(p, "is_flagged", False)),
                "flag_reason":     getattr(p, "flag_reason", None),
                "current_stock":   last.carbon_stock if last else None,
                "previous_stock":  prev.carbon_stock if prev else None,
                "last_measured":   last.measured_at  if last else None,
                "total_records":   db.query(models.CarbonRecord).filter_by(project_id=p.id).count(),
            })

    return {
        "total_cct_issued":    float(total_issued),
        "total_cct_retired":   float(total_retired),
        "total_marketplace":   float(total_marketplace),
        "buffer_pool":         buffer_pool_credits,
        "project_breakdown":   breakdown,
    }