from flask import Blueprint, current_app, make_response
from io import BytesIO
import os, re, requests
from datetime import datetime
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_JUSTIFY
from reportlab.platypus import (
    BaseDocTemplate, PageTemplate, Frame, Paragraph, Spacer,
    Table, TableStyle, PageBreak, HRFlowable, KeepTogether
)
from reportlab.pdfgen import canvas as pdfcanvas
from reportlab.pdfbase.pdfmetrics import stringWidth

exportar_bp = Blueprint("exportar", __name__, url_prefix="/exportar")

# ── Cores ──────────────────────────────────────────────────────────────────
C_VERDE       = colors.HexColor("#1A6B3A")
C_VERDE_LT    = colors.HexColor("#E8F5EE")
C_AZUL        = colors.HexColor("#0D2B5E")
C_AZUL_LT     = colors.HexColor("#E6EFF8")
C_VERMELHO    = colors.HexColor("#C0392B")
C_VERM_LT     = colors.HexColor("#FDECEA")
C_LARANJA     = colors.HexColor("#D35400")
C_LAR_LT      = colors.HexColor("#FEF0E7")
C_ROXO        = colors.HexColor("#6C3483")
C_ROXO_LT     = colors.HexColor("#F4ECF7")
C_CINZA       = colors.HexColor("#555555")
C_CINZA_LT    = colors.HexColor("#F5F5F5")
C_AMARELO_LT  = colors.HexColor("#FFFDE7")
C_AMARELO     = colors.HexColor("#B7950B")

# Orientação → cor
CORES_ORI = {
    "SIM":        (C_VERDE,    C_VERDE_LT),
    "NÃO":        (C_VERMELHO, C_VERM_LT),
    "NEGOCIAÇÃO": (C_AMARELO,  C_AMARELO_LT),
    "LIBERADO":   (C_AZUL,     C_AZUL_LT),
    "OBSTRUÇÃO":  (C_VERMELHO, C_VERM_LT),
    "ABSTENÇÃO":  (C_CINZA,    C_CINZA_LT),
}

# Tipo de proposição → (cor_texto, cor_fundo)
def cor_tipo(projeto):
    p = (projeto or "").upper().split()[0] if projeto else ""
    if any(p.startswith(s) for s in ("REQ","RQS","RQU","REC")):
        # Verifica se é urgência
        proj_upper = (projeto or "").upper()
        if "URGÊN" in proj_upper or "URGENCIA" in proj_upper or "URGÊNCIA" in proj_upper:
            return C_VERMELHO, C_VERM_LT       # vermelho
        return C_LARANJA, C_LAR_LT             # laranja para outros req
    if p in ("PEC","PLP"):
        return C_ROXO, C_ROXO_LT               # roxo
    if p in ("MPV","PDL","PLV","PRS"):
        return C_LARANJA, C_LAR_LT             # laranja
    return C_AZUL, C_AZUL_LT                   # azul (PL padrão)

MESES_PT = {
    "January":"Janeiro","February":"Fevereiro","March":"Março",
    "April":"Abril","May":"Maio","June":"Junho","July":"Julho",
    "August":"Agosto","September":"Setembro","October":"Outubro",
    "November":"Novembro","December":"Dezembro"
}
def data_ptbr(dt_str):
    try:
        dt = datetime.fromisoformat(dt_str)
        mes_pt = MESES_PT.get(dt.strftime("%B"), dt.strftime("%B"))
        return f"{dt.day:02d} DE {mes_pt.upper()} DE {dt.year}"
    except Exception:
        return "DATA DESCONHECIDA"

def _hex(color):
    """Retorna string hex pura ex: '1a6b3a' para uso em tags XML do ReportLab"""
    try:
        r = int(round(color.red * 255))
        g = int(round(color.green * 255))
        b = int(round(color.blue * 255))
        return '%02x%02x%02x' % (r, g, b)
    except Exception:
        return '000000'

