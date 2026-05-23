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
    return re.sub(r"<[^>]+>", "", str(s or "")).strip()

def _header_footer(canvas, doc, logos, header_text):
    w, h = A4
    camara_path, pl_path = logos
    canvas.saveState()

    canvas.setStrokeColorRGB(0, 0.4, 0.2)
    canvas.line(1.5*cm, h-1.8*cm, w-1.5*cm, h-1.8*cm)

    for path, x in [(camara_path, 1.5*cm), (pl_path, w-3.7*cm)]:
        if os.path.exists(path):
            try:
                canvas.drawImage(path, x, h-2.5*cm, width=2.3*cm,
                                 preserveAspectRatio=True, mask='auto')
            except Exception:
                pass

    canvas.setFont("Helvetica-Bold", 10)
    text_w = stringWidth(header_text, "Helvetica-Bold", 10)
    canvas.drawString((w - text_w) / 2, h - 1.7*cm, header_text)

    canvas.setStrokeColorRGB(0, 0.4, 0.2)
    canvas.line(1.5*cm, 1.5*cm, w-1.5*cm, 1.5*cm)
    canvas.setFont("Helvetica", 9)
    canvas.drawString(1.6*cm, 1.1*cm, "Liderança da Minoria — Plenário / Câmara dos Deputados")
    canvas.drawRightString(w-1.6*cm, 1.1*cm, str(doc.page))
    canvas.restoreState()

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

def _get_evento(evento_id):
    url = f"https://dadosabertos.camara.leg.br/api/v2/eventos/{evento_id}"
    try:
        r = requests.get(url, timeout=10)
        d = r.json().get("dados", {})
        return {
            "descricao": d.get("descricao", "Sessão Deliberativa"),
            "dataHoraInicio": d.get("dataHoraInicio", ""),
            "local": (
                d.get("localCamara", {}).get("nome", "CCJC")
                if isinstance(d.get("localCamara"), dict)
                else d.get("localCamara", "CCJC")
            )
        }
    except Exception:
        return {"descricao": "Sessão Deliberativa", "dataHoraInicio": "", "local": "CCJC"}

def _get_itens(evento_id):
    try:
        from app import fetch_pauta, pauta_cache
        cache_key = str(evento_id)
        if cache_key in pauta_cache:
            return pauta_cache[cache_key]['itens']
        itens, _ = fetch_pauta(evento_id, force_reload=False)
        if isinstance(itens, list):
            return itens
        r = requests.get(
            f"https://dadosabertos.camara.leg.br/api/v2/eventos/{evento_id}/pauta",
            timeout=10
        )
        return r.json().get("dados", [])
    except Exception as e:
        current_app.logger.error(f"Erro ao obter itens: {e}")
        return []

