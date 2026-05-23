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

        # ── PRIMEIRA PÁGINA: formato idêntico ao infográfico ──────────────
        from gerar_infografico import (
            gerar_infografico_pdf as _gif,
            strip_html as _sh,
            wrap_text as _wt,
            COR_AZUL_ESCURO, COR_AZUL_CLARO, COR_VERDE, COR_CINZA,
            COR_BORDA, COR_CINZA_CLARO, CORES_ORIENTACAO
        )

        buf_inf = BytesIO()
        W, H = A4
        MARGIN = 1.2 * cm
        CONTENT_W = W - 2 * MARGIN

        cv = pdfcanvas.Canvas(buf_inf, pagesize=A4)

        def _header_inf():
            cab_h = 1.2*cm
            cv.setFillColor(colors.white)
            cv.rect(0, H - cab_h, W, cab_h, fill=1, stroke=0)
            cv.setStrokeColor(COR_VERDE)
            cv.setLineWidth(1.5)
            cv.line(0, H - cab_h, W, H - cab_h)
            cv.setFillColor(COR_VERDE)
            cv.setFont("Helvetica-Bold", 9)
            cv.drawCentredString(W/2, H - 0.85*cm,
                "Lideranças da Minoria e da Oposição — Câmara dos Deputados")

        def _footer_inf():
            rod_h = 1.5*cm
            cv.setFillColor(colors.white)
            cv.rect(0, 0, W, rod_h, fill=1, stroke=0)
            cv.setStrokeColor(COR_VERDE)
            cv.setLineWidth(1)
            cv.line(MARGIN, rod_h, W - MARGIN, rod_h)
            logo_w, logo_h = 2.0*cm, 1.1*cm
            y_rod = (rod_h - logo_h) / 2
            static_path = os.path.join(current_app.root_path, 'static')
            for fname, xpos in [('logo_minoria.png', MARGIN),
                                 ('logo_oposicao.png', MARGIN + logo_w + 0.2*cm)]:
                p = os.path.join(static_path, fname)
                if os.path.exists(p):
                    try: cv.drawImage(p, xpos, y_rod, width=logo_w, height=logo_h,
                                      preserveAspectRatio=True, mask='auto')
                    except Exception: pass
            cv.setFillColor(COR_CINZA)
            cv.setFont("Helvetica", 7)
            cv.drawCentredString(W/2, 0.55*cm,
                "Lideranças da Minoria e da Oposição — Plenário / Câmara dos Deputados")

        # Fundo branco
        cv.setFillColor(colors.white)
        cv.rect(0, 0, W, H, fill=1, stroke=0)
        _header_inf()
        _footer_inf()

        # Título
        y = H - 1.2*cm - 0.3*cm
        titulo_h = 1.8*cm
        cv.setFillColor(COR_AZUL_CLARO)
        cv.rect(MARGIN, y - titulo_h, CONTENT_W, titulo_h, fill=1, stroke=0)
        cv.setStrokeColor(COR_AZUL_ESCURO)
        cv.setLineWidth(1)
        cv.rect(MARGIN, y - titulo_h, CONTENT_W, titulo_h, fill=0, stroke=1)
        dh = evento.get('dataHoraInicio', '')
        try:
            dt = datetime.fromisoformat(dh)
            data_fmt = dt.strftime('%d/%m/%Y')
            hora_fmt = dt.strftime('%H:%M')
        except Exception:
            data_fmt = hora_fmt = ''
        cv.setFillColor(COR_AZUL_ESCURO)
        cv.setFont("Helvetica-Bold", 10)
        cv.drawCentredString(W/2, y - 0.7*cm,
            f"Resumo da Pauta — {evento.get('descricao','Sessão Deliberativa')}")
        cv.setFillColor(COR_CINZA)
        cv.setFont("Helvetica", 8)
        cv.drawCentredString(W/2, y - 1.35*cm,
            f"Data: {data_fmt}  |  Hora: {hora_fmt}  |  Local: {evento.get('local','')}")
        y -= titulo_h + 0.3*cm

        # Cards dos itens
        page_bottom = 1.5*cm + 0.3*cm
        CARD_MARGIN = 0.3*cm
        BADGE_W = 2.4*cm
        INNER_W = CONTENT_W - 2*CARD_MARGIN - BADGE_W - 0.4*cm

        for item in itens:
            orientacao = (item.get('orientacao') or '').strip().upper()
            cor_badge, cor_badge_bg, _ = CORES_ORIENTACAO.get(
                orientacao, (COR_CINZA, COR_CINZA_CLARO, colors.white))
            projeto = item.get('projeto', '')
            autor   = _sh(item.get('autor', 'N/D'))
            relator = item.get('relator', 'N/D')
            ementa  = _sh(item.get('ementa', ''))
            resumo  = resumos_ia.get(str(item.get('id_principal', '')), '')
            ordem   = item.get('ordem', '')

            # Estima altura
            chars_per_line = int(INNER_W / 4.8)
            n_lines = max(1, len(ementa) // chars_per_line + 1)
            if resumo:
                n_lines += max(1, len(resumo) // chars_per_line + 1)
            n_lines = min(n_lines, 7)
            card_h = max(1.0*cm + n_lines * 0.38*cm + 0.5*cm, 1.8*cm)

            if y - card_h < page_bottom:
                cv.showPage()
                cv.setFillColor(colors.white)
                cv.rect(0, 0, W, H, fill=1, stroke=0)
                _header_inf()
                _footer_inf()
                y = H - 1.2*cm - 0.3*cm

            # Card background
            cv.setFillColor(COR_CINZA_CLARO)
            cv.setStrokeColor(COR_BORDA)
            cv.setLineWidth(0.5)
            cv.roundRect(MARGIN, y - card_h, CONTENT_W, card_h, 4, fill=1, stroke=1)

            # Badge orientação
            bx = MARGIN + CONTENT_W - BADGE_W - CARD_MARGIN
            cv.setFillColor(cor_badge_bg)
            cv.roundRect(bx, y - card_h + CARD_MARGIN,
                         BADGE_W, card_h - 2*CARD_MARGIN, 3, fill=1, stroke=0)
            cv.setFillColor(cor_badge)
            cv.setFont("Helvetica-Bold", 7.5)
            ori_txt = orientacao or "—"
            cv.drawCentredString(bx + BADGE_W/2,
                                 y - card_h/2 - 3, ori_txt)

            tx = MARGIN + CARD_MARGIN
            ty = y - CARD_MARGIN - 0.3*cm

            # Número + Projeto
            cv.setFillColor(COR_AZUL_ESCURO)
            cv.setFont("Helvetica-Bold", 8.5)
            header_txt = f"{ordem}. {projeto}"
            cv.drawString(tx, ty, header_txt[:80])
            ty -= 0.35*cm

            # Ementa
            cv.setFillColor(colors.black)
            cv.setFont("Helvetica", 7.8)
            line, lines_drawn = "", 0
            for word in ementa.split():
                test = (line + " " + word).strip()
                if cv.stringWidth(test, "Helvetica", 7.8) <= INNER_W:
                    line = test
                else:
                    if lines_drawn < 4:
                        cv.drawString(tx, ty - lines_drawn*0.33*cm, line)
                        lines_drawn += 1
                    line = word
            if line and lines_drawn < 4:
                cv.drawString(tx, ty - lines_drawn*0.33*cm, line)
                lines_drawn += 1

            # Resumo IA em verde
            if resumo:
                ty2 = ty - lines_drawn*0.33*cm - 0.1*cm
                cv.setFillColor(COR_VERDE)
                cv.setFont("Helvetica-Bold", 7)
                resumo_txt = f"Resumo: {resumo}"
                line2, ld2 = "", 0
                for word in resumo_txt.split():
                    test2 = (line2 + " " + word).strip()
                    if cv.stringWidth(test2, "Helvetica-Bold", 7) <= INNER_W:
                        line2 = test2
                    else:
                        if ld2 < 3:
                            cv.drawString(tx, ty2 - ld2*0.28*cm, line2)
                            ld2 += 1
                        line2 = word
                if line2 and ld2 < 3:
                    cv.drawString(tx, ty2 - ld2*0.28*cm, line2)

            # Autor / Relator
            ty3 = y - card_h + CARD_MARGIN + 0.15*cm
            cv.setFillColor(COR_CINZA)
            cv.setFont("Helvetica", 6.5)
            cv.drawString(tx, ty3, f"Autor: {autor[:60]}  |  Relator: {relator[:50]}")

            y -= card_h + 0.15*cm

        cv.save()

        # Combina: primeira página (canvas) + restante (platypus)
        from reportlab.lib.utils import ImageReader
        from pypdf import PdfWriter, PdfReader
        buf_inf.seek(0)

        # ── PÁGINAS SEGUINTES: detalhes via Platypus ──────────────────────
        buf_det = BytesIO()
        story = []
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

        doc2 = BaseDocTemplate(buf_det, pagesize=A4,
                               leftMargin=1.5*cm, rightMargin=1.5*cm,
                               topMargin=3.0*cm, bottomMargin=2.5*cm)
        frame2 = Frame(doc2.leftMargin, doc2.bottomMargin, doc2.width, doc2.height-0.5*cm, id="normal")
        header_text2 = f"Sessão Deliberativa — {data_ptbr(evento.get('dataHoraInicio',''))}"
        doc2.addPageTemplates([PageTemplate(
            id="main", frames=[frame2],
            onPage=lambda c, d: _header_footer(c, d, (camara_logo, pl_logo), header_text2)
        )])
        doc2.build(story)

        # Merge: infográfico (primeira(s) página(s)) + detalhes
        try:
            from pypdf import PdfWriter, PdfReader
            writer = PdfWriter()
            buf_inf.seek(0)
            buf_det.seek(0)
            for pdf_buf in [buf_inf, buf_det]:
                reader = PdfReader(pdf_buf)
                for page in reader.pages:
                    writer.add_page(page)
            final = BytesIO()
            writer.write(final)
            pdf = final.getvalue()
        except ImportError:
            # Sem pypdf — retorna só o infográfico
            buf_inf.seek(0)
            pdf = buf_inf.read()

        resp = make_response(pdf)
        resp.headers["Content-Type"] = "application/pdf"
        resp.headers["Content-Disposition"] = f'inline; filename="Pauta_Plenario_{evento_id}.pdf"'
        return resp

    except Exception as e:
        current_app.logger.error(f"Erro ao exportar pauta {evento_id}: {e}")
        import traceback
        return f"Erro ao gerar PDF: {e}<br><pre>{traceback.format_exc()}</pre>", 200
