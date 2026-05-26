import requests
from bs4 import BeautifulSoup
import re
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

def _extrair_partido_via_api(nome):
    """Busca partido de um deputado pelo nome via API."""
    try:
        r = requests.get(
            f"https://dadosabertos.camara.leg.br/api/v2/deputados?nome={requests.utils.quote(nome)}&itens=1",
            timeout=6
        )
        if r.ok:
            deps = r.json().get("dados", [])
            if deps:
                sigla = deps[0].get("siglaPartido", "")
                uf    = deps[0].get("siglaUf", "")
                return f"{sigla}-{uf}" if sigla and uf else sigla
    except Exception:
        pass
    return ""

def obter_detalhes_proposicao(id_prop):
    """Obtém detalhes via API, incluindo partido dos autores."""
    base = f"https://dadosabertos.camara.leg.br/api/v2/proposicoes/{id_prop}"
    detalhes = {"autores": "", "relator": "", "situacao": "", "ementa": "",
                "urlInteiroTeor": "", "tem_mais_autores": False}
    try:
        r = requests.get(base, timeout=8)
        if r.ok:
            j = r.json().get("dados", {})
            detalhes["situacao"] = j.get("statusProposicao", {}).get("descricaoSituacao", "")
            detalhes["ementa"]   = j.get("ementa", "")
            detalhes["urlInteiroTeor"] = j.get("urlInteiroTeor", "")

        # Autores com partido
        r_autores = requests.get(base + "/autores", timeout=8)
        if r_autores.ok:
            autores_dados = r_autores.json().get("dados", [])
            autores = []
            for a in autores_dados:
                if "nome" not in a:
                    continue
                # API retorna siglaPartido diretamente
                partido = (a.get("siglaPartido") or "").strip()
                uf      = (a.get("siglaUf") or "").strip()
                sufixo  = f"{partido}-{uf}" if partido and uf else partido
                autores.append(f"{a['nome']} ({sufixo})" if sufixo else a['nome'])
            if len(autores) > 3:
                detalhes["autores"] = ", ".join(autores[:3]) + " e outros."
                detalhes["tem_mais_autores"] = True
            else:
                detalhes["autores"] = ", ".join(autores)

    except Exception as e:
        logger.warning(f"⚠️ Falha ao obter detalhes da proposição {id_prop}: {e}")

    return detalhes

def _extrair_info_pauta_html(info_div):
    """Extrai autor e relator diretamente do HTML do card — já vem com partido."""
    autores = ""
    relator = ""
    if not info_div:
        return autores, relator
    for li in info_div.find_all("li"):
        texto = li.get_text(separator=" ", strip=True)
        # Remove o rótulo ("Autor:", "Autores:", "Relator:", "Relatora:")
        m_autor   = re.match(r'Autores?:\s*(.*)', texto, re.IGNORECASE)
        m_relator = re.match(r'Relatora?:\s*(.*)', texto, re.IGNORECASE)
        if m_autor:
            autores = m_autor.group(1).strip()
        elif m_relator:
            relator = m_relator.group(1).strip()
    return autores, relator

def buscar_id_proposicao_por_codigo(codigo):
    try:
        match = re.match(r"(\w+)\s+(\d+)/(\d+)", codigo.strip())
        if not match:
            return None
        sigla_tipo, numero, ano = match.groups()
        url = f"https://dadosabertos.camara.leg.br/api/v2/proposicoes?siglaTipo={sigla_tipo}&numero={numero}&ano={ano}"
        r = requests.get(url, timeout=8)
        if r.ok:
            dados = r.json().get("dados", [])
            if dados:
                return str(dados[0].get("id"))
        return None
    except Exception as e:
        logger.warning(f"⚠️ Erro ao buscar idProposicao para {codigo}: {e}")
        return None