@exportar_bp.route("/<int:evento_id>")
def exportar_pauta(evento_id):
    try:
        evento = _get_evento(evento_id)
        itens  = _get_itens(evento_id)

        if not itens:
            return "Nenhum item encontrado para esta pauta.", 200

        # Carrega resumos IA do banco (PostgreSQL ou SQLite)
        resumos_ia = {}
        try:
            from app import get_conn as _get_conn
            conn_r = _get_conn()
            c_r = conn_r.cursor()
            c_r.execute('CREATE TABLE IF NOT EXISTS resumos_ia (evento_id INTEGER, id_proposicao TEXT, resumo TEXT, PRIMARY KEY (evento_id, id_proposicao))')
            c_r.execute('SELECT id_proposicao, resumo FROM resumos_ia WHERE evento_id=?', (evento_id,))
            resumos_ia = {str(r[0]): r[1] for r in c_r.fetchall()}
            conn_r.commit()
            conn_r.close()
        except Exception as e:
            current_app.logger.warning(f"Resumos IA não carregados: {e}")

        static_path = os.path.join(current_app.root_path, "static")
        camara_logo = os.path.join(static_path, "logo_camara.png")
        pl_logo = os.path.join(static_path, "logo_pl.png")

        styles = getSampleStyleSheet()
        title_style = ParagraphStyle(name="Title", parent=styles["Title"], alignment=1, fontSize=16, leading=18)
        normal = ParagraphStyle(name="Normal", parent=styles["Normal"], fontSize=10.5, leading=14, wordWrap="CJK")
        bold = ParagraphStyle(name="Bold", parent=styles["Normal"], fontName="Helvetica-Bold", fontSize=11, leading=14)
        heading = ParagraphStyle(name="HeadingItem", parent=styles["Heading1"], fontSize=13, leading=16, spaceBefore=12)

        buffer = BytesIO()
        pdf_title = f"Pauta_Plenario_{evento_id}"
        doc = PautaDocTemplate(
            buffer,
            pdf_title=pdf_title,
            pagesize=A4,
            leftMargin=2.2*cm, rightMargin=2.2*cm,
            topMargin=2.6*cm, bottomMargin=2.0*cm
        )
        frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height-0.5*cm, id="normal")

        data_txt = data_ptbr(evento.get("dataHoraInicio", ""))
        header_text = f"Sessão Deliberativa — {data_txt}"

        doc.addPageTemplates([
            PageTemplate(
                id="main", frames=[frame],
                onPage=lambda c, d: _header_footer(c, d, (camara_logo, pl_logo), header_text)
            )
        ])

        story = []
        story.append(Paragraph("Sessão Deliberativa — Plenário da Câmara dos Deputados", title_style))
        story.append(Paragraph(f"<b>Data/Hora:</b> {evento.get('dataHoraInicio', '')}", normal))
        story.append(Paragraph(f"<b>Descrição:</b> {evento.get('descricao', '')}", normal))
        story.append(Paragraph(f"<b>Local:</b> {evento.get('local', 'CCJC')}", normal))
        story.append(Spacer(1, 12))

        story.append(Paragraph("Resumo dos Itens", bold))
        table_data = [["Item", "Título", "Relator", "Ementa"]]
        for it in itens:
            id_p = str(it.get('id_principal', ''))
            resumo = resumos_ia.get(id_p, '')
            ementa_txt = _strip_html(it.get("ementa", "—"))
            if resumo:
                ementa_cell = Paragraph(f"{ementa_txt}<br/><b>Resumo: {resumo}</b>", normal)
            else:
                ementa_cell = Paragraph(ementa_txt, normal)
            table_data.append([
                Paragraph(str(it.get("ordem", "—")), normal),
                Paragraph(it.get("projeto", "—"), normal),
                Paragraph(it.get("relator", "N/D"), normal),
                ementa_cell
            ])
        tbl = Table(table_data, colWidths=[1.5*cm, 4*cm, 4.5*cm, 7*cm])
        tbl.setStyle(TableStyle([
            ("GRID", (0,0), (-1,-1), 0.3, colors.gray),
            ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#E8F3EC")),
            ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
            ("FONTSIZE", (0,0), (-1,-1), 9),
            ("VALIGN", (0,0), (-1,-1), "TOP"),
        ]))
        story.append(tbl)
        story.append(PageBreak())

        for it in itens:
            story.append(Paragraph(f"Item {it.get('ordem','—')} — {it.get('projeto','')}", heading))
            story.append(Paragraph(f"<b>Autor:</b> {it.get('autor','N/D')}", normal))
            story.append(Paragraph(f"<b>Relator:</b> {it.get('relator','N/D')}", normal))
            story.append(Paragraph(f"<b>Situação:</b> {it.get('situacao','N/D')}", normal))
            story.append(Spacer(1, 6))

            ementa = _strip_html(it.get("ementa", ""))
            resumo = resumos_ia.get(str(it.get('id_principal', '')), '')
            if ementa:
                story.append(Paragraph(f"<b>Ementa:</b> {ementa}", normal))
            if resumo:
                story.append(Paragraph(f"<b>Resumo:</b> {resumo}", normal))
            if ementa or resumo:
                story.append(Spacer(1, 4))
            if it.get("resumo_materia"):
                story.append(Paragraph("Nota Técnica", bold))
                story.append(Paragraph(_strip_html(it["resumo_materia"]), normal))
                story.append(Spacer(1, 6))

            if it.get("orientacao"):
                story.append(Paragraph(f"<b>Orientação:</b> {it['orientacao']}", bold))
                story.append(Spacer(1, 10))

        doc.build(story)
        pdf = buffer.getvalue()
        buffer.close()

        resp = make_response(pdf)
        resp.headers["Content-Type"] = "application/pdf"
        resp.headers["Content-Disposition"] = f'inline; filename="Pauta_Plenario_{evento_id}.pdf"'
        return resp

    except Exception as e:
        current_app.logger.error(f"Erro ao exportar pauta CCJC {evento_id}: {e}")
        return f"Erro ao gerar PDF: {e}", 200