def _strip_html(s):
    txt = re.sub(r"<[^>]+>", " ", str(s or ""))
    txt = re.sub(r"&nbsp;", " ", txt)
    txt = re.sub(r"&amp;", "&", txt)
    txt = re.sub(r"\s+", " ", txt)
    return txt.strip()

# ── Cabeçalho / Rodapé ─────────────────────────────────────────────────────
def _header_footer(canvas, doc, logos, header_text):
    w, h = A4
    logo_min, logo_opo = logos
    canvas.saveState()

    # ── Cabeçalho ──
    cab_h = 1.8*cm
    # fundo branco cabeçalho
    canvas.setFillColor(colors.white)
    canvas.rect(0, h - cab_h, w, cab_h, fill=1, stroke=0)
    # linha verde
    canvas.setStrokeColor(C_VERDE)
    canvas.setLineWidth(1.5)
    canvas.line(0, h - cab_h, w, h - cab_h)
    # logos lado a lado à esquerda
    lw, lh = 2.0*cm, 1.2*cm
    y_logo = h - cab_h + (cab_h - lh)/2
    for path, xpos in [(logo_min, 0.8*cm), (logo_opo, 0.8*cm + lw + 0.2*cm)]:
        if path and os.path.exists(path):
            try:
                canvas.drawImage(path, xpos, y_logo, width=lw, height=lh,
                                 preserveAspectRatio=True, mask='auto')
            except Exception:
                pass
    # texto central
    canvas.setFillColor(C_AZUL)
    canvas.setFont("Helvetica-Bold", 9)
    tw = stringWidth(header_text, "Helvetica-Bold", 9)
    canvas.drawString((w - tw)/2, h - cab_h + 0.55*cm, header_text)

    # ── Rodapé ──
    rod_h = 1.5*cm
    canvas.setFillColor(colors.white)
    canvas.rect(0, 0, w, rod_h, fill=1, stroke=0)
    canvas.setStrokeColor(C_VERDE)
    canvas.setLineWidth(1)
    canvas.line(0, rod_h, w, rod_h)
    # mesmos logos no rodapé
    y_rod = (rod_h - lh)/2
    for path, xpos in [(logo_min, 0.8*cm), (logo_opo, 0.8*cm + lw + 0.2*cm)]:
        if path and os.path.exists(path):
            try:
                canvas.drawImage(path, xpos, y_rod, width=lw, height=lh,
                                 preserveAspectRatio=True, mask='auto')
            except Exception:
                pass
    # texto rodapé
    canvas.setFillColor(C_CINZA)
    canvas.setFont("Helvetica", 7.5)
    canvas.drawCentredString(w/2, 0.55*cm,
        "Lideranças da Minoria e da Oposição — Plenário / Câmara dos Deputados")
    canvas.setFont("Helvetica-Bold", 8)
    canvas.setFillColor(C_AZUL)
    canvas.drawRightString(w - 0.8*cm, 0.55*cm, str(doc.page))

    canvas.restoreState()

# ── Doc template ───────────────────────────────────────────────────────────
class PautaDocTemplate(BaseDocTemplate):
    def __init__(self, *args, **kwargs):
        self.pdf_title = kwargs.pop("pdf_title", None)
        super().__init__(*args, **kwargs)
    def build(self, flowables, **kwargs):
        def canvasmaker(*args, **kw):
            c = pdfcanvas.Canvas(*args, **kw)
            if self.pdf_title:
                c.setTitle(self.pdf_title)
            return c
        super().build(flowables, canvasmaker=canvasmaker)

# ── Dados ──────────────────────────────────────────────────────────────────
def _get_evento(evento_id):
    try:
        r = requests.get(f"https://dadosabertos.camara.leg.br/api/v2/eventos/{evento_id}", timeout=10)
        d = r.json().get("dados", {})
        return {
            "descricao": d.get("descricao",""),
            "dataHoraInicio": d.get("dataHoraInicio",""),
            "local": d.get("localCamara",{}).get("nome","Plenário")
                     if isinstance(d.get("localCamara"), dict) else d.get("localCamara","Plenário")
        }
    except Exception:
        return {"descricao":"","dataHoraInicio":"","local":"Plenário"}

