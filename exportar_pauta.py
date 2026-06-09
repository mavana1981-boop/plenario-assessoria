# -*- coding: utf-8 -*-
from flask import Blueprint, current_app, make_response
from io import BytesIO
import os, re, requests, html as _html_mod
from datetime import datetime
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (
    BaseDocTemplate, PageTemplate, Frame,
    Paragraph, Spacer, Table, TableStyle, PageBreak, KeepTogether
)
from reportlab.pdfgen import canvas as pdfcanvas
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.lib.utils import ImageReader
import xml.sax.saxutils as _sax

exportar_bp = Blueprint("exportar", __name__, url_prefix="/exportar")

MESES_PT = {
    "January":"Janeiro","February":"Fevereiro","March":"Março",
    "April":"Abril","May":"Maio","June":"Junho",
    "July":"Julho","August":"Agosto","September":"Setembro",
    "October":"Outubro","November":"Novembro","December":"Dezembro"
}

def _data_ptbr(dt_str):
    try:
        dt = datetime.fromisoformat(dt_str)
        return f"{dt.day:02d} de {MESES_PT.get(dt.strftime('%B'), dt.strftime('%B'))} de {dt.year}"
    except Exception:
        return dt_str or "Data desconhecida"

def _html_para_texto(html):
    """Converte HTML do Quill em texto puro, preservando quebras de linha."""
    s = str(html or "")
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"</p>",      "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"</li>",     "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"<li[^>]*>", "• ", s, flags=re.IGNORECASE)
    s = re.sub(r"<[^>]+>",   "",   s)
    s = _html_mod.unescape(s)
    # Remove linhas em branco consecutivas
    linhas = []
    anterior_vazia = False
    for linha in s.split("\n"):
        vazia = not linha.strip()
        if vazia and anterior_vazia:
            continue
        linhas.append(linha.strip())
        anterior_vazia = vazia
    return "\n".join(linhas).strip()

def _get_evento(evento_id):
    try:
        r = requests.get(
            f"https://dadosabertos.camara.leg.br/api/v2/eventos/{evento_id}",
            timeout=10)
        d = r.json().get("dados", {})
        local = d.get("localCamara", {})
        if isinstance(local, dict):
            local = local.get("nome", "Plenário")
        return {
            "descricao":      d.get("descricao", ""),
            "dataHoraInicio": d.get("dataHoraInicio", ""),
            "local":          local or "Plenário",
        }
    except Exception:
        return {"descricao": "", "dataHoraInicio": "", "local": "Plenário"}

def _get_itens(evento_id):
    """Busca itens, injeta resumo_materia E resumo_ia sempre frescos do banco."""
    try:
        from app import fetch_pauta, load_notas, get_conn
        itens, _ = fetch_pauta(evento_id, force_reload=False)
        notas = load_notas()

        # Carrega resumos_ia da tabela resumos_ia
        resumos_ia = {}
        try:
            conn = get_conn()
            c = conn.cursor()
            c.execute('SELECT id_proposicao, resumo FROM resumos_ia WHERE evento_id=? OR evento_id=?',
                      (evento_id, str(evento_id)))
            resumos_ia = {str(r[0]): r[1] for r in c.fetchall()}
            conn.close()
        except Exception:
            pass

        for item in itens:
            rid = str(item.get('id_principal', ''))
            key = f"PROP_{rid}"
            if key in notas:
                nota = notas[key]
                item["resumo_materia"] = nota.get("resumo_materia", "") or ""
                item["orientacao"]     = nota.get("orientacao", "") or item.get("orientacao", "")
            # Injeta resumo_ia
            if rid in resumos_ia:
                item["resumo_ia"] = resumos_ia[rid]
        return itens
    except Exception as e:
        current_app.logger.error(f"[exportar] _get_itens erro: {e}")
        return []

