from flask import Blueprint, current_app, make_response
from io import BytesIO
import os, re, requests
from datetime import datetime
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER
from reportlab.platypus import (
    BaseDocTemplate, PageTemplate, Frame, Paragraph, Spacer,
    Table, TableStyle, PageBreak, HRFlowable
)
from reportlab.pdfgen import canvas as pdfcanvas
from reportlab.pdfbase.pdfmetrics import stringWidth

exportar_bp = Blueprint("exportar", __name__, url_prefix="/exportar")

COR_VERDE        = colors.HexColor("#1A6B3A")
COR_VERDE_CLARO  = colors.HexColor("#E8F5EE")
COR_AZUL         = colors.HexColor("#0D2B5E")
COR_AZUL_CLARO   = colors.HexColor("#E8EEF7")
COR_CINZA        = colors.HexColor("#555555")
COR_CINZA_CLARO  = colors.HexColor("#F5F5F5")
COR_BORDA        = colors.HexColor("#CCCCCC")

CORES_ORI = {
    "SIM":        (colors.HexColor("#1A6B3A"), colors.HexColor("#E8F5EE")),
    "NÃO":        (colors.HexColor("#8B0000"), colors.HexColor("#FDEAEA")),
    "OBSTRUÇÃO":  (colors.HexColor("#8B0000"), colors.HexColor("#FDEAEA")),
    "NEGOCIAÇÃO": (colors.HexColor("#7B5C00"), colors.HexColor("#FFF8E1")),
    "LIBERADO":   (colors.HexColor("#7B5C00"), colors.HexColor("#FFF8E1")),
    "ABSTENÇÃO":  (colors.HexColor("#555555"), colors.HexColor("#F5F5F5")),
}

MESES_PT = {"January":"Janeiro","February":"Fevereiro","March":"Março","April":"Abril",
            "May":"Maio","June":"Junho","July":"Julho","August":"Agosto",
            "September":"Setembro","October":"Outubro","November":"Novembro","December":"Dezembro"}

def _data(s):
    try:
        dt = datetime.fromisoformat(s)
        return f"{dt.day:02d} de {MESES_PT.get(dt.strftime('%B'),'')} de {dt.year}", dt.strftime("%H:%M")
    except: return "", ""

def _strip(s):
    s = re.sub(r'<br\s*/?>', '\n', str(s or ''), flags=re.I)
    s = re.sub(r'<li[^>]*>', '• ', s, flags=re.I)
    s = re.sub(r'<[^>]+>', '', s)
    return re.sub(r'\n{3,}', '\n\n', s).strip()

def _header_footer(canvas, doc, logos, h1, h2):
    w, h = A4
    canvas.saveState()
    canvas.setFillColor(COR_VERDE)
    canvas.rect(0, h-2.0*cm, w, 2.0*cm, fill=1, stroke=0)
    for path, x in zip(logos, [0.5*cm, w-3.8*cm]):
        if path and os.path.exists(path):
            try: canvas.drawImage(path, x, h-1.85*cm, width=3.2*cm, preserveAspectRatio=True, mask='auto')
            except: pass
    canvas.setFillColor(colors.white)
    canvas.setFont("Helvetica-Bold", 9.5)
    canvas.drawCentredString(w/2, h-0.9*cm, h1)
    canvas.setFont("Helvetica", 7.5)
    canvas.drawCentredString(w/2, h-1.45*cm, h2)
    canvas.setStrokeColor(COR_VERDE)
    canvas.setLineWidth(0.8)
    canvas.line(1.5*cm, 1.4*cm, w-1.5*cm, 1.4*cm)
    canvas.setFillColor(COR_CINZA)
    canvas.setFont("Helvetica", 7.5)
    canvas.drawString(1.6*cm, 0.9*cm, "Liderança da Minoria — Plenário / Câmara dos Deputados")
    canvas.drawRightString(w-1.6*cm, 0.9*cm, f"Página {doc.page}")
    canvas.restoreState()

class PautaDoc(BaseDocTemplate):
    def __init__(self, *a, **kw):
        self._title = kw.pop("pdf_title", None)
        super().__init__(*a, **kw)
    def build(self, flowables, **kw):
        def cm(*a, **k):
            c = pdfcanvas.Canvas(*a, **k)
            if self._title: c.setTitle(self._title)
            return c
        super().build(flowables, canvasmaker=cm)

