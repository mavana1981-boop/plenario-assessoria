from flask import Blueprint, current_app, make_response
from io import BytesIO
import os
import re
import requests
from datetime import datetime
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (
    BaseDocTemplate, PageTemplate, Frame, Paragraph, Spacer,
    Table, TableStyle, PageBreak
)
from reportlab.pdfgen import canvas as pdfcanvas
from reportlab.pdfbase.pdfmetrics import stringWidth

exportar_bp = Blueprint("exportar", __name__, url_prefix="/exportar")

# ---------------------------------------------------------------------
# Tradução manual de meses
# ---------------------------------------------------------------------
MESES_PT = {
    "January": "Janeiro", "February": "Fevereiro", "March": "Março",
    "April": "Abril", "May": "Maio", "June": "Junho",
    "July": "Julho", "August": "Agosto", "September": "Setembro",
    "October": "Outubro", "November": "Novembro", "December": "Dezembro"
}

def data_ptbr(dt_str):
    try:
        dt = datetime.fromisoformat(dt_str)
        mes_en = dt.strftime("%B")
        mes_pt = MESES_PT.get(mes_en, mes_en)
        return f"{dt.day:02d} DE {mes_pt.upper()} DE {dt.year}"
    except Exception:
        return "DATA DESCONHECIDA"

def _strip_html(s):
    import html as _h
    import xml.sax.saxutils as _x
    t = re.sub(r'<br\s*/?>', '\n', str(s or ''), flags=re.IGNORECASE)
    t = re.sub(r'</p>',      '\n', t, flags=re.IGNORECASE)
    t = re.sub(r'</li>',     '\n', t, flags=re.IGNORECASE)
    t = re.sub(r'<li[^>]*>', '- ', t, flags=re.IGNORECASE)
    t = re.sub(r'<[^>]+>',   '',   t)
    t = _h.unescape(t).strip()
    return _x.escape(t)

def _get_itens(evento_id):
    try:
        from app import fetch_pauta, load_notas
        itens, _ = fetch_pauta(evento_id, force_reload=False)
        # Busca notas SEMPRE frescas do banco
        notas = load_notas()
        for item in itens:
            key = f"PROP_{item.get('id_principal','')}"
            if key in notas and notas[key].get('resumo_materia'):
                item['resumo_materia'] = notas[key]['resumo_materia']
        return itens
    except Exception as e:
        current_app.logger.error(f"Erro ao obter itens: {e}")
        return []
def _header_footer(canvas, doc, logos, header_text):
    w, h = A4
    camara_path, pl_path = logos
    canvas.saveState()

    # linha superior
    canvas.setStrokeColorRGB(0, 0.4, 0.2)
    canvas.line(1.5*cm, h-1.8*cm, w-1.5*cm, h-1.8*cm)

    # logos
    for path, x in [(camara_path, 1.5*cm), (pl_path, w-3.7*cm)]:
        if os.path.exists(path):
            try:
                canvas.drawImage(path, x, h-2.5*cm, width=2.3*cm,
                                 preserveAspectRatio=True, mask='auto')
            except Exception:
                pass

    # título central
    canvas.setFont("Helvetica-Bold", 10)
    text_w = stringWidth(header_text, "Helvetica-Bold", 10)
    canvas.drawString((w - text_w) / 2, h - 1.7*cm, header_text)

    # rodapé
    canvas.setStrokeColorRGB(0, 0.4, 0.2)
    canvas.line(1.5*cm, 1.5*cm, w-1.5*cm, 1.5*cm)
    canvas.setFont("Helvetica", 9)
    canvas.drawString(1.6*cm, 1.1*cm, "Liderança do Partido Liberal — Câmara dos Deputados")
    canvas.drawRightString(w-1.6*cm, 1.1*cm, str(doc.page))
    canvas.restoreState()

# ---------------------------------------------------------------------
# Documento personalizado
# ---------------------------------------------------------------------
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

# ---------------------------------------------------------------------
# Funções de dados
# ---------------------------------------------------------------------
def _get_evento(evento_id):
    url = f"https://dadosabertos.camara.leg.br/api/v2/eventos/{evento_id}"
    try:
        r = requests.get(url, timeout=10)
        d = r.json().get("dados", {})
        return {
            "descricao": d.get("descricao", ""),
            "dataHoraInicio": d.get("dataHoraInicio", ""),
            "local": d.get("localCamara", {}).get("nome", "Plenário")
            if isinstance(d.get("localCamara"), dict)
            else d.get("localCamara", "Plenário")
        }
    except Exception:
        return {"descricao": "", "dataHoraInicio": "", "local": "Plenário"}

