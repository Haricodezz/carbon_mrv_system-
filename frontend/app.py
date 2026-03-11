from flask import Flask, render_template, request, session, redirect, url_for, flash, jsonify
from werkzeug.utils import secure_filename
from datetime import datetime, timedelta
from functools import wraps
import os
import requests
import re
from flask import Response, stream_with_context

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'your-secret-key-change-this')
app.config['UPLOAD_FOLDER'] = os.getenv('UPLOAD_FOLDER', '/var/data/uploads')
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024

ALLOWED_EXTENSIONS = {'pdf', 'jpg', 'jpeg', 'png', 'doc', 'docx', 'txt'}
ALLOWED_DOC_TYPES = ['land_deed', 'lease_agreement', 'aadhaar', 'ngo_cert', 'gst']

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

BACKEND_URL = os.getenv('BACKEND_URL', 'http://127.0.0.1:8000')
COORDS_PATTERN = re.compile(r"^-?\d+(?:\.\d+)?,-?\d+(?:\.\d+)?(?:\|-?\d+(?:\.\d+)?,-?\d+(?:\.\d+)?)+$")


def backend_headers():
    headers = {}
    token = session.get('token')
    if token:
        headers['Authorization'] = f'Bearer {token}'
    return headers


def backend_get(path, params=None):
    url = f"{BACKEND_URL}{path}"
    resp = requests.get(url, headers=backend_headers(), params=params or {})
    if not resp.ok:
        raise RuntimeError(f"Backend GET {path} failed: {resp.status_code} {resp.text}")
    return resp.json()


def backend_post(path, json=None, data=None, files=None):
    url = f"{BACKEND_URL}{path}"
    resp = requests.post(url, headers=backend_headers(), json=json, data=data, files=files)
    if not resp.ok:
        raise RuntimeError(f"Backend POST {path} failed: {resp.status_code} {resp.text}")
    return resp.json()


def backend_delete(path):
    url = f"{BACKEND_URL}{path}"
    resp = requests.delete(url, headers=backend_headers())
    if not resp.ok:
        raise RuntimeError(f"Backend DELETE {path} failed: {resp.status_code} {resp.text}")
    return resp.json()


def backend_get_raw(path, params=None):
    """GET backend endpoint and return raw Response (no JSON parsing)."""
    url = f"{BACKEND_URL}{path}"
    resp = requests.get(url, headers=backend_headers(), params=params or {}, stream=True)
    return resp


def sync_session_user_status():
    """Refresh current user's approval/verification flags from backend."""
    user_id = session.get('user_id')
    if not user_id:
        return None

    try:
        status = backend_get(f"/auth/user/{user_id}/verification-status")
    except Exception:
        return None

    session['is_verified'] = bool(status.get('is_verified'))
    session['is_approved'] = bool(status.get('is_approved'))
    session['is_active'] = bool(status.get('is_active'))
    return status


def get_latest_identity_document(documents):
    if not documents:
        return None

    identity_priority = {
        "pan_individual",
        "pan_organization",
        "aadhaar",
        "gst",
        "cin",
        "incorporation",
        "auditor_id",
    }
    identity_docs = [d for d in documents if d.get("doc_type") in identity_priority]
    if not identity_docs:
        return None

    # Most recent identity document by uploaded timestamp string.
    return sorted(
        identity_docs,
        key=lambda d: d.get("uploaded_at") or "",
        reverse=True,
    )[0]


def normalize_projects(projects):
    normalized = []
    for p in projects or []:
        proj = dict(p)
        proj_id = proj.get("id") or proj.get("project_id")
        proj["id"] = proj_id
        proj["project_id"] = proj_id
        if not proj.get("project_name"):
            proj["project_name"] = f"Project #{proj_id}" if proj_id else "Project"
        normalized.append(proj)
    return normalized

# ==================== DECORATORS ====================

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'token' not in session:
            flash('Please login first', 'error')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def role_required(*roles):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if 'token' not in session:
                flash('Please login first', 'error')
                return redirect(url_for('login'))
            if session.get('role') not in roles:
                flash('Access denied', 'error')
                return redirect(url_for('dashboard'))
            return f(*args, **kwargs)
        return decorated_function
    return decorator