def _get_itens(evento_id):
    try:
        from app import fetch_pauta, get_conn
        itens, _ = fetch_pauta(evento_id, force_reload=False)
        # Carrega resumos IA
        resumos = {}
        try:
            conn = get_conn()
            c = conn.cursor()
            c.execute('SELECT id_proposicao, resumo FROM resumos_ia WHERE evento_id=?', (evento_id,))
            resumos = {str(r[0]): r[1] for r in c.fetchall()}
            conn.close()
        except Exception:
            pass
        for item in itens:
            rid = str(item.get('id_principal',''))
            if rid in resumos:
                item['resumo_ia'] = resumos[rid]
        return itens
    except Exception as e:
        current_app.logger.error(f"Erro ao obter itens: {e}")
        return []

# ── Rota ──────────────────────────────────────────────────────────────────
@exportar_bp.route("/<int:evento_id>")
def exportar_pauta(evento_id):
    try:
        evento = _get_evento(evento_id)
        itens  = _get_itens(evento_id)
        if not itens:
            return "Nenhum item encontrado para esta pauta.", 200

        static_path = os.path.join(current_app.root_path, "static")
        logo_min = os.path.join(static_path, "logo_minoria.png")
        logo_opo = os.path.join(static_path, "logo_oposicao.png")

        # ── Estilos ──
        SS = getSampleStyleSheet()

        sTitle = ParagraphStyle("sTitle", parent=SS["Title"],
            fontSize=16, leading=20, alignment=TA_CENTER,
            textColor=C_AZUL, spaceAfter=6)
        sSubtitle = ParagraphStyle("sSubtitle", parent=SS["Normal"],
            fontSize=10, leading=13, alignment=TA_CENTER,
            textColor=C_CINZA, spaceAfter=12)
        sNormal = ParagraphStyle("sNormal", parent=SS["Normal"],
            fontSize=9.5, leading=13, wordWrap="CJK")
        sBold = ParagraphStyle("sBold", parent=SS["Normal"],
            fontName="Helvetica-Bold", fontSize=10, leading=13)
        # Nota técnica: fonte Courier para destacar
        sNota = ParagraphStyle("sNota", parent=SS["Normal"],
            fontName="Courier", fontSize=9, leading=13,
            textColor=colors.HexColor("#1a1a2a"), wordWrap="CJK",
            leftIndent=8, rightIndent=8)
        sOri = ParagraphStyle("sOri", parent=SS["Normal"],
            fontName="Helvetica-Bold", fontSize=13, leading=16,
            alignment=TA_CENTER)
        sResumoIA = ParagraphStyle("sResumoIA", parent=SS["Normal"],
            fontName="Helvetica-Oblique", fontSize=9, leading=12,
            textColor=C_VERDE, leftIndent=6)

        buffer = BytesIO()
        data_txt = data_ptbr(evento.get("dataHoraInicio",""))
        header_text = f"Sessão Deliberativa — Plenário  |  {data_txt}"

        doc = PautaDocTemplate(buffer,
            pdf_title=f"Pauta_{evento_id}",
            pagesize=A4,
            leftMargin=1.8*cm, rightMargin=1.8*cm,
            topMargin=2.4*cm, bottomMargin=2.2*cm)
        frame = Frame(doc.leftMargin, doc.bottomMargin,
                      doc.width, doc.height - 0.3*cm, id="normal")
        doc.addPageTemplates([PageTemplate(
            id="main", frames=[frame],
            onPage=lambda c, d: _header_footer(c, d, (logo_min, logo_opo), header_text)
        )])

        story = []

        # ── Capa ──
        story.append(Spacer(1, 0.5*cm))
        story.append(Paragraph("PAUTA DO PLENÁRIO", sTitle))
        story.append(Paragraph(
            f"{evento.get('descricao','')}  |  {data_txt}  |  {evento.get('local','Plenário')}",
            sSubtitle))
        story.append(HRFlowable(width="100%", thickness=1.5, color=C_VERDE, spaceAfter=8))

        # ── Tabela resumo ──
        story.append(Paragraph("Itens da Pauta", sBold))
        story.append(Spacer(1, 4))

        thead = [
            Paragraph("<b>#</b>", sNormal),
            Paragraph("<b>Proposição</b>", sNormal),
            Paragraph("<b>Ementa</b>", sNormal),
            Paragraph("<b>Orientação</b>", sNormal),
        ]
        tdata = [thead]
        for it in itens:
            cor_t, cor_bg = cor_tipo(it.get("projeto",""))
            ori = (it.get("orientacao","") or "").strip().upper()
            cor_ori, _ = CORES_ORI.get(ori, (C_CINZA, C_CINZA_LT))
            tdata.append([
                Paragraph(str(it.get("ordem","—")), sNormal),
                Paragraph(it.get("projeto","—"),
                    ParagraphStyle("pt"+str(i), parent=sNormal, fontName="Helvetica-Bold", textColor=cor_t)),
                Paragraph(_strip_html(it.get("ementa","—"))[:160] + "…", sNormal),
                Paragraph(ori or "—",
                    ParagraphStyle("po"+str(i), parent=sNormal, fontName="Helvetica-Bold", textColor=cor_ori)),
            ])

        tbl = Table(tdata, colWidths=[1.2*cm, 4.2*cm, 9.0*cm, 2.4*cm], repeatRows=1)
        tbl_style = [
            ("BACKGROUND", (0,0), (-1,0), C_AZUL),
            ("TEXTCOLOR",  (0,0), (-1,0), colors.white),
            ("FONTNAME",   (0,0), (-1,0), "Helvetica-Bold"),
            ("FONTSIZE",   (0,0), (-1,-1), 8.5),
            ("GRID",       (0,0), (-1,-1), 0.3, colors.HexColor("#cccccc")),
            ("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.white, C_CINZA_LT]),
            ("VALIGN",     (0,0), (-1,-1), "TOP"),
            ("TOPPADDING", (0,0), (-1,-1), 4),
            ("BOTTOMPADDING",(0,0),(-1,-1), 4),
            ("LEFTPADDING", (0,0), (-1,-1), 5),
        ]
        tbl.setStyle(TableStyle(tbl_style))
        story.append(tbl)
        story.append(PageBreak())

        # ── Itens detalhados ──
        for it in itens:
            projeto = it.get("projeto","")
            cor_t, cor_bg = cor_tipo(projeto)
            ori = (it.get("orientacao","") or "").strip().upper()
            cor_ori, cor_ori_bg = CORES_ORI.get(ori, (C_CINZA, C_CINZA_LT))
            resumo_materia = _strip_html(it.get("resumo_materia",""))
            resumo_ia      = it.get("resumo_ia","") or ""

            bloco = []

            # Cabeçalho do item: fundo colorido por tipo
            hdr_data = [[
                Paragraph(f'<b>Item {it.get("ordem","—")} — {projeto}</b>',
                           ParagraphStyle("hdr", parent=SS["Normal"],
                               fontName="Helvetica-Bold", fontSize=11, leading=14,
                               textColor=colors.white)),
            ]]
            hdr_tbl = Table(hdr_data, colWidths=[doc.width])
            hdr_tbl.setStyle(TableStyle([
                ("BACKGROUND", (0,0), (-1,-1), cor_t),
                ("TOPPADDING", (0,0), (-1,-1), 6),
                ("BOTTOMPADDING",(0,0),(-1,-1), 6),
                ("LEFTPADDING", (0,0), (-1,-1), 8),
                ]))
            bloco.append(hdr_tbl)

            # Info autor/relator/situação — fundo claro
            info_rows = [
                [Paragraph(f"<b>Autor:</b> {it.get('autor','N/D')}", sNormal),
                 Paragraph(f"<b>Relator:</b> {it.get('relator','N/D')}", sNormal)],
            ]
            if it.get("situacao") and it["situacao"] != "N/D":
                info_rows.append([
                    Paragraph(f"<b>Situação:</b> {it['situacao']}", sNormal),
                    Paragraph("", sNormal),
                ])
            info_tbl = Table(info_rows, colWidths=[doc.width/2, doc.width/2])
            info_tbl.setStyle(TableStyle([
                ("BACKGROUND", (0,0), (-1,-1), cor_bg),
                ("TOPPADDING", (0,0), (-1,-1), 4),
                ("BOTTOMPADDING",(0,0),(-1,-1), 4),
                ("LEFTPADDING", (0,0), (-1,-1), 8),
                ("LINEBELOW", (0,-1), (-1,-1), 0.5, cor_t),
            ]))
            bloco.append(info_tbl)

            # Ementa
            ementa = _strip_html(it.get("ementa",""))
            if ementa:
                bloco.append(Spacer(1, 3))
                bloco.append(Paragraph(ementa, sNormal))

            # Resumo IA em verde itálico
            if resumo_ia:
                bloco.append(Spacer(1, 2))
                bloco.append(Paragraph(f"<i>Resumo: {resumo_ia}</i>", sResumoIA))

            # Orientação — destaque grande
            if ori:
                bloco.append(Spacer(1, 5))
                ori_tbl = Table([[
                    Paragraph(f"ORIENTAÇÃO: {ori}",
                        ParagraphStyle("sOri2", parent=sOri, textColor=cor_ori))
                ]], colWidths=[doc.width])
                ori_tbl.setStyle(TableStyle([
                    ("BACKGROUND", (0,0), (-1,-1), cor_ori_bg),
                    ("TOPPADDING", (0,0), (-1,-1), 6),
                    ("BOTTOMPADDING",(0,0),(-1,-1), 6),
                    ("LINEBELOW",  (0,0), (-1,-1), 2, cor_ori),
                ]))
                bloco.append(ori_tbl)

            # Nota técnica — fonte Courier, fundo levemente cinza
            if resumo_materia:
                bloco.append(Spacer(1, 5))
                nota_hdr = Table([[Paragraph("<b>NOTA TÉCNICA</b>",
                    ParagraphStyle("nh", parent=SS["Normal"], fontName="Helvetica-Bold",
                        fontSize=8, textColor=colors.white))]],
                    colWidths=[doc.width])
                nota_hdr.setStyle(TableStyle([
                    ("BACKGROUND",(0,0),(-1,-1), C_CINZA),
                    ("TOPPADDING",(0,0),(-1,-1), 3),
                    ("BOTTOMPADDING",(0,0),(-1,-1), 3),
                    ("LEFTPADDING",(0,0),(-1,-1), 8),
                ]))
                bloco.append(nota_hdr)

                nota_tbl = Table([[Paragraph(resumo_materia, sNota)]],
                    colWidths=[doc.width])
                nota_tbl.setStyle(TableStyle([
                    ("BACKGROUND",(0,0),(-1,-1), colors.HexColor("#F8F8F8")),
                    ("TOPPADDING",(0,0),(-1,-1), 6),
                    ("BOTTOMPADDING",(0,0),(-1,-1), 6),
                    ("LEFTPADDING",(0,0),(-1,-1), 8),
                    ("RIGHTPADDING",(0,0),(-1,-1), 8),
                    ("BOX",(0,0),(-1,-1), 0.5, C_CINZA),
                ]))
                bloco.append(nota_tbl)

            bloco.append(Spacer(1, 14))
            story.append(KeepTogether(bloco[:4]))  # cabeçalho junto
            story.extend(bloco[4:])

        doc.build(story)
        pdf = buffer.getvalue()
        buffer.close()

        resp = make_response(pdf)
        resp.headers["Content-Type"] = "application/pdf"
        resp.headers["Content-Disposition"] = f'inline; filename="Pauta_{evento_id}.pdf"'
        return resp

    except Exception as e:
        import traceback
        current_app.logger.error(f"Erro exportar_pauta {evento_id}: {e}")
        return f"Erro ao gerar PDF: {e}<br><pre>{traceback.format_exc()}</pre>", 200