def _get_itens(evento_id):
    """
    Primeiro tenta buscar do cache 'pauta_cache' do app.
    Se não existir, tenta via fetch_pauta().
    Caso ainda vazio, faz fallback para a API oficial.
    """
    try:
        # 1️⃣ do cache da aplicação
        pauta_cache = getattr(current_app, "pauta_cache", None)
        if pauta_cache and evento_id in pauta_cache:
            return pauta_cache[evento_id]

        # 2️⃣ via função do app
        from app import fetch_pauta
        itens = fetch_pauta(evento_id, force_reload=False)
        if isinstance(itens, dict) and "dados" in itens:
            return itens["dados"]
        elif isinstance(itens, list):
            return itens

        # 3️⃣ fallback: API oficial
        r = requests.get(
            f"https://dadosabertos.camara.leg.br/api/v2/eventos/{evento_id}/pauta",
            timeout=10
        )
        return r.json().get("dados", [])

    except Exception as e:
        current_app.logger.error(f"Erro ao obter itens: {e}")
        return []

# ---------------------------------------------------------------------
# Rota principal
# ---------------------------------------------------------------------
@exportar_bp.route("/<int:evento_id>")
def exportar_pauta(evento_id):
    try:
        evento = _get_evento(evento_id)
        itens = _get_itens(evento_id)

        if not itens:
            return "Nenhum item encontrado para esta pauta.", 200

        static_path = os.path.join(current_app.root_path, "static")
        camara_logo = os.path.join(static_path, "logo_camara.png")
        pl_logo = os.path.join(static_path, "logo_pl.png")

        # estilos
        styles = getSampleStyleSheet()
        title = ParagraphStyle(name="Title", parent=styles["Title"], alignment=1, fontSize=16, leading=18)
        normal = ParagraphStyle(name="Normal", parent=styles["Normal"], fontSize=10.5, leading=14, wordWrap="CJK")
        bold = ParagraphStyle(name="Bold", parent=styles["Normal"], fontName="Helvetica-Bold", fontSize=11, leading=14)
        heading = ParagraphStyle(name="HeadingItem", parent=styles["Heading1"], fontSize=13, leading=16, spaceBefore=12)

        buffer = BytesIO()
        pdf_title = f"Pauta_{evento_id}"
        doc = PautaDocTemplate(
            buffer,
            pdf_title=pdf_title,
            pagesize=A4,
            leftMargin=2.2*cm, rightMargin=2.2*cm,
            topMargin=2.6*cm, bottomMargin=2.0*cm
        )
        frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height-0.5*cm, id="normal")

        # Cabeçalho
        data_txt = data_ptbr(evento.get("dataHoraInicio", ""))
        header_text = f"Sessão Deliberativa - Plenário — {data_txt}"

        doc.addPageTemplates([
            PageTemplate(
                id="main", frames=[frame],
                onPage=lambda c, d: _header_footer(c, d, (camara_logo, pl_logo), header_text)
            )
        ])

        # -----------------------------------------------------------------
        # Montagem do conteúdo
        # -----------------------------------------------------------------
        story = []
        story.append(Paragraph("Sessão Deliberativa", title))
        story.append(Paragraph(f"<b>Data/Hora:</b> {evento.get('dataHoraInicio','')}", normal))
        story.append(Paragraph(f"<b>Descrição:</b> {evento.get('descricao','')}", normal))
        story.append(Paragraph(f"<b>Local:</b> {evento.get('local','Plenário')}", normal))
        story.append(Spacer(1, 12))

        # Resumo dos Itens
        story.append(Paragraph("Resumo dos Itens", bold))
        table_data = [["Item", "Título", "Ementa"]]
        for it in itens:
            table_data.append([
                Paragraph(str(it.get("ordem", "—")), normal),
                Paragraph(it.get("projeto", "—"), normal), 
                Paragraph(_strip_html(it.get("ementa", "—")), normal)
            ])
        tbl = Table(table_data, colWidths=[2*cm, 7*cm, 8*cm])
        tbl.setStyle(TableStyle([
            ("GRID", (0,0), (-1,-1), 0.3, colors.gray),
            ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#E8F3EC")),
            ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold")
        ]))
        story.append(tbl)
        story.append(PageBreak())

        # Itens detalhados
        for it in itens:
            story.append(Paragraph(f"Item {it.get('ordem','—')} — {it.get('projeto','')}", heading))
            story.append(Paragraph(f"<b>Autor:</b> {it.get('autor','N/D')}", normal))
            story.append(Paragraph(f"<b>Relator:</b> {it.get('relator','N/D')}", normal))
            story.append(Paragraph(f"<b>Situação:</b> {it.get('situacao','N/D')}", normal))
            story.append(Spacer(1, 6))

            if it.get("resumo_materia"):
                story.append(Paragraph("Nota Técnica", bold))
                for linha in _strip_html(it["resumo_materia"]).split('\n'):
                    linha = linha.strip()
                    if linha:
                        story.append(Paragraph(linha, normal))
                    else:
                        story.append(Spacer(1, 4))
                story.append(Spacer(1, 6))

        # Geração do PDF
        doc.build(story)
        pdf = buffer.getvalue()
        buffer.close()

        resp = make_response(pdf)
        resp.headers["Content-Type"] = "application/pdf"
        resp.headers["Content-Disposition"] = f'inline; filename="Pauta_{evento_id}.pdf"'
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        return resp

    except Exception as e:
        current_app.logger.error(f"Erro ao exportar pauta {evento_id}: {e}")
        return f"Erro ao gerar PDF: {e}", 200
