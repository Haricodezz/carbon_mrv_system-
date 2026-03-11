import os
from datetime import datetime
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import cm
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, HRFlowable
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT

CERT_DIR = "certificates"
os.makedirs(CERT_DIR, exist_ok=True)


def generate_certificate(
    project_id: int,
    user_id: int,
    farmer_name: str,
    farmer_email: str,
    wallet_address: str,
    area_ha: float,
    carbon_stock: float,
    credits_issued: float,
    buffer_held: float,
    blockchain_tx: str,
    biomass_per_ha: float,
    carbon_per_ha: float,
    credits_per_ha: float,
) -> str:
    """Generate PDF certificate and return file path."""

    filename  = f"certificate_project_{project_id}_user_{user_id}_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}.pdf"
    filepath  = os.path.join(CERT_DIR, filename)
    styles    = getSampleStyleSheet()
    issued_at = datetime.utcnow().strftime("%B %d, %Y at %H:%M UTC")

    # Custom styles
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
        alignment=TA_LEFT
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
    elements.append(Paragraph("Official Carbon Credit Certificate", subtitle_style))
    elements.append(HRFlowable(width="100%", thickness=2, color=colors.HexColor("#1a7a4a")))
    elements.append(Spacer(1, 0.4*cm))

    # ── Certificate number ────────────────────────────────
    cert_no = f"CCT-{project_id:04d}-{user_id:04d}-{datetime.utcnow().strftime('%Y%m')}"
    elements.append(Paragraph(f"Certificate No: <b>{cert_no}</b>", normal))
    elements.append(Paragraph(f"Issued: {issued_at}", normal))
    elements.append(Spacer(1, 0.4*cm))

    # ── Farmer Info ───────────────────────────────────────
    elements.append(Paragraph("Farmer Details", section_style))
    farmer_data = [
        ["Name",           farmer_name],
        ["Email",          farmer_email],
        ["Wallet Address", wallet_address],
        ["User ID",        f"#{user_id}"],
    ]
    farmer_table = Table(farmer_data, colWidths=[5*cm, 12*cm])
    farmer_table.setStyle(TableStyle([
        ("BACKGROUND",  (0, 0), (0, -1), colors.HexColor("#e8f5e9")),
        ("TEXTCOLOR",   (0, 0), (0, -1), colors.HexColor("#1a7a4a")),
        ("FONTNAME",    (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTSIZE",    (0, 0), (-1, -1), 9),
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.white, colors.HexColor("#f9f9f9")]),
        ("GRID",        (0, 0), (-1, -1), 0.5, colors.HexColor("#dddddd")),
        ("PADDING",     (0, 0), (-1, -1), 6),
        ("WORDWRAP",    (1, 0), (1, -1), True),
    ]))
    elements.append(farmer_table)

    # ── Project Info ──────────────────────────────────────
    elements.append(Paragraph("Project Details", section_style))
    project_data = [
        ["Project ID",     f"#{project_id}"],
        ["Forest Area",    f"{area_ha:,.2f} hectares"],
        ["Carbon Stock",   f"{carbon_stock:,.2f} tons CO₂"],
        ["Biomass / ha",   f"{biomass_per_ha:.4f} tons/ha"],
        ["Carbon / ha",    f"{carbon_per_ha:.4f} tons CO₂/ha"],
        ["Credits / ha",   f"{credits_per_ha:.4f} CCT/ha"],
    ]
    project_table = Table(project_data, colWidths=[5*cm, 12*cm])
    project_table.setStyle(TableStyle([
        ("BACKGROUND",     (0, 0), (0, -1), colors.HexColor("#e8f5e9")),
        ("TEXTCOLOR",      (0, 0), (0, -1), colors.HexColor("#1a7a4a")),
        ("FONTNAME",       (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTSIZE",       (0, 0), (-1, -1), 9),
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.white, colors.HexColor("#f9f9f9")]),
        ("GRID",           (0, 0), (-1, -1), 0.5, colors.HexColor("#dddddd")),
        ("PADDING",        (0, 0), (-1, -1), 6),
    ]))
    elements.append(project_table)

    # ── Credit Summary ────────────────────────────────────
    elements.append(Paragraph("Carbon Credit Summary", section_style))
    credit_data = [
        ["Total Carbon Credits",   f"{carbon_stock * 1.0:,.2f} CCT"],
        ["Credits to Farmer (80%)", f"{credits_issued:,.2f} CCT"],
        ["Buffer Pool (20%)",       f"{buffer_held:,.2f} CCT"],
        ["Calculation",             "Biomass × 0.47 × 3.67 = CCT"],
    ]
    credit_table = Table(credit_data, colWidths=[6*cm, 11*cm])
    credit_table.setStyle(TableStyle([
        ("BACKGROUND",     (0, 0), (0, -1), colors.HexColor("#e8f5e9")),
        ("TEXTCOLOR",      (0, 0), (0, -1), colors.HexColor("#1a7a4a")),
        ("FONTNAME",       (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTSIZE",       (0, 0), (-1, -1), 9),
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [colors.white, colors.HexColor("#f9f9f9")]),
        ("GRID",           (0, 0), (-1, -1), 0.5, colors.HexColor("#dddddd")),
        ("PADDING",        (0, 0), (-1, -1), 6),
        # Highlight farmer credits row
        ("BACKGROUND",     (0, 1), (-1, 1), colors.HexColor("#c8e6c9")),
        ("FONTNAME",       (0, 1), (-1, 1), "Helvetica-Bold"),
        ("FONTSIZE",       (0, 1), (-1, 1), 11),
    ]))
    elements.append(credit_table)

    # ── Blockchain Proof ──────────────────────────────────
    elements.append(Paragraph("Blockchain Proof", section_style))
    tx_display = blockchain_tx if blockchain_tx else "Pending"
    bc_data = [
        ["Network",          "Polygon Amoy Testnet"],
        ["Transaction Hash", tx_display],
        ["Explorer",         f"https://amoy.polygonscan.com/tx/{tx_display}"],
        ["Smart Contract",   "0xa90d9ae8b930d8a13a6b4b9eecfae72085bbe6c2"],
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
        ("WORDWRAP",       (1, 0), (1, -1), True),
    ]))
    elements.append(bc_table)

    # ── Methodology ───────────────────────────────────────
    elements.append(Spacer(1, 0.5*cm))
    elements.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor("#dddddd")))
    elements.append(Spacer(1, 0.3*cm))
    elements.append(Paragraph(
        "<b>Methodology:</b> Carbon stock measured using Sentinel-2 satellite imagery "
        "(NDVI + EVI indices) processed through a trained XGBoost machine learning model. "
        "Carbon conversion follows IPCC standard (Biomass × 0.47 = Carbon). "
        "CO₂ equivalent calculated as Carbon × 3.67. "
        "20% buffer pool held as insurance against carbon reversals.",
        ParagraphStyle("method", parent=styles["Normal"], fontSize=8,
                       textColor=colors.HexColor("#666666"))
    ))

    # ── Footer ────────────────────────────────────────────
    elements.append(Spacer(1, 0.5*cm))
    elements.append(HRFlowable(width="100%", thickness=2, color=colors.HexColor("#1a7a4a")))
    elements.append(Spacer(1, 0.2*cm))
    elements.append(Paragraph(
        "Carbon MRV Registry · AI + Satellite + Blockchain · This certificate is immutably recorded on Polygon blockchain.",
        footer_style
    ))

    doc.build(elements)
    print(f"[PDF] Certificate generated: {filepath}")
    return filepath
