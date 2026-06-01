import requests
from bs4 import BeautifulSoup
import re
import logging
import sys

# -----------------------------------------------------------------------------
# LOGGING
# -----------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# FUNÇÃO AUXILIAR PARA DETALHES DE PROPOSIÇÃO
# -----------------------------------------------------------------------------
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

        # Autores
        r_autores = requests.get(base + "/autores", timeout=8)
        if r_autores.ok:
            autores_dados = r_autores.json().get("dados", [])
            autores = [f"{a['nome']}" for a in autores_dados if "nome" in a]
            # Limitar a 3 autores, com "e outros" se houver mais
            if len(autores) > 3:
                detalhes["autores"] = ", ".join(autores[:3]) + " e outros."
                detalhes["tem_mais_autores"] = True
            else:
                detalhes["autores"] = ", ".join(autores)
                detalhes["tem_mais_autores"] = False

    except Exception as e:
        logger.warning(f"⚠️ Falha ao obter detalhes da proposição {id_prop}: {e}")

    return detalhes

# -----------------------------------------------------------------------------
# FUNÇÃO AUXILIAR PARA BUSCAR ID DA PROPOSIÇÃO
# -----------------------------------------------------------------------------
def buscar_id_proposicao_por_codigo(codigo):
    """Busca o idProposicao pela API usando siglaTipo, numero e ano extraídos do código"""
    try:
        match = re.match(r"(\w+)\s+(\d+)/(\d+)", codigo.strip())
        if not match:
            logger.warning(f"⚠️ Formato de código inválido: {codigo}")
            return None
        sigla_tipo, numero, ano = match.groups()
        url = f"https://dadosabertos.camara.leg.br/api/v2/proposicoes?siglaTipo={sigla_tipo}&numero={numero}&ano={ano}"
        r = requests.get(url, timeout=8)
        if r.ok:
            dados = r.json().get("dados", [])
            if dados:
                return str(dados[0].get("id"))
        logger.warning(f"⚠️ Nenhuma proposição encontrada para {codigo}")
        return None
    except Exception as e:
        logger.warning(f"⚠️ Erro ao buscar idProposicao para {codigo}: {e}")
        return None

