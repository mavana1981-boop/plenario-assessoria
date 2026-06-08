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
    t = re.sub(r"<[^>]+>", "", str(s or ""))
    t = _h.unescape(t)
    return _x.escape(t).strip()

# Títulos de seção predefinidos — APENAS estes recebem cor/negrito no PDF
_SECOES = {
    '📘 Resumo Técnico':            ('#0D2B5E', True),
    '🟢 Pontos Positivos':           ('#1A6B3A', True),
    '🔴 Pontos Negativos':           ('#8B0000', True),
    '⚖️ Riscos Políticos e de Imagem': ('#7B5C00', True),
    '↔️ Orientação Sugerida':        ('#0D2B5E', True),
    '⚠️ Críticas e Pontos de Combate': ('#8B0000', True),
}
# Também detecta só pelo emoji inicial (caso o usuário edite o texto do título)
_EMOJI_SECAO = {'📘', '🟢', '🔴', '⚖️', '↔️', '⚠️'}
_COR_EMOJI = {
    '📘': '#0D2B5E', '🟢': '#1A6B3A', '🔴': '#8B0000',
    '⚖️': '#7B5C00', '↔️': '#0D2B5E', '⚠️': '#8B0000',
}

def _nota_paragrafos(html_raw, st_normal):
    """Converte HTML do Quill em Paragraphs do ReportLab.
    REGRA: somente linhas que começam com emoji de seção predefinido
    recebem cor e negrito. Todo o resto sai PRETO, fonte normal.
    """
    import html as _h
    import xml.sax.saxutils as _x
    from reportlab.lib import colors as _c
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import Spacer

    # 1. Converte HTML em texto puro linha por linha
    s = str(html_raw or "")
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"</p>",      "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"</li>",     "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"<li[^>]*>", "- ", s, flags=re.IGNORECASE)
    s = re.sub(r"<[^>]+>",   "",   s)   # remove TODAS as tags restantes
    s = _h.unescape(s)                  # &amp; → & etc.

    result = []
    for linha in s.split("\n"):
        linha = linha.strip()
        if not linha:
            if result:
                result.append(Spacer(1, 2))
            continue

        # Verifica se é um título de seção pelo emoji inicial
        emoji = None
        for e in _EMOJI_SECAO:
            if linha.startswith(e):
                emoji = e
                break

        # Texto seguro para ReportLab (escapa & < >)
        safe = _x.escape(linha)

        if emoji:
            cor = _COR_EMOJI[emoji]
            st = ParagraphStyle("_sec",
                parent=st_normal,
                fontName="Helvetica-Bold",
                fontSize=st_normal.fontSize + 1,
                textColor=_c.HexColor(cor),
                spaceBefore=6, spaceAfter=1)
        else:
            # PRETO PURO — sem cor, sem negrito extra
            st = ParagraphStyle("_body",
                parent=st_normal,
                textColor=_c.black,
                fontName="Helvetica")

        result.append(Paragraph(safe, st))

    return result or [Paragraph("", st_normal)]

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
                story.extend(_nota_paragrafos(it["resumo_materia"], normal))
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
