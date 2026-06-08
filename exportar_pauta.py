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
    import html as _html_mod
    # Remove todas as tags HTML
    t = re.sub(r"<[^>]+>", "", str(s or ""))
    # Converte entidades HTML (&amp; &lt; etc.)
    t = _html_mod.unescape(t)
    # Escapa para uso seguro no ReportLab (que interpreta < > & como XML)
    import xml.sax.saxutils as _sax
    return _sax.escape(t).strip()


# Emojis de título de seção e suas cores
_TITULOS_SECAO = {
    '📘': '#0D2B5E',
    '🟢': '#1A6B3A',
    '🔴': '#8B0000',
    '⚖️': '#7B5C00',
    '↔️': '#0D2B5E',
    '⚠️': '#8B0000',
}

def _nota_para_paragrafos(html_raw, estilo_normal, estilo_titulo=None):
    """Converte HTML do Quill em lista de Paragraphs.
    Títulos (linhas com emoji): negrito, cor própria.
    Todo o resto: preto, sem cor.
    """
    import html as _html_mod
    import xml.sax.saxutils as _sax
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib import colors as _colors
    from reportlab.platypus import Spacer

    s = str(html_raw or "")
    # Converte tags estruturais em quebras
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"</p>",      "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"</li>",     "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"<li[^>]*>", "- ", s, flags=re.IGNORECASE)
    # Remove TODAS as tags (inclusive spans com color, font-size etc.)
    s = re.sub(r"<[^>]+>", "", s)
    s = _html_mod.unescape(s)

    linhas = [l.strip() for l in s.split("\n")]
    result = []
    for linha in linhas:
        if not linha:
            if result:
                result.append(Spacer(1, 3))
            continue

        # Detecta emoji de título
        emoji_enc = None
        for emoji in _TITULOS_SECAO:
            if linha.startswith(emoji):
                emoji_enc = emoji
                break

        linha_safe = _sax.escape(linha)

        if emoji_enc:
            cor_hex = _TITULOS_SECAO[emoji_enc]
            st = estilo_titulo or ParagraphStyle("_st",
                parent=estilo_normal, fontName="Helvetica-Bold",
                fontSize=estilo_normal.fontSize + 1, leading=estilo_normal.leading + 2,
                textColor=_colors.HexColor(cor_hex), spaceBefore=6)
            result.append(Paragraph(linha_safe, st))
        else:
            # PRETO — sem exceção
            st = ParagraphStyle("_sb", parent=estilo_normal,
                textColor=_colors.black)
            result.append(Paragraph(linha_safe, st))

    return result if result else [Paragraph("", estilo_normal)]

# ---------------------------------------------------------------------
# Cabeçalho e rodapé
# ---------------------------------------------------------------------
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
                Paragraph(it.get("projeto", "—")), 
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
                story.extend(_nota_para_paragrafos(it["resumo_materia"], normal))
                story.append(Spacer(1, 6))

        # Geração do PDF
        doc.build(story)
        pdf = buffer.getvalue()
        buffer.close()

        resp = make_response(pdf)
        resp.headers["Content-Type"] = "application/pdf"
        resp.headers["Content-Disposition"] = f'inline; filename="Pauta_{evento_id}.pdf"'
        return resp

    except Exception as e:
        current_app.logger.error(f"Erro ao exportar pauta {evento_id}: {e}")
        return f"Erro ao gerar PDF: {e}", 200