# ==================== AUTH ROUTES ====================

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        try:
            data = backend_post("/auth/login", json={"email": email, "password": password})
        except Exception as e:
            flash(f'Login failed: {str(e)}', 'error')
            return render_template('auth/login.html')

        session['token'] = data['access_token']
        session['user_id'] = data['user_id']
        session['email'] = email
        session['name'] = data.get('name')
        session['role'] = data.get('role')
        session['is_verified'] = bool(data.get('is_verified', False))
        session['wallet_address'] = data.get('wallet_address', '')
        session.permanent = True
        app.permanent_session_lifetime = timedelta(days=7)
        flash(f'Welcome {session["name"]}!', 'success')

        role = session.get('role')
        if role == 'land_owner':
            return redirect(url_for('land_owner_dashboard'))
        elif role == 'organization':
            return redirect(url_for('org_dashboard'))
        elif role in ['auditor', 'admin']:
            return redirect(url_for('auditor_dashboard'))
        return redirect(url_for('dashboard'))
    return render_template('auth/login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        email = request.form.get('email')
        name = request.form.get('name')
        password = request.form.get('password')
        role = request.form.get('role', 'land_owner')
        wallet_address = request.form.get('wallet_address', '').strip() or None
        pan_file = request.files.get('pan_file')

        if not pan_file or pan_file.filename == '':
            flash('PAN document is required for registration.', 'error')
            return render_template('auth/register.html')

        try:
            files = {
                "identity_doc": (
                    pan_file.filename,
                    pan_file.stream,
                    pan_file.mimetype or "application/octet-stream",
                )
            }
            data = {
                "email": email,
                "name": name,
                "password": password,
                "role": role,
                "wallet_address": wallet_address or "",
            }
            backend_post(
                "/auth/register_with_document",
                data=data,
                files=files,
            )

            flash('Registration successful! Your PAN document has been submitted for verification.', 'success')
            return redirect(url_for('login'))
        except Exception as e:
            # Provide cleaner, role-aware error messages
            msg = str(e)
            if "Email already registered" in msg or "409" in msg and "Email" in msg:
                flash("This email is already registered. Please use a different email or login.", "error")
            elif "Wallet address is required for" in msg:
                if role == "land_owner":
                    flash("Wallet address is required for Land Owners. Please enter a valid MetaMask address.", "error")
                elif role == "organization":
                    flash("Wallet address is required for Organizations. Please enter a valid MetaMask address.", "error")
                else:
                    flash("Wallet address validation failed.", "error")
            elif "Wallet address already registered" in msg:
                flash("This wallet address is already registered with another account.", "error")
            elif "Invalid doc_type" in msg or "Invalid file type" in msg or "File too large" in msg:
                flash("Invalid PAN document. Only PDF, JPG, or PNG up to 5MB are allowed.", "error")
            elif "Invalid email format" in msg or "Email is required" in msg:
                flash("Please enter a valid email address.", "error")
            elif "Password must be at least 8 characters" in msg:
                flash("Password must be at least 8 characters long.", "error")
            else:
                flash("Registration failed. Please check your details and try again.", "error")
    return render_template('auth/register.html')

@app.route('/logout')
def logout():
    session.clear()
    flash('Logged out successfully', 'success')
    return redirect(url_for('index'))

# ==================== DASHBOARDS ====================

@app.route('/dashboard')
@login_required
def dashboard():
    role = session.get('role')
    if role == 'land_owner':
        return redirect(url_for('land_owner_dashboard'))
    elif role == 'organization':
        return redirect(url_for('org_dashboard'))
    elif role in ['auditor', 'admin']:
        return redirect(url_for('auditor_dashboard'))
    return render_template('dashboard.html')

@app.route('/dashboard/land-owner')
@login_required
@role_required('land_owner')
def land_owner_dashboard():
    user_id = session.get('user_id')
    user_status = None
    documents = []
    latest_identity_document = None
    wallet = None
    projects = []
    transfers = []
    bank_account = {"is_configured": False}
    if user_id:
        user_status = sync_session_user_status()
        try:
            documents = backend_get(f"/documents/{user_id}")
            latest_identity_document = get_latest_identity_document(documents)
        except Exception as e:
            flash(f'Failed to load documents: {str(e)}', 'error')
        try:
            wallet = backend_get(f"/wallet/{user_id}")
        except Exception:
            wallet = None
        try:
            projects = normalize_projects(backend_get(f"/projects/{user_id}"))
        except Exception:
            projects = []
        try:
            transfers = backend_get(f"/transfers/{user_id}")
        except Exception:
            transfers = []
        # ── CCT auto-payment history ──────────────────────
        cct_payments = {"count": 0, "history": []}
        try:
            raw = backend_get("/marketplace/auto_listings")
            history = []
            for item in (raw or []):
                history.append({
                    "tx_hash":         item.get("payment_tx") or "",
                    "amount_received": (item.get("credits_amount", 0) or 0) + (item.get("platform_fee", 0) or 0),
                    "platform_fee":    item.get("platform_fee", 0) or 0,
                    "amount_listed":   item.get("credits_amount", 0) or 0,
                    "listing_id":      item.get("listing_id"),
                    "status":          item.get("status", "active"),
                    "detected_at":     str(item.get("listed_at", "")),
                    "price_per_credit": item.get("price_per_credit", 1.0),
                })
            cct_payments = {"count": len(history), "history": history}
        except Exception:
            cct_payments = {"count": 0, "history": []}
        try:
            bank_account = backend_get("/landowner/payout-bank-account")
        except Exception:
            bank_account = {"is_configured": False}

    return render_template(
        'land_owner/dashboard.html',
        documents=documents,
        latest_identity_document=latest_identity_document,
        wallet=wallet,
        projects=projects,
        transfers=transfers,
        user_status=user_status,
        cct_payments=cct_payments,
        bank_account=bank_account,
    )


@app.route('/projects/register', methods=['POST'])
@login_required
@role_required('land_owner')
def register_project():
    project_name = request.form.get('project_name', '').strip()
    coords_str = request.form.get('coordinates', '').strip()
    project_doc_type = request.form.get('project_doc_type', '').strip()
    project_docs = [f for f in request.files.getlist('project_documents') if f and f.filename]

    if not project_name:
        flash('Project name is required.', 'error')
        return redirect(url_for('land_owner_dashboard'))
    if not coords_str:
        flash('Coordinates are required.', 'error')
        return redirect(url_for('land_owner_dashboard'))
    if not COORDS_PATTERN.fullmatch(coords_str):
        flash(
            'Invalid coordinates format. Use: 28.385,77.170|28.390,77.180|28.380,77.185|28.375,77.175',
            'error'
        )
        return redirect(url_for('land_owner_dashboard'))
    if len(coords_str.split('|')) < 3:
        flash('At least 3 coordinate points are required.', 'error')
        return redirect(url_for('land_owner_dashboard'))
    if not project_doc_type:
        flash('Document type is required.', 'error')
        return redirect(url_for('land_owner_dashboard'))
    if project_doc_type not in ['land_deed', 'lease_agreement']:
        flash('Invalid project document type selected.', 'error')
        return redirect(url_for('land_owner_dashboard'))
    if not project_docs:
        flash('At least one project document is required.', 'error')
        return redirect(url_for('land_owner_dashboard'))

    try:
        files = []
        for doc in project_docs:
            if not allowed_file(doc.filename):
                flash(f'Invalid file type for {doc.filename}. Allowed: PDF, JPG, JPEG, PNG.', 'error')
                return redirect(url_for('land_owner_dashboard'))
            files.append(
                (
                    "project_documents",
                    (secure_filename(doc.filename), doc.stream, doc.mimetype or "application/octet-stream"),
                )
            )

        backend_post(
            "/register_project_with_documents",
            data={
                "project_name": project_name,
                "coordinates": coords_str,
                "project_doc_type": project_doc_type,
            },
            files=files,
        )
        flash('Project registered successfully. Credits will be processed shortly.', 'success')
    except Exception as e:
        flash(f'Failed to register project: {str(e)}', 'error')

    return redirect(url_for('land_owner_dashboard'))


@app.route('/projects/<int:project_id>/history')
@login_required
@role_required('land_owner')
def project_history(project_id):
    try:
        history = backend_get(f"/project_history/{project_id}")
    except Exception as e:
        flash(f'Failed to load project history: {str(e)}', 'error')
        return redirect(url_for('land_owner_dashboard'))

    return render_template(
        'land_owner/project_history.html',
        project_id=project_id,
        history=history,
    )


@app.route('/marketplace/update_price', methods=['POST'])
@login_required
@role_required('land_owner')
def update_listing_price():
    listing_id = request.form.get('listing_id')
    price_per_credit = request.form.get('price_per_credit')

    if not listing_id or not price_per_credit:
        flash('Listing ID and price are required.', 'error')
        return redirect(url_for('land_owner_dashboard'))
    try:
        price = float(price_per_credit)
        if price <= 0:
            raise ValueError
    except ValueError:
        flash('Price must be a positive number.', 'error')
        return redirect(url_for('land_owner_dashboard'))
    try:
        backend_post(
            f"/marketplace/{listing_id}/update_price",
            json={"price_per_credit": price}
        )
        flash(f'Price updated to ₹{price:.2f} per CCT for listing #{listing_id}.', 'success')
    except Exception as e:
        flash(f'Failed to update price: {str(e)}', 'error')
    return redirect(url_for('land_owner_dashboard'))


@app.route('/marketplace/list', methods=['POST'])
@login_required
@role_required('land_owner')
def list_credits():
    project_id = request.form.get('project_id')
    credits_amount = request.form.get('credits_amount')
    price_per_credit = request.form.get('price_per_credit')

    if not project_id or not credits_amount or not price_per_credit:
        flash('Project, credits amount, and price per credit are required.', 'error')
        return redirect(url_for('land_owner_dashboard'))

    try:
        payload = {
            "project_id": int(project_id),
            "credits_amount": float(credits_amount),
            "price_per_credit": float(price_per_credit),
        }
    except ValueError:
        flash('Credits amount and price must be numbers.', 'error')
        return redirect(url_for('land_owner_dashboard'))

    try:
        backend_post("/marketplace/list", json=payload)
        flash('Listing created successfully. Please follow on-chain deposit instructions.', 'success')
    except Exception as e:
        flash(f'Failed to create listing: {str(e)}', 'error')

    return redirect(url_for('land_owner_dashboard'))


@app.route('/land-owner/payout-bank-account', methods=['POST'])
@login_required
@role_required('land_owner')
def update_land_owner_bank_account():
    account_holder_name = request.form.get('account_holder_name', '').strip()
    bank_name = request.form.get('bank_name', '').strip()
    account_number = re.sub(r"\s+", "", request.form.get('account_number', '').strip())
    confirm_account_number = re.sub(r"\s+", "", request.form.get('confirm_account_number', '').strip())
    ifsc_code = request.form.get('ifsc_code', '').strip().upper()
    branch_name = request.form.get('branch_name', '').strip()

    if not account_holder_name or not bank_name or not account_number or not ifsc_code:
        flash('Account holder name, bank name, account number, and IFSC code are required.', 'error')
        return redirect(url_for('land_owner_dashboard'))
    if account_number != confirm_account_number:
        flash('Account number and confirm account number do not match.', 'error')
        return redirect(url_for('land_owner_dashboard'))
    if not re.fullmatch(r"\d{9,18}", account_number):
        flash('Account number must contain 9 to 18 digits.', 'error')
        return redirect(url_for('land_owner_dashboard'))
    if not re.fullmatch(r"[A-Za-z]{4}0[A-Za-z0-9]{6}", ifsc_code):
        flash('Invalid IFSC code format. Example: HDFC0001234', 'error')
        return redirect(url_for('land_owner_dashboard'))

    payload = {
        "account_holder_name": account_holder_name,
        "bank_name": bank_name,
        "account_number": account_number,
        "ifsc_code": ifsc_code,
        "branch_name": branch_name,
    }

    try:
        backend_post("/landowner/payout-bank-account", json=payload)
        flash('Payout bank account saved. Platform payouts for sold credits will be sent here.', 'success')
    except Exception as e:
        flash(f'Failed to save payout bank account: {str(e)}', 'error')

    return redirect(url_for('land_owner_dashboard'))


@app.route('/dashboard/organization')
@login_required
@role_required('organization')
def org_dashboard():
    user_id = session.get('user_id')
    documents = []
    wallet = None
    market = []
    retirements = []

    if user_id:
        try:
            documents = backend_get(f"/documents/{user_id}")
        except Exception as e:
            flash(f'Failed to load documents: {str(e)}', 'error')

        try:
            wallet = backend_get(f"/wallet/{user_id}")
        except Exception:
            wallet = None

        try:
            # /marketplace returns all active listings (accessible to all authenticated users)
            raw_market = backend_get("/marketplace")
            market = raw_market if isinstance(raw_market, list) else raw_market.get('listings', [])
        except Exception:
            market = []

        try:
            retirements = backend_get(f"/retirements/{user_id}")
            if not isinstance(retirements, list):
                retirements = retirements.get('retirements', [])
        except Exception:
            retirements = []

    transfers = []
    if user_id:
        try:
            transfers = backend_get(f"/transfers/{user_id}")
            if not isinstance(transfers, list):
                transfers = []
        except Exception:
            transfers = []

    return render_template(
        'organization/dashboard.html',
        documents=documents,
        wallet=wallet,
        market=market,
        retirements=retirements,
        transfers=transfers,
    )


@app.route('/deposit-cct', methods=['POST'])
@login_required
@role_required('organization')
def deposit_cct():
    amount  = request.form.get('amount', '').strip()
    tx_hash = request.form.get('tx_hash', '').strip()

    if not amount or not tx_hash:
        flash('Amount and transaction hash are both required.', 'error')
        return redirect(url_for('org_dashboard'))
    try:
        amount = float(amount)
        if amount <= 0:
            raise ValueError()
    except ValueError:
        flash('Amount must be a positive number.', 'error')
        return redirect(url_for('org_dashboard'))
    if not tx_hash.startswith('0x') or len(tx_hash) < 10:
        flash('Please enter a valid transaction hash starting with 0x.', 'error')
        return redirect(url_for('org_dashboard'))

    payload = {
        "amount": amount,
        "payment_tx": tx_hash,
        "source": "org_wallet_deposit",
    }
    try:
        backend_post("/marketplace/submit_deposit", json=payload)
        flash(f'Transaction submitted! {amount:,.2f} CCT will be credited to your vault after on-chain verification.', 'success')
    except Exception as e:
        flash(f'Failed to process deposit: {str(e)}', 'error')
    return redirect(url_for('org_dashboard'))


@app.route('/withdraw-credits', methods=['POST'])
@login_required
@role_required('organization')
def withdraw_credits():
    amount = request.form.get('amount', '').strip()

    if not amount:
        flash('Amount is required to withdraw credits.', 'error')
        return redirect(url_for('org_dashboard'))
    try:
        amount = float(amount)
        if amount <= 0:
            raise ValueError()
    except ValueError:
        flash('Amount must be a positive number.', 'error')
        return redirect(url_for('org_dashboard'))

    payload = {
        "amount": amount,
        "note": "Org vault withdrawal to registered wallet",
    }
    try:
        backend_post("/withdraw_to_wallet", json=payload)
        flash(f'Withdrawal of {amount:,.2f} CCT initiated. Tokens will arrive in your registered wallet shortly.', 'success')
    except Exception as e:
        flash(f'Failed to withdraw credits: {str(e)}', 'error')
    return redirect(url_for('org_dashboard'))


@app.route('/retire-credits', methods=['POST'])
@login_required
@role_required('organization')
def retire_credits():
    amount = request.form.get('amount')
    reason = request.form.get('reason', '').strip()

    if not amount:
        flash('Amount is required to retire credits.', 'error')
        return redirect(url_for('org_dashboard'))

    try:
        amount = float(amount)
        if amount <= 0:
            raise ValueError()
    except ValueError:
        flash('Amount must be a positive number.', 'error')
        return redirect(url_for('org_dashboard'))

    payload = {
        "amount": amount,
        "reason": reason or "ESG Carbon Offset Compliance",
    }

    try:
        result = backend_post("/retire_credits", json=payload)
        ret_id = result.get('retirement_id', '') if isinstance(result, dict) else ''
        cert_generated = result.get('certificate_generated', False) if isinstance(result, dict) else False
        if ret_id and cert_generated:
            flash(f'RETIREMENT_SUCCESS:{ret_id}:{amount:.2f}', 'retirement_ready')
        elif ret_id:
            flash(f'Successfully retired {amount:,.2f} CCT credits. Certificate ID: {ret_id} — check the table below.', 'success')
        else:
            flash(f'Successfully retired {amount:,.2f} CCT credits. Your PDF certificate is ready in the table below.', 'success')
    except Exception as e:
        flash(f'Failed to retire credits: {str(e)}', 'error')

    return redirect(url_for('org_dashboard', _anchor='retirement-certs'))


@app.route('/buy-credits', methods=['POST'])
@login_required
@role_required('organization')
def buy_credits():
    listing_id = request.form.get('listing_id')

    if not listing_id:
        flash('Listing ID is required.', 'error')
        return redirect(url_for('org_dashboard'))

    try:
        listing_id = int(listing_id)
    except ValueError:
        flash('Listing ID must be a number.', 'error')
        return redirect(url_for('org_dashboard'))

    payload = {"listing_id": listing_id}

    try:
        backend_post("/buy_credits", json=payload)
        flash('Credits purchased successfully!', 'success')
    except Exception as e:
        flash(f'Failed to buy credits: {str(e)}', 'error')

    return redirect(url_for('org_dashboard'))

@app.route('/dashboard/auditor')
@login_required
@role_required('auditor', 'admin')
def auditor_dashboard():
    stats = {}
    users = []
    logs = []
    buffer = None
    documents = []
    projects = []
    mrv_stats = {}

    # Core system stats
    try:
        admin_stats = backend_get("/admin/stats")
        buffer = backend_get("/buffer_pool")
    except Exception as e:
        flash(f'Failed to load system stats: {e}', 'error')
        admin_stats = None

    # User list for verification management
    try:
        users = backend_get("/admin/users")
    except Exception as e:
        flash(f'Failed to load users: {e}', 'error')
        users = []

    # Documents for review
    try:
        documents = backend_get("/admin/documents")
    except Exception as e:
        flash(f'Failed to load documents: {e}', 'error')
        documents = []

    # Recent projects
    try:
        projects = backend_get("/admin/projects")
    except Exception as e:
        flash(f'Failed to load projects: {e}', 'error')
        projects = []

    # Derived stats for cards
    try:
        total_users = len(users)
        verified_users = sum(1 for u in users if u.get('is_verified'))
        total_projects = admin_stats["projects"]["total"] if admin_stats else 0
        total_credits_issued = 0  # Not exposed by backend; placeholder
        stats = {
            "total_users": total_users,
            "verified_users": verified_users,
            "total_projects": total_projects,
            "total_credits_issued": total_credits_issued,
        }
    except Exception:
        stats = {}

    # Recent audit logs
    try:
        logs = backend_get("/audit_logs")
    except Exception as e:
        flash(f'Failed to load audit logs: {e}', 'error')
        logs = []

    try:
        mrv_stats = backend_get("/admin/mrv_stats")
    except Exception as e:
        mrv_stats = {}

    # Fallback: if mrv_stats has no project_breakdown yet (projects never monitored),
    # build one from the projects list so the ▶ Monitor buttons are always visible.
    if not (mrv_stats and mrv_stats.get("project_breakdown")):
        if projects:
            fallback = []
            for p in projects:
                fallback.append({
                    "project_id":     p.get("project_id") or p.get("id"),
                    "project_name":   p.get("project_name", f"Project #{p.get('project_id') or p.get('id')}"),
                    "owner_name":     p.get("owner_name", ""),
                    "area_ha":        p.get("area_hectares") or p.get("area_ha") or 0,
                    "previous_stock": None,
                    "current_stock":  None,
                    "last_measured":  None,
                    "total_records":  0,
                    "is_flagged":     p.get("is_flagged", False),
                    "flag_reason":    p.get("flag_reason", ""),
                    "is_blacklisted": p.get("is_blacklisted", False),
                    "status":         p.get("status", "pending"),
                })
            if not mrv_stats:
                mrv_stats = {}
            mrv_stats["project_breakdown"] = fallback

    return render_template(
        'auditor/dashboard.html',
        stats=stats,
        users=users,
        logs=logs,
        buffer=buffer,
        documents=documents,
        projects=projects,
        mrv_stats=mrv_stats,
    )


@app.route("/auditor/users/<int:user_id>/approve", methods=["POST"])
@login_required
@role_required("auditor", "admin")
def approve_user(user_id):
    """Approve user from auditor dashboard (uses backend admin endpoint)."""
    try:
        backend_post(f"/admin/approve_user/{user_id}", json={})
        flash(f"User #{user_id} approved.", "success")
    except Exception as e:
        flash(f"Failed to approve user: {e}", "error")
    return redirect(url_for("auditor_dashboard"))


@app.route("/auditor/users/<int:user_id>/verify", methods=["POST"])
@login_required
@role_required("auditor", "admin")
def auditor_verify_user(user_id):
    """Mark user as verified (allows minting credits)."""
    try:
        backend_post(
            f"/auth/verify_user/{user_id}",
            json={"verified": True, "reason": "Verified via auditor dashboard"},
        )
        flash(f"User #{user_id} verified.", "success")
    except Exception as e:
        flash(f"Failed to verify user: {e}", "error")
    return redirect(url_for("auditor_dashboard"))


@app.route("/auditor/users/<int:user_id>/unverify", methods=["POST"])
@login_required
@role_required("auditor", "admin")
def auditor_unverify_user(user_id):
    """Remove verification flag from user."""
    try:
        backend_post(
            f"/auth/verify_user/{user_id}",
            json={"verified": False, "reason": "Verification removed via auditor dashboard"},
        )
        flash(f"User #{user_id} marked as unverified.", "success")
    except Exception as e:
        flash(f"Failed to unverify user: {e}", "error")
    return redirect(url_for("auditor_dashboard"))



@app.route("/auditor/projects/<int:project_id>/approve", methods=["POST"])
@login_required
@role_required("auditor", "admin")
def auditor_approve_project(project_id):
    try:
        backend_post(
            f"/projects/{project_id}/review",
            json={"action": "approve", "review_note": "Approved via auditor dashboard"},
        )
        flash(f"Project #{project_id} approved and tokens minted.", "success")
    except Exception as e:
        flash(f"Failed to approve project #{project_id}: {e}", "error")
    return redirect(url_for("auditor_dashboard"))


@app.route("/auditor/projects/<int:project_id>/reject", methods=["POST"])
@login_required
@role_required("auditor", "admin")
def auditor_reject_project(project_id):
    try:
        backend_post(
            f"/projects/{project_id}/review",
            json={"action": "reject", "review_note": "Rejected via auditor dashboard"},
        )
        flash(f"Project #{project_id} rejected.", "success")
    except Exception as e:
        flash(f"Failed to reject project #{project_id}: {e}", "error")
    return redirect(url_for("auditor_dashboard"))


@app.route("/auditor/users/<int:user_id>/pan-review")
@login_required
@role_required("auditor", "admin")
def auditor_pan_review(user_id):
    try:
        users = backend_get("/admin/users")
        user = next((u for u in users if u["id"] == user_id), None)
    except Exception as e:
        flash(f"Failed to load user: {e}", "error")
        return redirect(url_for("auditor_dashboard"))

    if not user:
        flash("User not found.", "error")
        return redirect(url_for("auditor_dashboard"))

    identity_doc = user.get("identity_document")
    if not identity_doc:
        flash("No PAN document found for this user.", "error")
        return redirect(url_for("auditor_dashboard"))

    return render_template(
        "auditor/pan_review.html",
        user=user,
        identity_doc=identity_doc,
        restricted=user.get("role") in ("auditor", "admin"),
    )


@app.route("/auditor/users/<int:user_id>/pan-approve", methods=["POST"])
@login_required
@role_required("auditor", "admin")
def auditor_pan_approve(user_id):
    action = request.form.get("action")
    also_verify = request.form.get("also_verify") == "true"
    review_note = request.form.get("review_note", "Reviewed via auditor PAN panel")
    doc_id = request.form.get("doc_id")

    if action not in ("approve", "reject") or not doc_id:
        flash("Invalid request.", "error")
        return redirect(url_for("auditor_dashboard"))

    try:
        users = backend_get("/admin/users")
        target = next((u for u in users if u["id"] == user_id), None)
        if target and target.get("role") in ("auditor", "admin"):
            flash("Auditors cannot review PAN documents for Auditor or Admin accounts.", "error")
            return redirect(url_for("auditor_dashboard"))

        backend_post(
            f"/admin/review_document/{doc_id}",
            json={"action": action, "review_note": review_note},
        )
        flash(f"PAN document {'approved' if action == 'approve' else 'rejected'} successfully.", "success")
    except Exception as e:
        flash(f"Failed to review PAN: {e}", "error")
        return redirect(url_for("auditor_dashboard"))

    if action == "approve" and also_verify:
        try:
            backend_post(
                f"/auth/verify_user/{user_id}",
                json={"verified": True, "reason": "PAN approved and user verified via auditor panel"},
            )
            flash(f"User #{user_id} has also been verified.", "success")
        except Exception as e:
            flash(f"PAN approved but failed to verify user: {e}", "warning")

    return redirect(url_for("auditor_dashboard"))


@app.route('/dashboard/admin')
@login_required
@role_required('admin')
def admin_dashboard():
    stats = {}
    users = []
    settings = []
    logs = []
    buffer = None
    projects = []
    mrv_stats = {}

    try:
        # Admin stats and buffer pool
        admin_stats = backend_get("/admin/stats")
        buffer = backend_get("/buffer_pool")
        stats = {
            "total_users": admin_stats["users"]["total"],
            "total_projects": admin_stats["projects"]["total"],
            "total_credits_issued": 0,  # Backend does not expose; set 0 for now
        }
    except Exception as e:
        flash(f"Failed to load admin stats: {e}", "error")

    try:
        users = backend_get("/admin/users")
    except Exception as e:
        flash(f"Failed to load users: {e}", "error")

    try:
        settings = backend_get("/admin/settings")
    except Exception as e:
        flash(f"Failed to load settings: {e}", "error")

    try:
        logs = backend_get("/audit_logs")
    except Exception as e:
        flash(f"Failed to load audit logs: {e}", "error")

    try:
        projects = backend_get("/admin/projects")
    except Exception as e:
        flash(f"Failed to load projects: {e}", "error")

    try:
        mrv_stats = backend_get("/admin/mrv_stats")
    except Exception as e:
        mrv_stats = {}

    # Fallback: if mrv_stats has no project_breakdown yet (projects never monitored),
    # build one from the projects list so the ▶ Monitor buttons are always visible.
    if not (mrv_stats and mrv_stats.get("project_breakdown")):
        if projects:
            fallback = []
            for p in projects:
                fallback.append({
                    "project_id":     p.get("project_id") or p.get("id"),
                    "project_name":   p.get("project_name", f"Project #{p.get('project_id') or p.get('id')}"),
                    "owner_name":     p.get("owner_name", ""),
                    "area_ha":        p.get("area_hectares") or p.get("area_ha") or 0,
                    "previous_stock": None,
                    "current_stock":  None,
                    "last_measured":  None,
                    "total_records":  0,
                    "is_flagged":     p.get("is_flagged", False),
                    "flag_reason":    p.get("flag_reason", ""),
                    "is_blacklisted": p.get("is_blacklisted", False),
                    "status":         p.get("status", "pending"),
                })
            if not mrv_stats:
                mrv_stats = {}
            mrv_stats["project_breakdown"] = fallback

    return render_template(
        "admin/dashboard.html",
        stats=stats,
        buffer=buffer,
        users=users,
        settings=settings,
        logs=logs,
        projects=projects,
        mrv_stats=mrv_stats,
    )


@app.route("/admin/users/<int:user_id>/approve", methods=["POST"])
@login_required
@role_required("admin")
def admin_approve(user_id):
    try:
        backend_post(f"/admin/approve_user/{user_id}", json={})
        flash(f"User #{user_id} approved.", "success")
    except Exception as e:
        flash(f"Failed to approve user: {e}", "error")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/users/<int:user_id>/suspend", methods=["POST"])
@login_required
@role_required("admin")
def admin_suspend(user_id):
    try:
        backend_post(f"/admin/suspend_user/{user_id}", json={})
        flash(f"User #{user_id} suspended.", "success")
    except Exception as e:
        flash(f"Failed to suspend user: {e}", "error")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/settings/update", methods=["POST"])
@login_required
@role_required("admin")
def admin_setting():
    key = request.form.get("key", "").strip()
    value = request.form.get("value", "").strip()
    if not key or not value:
        flash("Key and value are required.", "error")
        return redirect(url_for("admin_dashboard"))
    try:
        backend_post("/admin/settings", json={"key": key, "value": value})
        flash(f"Setting '{key}' updated.", "success")
    except Exception as e:
        flash(f"Failed to update setting: {e}", "error")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/users/<int:user_id>/verify", methods=["POST"])
@login_required
@role_required("admin", "auditor")
def admin_verify_user(user_id):
    try:
        backend_post(
            f"/auth/verify_user/{user_id}",
            json={"verified": True, "reason": "Verified via admin dashboard"},
        )
        flash(f"User #{user_id} marked as verified.", "success")
    except Exception as e:
        flash(f"Failed to verify user: {e}", "error")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/users/<int:user_id>/unverify", methods=["POST"])
@login_required
@role_required("admin", "auditor")
def admin_unverify_user(user_id):
    try:
        backend_post(
            f"/auth/verify_user/{user_id}",
            json={"verified": False, "reason": "Verification removed via admin dashboard"},
        )
        flash(f"User #{user_id} marked as unverified.", "success")
    except Exception as e:
        flash(f"Failed to unverify user: {e}", "error")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/users/<int:user_id>/pan-review")
@login_required
@role_required("admin")
def admin_pan_review(user_id):
    try:
        users = backend_get("/admin/users")
        user = next((u for u in users if u["id"] == user_id), None)
    except Exception as e:
        flash(f"Failed to load user: {e}", "error")
        return redirect(url_for("admin_dashboard"))

    if not user:
        flash("User not found.", "error")
        return redirect(url_for("admin_dashboard"))

    identity_doc = user.get("identity_document")
    if not identity_doc:
        flash("No PAN document found for this user.", "error")
        return redirect(url_for("admin_dashboard"))

    return render_template("admin/pan_review.html", user=user, identity_doc=identity_doc)


@app.route("/admin/users/<int:user_id>/pan-approve", methods=["POST"])
@login_required
@role_required("admin")
def admin_pan_approve(user_id):
    action = request.form.get("action")
    also_verify = request.form.get("also_verify") == "true"
    review_note = request.form.get("review_note", "Reviewed via admin PAN panel")
    doc_id = request.form.get("doc_id")

    if action not in ("approve", "reject") or not doc_id:
        flash("Invalid request.", "error")
        return redirect(url_for("admin_dashboard"))

    try:
        backend_post(
            f"/admin/review_document/{doc_id}",
            json={"action": action, "review_note": review_note},
        )
        flash(f"PAN document {'approved' if action == 'approve' else 'rejected'} successfully.", "success")
    except Exception as e:
        flash(f"Failed to review PAN: {e}", "error")
        return redirect(url_for("admin_dashboard"))

    if action == "approve" and also_verify:
        try:
            backend_post(
                f"/auth/verify_user/{user_id}",
                json={"verified": True, "reason": "PAN approved and user verified via admin panel"},
            )
            flash(f"User #{user_id} has also been verified.", "success")
        except Exception as e:
            flash(f"PAN approved but failed to verify user: {e}", "warning")

    return redirect(url_for("admin_dashboard"))

# ==================== DOCUMENT ROUTES ====================

@app.route('/documents-management')
@login_required
@role_required('auditor', 'admin')
def documents_management():
    status_filter = request.args.get('status', '')
    doc_type_filter = request.args.get('doc_type', '')
    params = {}
    if status_filter:
        params["status"] = status_filter
    if doc_type_filter:
        params["doc_type"] = doc_type_filter
    try:
        documents = backend_get("/admin/documents", params=params)
    except Exception as e:
        flash(f'Failed to load documents: {str(e)}', 'error')
        documents = []

    return render_template('documents_management.html', documents=documents)

@app.route('/upload-document', methods=['GET', 'POST'])
@login_required
def upload_document():
    if request.method == 'POST':
        if 'file' not in request.files:
            flash('No file selected', 'error')
            return redirect(request.referrer or url_for('dashboard'))
        
        file = request.files['file']
        doc_type = request.form.get('doc_type')
        
        if file.filename == '' or not allowed_file(file.filename) or doc_type not in ALLOWED_DOC_TYPES:
            flash('Invalid file or document type', 'error')
            return redirect(request.referrer or url_for('dashboard'))
        
        try:
            filename = secure_filename(file.filename)
            files = {
                "file": (filename, file.stream, file.mimetype or "application/octet-stream")
            }
            data = {"doc_type": doc_type}
            backend_post("/upload_document", data=data, files=files)
            flash('Document uploaded successfully', 'success')
        except Exception as e:
            flash(f'Upload failed: {str(e)}', 'error')
        
        return redirect(request.referrer or url_for('dashboard'))
    
    return render_template('upload_document.html', doc_types=ALLOWED_DOC_TYPES)


@app.route('/upload-doc', methods=['POST'])
@login_required
def upload_doc():
    # Alias endpoint so templates using url_for('upload_doc') keep working
    return upload_document()

@app.route('/review-document', methods=['POST'])
@login_required
@role_required('auditor', 'admin')
def review_document():
    doc_id = request.args.get('doc_id')
    action = request.args.get('action')
    review_note = request.form.get('review_note', '')
    
    if not doc_id or action not in ['approve', 'reject']:
        flash('Invalid request', 'error')
        return redirect(url_for('documents_management'))
    
    try:
        backend_post(
            f"/admin/review_document/{doc_id}",
            json={"action": action, "review_note": review_note},
        )
        status = 'approved' if action == 'approve' else 'rejected'
        flash(f'Document {status} successfully via backend', 'success')
    except Exception as e:
        flash(f'Review failed: {str(e)}', 'error')
    
    return redirect(url_for('documents_management'))

# ==================== API ENDPOINTS ====================

@app.route('/api/documents', methods=['GET'])
@login_required
def api_documents():
    status = request.args.get('status', '')
    doc_type = request.args.get('doc_type', '')
    user_id = request.args.get('user_id') or session.get('user_id')

    try:
        # Admin/auditor: use admin endpoint, others: own documents
        if session.get('role') in ['auditor', 'admin']:
            params = {}
            if status:
                params["status"] = status
            if doc_type:
                params["doc_type"] = doc_type
            documents = backend_get("/admin/documents", params=params)
        else:
            if not user_id:
                return jsonify({'success': False, 'error': 'user_id required'}), 400
            documents = backend_get(f"/documents/{user_id}")

        return jsonify({
            'success': True,
            'total': len(documents),
            'limit': len(documents),
            'offset': 0,
            'documents': documents
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/account-status', methods=['GET'])
@login_required
@role_required('land_owner')
def api_account_status():
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'success': False, 'error': 'user_id missing'}), 400

    user_status = sync_session_user_status() or {}
    documents = []
    latest_identity_document = None

    try:
        documents = backend_get(f"/documents/{user_id}")
        latest_identity_document = get_latest_identity_document(documents)
    except Exception:
        pass

    return jsonify({
        'success': True,
        'is_approved': bool(user_status.get('is_approved', session.get('is_approved', True))),
        'is_verified': bool(user_status.get('is_verified', session.get('is_verified', False))),
        'identity_document_status': (latest_identity_document or {}).get('status'),
        'documents_count': len(documents),
    })

@app.route('/api/documents/<int:doc_id>', methods=['GET'])
@login_required
def api_document_detail(doc_id):
    try:
        # Fetch all documents and filter client-side
        documents = backend_get("/admin/documents") if session.get('role') in ['auditor', 'admin'] else backend_get(
            f"/documents/{session.get('user_id')}"
        )
        for d in documents:
            if d.get('id') == doc_id or d.get('document_id') == doc_id:
                return jsonify({'success': True, 'document': d})
        return jsonify({'success': False, 'error': 'Document not found'}), 404
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/documents/<int:doc_id>/status', methods=['GET'])
@login_required
def api_document_status(doc_id):
    try:
        documents = backend_get("/admin/documents") if session.get('role') in ['auditor', 'admin'] else backend_get(
            f"/documents/{session.get('user_id')}"
        )
        for d in documents:
            if d.get('id') == doc_id or d.get('document_id') == doc_id:
                return jsonify({
                    'success': True,
                    'id': d.get('id') or d.get('document_id'),
                    'status': d.get('status'),
                    'review_note': d.get('review_note'),
                    'reviewed_at': d.get('reviewed_at')
                })
        return jsonify({'success': False, 'error': 'Document not found'}), 404
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/documents/stats', methods=['GET'])
@login_required
@role_required('auditor', 'admin')
def api_documents_stats():
    try:
        documents = backend_get("/admin/documents")
        stats = {
            'total': len(documents),
            'pending': sum(1 for d in documents if d.get('status') == 'pending'),
            'approved': sum(1 for d in documents if d.get('status') == 'approved'),
            'rejected': sum(1 for d in documents if d.get('status') == 'rejected'),
            'by_type': {}
        }
        for doc_type in ALLOWED_DOC_TYPES:
            stats['by_type'][doc_type] = sum(1 for d in documents if d.get('doc_type') == doc_type)
        return jsonify({'success': True, 'stats': stats})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/documents/<int:doc_id>/download', methods=['GET'])
@login_required
def api_download_document(doc_id):
    # Backend does not expose direct download endpoint for documents.
    # For now, just surface metadata via api_document_detail.
    return api_document_detail(doc_id)


@app.route("/documents/<int:document_id>/download")
@login_required
def download_document(document_id: int):
    """
    Proxy backend document download so browser auth works (Bearer token in server-side request).
    """
    resp = backend_get_raw(f"/documents/{document_id}/download")
    if resp.status_code != 200:
        flash("Failed to download document.", "error")
        return redirect(request.referrer or url_for("dashboard"))

    content_type = resp.headers.get("Content-Type", "application/octet-stream")
    # Preserve backend filename if present
    content_disp = resp.headers.get("Content-Disposition")
    headers = {}
    if content_disp:
        headers["Content-Disposition"] = content_disp

    return Response(
        stream_with_context(resp.iter_content(chunk_size=8192)),
        content_type=content_type,
        headers=headers,
    )



@app.route("/retirement-certificate/<retirement_id>/download")
@login_required
def download_retirement_certificate(retirement_id):
    """
    Proxy the backend retirement-certificate PDF so the Bearer token is sent
    server-side. A plain <a href="...backend..."> link would be unauthenticated
    and return 401/403 - this route fixes that by fetching with the session token
    and streaming the PDF straight to the browser as a file download.
    """
    resp = backend_get_raw(f"/retirement_certificate/{retirement_id}")
    if resp.status_code == 404:
        flash(
            "Certificate PDF not found. It may still be generating - "
            "please try again in a moment.", "warning"
        )
        return redirect(request.referrer or url_for("org_dashboard"))
    if resp.status_code != 200:
        flash(f"Could not retrieve certificate (status {resp.status_code}).", "error")
        return redirect(request.referrer or url_for("org_dashboard"))

    filename = f"retirement_{retirement_id}.pdf"
    content_disp = f'attachment; filename="{filename}"'

    return Response(
        stream_with_context(resp.iter_content(chunk_size=8192)),
        content_type="application/pdf",
        headers={"Content-Disposition": content_disp},
    )

@app.route('/api/documents/<int:doc_id>/review', methods=['POST'])
@login_required
@role_required('auditor', 'admin')
def api_review_document(doc_id):
    action = request.json.get('action')
    review_note = request.json.get('review_note', '')

    if action not in ['approve', 'reject']:
        return jsonify({'success': False, 'error': 'Invalid action'}), 400

    try:
        backend_post(
            f"/admin/review_document/{doc_id}",
            json={"action": action, "review_note": review_note},
        )
        status = 'approved' if action == 'approve' else 'rejected'
        return jsonify({'success': True, 'message': f'Document {status} successfully', 'status': status})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# ==================== MRV ROUTES ====================

@app.route('/admin/mrv/monitor_all', methods=['POST'])
@login_required
@role_required('admin', 'auditor')
def mrv_monitor_all():
    try:
        result = backend_post('/admin/monitor_all', json={})
        if isinstance(result, dict):
            monitored = result.get('monitored', 0)
            results   = result.get('results', [])
            errors    = [r for r in results if r.get('status') == 'error']
            skipped   = [r for r in results if r.get('status') == 'skipped']
            msg = f'✅ Monitoring complete — {monitored} project(s) processed.'
            if result.get('message'):
                msg = f'ℹ️ {result["message"]}'
            flash(msg, 'success')
            for e in errors:
                flash(f'⚠️ Project #{e.get("project_id")}: {e.get("reason", "unknown error")}', 'warning')
            for s in skipped:
                flash(f'ℹ️ Project #{s.get("project_id")} skipped: {s.get("reason", "")}', 'info')
        else:
            flash('✅ Monitoring triggered.', 'success')
    except Exception as e:
        flash(f'Monitoring failed: {str(e)}', 'error')
    role = session.get('role')
    return redirect(url_for('admin_dashboard' if role == 'admin' else 'auditor_dashboard'))


@app.route('/admin/projects/<int:project_id>/monitor', methods=['POST'])
@login_required
@role_required('admin', 'auditor')
def mrv_monitor_single(project_id):
    try:
        backend_post('/monitor_project', json={'project_id': project_id})
        flash(f'✅ Project #{project_id} monitored successfully.', 'success')
    except Exception as e:
        flash(f'Monitoring failed: {str(e)}', 'error')
    role = session.get('role')
    return redirect(url_for('admin_dashboard' if role == 'admin' else 'auditor_dashboard'))


@app.route('/admin/projects/<int:project_id>/blacklist', methods=['POST'])
@login_required
@role_required('admin')
def mrv_blacklist_project(project_id):
    try:
        backend_post(f'/admin/projects/{project_id}/blacklist', json={})
        flash(f'Project #{project_id} has been blacklisted.', 'success')
    except Exception as e:
        flash(f'Failed: {str(e)}', 'error')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/projects/<int:project_id>/unblacklist', methods=['POST'])
@login_required
@role_required('admin')
def mrv_unblacklist_project(project_id):
    try:
        backend_post(f'/admin/projects/{project_id}/unblacklist', json={})
        flash(f'Project #{project_id} blacklist removed.', 'success')
    except Exception as e:
        flash(f'Failed: {str(e)}', 'error')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/projects/<int:project_id>/flag', methods=['POST'])
@login_required
@role_required('admin', 'auditor')
def mrv_flag_project(project_id):
    reason = request.form.get('reason', 'Flagged for manual review')
    try:
        backend_post(f'/admin/projects/{project_id}/flag', json={'reason': reason})
        flash(f'Project #{project_id} flagged as suspicious.', 'success')
    except Exception as e:
        flash(f'Failed: {str(e)}', 'error')
    role = session.get('role')
    return redirect(url_for('admin_dashboard' if role == 'admin' else 'auditor_dashboard'))


@app.route('/admin/projects/<int:project_id>/unflag', methods=['POST'])
@login_required
@role_required('admin', 'auditor')
def mrv_unflag_project(project_id):
    try:
        backend_post(f'/admin/projects/{project_id}/unflag', json={})
        flash(f'Project #{project_id} flag cleared.', 'success')
    except Exception as e:
        flash(f'Failed: {str(e)}', 'error')
    role = session.get('role')
    return redirect(url_for('admin_dashboard' if role == 'admin' else 'auditor_dashboard'))


@app.route('/admin/projects/<int:project_id>/delete', methods=['POST'])
@login_required
@role_required('admin')
def mrv_delete_project(project_id):
    try:
        backend_delete(f'/admin/projects/{project_id}')
        flash(f'Project #{project_id} deleted.', 'success')
    except Exception as e:
        flash(f'Failed to delete: {str(e)}', 'error')
    return redirect(url_for('admin_dashboard'))


@app.route('/api/admin/monitoring_logs')
@login_required
@role_required('admin', 'auditor')
def api_monitoring_logs():
    try:
        logs = backend_get('/admin/monitoring_logs')
        return jsonify(logs)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/admin/project_history/<int:project_id>')
@login_required
@role_required('admin', 'auditor')
def api_project_history(project_id):
    try:
        history = backend_get(f'/project_history/{project_id}')
        return jsonify(history)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ==================== ERROR HANDLERS ====================

@app.errorhandler(404)
def not_found(error):
    return render_template('error.html', error='Page not found'), 404

@app.errorhandler(500)
def server_error(error):
    return render_template('error.html', error='Server error'), 500

# ==================== CONTEXT ====================

@app.context_processor
def inject_user():
    return {
        'user': {
            'token': session.get('token'),
            'email': session.get('email'),
            'name': session.get('name'),
            'role': session.get('role')
        }
    }

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)