# -----------------------------------------------------------------------------
# FUNÇÃO PRINCIPAL DE SCRAPING
# -----------------------------------------------------------------------------
def obter_itens_pauta(id_evento):
    """Obtém a lista de proposições da pauta de um evento legislativo pelo site da Câmara"""
    url_evento = f"https://www.camara.leg.br/evento-legislativo/{id_evento}"
    logger.info(f"🌐 Acessando {url_evento} ...")

    try:
        resp = requests.get(url_evento, timeout=10)
        resp.raise_for_status()
    except Exception as e:
        logger.error(f"❌ Falha ao baixar HTML: {e}")
        return []

    html = resp.text
    logger.info(f"📄 HTML baixado ({len(html)} caracteres)")

    soup = BeautifulSoup(html, "html.parser")

    # Buscar todas as seções h2 com classe info-reveal__title
    secoes_h2 = soup.find_all("h2", class_="info-reveal__title")
    logger.info(f"🔍 Seções h2 detectadas: {[h2.get_text(strip=True) for h2 in secoes_h2]}")

    itens = []
    vistos = set()  # Para evitar duplicatas

    # Mapeamento de seções específicas baseadas no texto do h2 e no target do botão de toggle
    for h2 in secoes_h2:
        texto_raw = h2.get_text(strip=True)
        # Limpar números do título
        texto_limpo = re.sub(r'\s*\d+$', '', texto_raw).strip().lower()
        logger.info(f"Processando seção: '{texto_raw}' -> '{texto_limpo}'")

        # Normalizar o nome da seção
        if "previstas" in texto_limpo:
            secao_nome = "Proposta Prevista"
        elif "não analisadas" in texto_limpo:
            secao_nome = "Proposta Não Analisada"
        elif "analisadas" in texto_limpo:
            secao_nome = "Proposta Analisada"
        else:
            secao_nome = texto_limpo.title()

        # Encontrar o botão de toggle próximo ao h2 para identificar o target do collapse
        botao_toggle = h2.find_next_sibling("button", class_="info-reveal__toggle-button")
        if not botao_toggle:
            logger.warning(f"⚠️ Nenhum botão de toggle encontrado para a seção '{secao_nome}'. Pulando...")
            continue

        target_id = botao_toggle.get("data-target")
        if not target_id:
            logger.warning(f"⚠️ Botão de toggle sem data-target para a seção '{secao_nome}'. Pulando...")
            continue

        # Encontrar o div de collapse com esse ID e a ul dentro dele
        div_collapse = soup.find("div", id=target_id.replace("#", ""))
        if not div_collapse:
            logger.warning(f"⚠️ Div de collapse '{target_id}' não encontrado para a seção '{secao_nome}'. Pulando...")
            continue

        ul_lista = div_collapse.find("ul", class_="l-pauta__lista")
        if not ul_lista:
            logger.warning(f"⚠️ Nenhuma lista <ul class='l-pauta__lista'> encontrada no collapse '{target_id}' para a seção '{secao_nome}'. Pulando...")
            continue

        logger.info(f"Associada seção '{secao_nome}' à lista com {len(ul_lista.find_all('li', class_='l-pauta__item'))} itens.")

        # Processar cada item da lista
        for li in ul_lista.find_all("li", class_="l-pauta__item"):
            try:
                titulo_tag = li.find("a", class_="item-pauta__proposicao")

                # Fallback: itens especiais com código em negrito/strong alinhado à esquerda
                # Ex: <strong>PL 123/2026</strong> sem o padrão item-pauta__proposicao
                if not titulo_tag:
                    # Tenta achar código de proposição em tag strong/b
                    for tag in li.find_all(['strong', 'b', 'a']):
                        txt = tag.get_text(strip=True)
                        # Verifica se parece código de proposição (ex: "PL 123/2026", "MPV 1234/2026")
                        if re.match(r'^[A-Z]{2,5}\s+\d+/\d{4}$', txt):
                            # Cria um objeto simulado com as propriedades necessárias
                            class FakeTag:
                                def __init__(self, text, href=''):
                                    self._text = text
                                    self._href = href
                                def get_text(self, **kw): return self._text
                                def get(self, attr, default=''): return self._href if attr == 'href' else default
                                def find_parent(self, tag): return tag.find_parent(tag) if hasattr(tag, 'find_parent') else None
                            titulo_tag = FakeTag(txt, tag.get('href', '') if tag.name == 'a' else '')
                            logger.info(f"📌 Item especial (negrito/esquerda) detectado: {txt}")
                            break

                if not titulo_tag:
                    logger.warning("⚠️ Item sem título de proposição. Pulando...")
                    continue

                codigo = titulo_tag.get_text(strip=True)
                url = titulo_tag.get("href") or ""
                # find_parent só funciona em BeautifulSoup tags reais
                if hasattr(titulo_tag, 'find_parent') and callable(getattr(titulo_tag, 'find_parent', None)):
                    try:
                        ementa_tag = titulo_tag.find_parent("p")
                        ementa_html = ementa_tag.get_text(strip=True) if ementa_tag else ""
                    except Exception:
                        ementa_html = ""
                else:
                    # FakeTag — tenta pegar ementa do li
                    ementa_html = li.get_text(strip=True)

                # Autor e relator
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

                # ID da proposição
                match = re.search(r"idProposicao=(\d+)", url)
                id_prop = match.group(1) if match else None

                # Fallback: buscar idProposicao via API se não encontrado na URL
                if not id_prop:
                    logger.info(f"🔍 Buscando idProposicao para {codigo} via API...")
                    id_prop = buscar_id_proposicao_por_codigo(codigo)

                if not id_prop:
                    logger.warning(f"⚠️ idProposicao não encontrado para {codigo}. Pulando item.")
                    continue

                # Evitar duplicatas
                if id_prop in vistos:
                    logger.warning(f"⚠️ Proposição {codigo} (id {id_prop}) já processada. Pulando...")
                    continue
                vistos.add(id_prop)

                # Obter detalhes complementares via API
                info_extra = obter_detalhes_proposicao(id_prop)
                if info_extra["autores"]:
                    autores = info_extra["autores"]
                if info_extra["relator"]:
                    relator = info_extra["relator"]
                situacao = info_extra["situacao"]
                ementa_final = info_extra["ementa"] or ementa_html
                url_inteiro_teor = info_extra["urlInteiroTeor"]

                itens.append({
                    "id_principal": id_prop,
                    "codigo": codigo,
                    "ementa": ementa_final,
                    "autores": autores,
                    "relator": relator or "Não atribuído",
                    "situacao": situacao or "N/D",
                    "urlInteiroTeor": url_inteiro_teor,
                    "url": url,
                    "secao": secao_nome,
                    "tem_mais_autores": info_extra["tem_mais_autores"]
                })

            except Exception as e:
                logger.warning(f"⚠️ Erro ao processar item da pauta (seção {secao_nome}): {e}")

    logger.info(f"📊 Total de {len(itens)} proposições únicas coletadas.")
    return itens

# -----------------------------------------------------------------------------
# TESTE LOCAL DIRETO
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    # Aceita argumento de linha de comando ou usa evento padrão
    if len(sys.argv) > 1:
        try:
            evento_teste = int(sys.argv[1])
        except ValueError:
            logger.error("❌ ID do evento deve ser um número inteiro.")
            sys.exit(1)
    else:
        evento_teste = 79930  # Padrão para teste

    resultados = obter_itens_pauta(evento_teste)
    for i, r in enumerate(resultados, start=1):
        print(f"\n{i}. {r['codigo']} — {r['autores']}")
        print(f"   {r['ementa'][:120]}...")
        print(f"   Situação: {r['situacao']}")
        print(f"   Seção: {r['secao']}")
        print(f"   Inteiro teor: {r['urlInteiroTeor']}")
    print(f"\nTotal de proposições encontradas: {len(resultados)}")