def _header_footer(canvas, doc, logos, titulo):
    w, h = A4
    canvas.saveState()
    canvas.setStrokeColorRGB(0.1, 0.42, 0.23)
    canvas.setLineWidth(1.2)
    canvas.line(1.5*cm, h-1.9*cm, w-1.5*cm, h-1.9*cm)
    # logos lado a lado à esquerda — usa ImageReader para PNG com transparência
    x_logo = 1.5*cm
    for path in logos:
        try:
            img = ImageReader(path)
            iw, ih = img.getSize()
            # Mantém proporção: largura fixa 2cm
            draw_w = 2.0*cm
            draw_h = draw_w * ih / iw
            y_logo = h - 1.5*cm - draw_h
            canvas.drawImage(img, x_logo, y_logo, width=draw_w, height=draw_h,
                             preserveAspectRatio=True, mask="auto")
            x_logo += draw_w + 0.2*cm
        except Exception:
            pass
    canvas.setFont("Helvetica-Bold", 9.5)
    tw = stringWidth(titulo, "Helvetica-Bold", 9.5)
    canvas.drawString((w - tw) / 2, h-1.7*cm, titulo)
    canvas.setStrokeColorRGB(0.1, 0.42, 0.23)
    canvas.setLineWidth(0.8)
    canvas.line(1.5*cm, 1.4*cm, w-1.5*cm, 1.4*cm)
    canvas.setFont("Helvetica", 8)
    canvas.drawString(1.6*cm, 1.0*cm, "Lideranças da Minoria e da Oposição — Câmara dos Deputados")
    canvas.drawRightString(w-1.6*cm, 1.0*cm, f"Pág. {doc.page}")
    canvas.restoreState()

class PautaDoc(BaseDocTemplate):
    def __init__(self, *a, **kw):
        self._titulo = kw.pop("titulo", "")
        super().__init__(*a, **kw)
    def build(self, flowables, **kw):
        def maker(*a, **k):
            c = pdfcanvas.Canvas(*a, **k)
            if self._titulo:
                c.setTitle(self._titulo)
            return c
        super().build(flowables, canvasmaker=maker)

