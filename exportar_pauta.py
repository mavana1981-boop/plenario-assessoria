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
# REQ urgência = vermelho escuro; tudo mais = azul escuro
C_AZUL_TABELA = colors.HexColor("#2E5FA3")   # azul mais claro para índice

def cor_tipo(projeto):
    proj_upper = (projeto or "").upper()
    p = proj_upper.split()[0] if proj_upper else ""
    if any(p.startswith(s) for s in ("REQ","RQS","RQU","REC")):
        if any(x in proj_upper for x in ("URGÊN","URGENCIA","URGÊNCIA")):
            return C_VERMELHO, C_VERM_LT   # vermelho escuro
    return C_AZUL, C_AZUL_LT              # azul escuro para tudo mais

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

def _html_para_paragrafos(html, estilo):
    """
    Converte HTML do editor Quill em lista de Paragraphs ReportLab.
    Títulos de seção: negrito, 2pt maior, cor própria.
    Texto normal: cor preta, tamanho padrão — ignora cores inline do Quill.
    """
    from reportlab.platypus import Paragraph, Spacer
    from reportlab.lib.styles import ParagraphStyle
    import html as _html_mod

    TITULOS = {
        '📘': (colors.HexColor("#0D2B5E"), ),
        '🟢': (colors.HexColor("#1A6B3A"), ),
        '🔴': (colors.HexColor("#8B0000"), ),
        '⚖️': (colors.HexColor("#7B5C00"), ),
        '↔️': (colors.HexColor("#0D2B5E"), ),
        '⚠️': (colors.HexColor("#8B0000"), ),
    }
    # Palavras-chave fallback — só para linhas muito curtas (títulos antigos sem emoji)
    KW_TITULOS = {
        'PONTOS POSITIVOS': '🟢', 'PONTO POSITIVO': '🟢',
        'PONTOS NEGATIVOS': '🔴', 'PONTO NEGATIVO': '🔴',
        'RESUMO TÉCNICO': '📘',   'RESUMO TECNICO': '📘',
        'RISCOS POLÍTICOS': '⚖️', 'RISCOS POLITICOS': '⚖️',
        'ORIENTAÇÃO SUGERIDA': '↔️', 'ORIENTACAO SUGERIDA': '↔️',
        'CRÍTICAS E PONTOS': '⚠️', 'CRITICAS E PONTOS': '⚠️',
    }

    fTitulo = estilo.fontSize + 2
    fTexto  = estilo.fontSize

    s = str(html or "")
    # Quebras de linha
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"</p>",      "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"</li>",     "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"<li[^>]*>", "• ", s, flags=re.IGNORECASE)
    # Remove TODAS as tags — inclusive spans com color que contaminam o texto normal
    s = re.sub(r"<[^>]+>", "", s)
    s = _html_mod.unescape(s)

    linhas = [l.strip() for l in s.split("\n")]
    paragrafos = []

    for linha in linhas:
        if not linha:
            if paragrafos:
                paragrafos.append(Spacer(1, 3))
            continue

        # Detecta título pelo emoji (método principal)
        emoji_enc = None
        for emoji in TITULOS:
            if linha.startswith(emoji):
                emoji_enc = emoji
                break

        # Fallback por palavra-chave — só linhas curtas (< 50 chars = é um título)
        if not emoji_enc and len(linha) < 50:
            lu = linha.upper()
            for kw, emoji in KW_TITULOS.items():
                if kw in lu:
                    emoji_enc = emoji
                    break

        if emoji_enc:
            cor = TITULOS[emoji_enc][0]
            st = ParagraphStyle(f"sNT_{emoji_enc}", parent=estilo,
                fontName="Helvetica-Bold", fontSize=fTitulo, leading=fTitulo+4,
                textColor=cor, spaceBefore=8, spaceAfter=2)
            paragrafos.append(Paragraph(linha, st))
        else:
            # Texto normal: sempre preto, tamanho fixo — sem herdar cor de span anterior
            st = ParagraphStyle("sNT_body", parent=estilo,
                fontSize=fTexto, textColor=colors.black, leading=fTexto+3)
            paragrafos.append(Paragraph(linha, st))

    return paragrafos if paragrafos else [Paragraph("", estilo)]

    return paragrafos if paragrafos else [Paragraph("", estilo)]

