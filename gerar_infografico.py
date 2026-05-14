from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import cm, mm
from reportlab.pdfgen import canvas
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import Paragraph
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from io import BytesIO
import os
import re
import textwrap

# Cores do infográfico
COR_AZUL_ESCURO   = colors.HexColor("#0D2B5E")
COR_AZUL_MEDIO    = colors.HexColor("#1a3a6b")
COR_VERDE         = colors.HexColor("#1A6B3A")
COR_VERDE         = colors.HexColor("#1A7C3E")
COR_VERDE_CLARO   = colors.HexColor("#E8F5EE")
COR_VERMELHO      = colors.HexColor("#8B0000")
COR_VERMELHO_CLARO= colors.HexColor("#FDEAEA")
COR_AMARELO       = colors.HexColor("#B8860B")
COR_AMARELO_CLARO = colors.HexColor("#FFF8E1")
COR_CINZA         = colors.HexColor("#555555")
COR_CINZA_CLARO   = colors.HexColor("#F5F5F5")
COR_BORDA         = colors.HexColor("#CCCCCC")
COR_AZUL_CLARO    = colors.HexColor("#E8EEF7")
COR_LARANJA       = colors.HexColor("#C0392B")

CORES_ORIENTACAO = {
    "SIM":        (COR_VERDE,    COR_VERDE_CLARO,    colors.white),
    "NÃO":        (COR_VERMELHO, COR_VERMELHO_CLARO, colors.white),
    "OBSTRUÇÃO":  (COR_VERMELHO, COR_VERMELHO_CLARO, colors.white),
    "NEGOCIAÇÃO": (COR_AMARELO,  COR_AMARELO_CLARO,  colors.white),
    "LIBERADO":   (COR_AMARELO,  COR_AMARELO_CLARO,  colors.white),
    "ABSTENÇÃO":  (COR_CINZA,    COR_CINZA_CLARO,    colors.white),
}

def strip_html(text):
    text = re.sub(r'<[^>]+>', ' ', str(text or ''))
    text = re.sub(r'\s+', ' ', text).strip()
    return text

def wrap_text(c, text, x, y, max_width, font_name, font_size, line_height, color=None):
    """Escreve texto com quebra de linha manual."""
    if color:
        c.setFillColor(color)
    c.setFont(font_name, font_size)
    words = text.split()
    lines = []
    line = ""
    for word in words:
        test = (line + " " + word).strip()
        if c.stringWidth(test, font_name, font_size) <= max_width:
            line = test
        else:
            if line:
                lines.append(line)
            line = word
    if line:
        lines.append(line)
    for i, l in enumerate(lines):
        c.drawString(x, y - i * line_height, l)
    return len(lines)

