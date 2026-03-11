# Carbon MRV — Deployment Guide

## Architecture

```
Render (free tier)
├── carbon-mrv-backend   ← FastAPI  (Python)
├── carbon-mrv-frontend  ← Flask    (Python)
└── carbon-mrv-db        ← PostgreSQL (managed)
```

---

## 1. Google Earth Engine Service Account (REQUIRED first)

The app uses GEE satellite data. On a server you cannot run `ee.Authenticate()` interactively, so you need a **Service Account**.

### Step-by-step (takes ~10 minutes)

1. **Open your GCP project**
   Go to https://console.cloud.google.com and select project **carboncredits-487906**

2. **Create a Service Account**
   - Navigate to: IAM & Admin → Service Accounts → **+ Create Service Account**
   - Name: `carbon-mrv-backend`
   - Click *Create and Continue*
   - Role: **Earth Engine Resource Writer** (or Viewer if read-only is enough)
   - Click *Done*

3. **Download a JSON key**
   - Click the new service account → **Keys** tab → **Add Key** → **Create new key** → JSON
   - Save the downloaded file (e.g. `gee-service-account.json`)

4. **Register the service account with Earth Engine**
   - Visit: https://code.earthengine.google.com/register
   - Select "Use with a Cloud Project"
   - Enter the service account email (e.g. `carbon-mrv-backend@carboncredits-487906.iam.gserviceaccount.com`)
   - Complete registration

5. **Set the environment variable on Render** (see Section 3)
   - Key: `GEE_SERVICE_ACCOUNT_JSON`
   - Value: paste the **entire contents** of the JSON key file (all the `{...}` text)

---

## 2. Deploy to Render

### Option A — Automatic (render.yaml)

1. Push this repo to GitHub
2. Go to https://dashboard.render.com → **New** → **Blueprint**
3. Connect your GitHub repo
4. Render will read `render.yaml` and create all three services automatically
5. After creation, go to each service's **Environment** tab to fill in secret values (step 3)

### Option B — Manual (3 separate services)

#### 2a. Create PostgreSQL database
- Render dashboard → New → PostgreSQL
- Name: `carbon-mrv-db`
- Plan: Free
- Copy the **Internal Database URL** for use in the backend

#### 2b. Deploy Backend (FastAPI)
- New → Web Service → Connect your repo
- **Root Directory**: `backend`
- **Build Command**: `pip install -r requirements.txt`
- **Start Command**: `uvicorn api_mean_prediction:app --host 0.0.0.0 --port $PORT`

#### 2c. Deploy Frontend (Flask)
- New → Web Service → Connect your repo
- **Root Directory**: `frontend`
- **Build Command**: `pip install -r requirements.txt`
- **Start Command**: `gunicorn app:app --bind 0.0.0.0:$PORT --workers 2`

---

## 3. Environment Variables

Set these in the Render dashboard for **each service**.

### Backend environment variables

| Variable | Value | Notes |
|---|---|---|
| `DATABASE_URL` | (from Render PostgreSQL) | Copy "Internal Database URL" |
| `JWT_SECRET_KEY` | (random 64-char string) | Render can auto-generate |
| `GEE_PROJECT` | `carboncredits-487906` | Your GCP project ID |
| `GEE_SERVICE_ACCOUNT_JSON` | (full JSON key contents) | See Section 1 |
| `PRIVATE_KEY` | your wallet private key | Polygon wallet |
| `CONTRACT_ADDRESS` | `0xac9b428132898610873682700b0ae73182506e3b` | |
| `RPC_URL` | `https://rpc-amoy.polygon.technology` | |
| `PLATFORM_WALLET_ADDRESS` | `0x7f3846D0c4524Bf27d32019C9DBda08876427f5D` | |
| `SMTP_HOST` | `smtp.gmail.com` | |
| `SMTP_PORT` | `587` | |
| `SMTP_USER` | `carboncreditcct@gmail.com` | |
| `SMTP_PASSWORD` | (Gmail app password) | 16-char, no spaces |

### Frontend environment variables

| Variable | Value |
|---|---|
| `BACKEND_URL` | `https://carbon-mrv-backend.onrender.com` (your backend URL) |
| `SECRET_KEY` | (random string) |

---

## 4. Initialize the Database

After the backend first deploys, run the DB migration once.

In Render dashboard → **carbon-mrv-backend** → **Shell** tab:

```bash
python init_db.py
```

---

## 5. Local Development

```bash
# Clone and enter project
git clone <your-repo>
cd project

# Backend
cd backend
cp .env.example .env       # fill in your values
pip install -r requirements.txt
uvicorn api_mean_prediction:app --reload --port 8000

# Frontend (separate terminal)
cd frontend
cp .env.example .env       # set BACKEND_URL=http://127.0.0.1:8000
pip install -r requirements.txt
flask run --port 5000
```

For local GEE auth, either:
- Set `GEE_SERVICE_ACCOUNT_JSON` in your `.env`, OR
- Run `gcloud auth application-default login` (requires gcloud CLI)

---

## 6. Persistent File Storage

Render's free tier has an **ephemeral filesystem** — uploaded files and generated PDFs are lost on each deploy.

The `render.yaml` creates a **Disk** (`/var/data`) attached to the backend.

Update these paths in backend if needed:
- Certificates: `/var/data/certificates/`
- Retirement certs: `/var/data/retirement_certificates/`
- Uploads: `/var/data/uploads/`

---

## 7. Alternative Deployment Platforms

| Platform | Notes |
|---|---|
| **Railway** | Similar to Render, supports `railway.toml`. Free tier available. |
| **Fly.io** | Better free tier limits. Uses `fly.toml` + Dockerfile. |
| **Google Cloud Run** | Best GEE integration (same Google account). Uses container image. |
| **Heroku** | Reliable, paid only now. Add `Procfile`. |

### GCP Cloud Run (best for GEE)
If you deploy to Cloud Run in the same GCP project, GEE auth is automatic via the default service account — no JSON key needed.

---

## 8. Troubleshooting

**`Earth Engine could not be initialized`**
→ Check that `GEE_SERVICE_ACCOUNT_JSON` is set and contains valid JSON
→ Verify the service account email is registered at https://code.earthengine.google.com/register

**`FATAL: database does not exist`**
→ Run `python init_db.py` in the backend shell

**Frontend can't reach backend**
→ Check `BACKEND_URL` in frontend env vars points to the correct Render URL (no trailing slash)

**Uploads disappear after redeploy**
→ Ensure the Render Disk is mounted at `/var/data` and your code writes there