def obter_itens_pauta(id_evento):
    url_evento = f"https://www.camara.leg.br/evento-legislativo/{id_evento}"
    logger.info(f"🌐 Acessando {url_evento} ...")

    try:
        resp = requests.get(url_evento, timeout=10)
        resp.raise_for_status()
    except Exception as e:
        logger.error(f"❌ Falha ao baixar HTML: {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    secoes_h2 = soup.find_all("h2", class_="info-reveal__title")

    itens  = []
    vistos = set()

    for h2 in secoes_h2:
        texto_raw  = h2.get_text(strip=True)
        texto_limpo = re.sub(r'\s*\d+$', '', texto_raw).strip().lower()

        if "previstas" in texto_limpo:
            secao_nome = "Proposta Prevista"
        elif "não analisadas" in texto_limpo:
            secao_nome = "Proposta Não Analisada"
        elif "analisadas" in texto_limpo:
            secao_nome = "Proposta Analisada"
        else:
            secao_nome = texto_limpo.title()

        botao_toggle = h2.find_next_sibling("button", class_="info-reveal__toggle-button")
        if not botao_toggle:
            continue
        target_id = botao_toggle.get("data-target")
        if not target_id:
            continue
        div_collapse = soup.find("div", id=target_id.replace("#", ""))
        if not div_collapse:
            continue
        ul_lista = div_collapse.find("ul", class_="l-pauta__lista")
        if not ul_lista:
            continue

        for li in ul_lista.find_all("li", class_="l-pauta__item"):
            try:
                titulo_tag = li.find("a", class_="item-pauta__proposicao")
                if not titulo_tag:
                    continue

                codigo    = titulo_tag.get_text(strip=True)
                url       = titulo_tag.get("href") or ""
                ementa_tag = titulo_tag.find_parent("p")
                ementa_html = ementa_tag.get_text(strip=True) if ementa_tag else ""

                # ── Extrai autor/relator do HTML (já vem com partido) ──
                info_div = li.find("div", class_="info-pauta")
                autores, relator = _extrair_info_pauta_html(info_div)

                # ID da proposição
                match_id = re.search(r"idProposicao=(\d+)", url)
                id_prop  = match_id.group(1) if match_id else None
                if not id_prop:
                    id_prop = buscar_id_proposicao_por_codigo(codigo)
                if not id_prop:
                    continue
                if id_prop in vistos:
                    continue
                vistos.add(id_prop)

                # Detalhes via API (ementa completa, situação, autores c/ partido da API)
                info_extra = obter_detalhes_proposicao(id_prop)

                # Usa autores da API (com partido) se disponível; senão mantém o do HTML
                if info_extra["autores"]:
                    autores = info_extra["autores"]

                # Relator: mantém o do HTML (já tem partido); API não retorna relator facilmente
                if not relator and info_extra.get("relator"):
                    relator = info_extra["relator"]

                itens.append({
                    "id_principal":    id_prop,
                    "codigo":          codigo,
                    "ementa":          info_extra["ementa"] or ementa_html,
                    "autores":         autores,
                    "relator":         relator or "Não atribuído",
                    "situacao":        info_extra["situacao"] or "N/D",
                    "urlInteiroTeor":  info_extra["urlInteiroTeor"],
                    "url":             url,
                    "secao":           secao_nome,
                    "tem_mais_autores": info_extra["tem_mais_autores"]
                })

            except Exception as e:
                logger.warning(f"⚠️ Erro ao processar item da pauta ({secao_nome}): {e}")

    logger.info(f"📊 Total de {len(itens)} proposições coletadas.")
    return itens

if __name__ == "__main__":
    evento_teste = int(sys.argv[1]) if len(sys.argv) > 1 else 79930
    resultados = obter_itens_pauta(evento_teste)
    for i, r in enumerate(resultados, start=1):
        print(f"\n{i}. {r['codigo']} — {r['autores']} | Relator: {r['relator']}")
    print(f"\nTotal: {len(resultados)}")
