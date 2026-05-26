import requests
from bs4 import BeautifulSoup
import re
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

def obter_detalhes_proposicao(id_prop):
    """Obtém detalhes complementares de uma proposição pela API da Câmara"""
    base = f"https://dadosabertos.camara.leg.br/api/v2/proposicoes/{id_prop}"
    detalhes = {
        "autores": "",
        "relator": "",
        "situacao": "",
        "ementa": "",
        "urlInteiroTeor": "",
        "tem_mais_autores": False
    }
    try:
        r = requests.get(base, timeout=8)
        if r.ok:
            j = r.json().get("dados", {})
            detalhes["situacao"] = j.get("statusProposicao", {}).get("descricaoSituacao", "")
            detalhes["ementa"] = j.get("ementa", "")
            detalhes["urlInteiroTeor"] = j.get("urlInteiroTeor", "")

        # Autores com partido
        r_autores = requests.get(base + "/autores", timeout=8)
        if r_autores.ok:
            autores_dados = r_autores.ok and r_autores.json().get("dados", [])
            autores = []
            for a in autores_dados:
                if "nome" not in a:
                    continue
                partido = a.get("siglaPartido", "") or a.get("siglaPartidoAutor", "")
                if partido:
                    autores.append(f"{a['nome']} ({partido})")
                else:
                    autores.append(a['nome'])
            if len(autores) > 3:
                detalhes["autores"] = ", ".join(autores[:3]) + " e outros."
                detalhes["tem_mais_autores"] = True
            else:
                detalhes["autores"] = ", ".join(autores)
                detalhes["tem_mais_autores"] = False

        # Relator com partido — busca via tramitações
        try:
            r_tram = requests.get(base + "/tramitacoes?itens=10&ordem=DESC", timeout=8)
            if r_tram.ok:
                for t in r_tram.json().get("dados", []):
                    despacho = (t.get("despacho") or "").lower()
                    if "relator" in despacho:
                        # Extrai nome do relator do despacho
                        m = re.search(r"dep(?:\.|utad[ao])?\s+([A-ZÀ-Ú][a-zà-ú]+(?:\s+[A-ZÀ-Ú][a-zà-ú]+)+)", t.get("despacho",""))
                        if m:
                            relator_nome = m.group(1).strip()
                            # Busca partido do relator
                            r_dep = requests.get(
                                f"https://dadosabertos.camara.leg.br/api/v2/deputados?nome={requests.utils.quote(relator_nome)}&itens=1",
                                timeout=6
                            )
                            if r_dep.ok:
                                deps = r_dep.json().get("dados", [])
                                if deps:
                                    partido = deps[0].get("siglaPartido", "")
                                    detalhes["relator"] = f"{relator_nome} ({partido})" if partido else relator_nome
                                    break
        except Exception:
            pass

    except Exception as e:
        logger.warning(f"⚠️ Falha ao obter detalhes da proposição {id_prop}: {e}")

    return detalhes

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
    logger.info(f"🔍 Seções h2 detectadas: {[h2.get_text(strip=True) for h2 in secoes_h2]}")

    itens = []
    vistos = set()

    for h2 in secoes_h2:
        texto_raw = h2.get_text(strip=True)
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

                codigo = titulo_tag.get_text(strip=True)
                url = titulo_tag.get("href") or ""
                ementa_tag = titulo_tag.find_parent("p")
                ementa_html = ementa_tag.get_text(strip=True) if ementa_tag else ""

                info = li.find("div", class_="info-pauta")
                autores = ""
                relator = ""

                if info:
                    autor_tag = info.find(string=re.compile("Autor", re.I))
                    if autor_tag and autor_tag.parent:
                        autores = autor_tag.parent.find_next_sibling(string=True) or ""
                        autores = autores.strip(" :") if autores else ""
                    relator_tag = info.find(string=re.compile("Relator", re.I))
                    if relator_tag and relator_tag.parent:
                        relator = relator_tag.parent.find_next_sibling(string=True) or ""
                        relator = relator.strip(" :") if relator else ""

                match_id = re.search(r"idProposicao=(\d+)", url)
                id_prop = match_id.group(1) if match_id else None

                if not id_prop:
                    id_prop = buscar_id_proposicao_por_codigo(codigo)
                if not id_prop:
                    continue
                if id_prop in vistos:
                    continue
                vistos.add(id_prop)

                info_extra = obter_detalhes_proposicao(id_prop)
                if info_extra["autores"]:
                    autores = info_extra["autores"]
                if info_extra["relator"]:
                    relator = info_extra["relator"]

                itens.append({
                    "id_principal": id_prop,
                    "codigo": codigo,
                    "ementa": info_extra["ementa"] or ementa_html,
                    "autores": autores,
                    "relator": relator or "Não atribuído",
                    "situacao": info_extra["situacao"] or "N/D",
                    "urlInteiroTeor": info_extra["urlInteiroTeor"],
                    "url": url,
                    "secao": secao_nome,
                    "tem_mais_autores": info_extra["tem_mais_autores"]
                })

            except Exception as e:
                logger.warning(f"⚠️ Erro ao processar item: {e}")

    logger.info(f"📊 Total de {len(itens)} proposições coletadas.")
    return itens

if __name__ == "__main__":
    evento_teste = int(sys.argv[1]) if len(sys.argv) > 1 else 79930
    resultados = obter_itens_pauta(evento_teste)
    for i, r in enumerate(resultados, start=1):
        print(f"\n{i}. {r['codigo']} — {r['autores']} | Relator: {r['relator']}")