# ─────────────────────────────────────────────────────────────────────────────
@exportar_bp.route("/<int:evento_id>")
def exportar_pauta(evento_id):
    try:
        evento = _get_evento(evento_id)
        itens  = _get_itens(evento_id)
        if not itens:
            return "Nenhum item encontrado para esta pauta.", 200

        static = os.path.join(current_app.root_path, "static")

        def _logo_path(filename):
            """Retorna path físico do logo. Loga o resultado para diagnóstico."""
            path_local = os.path.join(static, filename)
            current_app.logger.info(f"[logo] tentando: {path_local} existe={os.path.exists(path_local)}")
            if os.path.exists(path_local):
                return path_local
            # Fallback: /tmp (cache de download anterior)
            tmp = f"/tmp/{filename}"
            if os.path.exists(tmp):
                current_app.logger.info(f"[logo] usando /tmp: {tmp}")
                return tmp
            # Tenta baixar via HTTP interno
            try:
                import urllib.request
                # Railway expõe na porta $PORT, internamente costuma ser 8080 ou 5000
                for porta in [os.environ.get("PORT","8080"), "8080", "5000"]:
                    try:
                        url = f"http://127.0.0.1:{porta}/static/{filename}"
                        urllib.request.urlretrieve(url, tmp)
                        if os.path.exists(tmp):
                            current_app.logger.info(f"[logo] baixado de {url}")
                            return tmp
                    except Exception:
                        continue
            except Exception as e:
                current_app.logger.warning(f"[logo] falha ao baixar {filename}: {e}")
            current_app.logger.warning(f"[logo] {filename} não encontrado")
            return path_local  # drawImage falhará silenciosamente

        logos = [_logo_path("logo_minoria.png"), _logo_path("logo_oposicao.png")]

        SS   = getSampleStyleSheet()
        VERDE = colors.HexColor("#1A6B3A")
        AZUL  = colors.HexColor("#0D2B5E")
        CINZA = colors.HexColor("#555555")

        sNormal = ParagraphStyle("sN", parent=SS["Normal"],
            fontSize=10, leading=14, wordWrap="CJK")
        sBold   = ParagraphStyle("sB", parent=SS["Normal"],
            fontName="Helvetica-Bold", fontSize=10.5, leading=14)
        sTitle  = ParagraphStyle("sT", parent=SS["Normal"],
            fontName="Helvetica-Bold", fontSize=15, leading=18,
            textColor=AZUL, alignment=1, spaceAfter=4)
        sSubt   = ParagraphStyle("sSub", parent=SS["Normal"],
            fontSize=9, leading=12, textColor=CINZA, alignment=1, spaceAfter=10)
        sHead   = ParagraphStyle("sH", parent=SS["Normal"],
            fontName="Helvetica-Bold", fontSize=12, leading=15,
            textColor=AZUL, spaceBefore=14, spaceAfter=4,
            borderPad=4)
        sNota   = ParagraphStyle("sNt", parent=SS["Normal"],
            fontSize=9.5, leading=13.5, wordWrap="CJK",
            textColor=colors.black)

        sNotaBaseada = ParagraphStyle("sNtB", parent=SS["Normal"],
            fontSize=9, leading=12, wordWrap="CJK",
            textColor=colors.HexColor("#CC0000"),
            fontName="Helvetica-Oblique")

        # Cores dos títulos de seção da nota
        SECOES = {
            "📘": colors.HexColor("#0D2B5E"),
            "🟢": colors.HexColor("#1A6B3A"),
            "🔴": colors.HexColor("#8B0000"),
            "⚖️": colors.HexColor("#7B5C00"),
            "↔️": colors.HexColor("#0D2B5E"),
            "⚠️": colors.HexColor("#8B0000"),
        }
        # Estilos dos títulos — nome único por emoji para evitar cache do ReportLab
        sSecao = {}
        for emoji, cor in SECOES.items():
            nome = "sSec_" + str(abs(hash(emoji)))
            sSecao[emoji] = ParagraphStyle(nome,
                parent=SS["Normal"],
                fontName="Helvetica-Bold",
                fontSize=10.5, leading=14,
                textColor=cor, spaceBefore=8, spaceAfter=2)

        # Estilo texto DENTRO da seção — sempre preto, nunca herda cor do título
        def _sNota_unico(i):
            return ParagraphStyle(f"sNtU_{i}",
                parent=SS["Normal"],
                fontSize=9.5, leading=13.5, wordWrap="CJK",
                textColor=colors.black,
                fontName="Helvetica")

        data_txt = _data_ptbr(evento.get("dataHoraInicio", ""))
        titulo_cabec = f"Sessão Deliberativa — Plenário — {data_txt}"

        buffer = BytesIO()
        doc = PautaDoc(buffer, titulo=f"Pauta_{evento_id}",
            pagesize=A4,
            leftMargin=2.0*cm, rightMargin=2.0*cm,
            topMargin=2.8*cm,  bottomMargin=2.2*cm)
        frame = Frame(doc.leftMargin, doc.bottomMargin,
                      doc.width, doc.height, id="normal")
        doc.addPageTemplates([PageTemplate(
            id="main", frames=[frame],
            onPage=lambda c, d: _header_footer(c, d, logos, titulo_cabec)
        )])

        story = []

        # ── Cabeçalho do documento ──────────────────────────────────────
        story.append(Paragraph("Pauta — Plenário da Câmara dos Deputados", sTitle))
        story.append(Paragraph(
            f"{evento.get('descricao','')} &nbsp;·&nbsp; {data_txt} &nbsp;·&nbsp; {evento.get('local','Plenário')}",
            sSubt))

        # ── Tabela de resumo ────────────────────────────────────────────
        story.append(Paragraph("Resumo da Pauta", sBold))
        story.append(Spacer(1, 6))

        COR_ORI = {
            "SIM":       colors.HexColor("#1A6B3A"),
            "NÃO":       colors.HexColor("#8B0000"),
            "LIBERADO":  colors.HexColor("#7B5C00"),
            "OBSTRUÇÃO": colors.HexColor("#0D2B5E"),
            "ABSTENÇÃO": colors.HexColor("#555555"),
            "NEGOCIAÇÃO":colors.HexColor("#7B5C00"),
        }

        def _hex(c):
            return "%02x%02x%02x" % (int(c.red*255), int(c.green*255), int(c.blue*255))

        # ── Estilos da tabela de resumo ──────────────────────────────────
        sTabNum  = ParagraphStyle("sTN", parent=SS["Normal"],
            fontSize=10, leading=13, alignment=1, fontName="Helvetica-Bold")

        # Proposição: estilo base da célula (texto normal, não negrito)
        sTabProj = ParagraphStyle("sTP", parent=SS["Normal"],
            fontSize=10, leading=13, alignment=1,
            fontName="Helvetica", textColor=colors.black)

        sTabObj  = ParagraphStyle("sTO2", parent=SS["Normal"],
            fontSize=9, leading=13, alignment=1, wordWrap="CJK",
            fontName="Helvetica")

        # Orientação: cabe numa linha — coluna mais larga, fonte menor
        sTabOri  = ParagraphStyle("sTO", parent=SS["Normal"],
            fontSize=8, leading=11, alignment=1, fontName="Helvetica-Bold",
            wordWrap="CJK")

        tdata = [[
            Paragraph("<b>Nº</b>",         sTabNum),
            Paragraph("<b>Proposição</b>",  sTabProj),
            Paragraph("<b>Objeto</b>",      sTabObj),
            Paragraph("<b>Orientação</b>",  sTabOri),
        ]]

        for it in itens:
            ori     = (it.get("orientacao") or "").strip()
            cor_ori = COR_ORI.get(ori, CINZA)

            # ── Coluna Proposição ──
            # Nome: negrito preto tamanho 10
            # linha em branco
            # Autor/Relator: itálico menor sem negrito
            proj_txt       = _sax.escape(str(it.get("projeto","—")))
            autor_full     = str(it.get("autor","") or "")
            relator_str    = str(it.get("relator","") or "")
            primeiro_autor = _sax.escape(autor_full.split(",")[0].strip()) if autor_full else ""
            relator_esc    = _sax.escape(relator_str[:80]) if relator_str and relator_str != "Não atribuído" else ""

            proj_xml = f'<b><font size="10">{proj_txt}</font></b>'
            if primeiro_autor or relator_esc:
                proj_xml += '<br/><br/>'
            if primeiro_autor:
                proj_xml += f'<font size="7.5"><i>Autor: {primeiro_autor}</i></font>'
            if relator_esc:
                proj_xml += f'<br/><font size="7.5"><i>Relator: {relator_esc}</i></font>'

            # ── Coluna Objeto ──
            # Resumo IA em azul negrito destaque
            # linha em branco
            # Ementa completa em itálico cinza
            resumo_ia  = _sax.escape(_html_para_texto(it.get("resumo_ia","") or ""))
            ementa_txt = _sax.escape(_html_para_texto(it.get("ementa","") or ""))

            obj_xml = ""
            if resumo_ia:
                obj_xml = f'<b><font color="#0D2B5E" size="9">{resumo_ia}</font></b>'
            if ementa_txt:
                sep = "<br/><br/>" if obj_xml else ""
                obj_xml += f'{sep}<font size="7.5" color="#555555"><i>{ementa_txt}</i></font>'
            if not obj_xml:
                obj_xml = "—"

            # ── Coluna Orientação ──
            # Fonte 9pt para todas — OBSTRUÇÃO cabe em 3.2cm com 9pt
            if ori:
                hex_ori = _hex(cor_ori)
                ori_xml = f'<b><font color="#{hex_ori}" size="9">{_sax.escape(ori)}</font></b>'
            else:
                ori_xml = "—"

            tdata.append([
                Paragraph(str(it.get("ordem","—")), sTabNum),
                Paragraph(proj_xml, sTabProj),
                Paragraph(obj_xml,  sTabObj),
                Paragraph(ori_xml,  sTabOri),
            ])

        # Coluna orientação maior (3.2cm) para caber OBSTRUÇÃO numa linha
        tbl = Table(tdata, colWidths=[0.9*cm, 4.0*cm, 9.1*cm, 3.2*cm],
                    repeatRows=1, splitByRow=True)
        tbl.setStyle(TableStyle([
            ("FONTNAME",      (0,0),(-1,0),  "Helvetica-Bold"),
            ("BACKGROUND",    (0,0),(-1,0),  colors.HexColor("#E8F3EC")),
            ("GRID",          (0,0),(-1,-1), 0.3, colors.HexColor("#cccccc")),
            ("ROWBACKGROUNDS",(0,1),(-1,-1), [colors.white, colors.HexColor("#F8F8F8")]),
            ("VALIGN",        (0,0),(-1,-1), "MIDDLE"),
            ("ALIGN",         (0,0),(-1,-1), "CENTER"),
            ("TOPPADDING",    (0,0),(-1,-1), 6),
            ("BOTTOMPADDING", (0,0),(-1,-1), 6),
            ("LEFTPADDING",   (0,0),(-1,-1), 4),
            ("RIGHTPADDING",  (0,0),(-1,-1), 4),
        ]))
        story.append(tbl)
        story.append(PageBreak())

        # ── Itens detalhados ────────────────────────────────────────────
        sHeadItem = ParagraphStyle("sHI", parent=SS["Normal"],
            fontName="Helvetica-Bold", fontSize=13, leading=16,
            textColor=AZUL, spaceBefore=14, spaceAfter=2)
        sResumoIA = ParagraphStyle("sRIA", parent=SS["Normal"],
            fontSize=11, leading=14, wordWrap="CJK",
            fontName="Helvetica-Oblique",
            textColor=VERDE, spaceAfter=4)

        for it in itens:
            projeto  = _sax.escape(str(it.get("projeto","—")))
            autor    = _sax.escape(str(it.get("autor","N/D") or "N/D"))
            relator  = _sax.escape(str(it.get("relator","N/D") or "N/D"))
            situacao = _sax.escape(str(it.get("situacao","") or ""))
            ori_raw  = str(it.get("orientacao","") or "")
            ori      = _sax.escape(ori_raw)
            cor_ori  = COR_ORI.get(ori_raw, CINZA)
            resumo_ia_det = _sax.escape(_html_para_texto(it.get("resumo_ia","") or ""))

            # Cabeçalho do item: título à esquerda, orientação à direita na mesma linha
            if ori_raw:
                ori_str = f'<font color="#{_hex(cor_ori)}" size="14"><b>{ori}</b></font>'
            else:
                ori_str = ""

            # Monta cabeçalho em tabela de 2 colunas (título | orientação com quadro colorido)
            if ori_raw:
                # Cor de fundo bem clara da orientação
                COR_BG_ORI = {
                    "SIM":       "#E8F5EC",
                    "NÃO":       "#FDECEA",
                    "LIBERADO":  "#FFF8E7",
                    "OBSTRUÇÃO": "#EEF2FA",
                    "ABSTENÇÃO": "#F5F5F5",
                    "NEGOCIAÇÃO":"#FFF8E7",
                }
                bg_ori = COR_BG_ORI.get(ori_raw, "#F5F5F5")
                hex_borda = _hex(cor_ori)
                ori_cell = Table([[
                    Paragraph(f'<font color="#{hex_borda}" size="14"><b>{ori}</b></font>',
                        ParagraphStyle("sOriD", parent=SS["Normal"],
                            fontSize=14, leading=16, alignment=1, fontName="Helvetica-Bold"))
                ]], colWidths=[doc.width * 0.28])
                ori_cell.setStyle(TableStyle([
                    ("BACKGROUND",    (0,0),(-1,-1), colors.HexColor(bg_ori)),
                    ("BOX",           (0,0),(-1,-1), 1.5, colors.HexColor(f"#{hex_borda}")),
                    ("VALIGN",        (0,0),(-1,-1), "MIDDLE"),
                    ("TOPPADDING",    (0,0),(-1,-1), 6),
                    ("BOTTOMPADDING", (0,0),(-1,-1), 6),
                    ("LEFTPADDING",   (0,0),(-1,-1), 4),
                    ("RIGHTPADDING",  (0,0),(-1,-1), 4),
                ]))
            else:
                ori_cell = Paragraph("", sNormal)

            cab_tbl = Table([[
                Paragraph(f'<font color="#0D2B5E"><b>Item {it.get("ordem","—")} — {projeto}</b></font>', sHeadItem),
                ori_cell,
            ]], colWidths=[doc.width * 0.72, doc.width * 0.28])
            cab_tbl.setStyle(TableStyle([
                ("VALIGN",        (0,0),(-1,-1), "MIDDLE"),
                ("LEFTPADDING",   (0,0),(-1,-1), 0),
                ("RIGHTPADDING",  (0,0),(-1,-1), 0),
                ("TOPPADDING",    (0,0),(-1,-1), 0),
                ("BOTTOMPADDING", (0,0),(-1,-1), 0),
            ]))

            bloco = [cab_tbl]
            bloco.append(Paragraph(f"<b>Autor:</b> {autor}", sNormal))
            bloco.append(Paragraph(f"<b>Relator:</b> {relator}", sNormal))
            if situacao:
                bloco.append(Paragraph(f"<b>Situação:</b> {situacao}", sNormal))

            # Resumo IA em verde itálico (em vez de ementa)
            if resumo_ia_det:
                bloco.append(Spacer(1, 4))
                bloco.append(Paragraph(f"<b>Resumo:</b>", sNormal))
                bloco.append(Paragraph(resumo_ia_det, sResumoIA))

            story.append(KeepTogether(bloco))

            # ── Nota técnica em quadro cinza ─────────────────────────────
            resumo = it.get("resumo_materia", "") or ""
            if resumo.strip():
                story.append(Spacer(1, 5))
                story.append(Paragraph("Nota Técnica", sBold))

                texto = _html_para_texto(resumo)
                paras_nota = []
                for idx_l, linha in enumerate(texto.split("\n")):
                    linha = linha.strip()
                    if not linha:
                        paras_nota.append(Spacer(1, 3))
                        continue

                    emoji_sec = next((e for e in SECOES if linha.startswith(e)), None)

                    if emoji_sec:
                        paras_nota.append(Paragraph(_sax.escape(linha), sSecao[emoji_sec]))
                    elif "Análise baseada em" in linha or "baseada em:" in linha.lower():
                        paras_nota.append(Paragraph(
                            f'<font color="#CC0000"><i>{_sax.escape(linha)}</i></font>',
                            ParagraphStyle(f"sNtB_{idx_l}", parent=SS["Normal"],
                                fontSize=9, leading=12, fontName="Helvetica-Oblique",
                                textColor=colors.HexColor("#CC0000"))))
                    else:
                        paras_nota.append(Paragraph(_sax.escape(linha),
                            ParagraphStyle(f"sNtU_{idx_l}", parent=SS["Normal"],
                                fontSize=9.5, leading=13.5, wordWrap="CJK",
                                textColor=colors.black, fontName="Helvetica")))

                if paras_nota:
                    rows_final = []
                    for p in paras_nota:
                        if isinstance(p, Spacer):
                            rows_final.append([Paragraph("", ParagraphStyle(f"sNtSp_{id(p)}",
                                parent=SS["Normal"], fontSize=4))])
                        else:
                            rows_final.append([p])

                    tbl_nota = Table(rows_final, colWidths=[doc.width])
                    tbl_nota.setStyle(TableStyle([
                        ("BACKGROUND",    (0,0),(-1,-1), colors.HexColor("#F5F5F5")),
                        ("BOX",           (0,0),(-1,-1), 0.5, colors.HexColor("#CCCCCC")),
                        ("LEFTPADDING",   (0,0),(-1,-1), 10),
                        ("RIGHTPADDING",  (0,0),(-1,-1), 10),
                        ("TOPPADDING",    (0,0),(-1,-1), 3),
                        ("BOTTOMPADDING", (0,0),(-1,-1), 3),
                        ("TOPPADDING",    (0,0),(-1,0),  8),
                        ("BOTTOMPADDING", (0,-1),(-1,-1),8),
                    ]))
                    story.append(tbl_nota)

            story.append(Spacer(1, 16))

        doc.build(story)
        pdf = buffer.getvalue()
        buffer.close()

        resp = make_response(pdf)
        resp.headers["Content-Type"]        = "application/pdf"
        resp.headers["Content-Disposition"] = f'inline; filename="Pauta_{evento_id}.pdf"'
        resp.headers["Cache-Control"]       = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"]              = "no-cache"
        return resp

    except Exception as e:
        import traceback
        current_app.logger.error(f"[exportar] erro: {e}\n{traceback.format_exc()}")
        return f"Erro ao gerar PDF: {e}<br><pre>{traceback.format_exc()}</pre>", 200