# ── Cabeçalho / Rodapé ─────────────────────────────────────────────────────
def _header_footer(canvas, doc, logos, header_text):
    w, h = A4
    logo_min, logo_opo, logo_novo, logo_pl = logos
    canvas.saveState()

    # ── Cabeçalho ──
    cab_h = 1.8*cm
    canvas.setFillColor(colors.white)
    canvas.rect(0, h - cab_h, w, cab_h, fill=1, stroke=0)
    canvas.setStrokeColor(C_VERDE)
    canvas.setLineWidth(1.5)
    canvas.line(0, h - cab_h, w, h - cab_h)

    # 4 logos lado a lado — tamanho fixo rigoroso (sem preserveAspectRatio)
    lw, lh = 1.8*cm, 1.1*cm
    gap = 0.15*cm
    x_start = 0.5*cm
    y_logo = h - cab_h + (cab_h - lh) / 2
    for path, xpos in [
        (logo_min,  x_start),
        (logo_opo,  x_start + (lw + gap)),
        (logo_novo, x_start + (lw + gap) * 2),
        (logo_pl,   x_start + (lw + gap) * 3),
    ]:
        if path and os.path.exists(path):
            try:
                canvas.drawImage(path, xpos, y_logo, width=lw, height=lh,
                                 preserveAspectRatio=False, mask='auto')
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

    lw_r, lh_r = 1.6*cm, 1.0*cm
    y_rod = (rod_h - lh_r) / 2
    for path, xpos in [
        (logo_min,  0.4*cm),
        (logo_opo,  0.4*cm + (lw_r + gap)),
        (logo_novo, 0.4*cm + (lw_r + gap) * 2),
        (logo_pl,   0.4*cm + (lw_r + gap) * 3),
    ]:
        if path and os.path.exists(path):
            try:
                canvas.drawImage(path, xpos, y_rod, width=lw_r, height=lh_r,
                                 preserveAspectRatio=False, mask='auto')
            except Exception:
                pass

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
        import requests as _req
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

            # Enriquece autor via API da Câmara (mesmo que o browser faz)
            if rid and rid.isdigit():
                try:
                    r = _req.get(
                        f"https://dadosabertos.camara.leg.br/api/v2/proposicoes/{rid}/autores",
                        headers={'Accept': 'application/json'}, timeout=8
                    )
                    if r.ok:
                        dados = r.json().get('dados', [])
                        autores = []
                        for a in dados:
                            nome = a.get('nome','')
                            p    = a.get('siglaPartido','')
                            uf   = a.get('siglaUf','')
                            suf  = f'({p}-{uf})' if p and uf else (f'({p})' if p else '')
                            if nome:
                                autores.append(f"{nome} {suf}".strip() if suf else nome)
                        if autores:
                            item['autor'] = ', '.join(autores[:2]) + (' e outros' if len(autores) > 2 else '')
                except Exception:
                    pass

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
        logo_min  = os.path.join(static_path, "logo_minoria.png")
        logo_opo  = os.path.join(static_path, "logo_oposicao.png")
        logo_novo = os.path.join(static_path, "logo_novo.png")
        logo_pl   = os.path.join(static_path, "logo_pl.png")

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
        # Nota técnica: Helvetica legível, preta
        sNota = ParagraphStyle("sNota", parent=SS["Normal"],
            fontName="Helvetica", fontSize=9, leading=14,
            textColor=colors.black, wordWrap="CJK",
            leftIndent=6, rightIndent=6)
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
            onPage=lambda c, d: _header_footer(c, d, (logo_min, logo_opo, logo_novo, logo_pl), header_text)
        )])

        story = []

        # ── Capa ──
        story.append(Spacer(1, 0.5*cm))
        story.append(Paragraph("PAUTA DO PLENÁRIO", sTitle))
        story.append(Paragraph(
            f"{evento.get('descricao','')}  |  {data_txt}  |  {evento.get('local','Plenário')}",
            sSubtitle))
        story.append(HRFlowable(width="100%", thickness=1.5, color=C_VERDE, spaceAfter=8))

        # ── Estilos da tabela resumo ──
        sThead = ParagraphStyle("sThead", parent=SS["Normal"],
            fontName="Helvetica-Bold", fontSize=9, leading=11,
            alignment=TA_CENTER, textColor=C_AZUL_TABELA)
        sNum = ParagraphStyle("sNum", parent=SS["Normal"],
            fontSize=9, leading=11, alignment=TA_CENTER)
        sObjeto = ParagraphStyle("sObjeto", parent=SS["Normal"],
            fontSize=8.5, leading=12, alignment=TA_CENTER, wordWrap="CJK",
            textColor=colors.black)
        sOri_tab = ParagraphStyle("sOri_tab", parent=SS["Normal"],
            fontName="Helvetica-Bold", fontSize=9, leading=11,
            alignment=TA_CENTER)
        sProjTitulo = ParagraphStyle("sProjTitulo", parent=SS["Normal"],
            fontName="Helvetica-Bold", fontSize=9.5, leading=12,
            alignment=TA_CENTER)

        # ── Tabela resumo ──
        story.append(Paragraph("Itens da Pauta", sBold))
        story.append(Spacer(1, 4))

        thead = [
            Paragraph("<b>#</b>", sThead),
            Paragraph("<b>Proposição</b>", sThead),
            Paragraph("<b>Objeto</b>", sThead),
            Paragraph("<b>Orientação</b>", sThead),
        ]
        tdata = [thead]
        for it in itens:
            cor_t, cor_bg = cor_tipo(it.get("projeto",""))
            ori = (it.get("orientacao","") or "").strip().upper()
            cor_ori, _ = CORES_ORI.get(ori, (C_CINZA, C_CINZA_LT))

            # Coluna proposição: título + autor (primeiro) + relator em itálico
            proj_titulo  = it.get("projeto","—")
            autor_raw    = it.get("autor","") or ""
            relator_raw  = it.get("relator","") or ""
            # Pega só o primeiro autor
            primeiro_autor = autor_raw.split(",")[0].strip() if autor_raw else ""

            try:
                cor_hex = '#' + ''.join(f'{int(x*255):02X}' for x in cor_t.rgb())
            except Exception:
                cor_hex = '#0D2B5E'

            import xml.sax.saxutils as _xml
            proj_titulo_esc   = _xml.escape(proj_titulo)
            primeiro_autor_esc = _xml.escape(primeiro_autor) if primeiro_autor else ''
            relator_esc        = _xml.escape(relator_raw) if relator_raw else ''

            proj_xml = f'<b><font color="{cor_hex}" size="9.5">{proj_titulo_esc}</font></b>'
            if primeiro_autor_esc:
                proj_xml += f'<br/><font size="7.5"><i>Autor: {primeiro_autor_esc}</i></font>'
            if relator_esc and relator_esc != 'Não atribuído':
                proj_xml += f'<br/><font size="7.5"><i>Relator: {relator_esc}</i></font>'

            proj_para = Paragraph(proj_xml,
                ParagraphStyle("pm"+str(it.get("ordem",0)),
                    parent=SS["Normal"], alignment=TA_CENTER, wordWrap="CJK"))

            # Coluna objeto: resumo IA + "Ementa:" abaixo em fonte menor, itálico
            resumo_ia_tab = _strip_html(it.get("resumo_ia","") or "")
            ementa_obj    = _strip_html(it.get("ementa","") or "")
            import xml.sax.saxutils as _xml2
            ementa_obj_esc = _xml2.escape(ementa_obj) if ementa_obj else ''

            if resumo_ia_tab and ementa_obj_esc:
                objeto_xml = (resumo_ia_tab +
                    f'<br/><font size="6" color="#777777"><i>Ementa: {ementa_obj_esc}</i></font>')
                objeto_para = Paragraph(objeto_xml, sObjeto)
            elif ementa_obj_esc:
                objeto_xml = f'<font size="6" color="#777777"><i>Ementa: {ementa_obj_esc}</i></font>'
                objeto_para = Paragraph(objeto_xml, sObjeto)
            else:
                objeto_para = Paragraph(resumo_ia_tab or "—", sObjeto)

            tdata.append([
                Paragraph(str(it.get("ordem","—")), sNum),
                proj_para,
                objeto_para,
                Paragraph(ori or "—",
                    ParagraphStyle("ot"+str(it.get("ordem",0)),
                        parent=sOri_tab, textColor=cor_ori)),
            ])

        tbl = Table(tdata, colWidths=[1.0*cm, 4.0*cm, 9.2*cm, 2.4*cm],
                    repeatRows=1, splitByRow=True)
        tbl_style = [
            # Cabeçalho
            ("BACKGROUND",    (0,0), (-1,0), colors.white),
            ("LINEBELOW",     (0,0), (-1,0), 1.5, C_AZUL_TABELA),
            # Grid
            ("GRID",          (0,0), (-1,-1), 0.3, colors.HexColor("#cccccc")),
            ("ROWBACKGROUNDS",(0,1), (-1,-1), [colors.white, C_CINZA_LT]),
            # Alinhamento vertical MIDDLE para todas as células
            ("VALIGN",        (0,0), (-1,-1), "MIDDLE"),
            # Centralização horizontal (via Paragraph com TA_CENTER)
            ("ALIGN",         (0,0), (-1,-1), "CENTER"),
            # Padding
            ("TOPPADDING",    (0,0), (-1,-1), 5),
            ("BOTTOMPADDING", (0,0), (-1,-1), 5),
            ("LEFTPADDING",   (0,0), (-1,-1), 4),
            ("RIGHTPADDING",  (0,0), (-1,-1), 4),
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
            import xml.sax.saxutils as _xml2

            # Cabeçalho do item: fundo colorido por tipo
            hdr_data = [[
                Paragraph(f'<b>Item {it.get("ordem","—")} — {_xml2.escape(projeto)}</b>',
                           ParagraphStyle("hdr", parent=SS["Normal"],
                               fontName="Helvetica-Bold", fontSize=11, leading=14,
                               textColor=cor_t)),
            ]]
            hdr_tbl = Table(hdr_data, colWidths=[doc.width])
            hdr_tbl.setStyle(TableStyle([
                ("BACKGROUND",    (0,0), (-1,-1), colors.white),
                ("LINEBELOW",     (0,0), (-1,-1), 2, cor_t),
                ("TOPPADDING",    (0,0), (-1,-1), 6),
                ("BOTTOMPADDING", (0,0), (-1,-1), 6),
                ("LEFTPADDING",   (0,0), (-1,-1), 8),
                ]))
            bloco.append(hdr_tbl)

            # Info autor/relator/situação — fundo claro, autor truncado
            def _trunc_autor(txt, max_chars=120):
                if not txt: return 'N/D'
                partes = txt.split(',')
                out = []
                for p in partes:
                    if sum(len(x) for x in out) + len(p) > max_chars:
                        out.append(' e outros.')
                        break
                    out.append(p)
                return ','.join(out).strip()

            autor_txt  = _trunc_autor(it.get('autor','N/D'))
            relator_txt = (it.get('relator','') or 'N/D')[:80]
            sit_txt    = it.get('situacao','') or ''

            sInfo = ParagraphStyle("sInfo"+str(it.get('ordem',0)),
                parent=sNormal, fontSize=8.5, leading=11, wordWrap='CJK')

            info_rows = [[
                Paragraph(f"<b>Autor:</b> {autor_txt}", sInfo),
                Paragraph(f"<b>Relator:</b> {relator_txt}", sInfo),
            ]]
            if sit_txt and sit_txt != "N/D":
                info_rows.append([
                    Paragraph(f"<b>Situação:</b> {sit_txt}", sInfo),
                    Paragraph("", sInfo),
                ])
            info_tbl = Table(info_rows, colWidths=[doc.width/2, doc.width/2])
            info_tbl.setStyle(TableStyle([
                ("BACKGROUND",    (0,0), (-1,-1), colors.white),
                ("TOPPADDING",    (0,0), (-1,-1), 3),
                ("BOTTOMPADDING", (0,0), (-1,-1), 3),
                ("LEFTPADDING",   (0,0), (-1,-1), 8),
                ("LINEBELOW",     (0,-1), (-1,-1), 0.5, cor_t),
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
                    ("BACKGROUND",    (0,0), (-1,-1), colors.white),
                    ("TOPPADDING",    (0,0), (-1,-1), 4),
                    ("BOTTOMPADDING", (0,0), (-1,-1), 4),
                    ("LINEABOVE",     (0,0), (-1,0), 0.5, cor_ori),
                    ("LINEBELOW",     (0,0), (-1,0), 0.5, cor_ori),
                ]))
                bloco.append(ori_tbl)

            # Nota técnica — Helvetica preta, preservando ícones e quebras de linha
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

                # Converte HTML preservando ícones e quebras de linha
                raw_html = it.get("resumo_materia", "") or ""
                paras_nota = _html_para_paragrafos(raw_html, sNota)

                # Monta tabela com todos os parágrafos
                rows = [[p] for p in paras_nota]
                nota_tbl = Table(rows, colWidths=[doc.width])
                nota_tbl.setStyle(TableStyle([
                    ("BACKGROUND",(0,0),(-1,-1), colors.HexColor("#F8F8F8")),
                    ("TOPPADDING",(0,0),(-1,-1), 3),
                    ("BOTTOMPADDING",(0,0),(-1,-1), 3),
                    ("LEFTPADDING",(0,0),(-1,-1), 8),
                    ("RIGHTPADDING",(0,0),(-1,-1), 8),
                    ("BOX",(0,0),(-1,-1), 0.5, C_CINZA),
                    ("TOPPADDING",(0,0),(-1,0), 6),
                    ("BOTTOMPADDING",(0,-1),(-1,-1), 6),
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
