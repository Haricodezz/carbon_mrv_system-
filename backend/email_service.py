import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders

# ── Config (set these in .env) ────────────────────────────
SMTP_HOST     = os.getenv("SMTP_HOST",     "smtp.gmail.com")
SMTP_PORT     = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER     = os.getenv("SMTP_USER",     "")   # your Gmail
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")   # Gmail app password
FROM_NAME     = "Carbon MRV Registry"


def send_email(to_email: str, subject: str, html_body: str, attachment_path: str = None):
    """Send HTML email with optional PDF attachment."""
    if not SMTP_USER or not SMTP_PASSWORD:
        print(f"[EMAIL SKIPPED] SMTP not configured. Would send to {to_email}: {subject}")
        return False

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = f"{FROM_NAME} <{SMTP_USER}>"
        msg["To"]      = to_email

        msg.attach(MIMEText(html_body, "html"))

        # Attach PDF if provided
        if attachment_path and os.path.exists(attachment_path):
            with open(attachment_path, "rb") as f:
                part = MIMEBase("application", "octet-stream")
                part.set_payload(f.read())
            encoders.encode_base64(part)
            part.add_header(
                "Content-Disposition",
                f"attachment; filename={os.path.basename(attachment_path)}"
            )
            msg.attach(part)

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.send_message(msg)

        print(f"[EMAIL SENT] {subject} → {to_email}")
        return True

    except Exception as e:
        print(f"[EMAIL ERROR] {e}")
        return False


# ── Email Templates ───────────────────────────────────────

def send_credits_minted_email(
    to_email: str,
    farmer_name: str,
    credits_issued: float,
    blockchain_tx: str,
    project_id: int,
    area_ha: float,
    carbon_stock: float,
    attachment_path: str = None,
):
    subject = f"✅ {credits_issued:,.2f} Carbon Credits Minted to Your Wallet"
    html = f"""
    <html><body style="font-family:Arial,sans-serif;max-width:600px;margin:auto;padding:20px">
      <div style="background:#1a7a4a;padding:20px;border-radius:8px;text-align:center">
        <h1 style="color:white;margin:0">🌿 Carbon MRV Registry</h1>
        <p style="color:#a8f0c6;margin:5px 0">Carbon Credits Successfully Minted</p>
      </div>

      <div style="padding:20px;background:#f9f9f9;border-radius:8px;margin-top:16px">
        <p>Dear <strong>{farmer_name}</strong>,</p>
        <p>Your carbon credits have been successfully minted on the Polygon blockchain.</p>

        <table style="width:100%;border-collapse:collapse;margin:16px 0">
          <tr style="background:#e8f5e9">
            <td style="padding:10px;border:1px solid #ddd"><strong>Credits Issued</strong></td>
            <td style="padding:10px;border:1px solid #ddd;color:#1a7a4a;font-size:18px">
              <strong>{credits_issued:,.2f} CCT</strong>
            </td>
          </tr>
          <tr>
            <td style="padding:10px;border:1px solid #ddd"><strong>Project ID</strong></td>
            <td style="padding:10px;border:1px solid #ddd">#{project_id}</td>
          </tr>
          <tr style="background:#f5f5f5">
            <td style="padding:10px;border:1px solid #ddd"><strong>Forest Area</strong></td>
            <td style="padding:10px;border:1px solid #ddd">{area_ha:,.2f} hectares</td>
          </tr>
          <tr>
            <td style="padding:10px;border:1px solid #ddd"><strong>Carbon Stock</strong></td>
            <td style="padding:10px;border:1px solid #ddd">{carbon_stock:,.2f} tons CO₂</td>
          </tr>
          <tr style="background:#f5f5f5">
            <td style="padding:10px;border:1px solid #ddd"><strong>Blockchain TX</strong></td>
            <td style="padding:10px;border:1px solid #ddd;word-break:break-all;font-size:12px">
              <a href="https://amoy.polygonscan.com/tx/{blockchain_tx}">{blockchain_tx}</a>
            </td>
          </tr>
        </table>

        <p style="color:#666;font-size:13px">
          Your PDF certificate is attached to this email as proof of carbon credit issuance.
        </p>
      </div>

      <div style="text-align:center;padding:16px;color:#999;font-size:12px">
        Carbon MRV Registry · Powered by Satellite AI + Polygon Blockchain
      </div>
    </body></html>
    """
    return send_email(to_email, subject, html, attachment_path)


def send_welcome_email(to_email: str, farmer_name: str):
    subject = "Welcome to Carbon MRV Registry 🌿"
    html = f"""
    <html><body style="font-family:Arial,sans-serif;max-width:600px;margin:auto;padding:20px">
      <div style="background:#1a7a4a;padding:20px;border-radius:8px;text-align:center">
        <h1 style="color:white">🌿 Welcome to Carbon MRV Registry</h1>
      </div>
      <div style="padding:20px">
        <p>Dear <strong>{farmer_name}</strong>,</p>
        <p>Your account has been created successfully.</p>
        <p>You can now register your forest land and start earning carbon credits.</p>
        <p>Every month your land will be scanned by satellite and new credits will be
        minted automatically based on carbon growth.</p>
        <br>
        <p>Best regards,<br>Carbon MRV Registry Team</p>
      </div>
    </body></html>
    """
    return send_email(to_email, subject, html)


def send_carbon_loss_alert(
    to_email: str,
    farmer_name: str,
    project_id: int,
    carbon_loss: float,
    buffer_penalty: float,
):
    subject = f"⚠️ Carbon Loss Alert — Project #{project_id}"
    html = f"""
    <html><body style="font-family:Arial,sans-serif;max-width:600px;margin:auto;padding:20px">
      <div style="background:#c62828;padding:20px;border-radius:8px;text-align:center">
        <h1 style="color:white">⚠️ Carbon Loss Detected</h1>
      </div>
      <div style="padding:20px">
        <p>Dear <strong>{farmer_name}</strong>,</p>
        <p>A decrease in carbon stock has been detected in your project <strong>#{project_id}</strong>.</p>
        <table style="width:100%;border-collapse:collapse;margin:16px 0">
          <tr style="background:#ffebee">
            <td style="padding:10px;border:1px solid #ddd"><strong>Carbon Loss</strong></td>
            <td style="padding:10px;border:1px solid #ddd;color:#c62828">
              <strong>{carbon_loss:,.2f} tons CO₂</strong>
            </td>
          </tr>
          <tr>
            <td style="padding:10px;border:1px solid #ddd"><strong>Buffer Penalty</strong></td>
            <td style="padding:10px;border:1px solid #ddd">{buffer_penalty:,.2f} CCT deducted</td>
          </tr>
        </table>
        <p>This may be due to deforestation, wildfire, or seasonal variation.
        Please check your land and contact support if needed.</p>
      </div>
    </body></html>
    """
    return send_email(to_email, subject, html)