def _evento(id):
    try:
        r = requests.get(f"https://dadosabertos.camara.leg.br/api/v2/eventos/{id}", timeout=10)
        d = r.json().get("dados", {})
        return {"descricao": d.get("descricao","Sessão Deliberativa"),
                "dataHoraInicio": d.get("dataHoraInicio",""),
                "local": d.get("localCamara",{}).get("nome","Plenário") if isinstance(d.get("localCamara"),dict) else "Plenário"}
    except: return {"descricao":"Sessão Deliberativa","dataHoraInicio":"","local":"Plenário"}

def _itens(id):
    try:
        from app import fetch_pauta, pauta_cache
        k = str(id)
        if k in pauta_cache: return pauta_cache[k]['itens']
        its, _ = fetch_pauta(id, force_reload=False)
        return its if isinstance(its, list) else []
    except Exception as e:
        current_app.logger.error(f"Erro itens: {e}"); return []

@exportar_bp.route("/<int:evento_id>")
def exportar_pauta(evento_id):
    try:
        ev = _evento(evento_id)
        its = _itens(evento_id)
        if not its: return "Nenhum item encontrado.", 200

        sp = os.path.join(current_app.root_path, "static")
        logos = [os.path.join(sp,"logo_minoria.png"), os.path.join(sp,"logo_oposicao.png")]

        SS = getSampleStyleSheet()
        N  = ParagraphStyle("N",  parent=SS["Normal"], fontSize=9.5, leading=13, wordWrap="CJK")
        S  = ParagraphStyle("S",  parent=SS["Normal"], fontSize=8.5, leading=12, textColor=COR_CINZA)
        B  = ParagraphStyle("B",  parent=SS["Normal"], fontSize=10,  leading=13, fontName="Helvetica-Bold")
        T  = ParagraphStyle("T",  parent=SS["Title"],  fontSize=13,  leading=16, alignment=TA_CENTER, textColor=COR_VERDE)
        H  = ParagraphStyle("H",  parent=SS["Normal"], fontSize=11,  leading=14, fontName="Helvetica-Bold", textColor=COR_AZUL)
        NT = ParagraphStyle("NT", parent=SS["Normal"], fontSize=9.5, leading=14, wordWrap="CJK")

        buf = BytesIO()
        doc = PautaDoc(buf, pdf_title=f"Pauta_{evento_id}", pagesize=A4,
                       leftMargin=1.8*cm, rightMargin=1.8*cm, topMargin=2.4*cm, bottomMargin=2.0*cm)
        frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height-0.2*cm, id="n")

        dt_str = ev.get("dataHoraInicio","")
        data_s, hora_s = _data(dt_str)
        h1 = "Resumo da Pauta — Sessão Deliberativa do Plenário"
        h2 = f"{data_s}  |  {hora_s}  |  {ev.get('local','')}"

        doc.addPageTemplates([PageTemplate(id="m", frames=[frame],
            onPage=lambda c,d: _header_footer(c,d,logos,h1,h2))])

        story = []
        story.append(Spacer(1,4))
        story.append(Paragraph("Sessão Deliberativa — Plenário da Câmara dos Deputados", T))
        story.append(Paragraph(f"<b>Data:</b> {data_s} &nbsp; <b>Hora:</b> {hora_s} &nbsp; <b>Local:</b> {ev.get('local','')}", S))
        story.append(Spacer(1,8))
        story.append(HRFlowable(width="100%", thickness=1, color=COR_VERDE))
        story.append(Spacer(1,8))

        # Tabela resumo
        story.append(Paragraph("Visão Geral da Pauta", B))
        story.append(Spacer(1,4))
        rows = [[Paragraph("<b>Item</b>",S), Paragraph("<b>Proposição / Ementa</b>",S),
                 Paragraph("<b>Autor / Relator</b>",S), Paragraph("<b>Orientação</b>",S)]]
        for it in its:
            ori = (it.get("orientacao") or "N/D").upper()
            cor_o, _ = CORES_ORI.get(ori, (COR_CINZA, COR_CINZA_CLARO))
            em = _strip(it.get("ementa",""))[:110] + "..."
            rows.append([
                Paragraph(str(it.get("ordem","—")), S),
                Paragraph(f"<b>{it.get('projeto','—')}</b><br/><font size='7.5' color='#555555'>{em}</font>", S),
                Paragraph(f"{str(it.get('autor','N/D'))[:40]}<br/><i>Rel: {it.get('relator','N/D')}</i>", S),
                Paragraph(f"<b>{ori}</b>", ParagraphStyle("oi",parent=S,textColor=cor_o,fontName="Helvetica-Bold")),
            ])
        tbl = Table(rows, colWidths=[1.0*cm, 7.5*cm, 4.5*cm, 2.2*cm])
        tbl.setStyle(TableStyle([
            ("BACKGROUND",(0,0),(-1,0),COR_VERDE_CLARO),
            ("GRID",(0,0),(-1,-1),0.3,COR_BORDA),
            ("FONTSIZE",(0,0),(-1,-1),8.5),
            ("VALIGN",(0,0),(-1,-1),"TOP"),
            ("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white,COR_CINZA_CLARO]),
            ("TOPPADDING",(0,0),(-1,-1),4),("BOTTOMPADDING",(0,0),(-1,-1),4),
        ]))
        story.append(tbl)
        story.append(PageBreak())

        # Detalhes
        for it in its:
            ori = (it.get("orientacao") or "").upper()
            cor_o, bg_o = CORES_ORI.get(ori, (COR_CINZA, COR_CINZA_CLARO))

            ih = Table([[
                Paragraph(f"<b>Item {it.get('ordem','—')} — {it.get('projeto','')}</b>", H),
                Paragraph(f"<b>{ori}</b>", ParagraphStyle("wh",parent=B,textColor=colors.white,alignment=TA_CENTER)) if ori else Paragraph("",B)
            ]], colWidths=[doc.width-2.5*cm, 2.5*cm])
            ih.setStyle(TableStyle([
                ("BACKGROUND",(0,0),(0,0),COR_AZUL_CLARO),
                ("BACKGROUND",(1,0),(1,0),cor_o),
                ("VALIGN",(0,0),(-1,-1),"MIDDLE"),
                ("TOPPADDING",(0,0),(-1,-1),5),("BOTTOMPADDING",(0,0),(-1,-1),5),
                ("LEFTPADDING",(0,0),(-1,-1),6),
            ]))
            story.append(ih)
            story.append(Spacer(1,4))

            meta = Table([[
                Paragraph(f"<b>Autor(es):</b> {it.get('autor','N/D')}", S),
                Paragraph(f"<b>Relator:</b> {it.get('relator','N/D')}", S),
                Paragraph(f"<b>Situação:</b> {it.get('situacao','N/D')}", S),
            ]], colWidths=[doc.width/3]*3)
            meta.setStyle(TableStyle([
                ("GRID",(0,0),(-1,-1),0.3,COR_BORDA),
                ("BACKGROUND",(0,0),(-1,-1),COR_CINZA_CLARO),
                ("TOPPADDING",(0,0),(-1,-1),3),("BOTTOMPADDING",(0,0),(-1,-1),3),
                ("LEFTPADDING",(0,0),(-1,-1),5),("FONTSIZE",(0,0),(-1,-1),8.5),
            ]))
            story.append(meta)
            story.append(Spacer(1,4))

            ementa = _strip(it.get("ementa",""))
            if ementa:
                story.append(Paragraph(f"<b>Ementa:</b> {ementa}", S))
                story.append(Spacer(1,6))

            nota = _strip(it.get("resumo_materia",""))
            if nota:
                story.append(Paragraph("Resumo / Nota Técnica", B))
                nb = Table([[Paragraph(nota.replace('\n','<br/>'), NT)]],
                            colWidths=[doc.width])
                nb.setStyle(TableStyle([
                    ("BACKGROUND",(0,0),(-1,-1),COR_AZUL_CLARO),
                    ("GRID",(0,0),(-1,-1),0.3,COR_BORDA),
                    ("TOPPADDING",(0,0),(-1,-1),6),("BOTTOMPADDING",(0,0),(-1,-1),6),
                    ("LEFTPADDING",(0,0),(-1,-1),8),("RIGHTPADDING",(0,0),(-1,-1),8),
                ]))
                story.append(nb)
                story.append(Spacer(1,8))

            story.append(HRFlowable(width="100%", thickness=0.5, color=COR_BORDA))
            story.append(Spacer(1,10))

        doc.build(story)
        pdf = buf.getvalue(); buf.close()
        resp = make_response(pdf)
        resp.headers["Content-Type"] = "application/pdf"
        resp.headers["Content-Disposition"] = f'inline; filename="Pauta_Plenario_{evento_id}.pdf"'
        return resp

    except Exception as e:
        current_app.logger.error(f"Erro exportar {evento_id}: {e}")
        return f"Erro ao gerar PDF: {e}", 200
