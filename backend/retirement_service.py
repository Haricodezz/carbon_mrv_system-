import os
import qrcode
from io import BytesIO
from datetime import datetime
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import cm
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, HRFlowable, Image
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT

RETIREMENT_DIR = "retirement_certificates"
os.makedirs(RETIREMENT_DIR, exist_ok=True)


def generate_retirement_id(retirement_db_id: int) -> str:
    """Generate unique retirement ID like RET-0001-202603"""
    month = datetime.utcnow().strftime("%Y%m")
    return f"RET-{retirement_db_id:04d}-{month}"


def generate_qr_code(data: str) -> BytesIO:
    """Generate QR code image as BytesIO."""
    qr  = qrcode.QRCode(version=1, box_size=4, border=2)
    qr.add_data(data)
    qr.make(fit=True)
    img    = qr.make_image(fill_color="black", back_color="white")
    buffer = BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer


def generate_retirement_certificate(
    retirement_id:  str,
    retirement_db_id: int,
    company_name:   str,
    company_email:  str,
    wallet_address: str,
    amount_retired: float,
    reason:         str,
    blockchain_tx:  str,
    retired_at:     datetime,
) -> str:
    """Generate PDF retirement certificate. Returns file path."""

    filename = f"retirement_{retirement_id}_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}.pdf"
    filepath = os.path.join(RETIREMENT_DIR, filename)
    styles   = getSampleStyleSheet()

    # ── Custom styles ─────────────────────────────────────
    title_style = ParagraphStyle(
        "Title", parent=styles["Heading1"],
        fontSize=22, textColor=colors.HexColor("#1a7a4a"),
        alignment=TA_CENTER, spaceAfter=4
    )
    subtitle_style = ParagraphStyle(
        "Subtitle", parent=styles["Normal"],
        fontSize=11, textColor=colors.HexColor("#555555"),
        alignment=TA_CENTER, spaceAfter=20
    )
    section_style = ParagraphStyle(
        "Section", parent=styles["Heading2"],
        fontSize=12, textColor=colors.HexColor("#1a7a4a"),
        spaceBefore=14, spaceAfter=6
    )
    normal = ParagraphStyle(
        "Body", parent=styles["Normal"],
        fontSize=10, textColor=colors.HexColor("#333333"),
    )
    highlight_style = ParagraphStyle(
        "Highlight", parent=styles["Normal"],
        fontSize=16, textColor=colors.HexColor("#1a7a4a"),
        alignment=TA_CENTER, spaceBefore=8, spaceAfter=8
    )
    footer_style = ParagraphStyle(
        "Footer", parent=styles["Normal"],
        fontSize=8, textColor=colors.HexColor("#999999"),
        alignment=TA_CENTER
    )

    doc      = SimpleDocTemplate(filepath, pagesize=A4,
                                  topMargin=2*cm, bottomMargin=2*cm,
                                  leftMargin=2*cm, rightMargin=2*cm)
    elements = []

    # ── Header ────────────────────────────────────────────
    elements.append(Paragraph("🌿 Carbon MRV Registry", title_style))
    elements.append(Paragraph("Official Carbon Credit Retirement Certificate", subtitle_style))
    elements.append(HRFlowable(width="100%", thickness=2, color=colors.HexColor("#1a7a4a")))
    elements.append(Spacer(1, 0.4*cm))

    # ── Retirement ID + Date ───────────────────────────────
    elements.append(Paragraph(f"Retirement ID: <b>{retirement_id}</b>", normal))
    elements.append(Paragraph(f"Retired On: {retired_at.strftime('%B %d, %Y at %H:%M UTC')}", normal))
    elements.append(Spacer(1, 0.3*cm))

    # ── Highlight box ─────────────────────────────────────
    highlight_data = [[
        Paragraph(f"<b>{amount_retired:,.2f} CCT</b><br/><font size=10>Carbon Credits Permanently Retired</font>",
                  highlight_style)
    ]]
    highlight_table = Table(highlight_data, colWidths=[17*cm])
    highlight_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#e8f5e9")),
        ("ROUNDEDCORNERS", [8]),
        ("BOX",       (0, 0), (-1, -1), 1.5, colors.HexColor("#1a7a4a")),
        ("PADDING",   (0, 0), (-1, -1), 12),
        ("ALIGN",     (0, 0), (-1, -1), "CENTER"),
    ]))
    elements.append(highlight_table)
    elements.append(Spacer(1, 0.4*cm))

    # ── Company Info ──────────────────────────────────────
    elements.append(Paragraph("Organization Details", section_style))
    company_data = [
        ["Company Name",    company_name],
        ["Email",           company_email],
        ["Wallet Address",  wallet_address],
    ]
    company_table = Table(company_data, colWidths=[5*cm, 12*cm])
    company_table.setStyle(TableStyle([
        ("BACKGROUND",     (0, 0), (0, -1), colors.HexColor("#e8f5e9")),
        ("TEXTCOLOR",      (0, 0), (0, -1), colors.HexColor("#1a7a4a")),
        ("FONTNAME",       (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTSIZE",       (0, 0), (-1, -1), 9),
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.white, colors.HexColor("#f9f9f9")]),
        ("GRID",           (0, 0), (-1, -1), 0.5, colors.HexColor("#dddddd")),
        ("PADDING",        (0, 0), (-1, -1), 6),
    ]))
    elements.append(company_table)

    # ── Retirement Details ────────────────────────────────
    elements.append(Paragraph("Retirement Details", section_style))
    ret_data = [
        ["Credits Retired",  f"{amount_retired:,.2f} CCT"],
        ["CO₂ Equivalent",   f"{amount_retired / 3.67:,.2f} tons CO₂"],
        ["Retirement Reason", reason or "Carbon offset / ESG compliance"],
        ["Standard",         "IPCC Guidelines (Biomass × 0.47 × 3.67)"],
    ]
    ret_table = Table(ret_data, colWidths=[5*cm, 12*cm])
    ret_table.setStyle(TableStyle([
        ("BACKGROUND",     (0, 0), (0, -1), colors.HexColor("#e8f5e9")),
        ("TEXTCOLOR",      (0, 0), (0, -1), colors.HexColor("#1a7a4a")),
        ("FONTNAME",       (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTSIZE",       (0, 0), (-1, -1), 9),
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.white, colors.HexColor("#f9f9f9")]),
        ("GRID",           (0, 0), (-1, -1), 0.5, colors.HexColor("#dddddd")),
        ("PADDING",        (0, 0), (-1, -1), 6),
        ("BACKGROUND",     (0, 0), (-1, 0), colors.HexColor("#c8e6c9")),
        ("FONTNAME",       (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE",       (0, 0), (-1, 0), 11),
    ]))
    elements.append(ret_table)

    # ── Blockchain Proof ──────────────────────────────────
    elements.append(Paragraph("Blockchain Proof", section_style))
    tx_display  = blockchain_tx or "Pending"
    explorer_url = f"https://amoy.polygonscan.com/tx/{tx_display}"
    bc_data = [
        ["Network",          "Polygon Amoy Testnet"],
        ["Transaction Hash", tx_display],
        ["Explorer",         explorer_url],
        ["Status",           "PERMANENTLY RETIRED — Cannot be reused"],
    ]
    bc_table = Table(bc_data, colWidths=[5*cm, 12*cm])
    bc_table.setStyle(TableStyle([
        ("BACKGROUND",     (0, 0), (0, -1), colors.HexColor("#e3f2fd")),
        ("TEXTCOLOR",      (0, 0), (0, -1), colors.HexColor("#1565c0")),
        ("FONTNAME",       (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTSIZE",       (0, 0), (-1, -1), 8),
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.white, colors.HexColor("#f9f9f9")]),
        ("GRID",           (0, 0), (-1, -1), 0.5, colors.HexColor("#dddddd")),
        ("PADDING",        (0, 0), (-1, -1), 6),
        ("TEXTCOLOR",      (1, 3), (1, 3), colors.HexColor("#c62828")),
        ("FONTNAME",       (1, 3), (1, 3), "Helvetica-Bold"),
    ]))
    elements.append(bc_table)

    # ── QR Code ───────────────────────────────────────────
    elements.append(Spacer(1, 0.4*cm))
    elements.append(Paragraph("Scan to Verify on Blockchain", section_style))
    try:
        qr_buffer = generate_qr_code(explorer_url)
        qr_image  = Image(qr_buffer, width=3*cm, height=3*cm)
        qr_data   = [[qr_image, Paragraph(
            f"<b>Retirement ID:</b> {retirement_id}<br/>"
            f"Scan this QR code to verify this retirement<br/>"
            f"on the Polygon blockchain explorer.",
            ParagraphStyle("qr", parent=styles["Normal"], fontSize=9,
                           textColor=colors.HexColor("#333333"))
        )]]
        qr_table = Table(qr_data, colWidths=[4*cm, 13*cm])
        qr_table.setStyle(TableStyle([
            ("VALIGN",  (0, 0), (-1, -1), "MIDDLE"),
            ("PADDING", (0, 0), (-1, -1), 6),
        ]))
        elements.append(qr_table)
    except Exception:
        elements.append(Paragraph(f"Verify at: {explorer_url}", normal))

    # ── Methodology ───────────────────────────────────────
    elements.append(Spacer(1, 0.4*cm))
    elements.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor("#dddddd")))
    elements.append(Spacer(1, 0.3*cm))
    elements.append(Paragraph(
        "<b>Methodology:</b> Carbon credits measured using Sentinel-2 satellite imagery "
        "processed through XGBoost ML model. Carbon conversion: Biomass × 0.47 = Carbon (IPCC). "
        "CO₂ equivalent: Carbon × 3.67. Credits retired are permanently locked on Polygon blockchain "
        "and cannot be transferred or reused.",
        ParagraphStyle("method", parent=styles["Normal"], fontSize=8,
                       textColor=colors.HexColor("#666666"))
    ))

    # ── Footer ────────────────────────────────────────────
    elements.append(Spacer(1, 0.4*cm))
    elements.append(HRFlowable(width="100%", thickness=2, color=colors.HexColor("#1a7a4a")))
    elements.append(Spacer(1, 0.2*cm))
    elements.append(Paragraph(
        "Carbon MRV Registry · AI + Satellite + Blockchain · "
        "This retirement is immutably recorded on Polygon blockchain and is valid for government compliance.",
        footer_style
    ))

    doc.build(elements)
    print(f"[PDF] Retirement certificate: {filepath}")
    return filepath
