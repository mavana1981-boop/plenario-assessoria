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
    """Busca itens da pauta e injeta resumo_materia SEMPRE fresco do banco."""
    try:
        from app import fetch_pauta, load_notas
        itens, _ = fetch_pauta(evento_id, force_reload=False)
        notas = load_notas()   # busca direto do banco, sem cache
        for item in itens:
            key = f"PROP_{item.get('id_principal', '')}"
            if key in notas:
                nota = notas[key]
                item["resumo_materia"] = nota.get("resumo_materia", "") or ""
                item["orientacao"]     = nota.get("orientacao", "")    or item.get("orientacao", "")
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
    # logos
    for path, x in logos:
        if os.path.exists(path):
            try:
                canvas.drawImage(path, x, h-2.55*cm, width=2.0*cm,
                                 preserveAspectRatio=True, mask="auto")
            except Exception:
                pass
    canvas.setFont("Helvetica-Bold", 9.5)
    tw = stringWidth(titulo, "Helvetica-Bold", 9.5)
    canvas.drawString((w - tw) / 2, h-1.7*cm, titulo)
    # rodapé
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
        logos  = [
            (os.path.join(static, "logo_minoria.png"),  1.5*cm),
            (os.path.join(static, "logo_oposicao.png"), A4[0]-3.5*cm),
        ]

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
        }

        sTabProj  = ParagraphStyle("sTP", parent=SS["Normal"],
            fontSize=9.5, leading=13, wordWrap="CJK", alignment=1)
        sTabMeta  = ParagraphStyle("sTM", parent=SS["Normal"],
            fontSize=7.5, leading=10, wordWrap="CJK", alignment=1,
            fontName="Helvetica-Oblique", textColor=colors.HexColor("#444444"))
        sTabEmenta = ParagraphStyle("sTE", parent=SS["Normal"],
            fontSize=9, leading=12, wordWrap="CJK", alignment=1)
        sTabEmentaSub = ParagraphStyle("sTES", parent=SS["Normal"],
            fontSize=7, leading=10, wordWrap="CJK", alignment=1,
            fontName="Helvetica-Oblique", textColor=colors.HexColor("#666666"))
        sTabOri   = ParagraphStyle("sTO", parent=SS["Normal"],
            fontSize=9.5, leading=13, wordWrap="CJK", alignment=1)
        sTabNum   = ParagraphStyle("sTN", parent=SS["Normal"],
            fontSize=9.5, leading=13, wordWrap="CJK", alignment=1)

        tdata = [[ Paragraph("<b>Nº</b>", sTabNum),
                   Paragraph("<b>Proposição</b>", sTabProj),
                   Paragraph("<b>Objeto</b>", sTabEmenta),
                   Paragraph("<b>Orientação</b>", sTabOri) ]]

        for it in itens:
            ori     = (it.get("orientacao") or "").strip()
            cor_ori = COR_ORI.get(ori, CINZA)

            # Coluna Proposição: título + autor + relator
            proj_txt   = _sax.escape(str(it.get("projeto","—")))
            autor_txt  = _sax.escape(str(it.get("autor","") or ""))
            relator_txt= _sax.escape(str(it.get("relator","") or ""))
            # Primeiro autor apenas
            primeiro_autor = autor_txt.split(",")[0].strip() if autor_txt else ""
            proj_xml = f"<b>{proj_txt}</b>"
            if primeiro_autor:
                proj_xml += f"<br/><i>{primeiro_autor}</i>"
            if relator_txt and relator_txt != "Não atribuído":
                proj_xml += f"<br/><i>Rel: {relator_txt[:50]}</i>"

            # Coluna Objeto: resumo da nota ou resumo_ia; abaixo ementa em cinza menor
            nota_txt   = _html_para_texto(it.get("resumo_materia","") or "")
            resumo_ia  = _html_para_texto(it.get("resumo_ia","") or "")
            ementa_txt = _html_para_texto(it.get("ementa","") or "")
            objeto_principal = nota_txt[:400] if nota_txt.strip() else resumo_ia[:400]
            objeto_xml = _sax.escape(objeto_principal)
            if ementa_txt:
                objeto_xml += f'<br/><font size="7" color="#666666"><i>{_sax.escape(ementa_txt[:250])}</i></font>'

            # Coluna orientação com cor
            if ori:
                hex_ori = "%02x%02x%02x" % (
                    int(cor_ori.red*255), int(cor_ori.green*255), int(cor_ori.blue*255))
                ori_xml = f'<b><font color="#{hex_ori}">{_sax.escape(ori)}</font></b>'
            else:
                ori_xml = "—"

            tdata.append([
                Paragraph(str(it.get("ordem","—")), sTabNum),
                Paragraph(proj_xml, sTabMeta),
                Paragraph(objeto_xml or "—", sTabEmenta),
                Paragraph(ori_xml, sTabOri),
            ])

        tbl = Table(tdata, colWidths=[1.0*cm, 4.0*cm, 9.4*cm, 2.8*cm],
                    repeatRows=1, splitByRow=True)
        tbl.setStyle(TableStyle([
            ("FONTNAME",      (0,0),(-1,0),  "Helvetica-Bold"),
            ("BACKGROUND",    (0,0),(-1,0),  colors.HexColor("#E8F3EC")),
            ("GRID",          (0,0),(-1,-1), 0.3, colors.HexColor("#cccccc")),
            ("ROWBACKGROUNDS",(0,1),(-1,-1), [colors.white, colors.HexColor("#F8F8F8")]),
            ("VALIGN",        (0,0),(-1,-1), "TOP"),
            ("ALIGN",         (0,0),(-1,-1), "CENTER"),
            ("TOPPADDING",    (0,0),(-1,-1), 5),
            ("BOTTOMPADDING", (0,0),(-1,-1), 5),
            ("LEFTPADDING",   (0,0),(-1,-1), 4),
            ("RIGHTPADDING",  (0,0),(-1,-1), 4),
        ]))
        story.append(tbl)
        story.append(PageBreak())

        # ── Itens detalhados ────────────────────────────────────────────
        for it in itens:
            projeto  = _sax.escape(str(it.get("projeto","—")))
            autor    = _sax.escape(str(it.get("autor","N/D") or "N/D"))
            relator  = _sax.escape(str(it.get("relator","N/D") or "N/D"))
            situacao = _sax.escape(str(it.get("situacao","") or ""))
            ori      = _sax.escape(str(it.get("orientacao","") or ""))
            ementa   = _sax.escape(_html_para_texto(it.get("ementa","") or ""))

            bloco = []
            bloco.append(Paragraph(
                f'<font color="#0D2B5E"><b>Item {it.get("ordem","—")} — {projeto}</b></font>',
                sHead))
            bloco.append(Paragraph(f"<b>Autor:</b> {autor}", sNormal))
            bloco.append(Paragraph(f"<b>Relator:</b> {relator}", sNormal))
            if situacao:
                bloco.append(Paragraph(f"<b>Situação:</b> {situacao}", sNormal))
            if ementa:
                bloco.append(Paragraph(f"<b>Ementa:</b> {ementa}", sNormal))
            if ori:
                cor_ori = COR_ORI.get(it.get("orientacao",""), CINZA)
                bloco.append(Paragraph(
                    f'<b>Orientação:</b> <font color="#{cor_ori.hexval()[2:]}">{ori}</font>',
                    sNormal))

            story.append(KeepTogether(bloco))

            # ── Nota técnica ──────────────────────────────────────────
            resumo = it.get("resumo_materia", "") or ""
            if resumo.strip():
                story.append(Spacer(1, 5))
                story.append(Paragraph("Nota Técnica", sBold))

                texto = _html_para_texto(resumo)
                linhas = texto.split("\n")

                # Monta lista de parágrafos para o quadro cinza
                paras_nota = []
                for idx_l, linha in enumerate(linhas):
                    linha = linha.strip()
                    if not linha:
                        paras_nota.append(Spacer(1, 3))
                        continue

                    emoji_sec = next((e for e in SECOES if linha.startswith(e)), None)

                    if emoji_sec:
                        # Título de seção — colorido e negrito
                        st = sSecao[emoji_sec]
                        paras_nota.append(Paragraph(_sax.escape(linha), st))
                    elif "Análise baseada em" in linha or "baseada em:" in linha.lower():
                        # "Análise baseada em" — vermelho itálico
                        paras_nota.append(Paragraph(
                            f'<font color="#CC0000"><i>{_sax.escape(linha)}</i></font>',
                            _sNota_unico(idx_l)))
                    else:
                        # Texto normal — SEMPRE preto, nome único para evitar cache
                        paras_nota.append(Paragraph(_sax.escape(linha), _sNota_unico(idx_l)))

                # Coloca os parágrafos em uma tabela de 1 coluna (quadro cinza claro)
                if paras_nota:
                    rows_nota = [[p] for p in paras_nota if not isinstance(p, Spacer)]
                    # Insere spacers como linhas vazias
                    rows_final = []
                    for p in paras_nota:
                        if isinstance(p, Spacer):
                            rows_final.append([Paragraph("", _sNota_unico(9999))])
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