def gerar_infografico_pdf(evento, itens, logo_minoria_path=None, logo_oposicao_path=None):
    buffer = BytesIO()
    W, H = A4  # 595 x 842 pts

    c = canvas.Canvas(buffer, pagesize=A4)
    c.setTitle(f"Resumo da Pauta — {evento.get('descricao', '')}")

    MARGIN = 1.2 * cm
    CONTENT_W = W - 2 * MARGIN

    def nova_pagina(primeira=False):
        if not primeira:
            c.showPage()
        c.setFillColor(colors.white)
        c.rect(0, 0, W, H, fill=1, stroke=0)

        # Faixa superior branca com linha verde
        cab_h = 1.2*cm
        c.setFillColor(colors.white)
        c.rect(0, H - cab_h, W, cab_h, fill=1, stroke=0)
        c.setStrokeColor(COR_VERDE)
        c.setLineWidth(1.5)
        c.line(0, H - cab_h, W, H - cab_h)

        # Texto no cabeçalho em verde
        c.setFillColor(COR_VERDE)
        c.setFont("Helvetica-Bold", 9)
        c.drawCentredString(W/2, H - 0.85*cm, "Lideranças da Minoria e da Oposição — Câmara dos Deputados")

        # Rodapé com logos do mesmo tamanho + texto
        rod_h = 1.5*cm
        c.setFillColor(colors.white)
        c.rect(0, 0, W, rod_h, fill=1, stroke=0)
        c.setStrokeColor(COR_VERDE)
        c.setLineWidth(1)
        c.line(MARGIN, rod_h, W - MARGIN, rod_h)

        logo_w = 2.0*cm
        logo_h = 1.1*cm
        y_rod  = (rod_h - logo_h) / 2

        if logo_minoria_path and os.path.exists(logo_minoria_path):
            try:
                c.drawImage(logo_minoria_path, MARGIN, y_rod,
                           width=logo_w, height=logo_h,
                           preserveAspectRatio=True, mask='auto')
            except Exception:
                pass

        if logo_oposicao_path and os.path.exists(logo_oposicao_path):
            try:
                c.drawImage(logo_oposicao_path, MARGIN + logo_w + 0.2*cm, y_rod,
                           width=logo_w, height=logo_h,
                           preserveAspectRatio=True, mask='auto')
            except Exception:
                pass

        c.setFillColor(COR_CINZA)
        c.setFont("Helvetica", 7)
        c.drawCentredString(W/2, 0.55*cm,
            "Lideranças da Minoria e da Oposição — Plenário / Câmara dos Deputados")
        c.drawRightString(W - MARGIN, 0.55*cm, f"Pág. {c.getPageNumber()}")

        return H - cab_h - 0.3*cm

    def desenhar_titulo(y, evento):
        titulo_h = 1.8*cm
        c.setFillColor(COR_AZUL_CLARO)
        c.rect(MARGIN, y - titulo_h, CONTENT_W, titulo_h, fill=1, stroke=0)
        c.setStrokeColor(COR_AZUL_ESCURO)
        c.setLineWidth(1)
        c.rect(MARGIN, y - titulo_h, CONTENT_W, titulo_h, fill=0, stroke=1)

        from datetime import datetime
        dt_str = evento.get('dataHoraInicio', '')
        try:
            dt = datetime.fromisoformat(dt_str)
            data_fmt = dt.strftime('%d/%m/%Y')
            hora_fmt = dt.strftime('%H:%M')
        except Exception:
            data_fmt = ''
            hora_fmt = ''

        desc   = evento.get('descricao', 'Sessão Deliberativa')
        titulo = f"Resumo da Pauta — {desc}"
        subtit = f"Data: {data_fmt}  |  Hora: {hora_fmt}  |  Local: {evento.get('local', '')}"

        # Trunca se necessário
        max_w = CONTENT_W - 0.4*cm
        while c.stringWidth(titulo, "Helvetica-Bold", 10) > max_w and len(titulo) > 10:
            titulo = titulo[:-4] + "..."

        c.setFillColor(COR_AZUL_ESCURO)
        c.setFont("Helvetica-Bold", 10)
        c.drawCentredString(W/2, y - 0.7*cm, titulo)

        c.setFillColor(COR_CINZA)
        c.setFont("Helvetica", 8)
        c.drawCentredString(W/2, y - 1.35*cm, subtit)

        return y - titulo_h - 0.25*cm

    def desenhar_item(c, item, y, page_bottom):
        """Desenha um card de item. Retorna novo y e se precisou de nova página."""
        CARD_MARGIN = 0.3*cm
        BADGE_W = 2.4*cm
        INNER_W = CONTENT_W - 2*CARD_MARGIN - BADGE_W - 0.4*cm

        orientacao = (item.get('orientacao') or '').strip().upper()
        cor_badge, cor_badge_bg, cor_badge_txt = CORES_ORIENTACAO.get(
            orientacao, (COR_CINZA, COR_CINZA_CLARO, colors.white)
        )

        projeto = item.get('projeto', '')
        autor = strip_html(item.get('autor', 'N/D'))
        relator = item.get('relator', 'N/D')
        ementa = strip_html(item.get('ementa', ''))
        ordem = item.get('ordem', '')

        # Estima altura do card
        ementa_chars_per_line = int(INNER_W / 4.8)
        ementa_lines = max(1, len(ementa) // ementa_chars_per_line + 1)
        ementa_lines = min(ementa_lines, 4)
        card_h = 1.0*cm + ementa_lines * 0.38*cm + 0.5*cm
        card_h = max(card_h, 1.8*cm)

        # Nova página se não couber
        if y - card_h < page_bottom:
            return None, True  # sinal de nova página

        # Fundo do card
        c.setFillColor(colors.white)
        c.setStrokeColor(COR_BORDA)
        c.setLineWidth(0.5)
        c.roundRect(MARGIN, y - card_h, CONTENT_W, card_h, 4, fill=1, stroke=1)

        # Borda esquerda colorida
        c.setFillColor(cor_badge)
        c.roundRect(MARGIN, y - card_h, 0.25*cm, card_h, 2, fill=1, stroke=0)

        # Badge de orientação (lado direito)
        badge_x = MARGIN + CONTENT_W - BADGE_W - 0.1*cm
        badge_y = y - card_h/2 - 0.4*cm
        badge_h = 0.85*cm
        c.setFillColor(cor_badge)
        c.roundRect(badge_x, badge_y, BADGE_W, badge_h, 4, fill=1, stroke=0)
        c.setFillColor(colors.white)
        c.setFont("Helvetica-Bold", 8 if len(orientacao) <= 8 else 6.5)
        c.drawCentredString(badge_x + BADGE_W/2, badge_y + 0.2*cm, orientacao or "—")

        # Conteúdo interno
        tx = MARGIN + 0.45*cm
        ty = y - 0.35*cm

        # Número do item + código
        c.setFillColor(COR_AZUL_ESCURO)
        c.setFont("Helvetica-Bold", 9)
        header = f"ITEM {ordem}  |  {projeto}"
        c.drawString(tx, ty, header)

        # Autor e Relator
        ty -= 0.38*cm
        c.setFillColor(COR_CINZA)
        c.setFont("Helvetica", 7.5)
        autor_short = autor[:55] + "..." if len(autor) > 55 else autor
        c.drawString(tx, ty, f"Autor: {autor_short}")
        ty -= 0.32*cm
        c.drawString(tx, ty, f"Relator: {relator}")

        # Ementa
        ty -= 0.35*cm
        c.setFillColor(colors.black)
        c.setFont("Helvetica", 7.8)
        ementa_max = ementa[:280] + "..." if len(ementa) > 280 else ementa
        words = ementa_max.split()
        line = ""
        lines_drawn = 0
        max_lines = 4
        for word in words:
            test = (line + " " + word).strip()
            if c.stringWidth(test, "Helvetica", 7.8) <= INNER_W:
                line = test
            else:
                if lines_drawn < max_lines:
                    c.drawString(tx, ty - lines_drawn * 0.33*cm, line)
                    lines_drawn += 1
                line = word
        if line and lines_drawn < max_lines:
            c.drawString(tx, ty - lines_drawn * 0.33*cm, line)

        return y - card_h - 0.2*cm, False

    # === RENDERIZAÇÃO ===
    page_bottom = 1.5*cm
    y = nova_pagina(primeira=True)
    y = desenhar_titulo(y, evento)
    y -= 0.2*cm

    for item in itens:
        novo_y, precisa_pagina = desenhar_item(c, item, y, page_bottom)
        if precisa_pagina:
            y = nova_pagina(primeira=False)
            novo_y, _ = desenhar_item(c, item, y, page_bottom)
        y = novo_y if novo_y else page_bottom

    # Rodapé última página
    c.setFillColor(COR_CINZA)
    c.setFont("Helvetica", 7)
    c.drawCentredString(W/2, 0.8*cm, "Assessoria Parlamentar — CCJC / Câmara dos Deputados")

    c.save()
    return buffer.getvalue()
