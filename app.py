from flask import Flask, jsonify, request, render_template, redirect, url_for, flash, make_response
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from flask_bcrypt import Bcrypt
import sqlite3
import requests
import json
import logging
from datetime import datetime, timedelta, timezone

# Fuso horário de Brasília (UTC-3)
TZ_BRASILIA = timezone(timedelta(hours=-3))

def now_brasilia():
    """Retorna datetime atual no fuso de Brasília."""
    return datetime.now(TZ_BRASILIA)
import os
import re
import html as ihtml
from urllib.parse import urlparse
from scraper_camara import obter_itens_pauta

# Logger — definido antes de tudo
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ── DB ABSTRACTION ────────────────────────────────────────────────────────────
DATABASE_URL = os.environ.get('DATABASE_URL', '')
USE_POSTGRES = bool(DATABASE_URL)
PG_PARAMS = {}

if USE_POSTGRES:
    try:
        import pg8000
        import pg8000.native
        if DATABASE_URL.startswith('postgres://'):
            DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)
        _parsed = urlparse(DATABASE_URL)
        PG_PARAMS = {
            'host':     _parsed.hostname,
            'port':     _parsed.port or 5432,
            'database': _parsed.path.lstrip('/'),
            'user':     _parsed.username,
            'password': _parsed.password,
            'ssl_context': True,
        }
        logger.info(f'✅ PostgreSQL configurado: host={_parsed.hostname} db={_parsed.path.lstrip("/")}')
    except ImportError:
        USE_POSTGRES = False
        logger.warning('⚠️ pg8000 não disponível — usando SQLite')
else:
    logger.warning('⚠️ DATABASE_URL não definida — usando SQLite (dados NÃO persistem entre deploys!)')

def get_conn():
    """Retorna conexão ao banco. No PostgreSQL usa pg8000 (pure Python)."""
    if USE_POSTGRES:
        conn = pg8000.connect(**PG_PARAMS)
        _orig_cursor = conn.cursor
        def _patched_cursor(*a, **kw):
            cur = _orig_cursor(*a, **kw)
            _orig_exec = cur.execute
            _orig_execmany = cur.executemany
            def _exec(sql, params=None):
                sql_orig = sql
                sql = sql.replace('?', '%s')
                if re.search(r'INSERT OR REPLACE INTO notas\b', sql_orig, re.I):
                    sql = re.sub(r'INSERT OR REPLACE INTO notas\b', 'INSERT INTO notas', sql, flags=re.I)
                    sql += (' ON CONFLICT (item_key) DO UPDATE SET '
                            'evento_id=EXCLUDED.evento_id, ordem=EXCLUDED.ordem, '
                            'resumo_materia=EXCLUDED.resumo_materia, orientacao=EXCLUDED.orientacao, '
                            'resumo_parecer=EXCLUDED.resumo_parecer, saved_by=EXCLUDED.saved_by, '
                            'saved_at=EXCLUDED.saved_at')
                elif re.search(r'INSERT OR REPLACE INTO pauta_cache_db\b', sql_orig, re.I):
                    sql = re.sub(r'INSERT OR REPLACE INTO pauta_cache_db\b', 'INSERT INTO pauta_cache_db', sql, flags=re.I)
                    sql += (' ON CONFLICT (evento_id) DO UPDATE SET '
                            'json_pauta=EXCLUDED.json_pauta, last_updated=EXCLUDED.last_updated')
                elif re.search(r'INSERT OR REPLACE INTO orientacoes_grupo\b', sql_orig, re.I):
                    sql = re.sub(r'INSERT OR REPLACE INTO orientacoes_grupo\b', 'INSERT INTO orientacoes_grupo', sql, flags=re.I)
                    sql += (' ON CONFLICT (evento_id, grupo, item_key) DO UPDATE SET '
                            'orientacao=EXCLUDED.orientacao, comentario=EXCLUDED.comentario, '
                            'saved_by=EXCLUDED.saved_by, saved_at=EXCLUDED.saved_at')
                elif re.search(r'INSERT OR IGNORE INTO users\b', sql_orig, re.I):
                    sql = re.sub(r'INSERT OR IGNORE INTO users\b', 'INSERT INTO users', sql, flags=re.I)
                    sql += ' ON CONFLICT (username) DO NOTHING'
                elif re.search(r'INSERT OR REPLACE INTO\b', sql_orig, re.I):
                    sql = re.sub(r'INSERT OR REPLACE INTO\b', 'INSERT INTO', sql, flags=re.I)
                elif re.search(r'INSERT OR IGNORE INTO\b', sql_orig, re.I):
                    sql = re.sub(r'INSERT OR IGNORE INTO\b', 'INSERT INTO', sql, flags=re.I)
                    sql += ' ON CONFLICT DO NOTHING'
                sql = re.sub(r'\bAUTOINCREMENT\b', '', sql, flags=re.I)
                # pg8000 não aceita params=None
                if params is not None:
                    return _orig_exec(sql, list(params) if not isinstance(params, (list, tuple)) else params)
                return _orig_exec(sql)
            def _execmany(sql, params):
                sql = sql.replace('?', '%s')
                return _orig_execmany(sql, params)
            cur.execute = _exec
            cur.executemany = _execmany
            return cur
        conn.cursor = _patched_cursor
        return conn
    return sqlite3.connect(DB)

def ph(n=1):
    """Placeholder: %s para postgres, ? para sqlite."""
    if USE_POSTGRES:
        return '%s'
    return '?'

def phs(n):
    """N placeholders separados por vírgula."""
    p = '%s' if USE_POSTGRES else '?'
    return ', '.join([p] * n)

def upsert_notas(c, item_key, evento_id, ordem, resumo_materia, orientacao, resumo_parecer, saved_by, saved_at):
    if USE_POSTGRES:
        c.execute('''INSERT INTO notas (item_key, evento_id, ordem, resumo_materia, orientacao, resumo_parecer, saved_by, saved_at)
                     VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                     ON CONFLICT (item_key) DO UPDATE SET
                       evento_id=EXCLUDED.evento_id, ordem=EXCLUDED.ordem,
                       resumo_materia=EXCLUDED.resumo_materia, orientacao=EXCLUDED.orientacao,
                       resumo_parecer=EXCLUDED.resumo_parecer, saved_by=EXCLUDED.saved_by,
                       saved_at=EXCLUDED.saved_at''',
                  (item_key, evento_id, ordem, resumo_materia, orientacao, resumo_parecer, saved_by, saved_at))
    else:
        c.execute('INSERT OR REPLACE INTO notas (item_key, evento_id, ordem, resumo_materia, orientacao, resumo_parecer, saved_by, saved_at) VALUES (?,?,?,?,?,?,?,?)',
                  (item_key, evento_id, ordem, resumo_materia, orientacao, resumo_parecer, saved_by, saved_at))

def upsert_pauta_cache(c, evento_id, json_pauta, last_updated):
    if USE_POSTGRES:
        c.execute('''INSERT INTO pauta_cache_db (evento_id, json_pauta, last_updated)
                     VALUES (%s,%s,%s)
                     ON CONFLICT (evento_id) DO UPDATE SET
                       json_pauta=EXCLUDED.json_pauta, last_updated=EXCLUDED.last_updated''',
                  (evento_id, json_pauta, last_updated))
    else:
        c.execute('INSERT OR REPLACE INTO pauta_cache_db (evento_id, json_pauta, last_updated) VALUES (?,?,?)',
                  (evento_id, json_pauta, last_updated))

def upsert_orientacoes(c, evento_id, grupo, item_key, orientacao, comentario, saved_by, saved_at):
    if USE_POSTGRES:
        c.execute('''INSERT INTO orientacoes_grupo (evento_id, grupo, item_key, orientacao, comentario, saved_by, saved_at)
                     VALUES (%s,%s,%s,%s,%s,%s,%s)
                     ON CONFLICT (evento_id, grupo, item_key) DO UPDATE SET
                       orientacao=EXCLUDED.orientacao, comentario=EXCLUDED.comentario,
                       saved_by=EXCLUDED.saved_by, saved_at=EXCLUDED.saved_at''',
                  (evento_id, grupo, item_key, orientacao, comentario, saved_by, saved_at))
    else:
        c.execute('''INSERT OR REPLACE INTO orientacoes_grupo (evento_id, grupo, item_key, orientacao, comentario, saved_by, saved_at)
                     VALUES (?,?,?,?,?,?,?)''',
                  (evento_id, grupo, item_key, orientacao, comentario, saved_by, saved_at))

def integrity_error():
    if USE_POSTGRES:
        return psycopg2.errors.UniqueViolation
    return sqlite3.IntegrityError
# ─────────────────────────────────────────────────────────────────────────────


logging.getLogger('werkzeug').setLevel(logging.WARNING)

app = Flask(__name__)
app.config['SECRET_KEY'] = 'plenario-chave-secreta-2025'
bcrypt = Bcrypt(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

pauta_cache = {}
CACHE_DURATION = timedelta(minutes=5)
DB = 'plenario.db'

# --------------------------------------------------------------------------
# INIT BANCO AUTOMÁTICO (Railway)
# --------------------------------------------------------------------------
with app.app_context():
    try:
        conn = get_conn()
        c = conn.cursor()
        _p = '%s' if USE_POSTGRES else '?'
        _AI = 'SERIAL' if USE_POSTGRES else 'INTEGER'
        _TXT = 'TEXT' if USE_POSTGRES else 'TEXT'

        c.execute(f'''CREATE TABLE IF NOT EXISTS users (
            id {_AI} PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            role TEXT NOT NULL,
            categoria TEXT NOT NULL DEFAULT 'geral',
            foto TEXT,
            nome_display TEXT,
            responsavel_pauta INTEGER DEFAULT 0)''')

        c.execute('''CREATE TABLE IF NOT EXISTS notas (
            item_key TEXT PRIMARY KEY,
            evento_id INTEGER,
            ordem TEXT,
            resumo_materia TEXT,
            orientacao TEXT,
            resumo_parecer TEXT,
            saved_by TEXT,
            saved_at TEXT,
            responsavel_username TEXT)''')

        c.execute('''CREATE TABLE IF NOT EXISTS pauta_cache_db (
            evento_id INTEGER PRIMARY KEY,
            json_pauta TEXT,
            last_updated TEXT,
            last_saved_by TEXT)''')

        c.execute('''CREATE TABLE IF NOT EXISTS orientacoes_grupo (
            id SERIAL PRIMARY KEY,
            evento_id INTEGER,
            id_principal TEXT,
            grupo TEXT,
            orientacao TEXT,
            comentario TEXT,
            saved_by TEXT,
            saved_at TEXT,
            UNIQUE(evento_id, id_principal, grupo))''' if USE_POSTGRES else
            '''CREATE TABLE IF NOT EXISTS orientacoes_grupo (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            evento_id INTEGER,
            id_principal TEXT,
            grupo TEXT,
            orientacao TEXT,
            comentario TEXT,
            saved_by TEXT,
            saved_at TEXT,
            UNIQUE(evento_id, id_principal, grupo))''')

        # Migrações seguras
        migrações = [
            'ALTER TABLE users ADD COLUMN IF NOT EXISTS foto TEXT' if USE_POSTGRES else 'ALTER TABLE users ADD COLUMN foto TEXT',
            'ALTER TABLE users ADD COLUMN IF NOT EXISTS nome_display TEXT' if USE_POSTGRES else 'ALTER TABLE users ADD COLUMN nome_display TEXT',
            'ALTER TABLE users ADD COLUMN IF NOT EXISTS responsavel_pauta INTEGER DEFAULT 0' if USE_POSTGRES else 'ALTER TABLE users ADD COLUMN responsavel_pauta INTEGER DEFAULT 0',
            'ALTER TABLE notas ADD COLUMN IF NOT EXISTS responsavel_username TEXT' if USE_POSTGRES else 'ALTER TABLE notas ADD COLUMN responsavel_username TEXT',
            # Migração: adiciona id_principal na tabela orientacoes_grupo (substitui item_key)
            'ALTER TABLE orientacoes_grupo ADD COLUMN IF NOT EXISTS id_principal TEXT' if USE_POSTGRES else 'ALTER TABLE orientacoes_grupo ADD COLUMN id_principal TEXT',
        ]
        for sql in migrações:
            try: c.execute(sql)
            except Exception: pass

        from flask_bcrypt import Bcrypt as _Bc
        _bcrypt = _Bc()
        _pw123 = _bcrypt.generate_password_hash('123').decode('utf-8')

        c.execute('''CREATE TABLE IF NOT EXISTS usuarios_deletados (
            username TEXT PRIMARY KEY)''')

        # Carrega lista de usuários já deletados para não recriar
        try:
            c.execute('SELECT username FROM usuarios_deletados')
            _deletados = {r[0] for r in c.fetchall()}
        except Exception:
            _deletados = set()

        _usuarios = [
            ('admin',             'Admin',            'admin',    'Admin'),
            ('assessor_plenario', 'Assessor Plenário','minoria',  'Assessor Plenário'),
            ('assessor',          'Assessor',         'geral',    'Assessor'),
            ('PL',                'Orientação',       'restrito', 'Orientação'),
            ('NOVO',              'Orientação',       'restrito', 'Orientação'),
            ('marcelo.oliveira',  'Assessor Plenário','minoria',  'Marcelo Oliveira'),
        ]
        for _un, _cat, _role_cat, _nome in _usuarios:
            if _un in _deletados:
                continue  # não recria usuários que foram deletados
            _role_val = 'Admin' if _un == 'admin' else 'Assessor'
            try:
                if USE_POSTGRES:
                    c.execute('INSERT INTO users (username, password, role, categoria, nome_display) VALUES (%s,%s,%s,%s,%s) ON CONFLICT (username) DO NOTHING',
                              (_un, _pw123, _role_val, _cat, _nome))
                else:
                    c.execute('INSERT OR IGNORE INTO users (username, password, role, categoria, nome_display) VALUES (?,?,?,?,?)',
                              (_un, _pw123, _role_val, _cat, _nome))
            except Exception:
                pass

        # Garante que o admin tenha role='Admin' caso já exista com role errado
        try:
            c.execute("UPDATE users SET role='Admin' WHERE username='admin' AND role != 'Admin'")
        except Exception:
            pass

        _cats = {
            # Oposição
            'vinicius.scheffel': 'oposicao', 'lianna.barros': 'oposicao',
            'marcelo.uvara': 'oposicao', 'elyesley.silva': 'oposicao',
            'pedro.chaves': 'oposicao',
            # Minoria
            'ulisses.branco': 'minoria', 'eduardo.borba': 'minoria',
            'luisa.marreco': 'minoria', 'luiz.garibaldi': 'minoria',
            'assessor_plenario': 'minoria', 'marcelo.oliveira': 'minoria',
        }
        # Atualiza categorias — sempre roda para corrigir registros existentes
        for _un, _cat in _cats.items():
            try:
                c.execute(f'UPDATE users SET categoria={_p} WHERE username={_p}', (_cat, _un))
            except Exception:
                pass

        conn.commit()
        conn.close()
        logger.info(f'✅ Banco inicializado ({"PostgreSQL" if USE_POSTGRES else "SQLite"}).')
    except Exception as _e:
        logger.error(f'❌ Erro banco: {_e}')

# --------------------------------------------------------------------------
# HELPERS DB
# --------------------------------------------------------------------------
def load_notas():
    conn = get_conn()
    c = conn.cursor()
    try:
        c.execute('SELECT item_key, resumo_materia, orientacao, resumo_parecer, saved_by, saved_at, responsavel_username FROM notas')
        notas = {row[0]: {'resumo_materia': row[1], 'orientacao': row[2], 'resumo_parecer': row[3],
                          'saved_by': row[4] or '', 'saved_at': row[5] or '',
                          'responsavel_username': row[6] or ''}
                 for row in c.fetchall()}
    except Exception:
        notas = {}
    finally:
        conn.close()
    return notas

# --------------------------------------------------------------------------
# LOGIN
# --------------------------------------------------------------------------
class User(UserMixin):
    def __init__(self, id, username, role, categoria='geral'):
        self.id = id; self.username = username; self.role = role; self.categoria = categoria

    def display_name(self):
        """Nome de exibição com categoria."""
        if self.categoria in ('oposicao', 'minoria'):
            return f"{self.username} - {self.categoria}"
        return self.username

@login_manager.user_loader
def load_user(user_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute('SELECT id, username, role, categoria FROM users WHERE id = ?', (user_id,))
    u = c.fetchone()
    conn.close()
    return User(u[0], u[1], u[2], u[3] if len(u) > 3 else 'geral') if u else None

# --------------------------------------------------------------------------
# EVENTOS & PAUTA
# --------------------------------------------------------------------------
def fetch_eventos_por_data(data):
    url = f"https://dadosabertos.camara.leg.br/api/v2/eventos?idOrgao=180&dataInicio={data}&dataFim={data}&itens=50"
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        dados = r.json().get('dados', [])
        return [
            {
                'id': str(e.get('id')),
                'descricao': e.get('descricao', 'Sem descrição'),
                'tipo': e.get('descricaoTipo', ''),
                'dataHoraInicio': e.get('dataHoraInicio', 'N/D'),
                'local': e.get('localCamara', {}).get('nome', 'N/D') if isinstance(e.get('localCamara'), dict) else e.get('localCamara', 'N/D'),
                'situacao': e.get('situacao', 'N/D')
            }
            for e in dados if e.get('descricaoTipo') == "Sessão Deliberativa"
        ]
    except Exception as e:
        logger.error(f"Erro ao buscar eventos: {e}")
        return []

def extrair_ref_pl(projeto, ementa):
    """
    Se for REQ/RQS/RQU/REC, extrai a referência ao PL/PEC/PLP/MPV da ementa.
    Retorna ex: 'REQ 2569/2026 ao PL 1811/2026'
    É idempotente: se já contém ' ao ', não processa novamente.
    """
    # Pega só a parte base do projeto (antes de " ao " se já processado)
    projeto_base = projeto.split(' ao ')[0].strip()

    siglas_req = ('REQ', 'RQS', 'RQU', 'REC', 'REQ.', 'RQS.')
    if not any(projeto_base.upper().startswith(s) for s in siglas_req):
        return projeto

    ementa_str = str(ementa or '')

    # Padrões do mais específico para o mais genérico
    padroes = [
        # "PL nº 1811/2026" ou "PL 1811/2026"
        r'\b(PL|PEC|PLP|PLC|MPV|PDL|PLV|PDS|PRS)\s+n[º°.]?\s*(\d+)[,\s/]+(?:de\s+)?(\d{4})\b',
        # "PL 1811, de 2026"
        r'\b(PL|PEC|PLP|PLC|MPV|PDL|PLV|PDS|PRS)\s+(\d+),?\s*de\s+(\d{4})\b',
        # "Projeto de Lei nº 1811/2026"
        r'Projeto de Lei\s+(?:Complementar\s+)?n[º°.]?\s*(\d+)[,\s/]+(?:de\s+)?(\d{4})',
        # "PL1811/2026" sem espaço
        r'\b(PL|PEC|PLP|PLC|MPV)\s*(\d{3,5})[/\-](\d{4})\b',
    ]

    for padrao in padroes:
        m = re.search(padrao, ementa_str, re.IGNORECASE)
        if m:
            grupos = m.groups()
            if len(grupos) == 3:
                sigla, num, ano = grupos
                return f"{projeto_base} ao {sigla.upper()} {num}/{ano}"
            elif len(grupos) == 2:
                num, ano = grupos
                return f"{projeto_base} ao PL {num}/{ano}"

    return projeto_base

def buscar_ordem_oficial(evento_id, data_evento=''):
    """
    Extrai a ordem oficial dos itens diretamente do PDF de pauta da sessão.
    
    Estratégia:
    1. Acessa a página do evento para encontrar o link do PDF de pauta
    2. Baixa o PDF e extrai o texto
    3. Parseia os números de ordem (padrão: "N. Proposição...")
    4. Retorna dict {codigo_normalizado: posicao}
    """
    try:
        from bs4 import BeautifulSoup
        import pdfplumber

        # Passo 1: Busca o PDF de pauta na página do evento
        url_evento = f"https://www.camara.leg.br/evento-legislativo/{evento_id}"
        r = requests.get(url_evento, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36'
        }, timeout=12)
        if not r.ok:
            logger.warning(f"Evento {evento_id} inacessível: {r.status_code}")
            return {}

        soup = BeautifulSoup(r.text, 'html.parser')

        # Busca PDF com texto "Pauta" — verifica se é da mesma data do evento
        pdf_url = None
        from bs4 import BeautifulSoup

        # Busca data do evento para validar
        data_evento = ''
        try:
            data_el = soup.find(string=re.compile(r'\d{2}/\d{2}/\d{4}'))
            if data_el:
                m_data = re.search(r'(\d{2})/(\d{2})/(\d{4})', str(data_el))
                if m_data:
                    data_evento = f"{m_data.group(3)}-{m_data.group(2)}-{m_data.group(1)}"
        except Exception:
            pass

        # Coleta todos os links "Pauta" para testar
        candidatos_pauta = []
        for a in soup.find_all('a', href=re.compile(r'codteor=\d+', re.I)):
            if a.get_text(strip=True).lower() == 'pauta':
                href = a['href']
                url_cand = (camara_url(href))
                url_cand += ('&' if '?' in url_cand else '?') + 'tipo=PDF'
                candidatos_pauta.append(url_cand)

        # Testa cada candidato — usa o que tiver a data correta do evento
        for url_cand in candidatos_pauta:
            rp_test = requests.get(url_cand, headers={
                'User-Agent': 'Mozilla/5.0 Chrome/124.0.0.0 Safari/537.36'
            }, timeout=20)
            if not rp_test.ok:
                continue
            # Extrai primeiras linhas do PDF para verificar data
            try:
                with pdfplumber.open(BytesIO(rp_test.content)) as pdf_test:
                    texto_inicio = pdf_test.pages[0].extract_text() or ''
                # Verifica se data do evento está no PDF
                data_ok = True
                if data_evento:
                    ano_ev = data_evento[:4]
                    mes_ev = data_evento[5:7].lstrip('0')
                    dia_ev = data_evento[8:10].lstrip('0')
                    # Verifica se dia E ano estão no texto do PDF
                    data_ok = ano_ev in texto_inicio and (
                        f"{dia_ev} de" in texto_inicio or
                        f"Em {dia_ev} de" in texto_inicio or
                        f"Em {dia_ev.zfill(2)} de" in texto_inicio
                    )
                if data_ok:
                    pdf_url = url_cand
                    rp = rp_test
                    logger.info(f"PDF de pauta válido: {url_cand}")
                    break
            except Exception:
                pdf_url = url_cand
                rp = rp_test
                break

        if not pdf_url:
            logger.warning(f"Nenhum PDF de pauta válido para evento {evento_id}")
            return {}

        # Passo 3: Extrai ordem usando posição X das palavras (números centralizados)
        from io import BytesIO
        ordem = {}

        with pdfplumber.open(BytesIO(rp.content)) as pdf:
            page_width = float(pdf.pages[0].width) if pdf.pages else 595.0

            for page in pdf.pages:
                words = page.extract_words(x_tolerance=3, y_tolerance=3)

                # Agrupa palavras por linha (y próximo)
                linhas = {}
                for w in words:
                    y = round(float(w['top']))
                    linhas.setdefault(y, []).append(w)

                ys = sorted(linhas.keys())

                for i, y in enumerate(ys):
                    palavras = linhas[y]

                    # Encontra na linha uma palavra que seja número 1-2 dígitos E centralizada
                    # Ignora qualquer outro texto na mesma linha (invisível ou não)
                    num_encontrado = None
                    for w in palavras:
                        txt = w['text'].strip()
                        if not re.match(r'^\d{1,2}$', txt):
                            continue
                        centro_w = (float(w['x0']) + float(w['x1'])) / 2
                        if abs(centro_w - page_width / 2) <= page_width * 0.05:
                            num_encontrado = (int(txt), float(w['x0']), float(w['x1']))
                            break

                    if not num_encontrado:
                        continue
                    num, x0, x1 = num_encontrado
                    if num < 1 or num > 30:
                        continue
                    centro = (x0 + x1) / 2
                    if abs(centro - page_width / 2) > page_width * 0.20:
                        continue

                    # Pega texto das próximas 10 linhas para extrair o código
                    prox_ys = ys[i+1:i+11]
                    bloco = ' '.join(
                        ' '.join(w['text'] for w in linhas[ny])
                        for ny in prox_ys if ny in linhas
                    )

                    codigo = _extrair_codigo_do_bloco(bloco)
                    if num == 15 or (codigo and '3066' in codigo):
                        logger.info(f"  DEBUG item {num}: bloco='{bloco[:150]}' → codigo={codigo}")
                    if codigo:
                        chave = _normalizar_codigo(codigo)
                        # Cada número de posição só pode ter UM item
                        # Se já existe outro item nessa posição, o mais recente vence
                        # (páginas posteriores têm o número correto)
                        posicoes_usadas = {v: k for k, v in ordem.items()}
                        if num in posicoes_usadas:
                            # Remove entrada anterior para esta posição
                            del ordem[posicoes_usadas[num]]
                            logger.info(f"  Posição {num} sobrescrita: {posicoes_usadas[num]} → {chave}")
                        ordem[chave] = num
                        logger.info(f"  Item {num} (centralizado): {codigo} → {chave}")

        # Fallback para REQ: formato "1. Requerimento nº X.XXX, de AAAA" (não centralizado)
        if pdf_bytes := rp.content if 'rp' in dir() else None:
            pass  # já processado acima

        # Extrai REQ do texto bruto (formato com ponto, não centralizado)
        with pdfplumber.open(BytesIO(rp.content)) as pdf:
            texto_total = '\n'.join(p.extract_text() or '' for p in pdf.pages)
        for m in re.finditer(
            r'^(\d+)\.\s+Requerimento\s+n[º°oa.]?\s*([\d.]+),\s*de\s+(\d{4})',
            texto_total, re.MULTILINE
        ):
            num   = int(m.group(1))
            num_p = m.group(2).replace('.', '')
            ano   = m.group(3)
            chave = _normalizar_codigo(f"REQ {num_p}/{ano}")
            if chave not in ordem and num <= 30:
                ordem[chave] = num
                logger.info(f"  Item {num} (REQ texto): REQ {num_p}/{ano} → {chave}")

        logger.info(f"Ordem extraída do PDF evento {evento_id}: {len(ordem)} itens — {dict(list(ordem.items())[:10])}")
        return ordem

    except ImportError:
        logger.warning("pdfplumber não disponível — usando fallback HTML")
        return _buscar_ordem_html(evento_id)
    except Exception as e:
        logger.warning(f"Erro ao extrair ordem do PDF {evento_id}: {e}")
        return _buscar_ordem_html(evento_id)

def camara_url(href):
    """Monta URL completa da Câmara garantindo o caminho correto."""
    if href.startswith('http'):
        url = href
    elif href.startswith('/'):
        url = f"https://www.camara.leg.br{href}"
    else:
        url = f"https://www.camara.leg.br/{href}"
    # Garante /proposicoesWeb/ quando há prop_mostrarintegra sem ele
    if 'prop_mostrarintegra' in url and '/proposicoesWeb/' not in url:
        url = url.replace('camara.leg.br/prop_mostrarintegra', 'camara.leg.br/proposicoesWeb/prop_mostrarintegra')
    return url
    """Fallback: tenta extrair ordem da página HTML de ordem do dia."""
    url = f"https://www.camara.leg.br/internet/ordemdodia/ordemDetalheReuniaoPle.asp?codReuniao={evento_id}"
    try:
        from bs4 import BeautifulSoup
        r = requests.get(url, headers={
            'User-Agent': 'Mozilla/5.0 Chrome/124.0.0.0 Safari/537.36'
        }, timeout=12)
        if not r.ok:
            return {}
        soup = BeautifulSoup(r.text, 'html.parser')
        texto = soup.get_text(separator='\n')
        ordem = {}
        padrao = re.compile(r'^(\d+)\s*[-–]\s*([A-Z]+\s+\d+/\d{4})', re.MULTILINE)
        for m in padrao.finditer(texto):
            num    = int(m.group(1))
            chave  = _normalizar_codigo(m.group(2))
            if chave not in ordem:
                ordem[chave] = num
        logger.info(f"Ordem via HTML: {len(ordem)} itens")
        return ordem
    except Exception as e:
        logger.warning(f"Fallback HTML também falhou: {e}")
        return {}

def _extrair_codigo_do_bloco(bloco):
    """
    Extrai código de QUALQUER proposição do bloco de texto após o número centralizado.
    Usa regex universal que captura sigla + número + sufixo + ano.
    Remove sufixos (-A, -B, -C, -F etc.) que o PDF usa mas a API não usa.
    """
    b = bloco.replace('\xa0', ' ').strip()

    # Mapa de texto completo → sigla curta
    tipos = [
        (r'PROJETO\s+DE\s+LEI\s+COMPLEMENTAR',           'PLP'),
        (r'PROJETO\s+DE\s+LEI',                           'PL'),
        (r'PROPOSTA\s+DE\s+EMENDA\s+[AÀ]\s+CONSTITUI[CÇ][AÃ]O', 'PEC'),
        (r'MEDIDA\s+PROVIS[OÓ]RIA',                       'MPV'),
        (r'PROJETO\s+DE\s+DECRETO\s+LEGISLATIVO',         'PDL'),
        (r'PROJETO\s+DE\s+RESOLU[CÇ][AÃ]O\s+DO\s+SENADO','PRS'),
        (r'PROJETO\s+DE\s+RESOLU[CÇ][AÃ]O',              'PRC'),
        (r'PROPOSTA\s+DE\s+FISCALIZA[CÇ][AÃ]O\s+E\s+CONTROLE', 'PFC'),
        (r'PROJETO\s+DE\s+LEI\s+DE\s+CONVERS[AÃ]O',      'PLV'),
        (r'Requerimento',                                  'REQ'),
    ]

    # Número: dígitos com pontos opcionais, sufixo -A/-B/-F etc. opcional
    num_pattern = r'([\d.]+(?:-[A-Z])?)'
    ano_pattern = r'(\d{4})'

    for tipo_regex, sigla in tipos:
        # Tenta: TIPO Nº NUM, DE ANO
        padrao = (rf'{tipo_regex}\s+N[º°oa.]?\s*{num_pattern}'
                  rf'(?:\s*[-–]\s*[A-Z])?,?\s*[Dd][Ee]\s+{ano_pattern}')
        m = re.search(padrao, b, re.IGNORECASE)
        if m:
            num = re.sub(r'[-–][A-Z]$', '', m.group(1).replace('.', '').replace('\xa0', ''))
            ano = m.group(2)
            return f"{sigla} {num}/{ano}"

    return None

def _normalizar_codigo(codigo):
    """Normaliza código para comparação.
    Remove: espaços, pontos, texto entre parênteses, sufixos -A/-B/-C no número.
    Ex: 'PDL 330-B/2022' → 'PDL330/2022'
        'PL 3.278-A/2021' → 'PL3278/2021'
        'PL 2199/2022 (Nº Anterior: PL 7750/2017)' → 'PL2199/2022'
    """
    c = codigo.upper().strip()
    c = re.sub(r'\(.*?\)', '', c)        # remove (Nº Anterior: ...) etc
    c = re.sub(r'\s+', '', c)            # remove espaços
    c = re.sub(r'\.', '', c)             # remove pontos
    c = re.sub(r'-[A-Z](?=/)', '', c)    # remove -A, -B, -C antes da /
    return c.strip()

def reordenar_por_ordem_oficial(itens, ordem_oficial):
    if not ordem_oficial or not itens:
        return itens

    def norm(item):
        proj = item.get('projeto_original') or item.get('projeto', '')
        return _normalizar_codigo(proj.split(' ao ')[0].strip())

    encontrados     = [(i, it, ordem_oficial[norm(it)]) for i, it in enumerate(itens) if norm(it) in ordem_oficial]
    nao_encontrados = [(i, it) for i, it in enumerate(itens) if norm(it) not in ordem_oficial]

    cobertura = len(encontrados) / len(itens)
    logger.info(f"Cobertura PDF: {len(encontrados)}/{len(itens)} ({cobertura:.0%})")

    if not encontrados or cobertura < 0.30:
        logger.warning("Cobertura insuficiente — mantendo ordem da API.")
        return itens

    # Ordena pelos encontrados pela posição do PDF
    encontrados.sort(key=lambda x: x[2])

    # Renumera sequencialmente para eliminar gaps (pauta reserva, etc.)
    encontrados = [(api_i, it, seq+1) for seq, (api_i, it, _) in enumerate(encontrados)]

    # Insere não encontrados pela posição relativa da API
    resultado = list(encontrados)
    for api_idx, item in nao_encontrados:
        pos = 0
        for j, (enc_idx, _, _) in enumerate(resultado):
            if enc_idx < api_idx:
                pos = j + 1
        resultado.insert(pos, (api_idx, item, -1))
        logger.info(f"Não encontrado: {item.get('projeto_original','')} → pos {pos+1}")

    itens_ord = [it for (_, it, _) in resultado]
    for i, it in enumerate(itens_ord, start=1):
        it['ordem'] = str(i)

    logger.info(f"Ordem final: {[(it.get('projeto_original','')[:12], it['ordem']) for it in itens_ord]}")
    return itens_ord

def fetch_pauta(evento_id, force_reload=False):
    now = now_brasilia()
    cache_key = str(evento_id)
    notas = load_notas()

    if not force_reload and cache_key in pauta_cache:
        cached = pauta_cache[cache_key]
        if now - cached['timestamp'] < CACHE_DURATION:
            return cached['itens'], False

    conn = get_conn()
    c = conn.cursor()

    if not force_reload:
        try:
            c.execute("SELECT json_pauta, last_updated FROM pauta_cache_db WHERE evento_id = ?", (evento_id,))
            row = c.fetchone()
            if row:
                itens = json.loads(row[0])
                # Reaplica notas e corrige título de REQ sempre
                for item in itens:
                    # projeto_original = código limpo (sem " ao PL...")
                    orig = item.get('projeto_original') or item.get('projeto', '')
                    # Garante que projeto_original seja o código base
                    item['projeto_original'] = orig.split(' ao ')[0].strip()
                    item['projeto'] = extrair_ref_pl(item['projeto_original'], item.get('ementa', ''))
                    key = f"PROP_{item['id_principal']}"
                    if key in notas:
                        item['resumo_materia'] = notas[key].get('resumo_materia', item.get('resumo_materia', ''))
                        item['orientacao']     = notas[key].get('orientacao', item.get('orientacao', ''))
                        item['resumo_parecer'] = notas[key].get('resumo_parecer', item.get('resumo_parecer', ''))
                        item['saved_by']       = notas[key].get('saved_by', item.get('saved_by', ''))
                        item['saved_at']       = notas[key].get('saved_at', item.get('saved_at', ''))
                pauta_cache[cache_key] = {'timestamp': now, 'itens': itens}
                conn.close()
                return itens, True
        except Exception:
            pass

    try:
        # Busca data do evento
        data_ev = ''
        try:
            r_ev = requests.get(f"https://dadosabertos.camara.leg.br/api/v2/eventos/{evento_id}", timeout=8)
            if r_ev.ok:
                data_ev = r_ev.json().get('dados', {}).get('dataHoraInicio', '')[:10]
        except Exception:
            pass

        # PASSO 1: PDF define a ordem e quantidade — fonte de verdade
        ordem_oficial = buscar_ordem_oficial(evento_id, data_ev)
        logger.info(f"PDF: {len(ordem_oficial)} itens extraídos")

        # PASSO 2: API fornece os dados completos
        itens_raw = obter_itens_pauta(evento_id)
        if not itens_raw and not ordem_oficial:
            raise ValueError("Sem dados do scraper e sem PDF")

        # Monta índice da API por código normalizado
        api_por_codigo = {}
        for item in (itens_raw or []):
            cod = _normalizar_codigo(item['codigo'])
            api_por_codigo[cod] = item
        logger.info(f"API: {len(api_por_codigo)} itens")

        now_str = now_brasilia().strftime('%Y-%m-%d %H:%M:%S')
        itens = []
        vistos_ids = set()

        if ordem_oficial:
            # PASSO 3a: PDF como base — cria item para cada código do PDF em ordem
            codigos_pdf = sorted(ordem_oficial.keys(), key=lambda k: ordem_oficial[k])

            for cod_pdf in codigos_pdf:
                item_api = api_por_codigo.get(cod_pdf)

                if item_api:
                    id_p = item_api.get('id_principal')
                    if id_p and id_p in vistos_ids:
                        continue
                    if id_p:
                        vistos_ids.add(id_p)
                    key = f"PROP_{id_p}" if id_p else ''
                    itens.append({
                        'ordem':            str(len(itens) + 1),
                        'id_principal':     id_p or '',
                        'projeto':          extrair_ref_pl(item_api['codigo'], item_api['ementa']),
                        'projeto_original': item_api['codigo'],
                        'ementa':           item_api['ementa'],
                        'autor':            item_api.get('autores', 'N/D'),
                        'relator':          item_api.get('relator', 'Não atribuído'),
                        'situacao':         item_api.get('situacao', 'N/D'),
                        'secao':            item_api.get('secao', 'N/D'),
                        'resumo_materia':   notas.get(key, {}).get('resumo_materia', ''),
                        'orientacao':       notas.get(key, {}).get('orientacao', ''),
                        'resumo_parecer':   notas.get(key, {}).get('resumo_parecer', ''),
                        'saved_by':         notas.get(key, {}).get('saved_by', ''),
                        'saved_at':         notas.get(key, {}).get('saved_at', ''),
                        'destaques_emendas': []
                    })
                else:
                    # Item do PDF não encontrado na API — adiciona com dados mínimos
                    logger.warning(f"PDF item '{cod_pdf}' não na API — adicionando com dados mínimos")
                    # Tenta buscar dados via API diretamente pelo código
                    ementa_min = ''
                    autor_min  = 'N/D'
                    relator_min = 'Não atribuído'
                    id_p_min = ''
                    try:
                        partes = re.match(r'([A-Z]+)(\d+)/(\d{4})', cod_pdf)
                        if partes:
                            sigla, num, ano = partes.groups()
                            r_min = requests.get(
                                f"https://dadosabertos.camara.leg.br/api/v2/proposicoes?siglaTipo={sigla}&numero={num}&ano={ano}&itens=1",
                                headers={'Accept': 'application/json'}, timeout=8
                            )
                            if r_min.ok:
                                dados_min = r_min.json().get('dados', [])
                                if dados_min:
                                    ementa_min = dados_min[0].get('ementa', '')
                                    id_p_min   = str(dados_min[0].get('id', ''))
                    except Exception:
                        pass

                    codigo_display = cod_pdf.replace('/', ' ').replace('MPV', 'MPV ').replace('PL', 'PL ')
                    itens.append({
                        'ordem':            str(len(itens) + 1),
                        'id_principal':     id_p_min,
                        'projeto':          codigo_display.strip(),
                        'projeto_original': codigo_display.strip(),
                        'ementa':           ementa_min or f'Item {cod_pdf} — dados não disponíveis no momento',
                        'autor':            autor_min,
                        'relator':          relator_min,
                        'situacao':         'Em votação',
                        'secao':            'N/D',
                        'resumo_materia':   '',
                        'orientacao':       '',
                        'resumo_parecer':   '',
                        'saved_by':         '',
                        'saved_at':         '',
                        'destaques_emendas': []
                    })

            # Adiciona itens da API não encontrados no PDF ao final
            for item in (itens_raw or []):
                id_p = item.get('id_principal')
                if not id_p or id_p in vistos_ids:
                    continue
                cod = _normalizar_codigo(item['codigo'])
                if cod in ordem_oficial:
                    continue  # já foi processado
                vistos_ids.add(id_p)
                key = f"PROP_{id_p}"
                itens.append({
                    'ordem':            str(len(itens) + 1),
                    'id_principal':     id_p,
                    'projeto':          extrair_ref_pl(item['codigo'], item['ementa']),
                    'projeto_original': item['codigo'],
                    'ementa':           item['ementa'],
                    'autor':            item.get('autores', 'N/D'),
                    'relator':          item.get('relator', 'Não atribuído'),
                    'situacao':         item.get('situacao', 'N/D'),
                    'secao':            item.get('secao', 'N/D'),
                    'resumo_materia':   notas.get(key, {}).get('resumo_materia', ''),
                    'orientacao':       notas.get(key, {}).get('orientacao', ''),
                    'resumo_parecer':   notas.get(key, {}).get('resumo_parecer', ''),
                    'saved_by':         notas.get(key, {}).get('saved_by', ''),
                    'saved_at':         notas.get(key, {}).get('saved_at', ''),
                    'destaques_emendas': []
                })
                logger.info(f"Item da API não no PDF adicionado ao final: {item['codigo']}")

        else:
            # PASSO 3b: sem PDF, usa ordem da API
            logger.info("⚠️ PDF não disponível — usando ordem da API.")
            for item in (itens_raw or []):
                id_p = item.get('id_principal')
                if not id_p or id_p in vistos_ids:
                    continue
                vistos_ids.add(id_p)
                key = f"PROP_{id_p}"
                itens.append({
                    'ordem':            str(len(itens) + 1),
                    'id_principal':     id_p,
                    'projeto':          extrair_ref_pl(item['codigo'], item['ementa']),
                    'projeto_original': item['codigo'],
                    'ementa':           item['ementa'],
                    'autor':            item.get('autores', 'N/D'),
                    'relator':          item.get('relator', 'Não atribuído'),
                    'situacao':         item.get('situacao', 'N/D'),
                    'secao':            item.get('secao', 'N/D'),
                    'resumo_materia':   notas.get(key, {}).get('resumo_materia', ''),
                    'orientacao':       notas.get(key, {}).get('orientacao', ''),
                    'resumo_parecer':   notas.get(key, {}).get('resumo_parecer', ''),
                    'saved_by':         notas.get(key, {}).get('saved_by', ''),
                    'saved_at':         notas.get(key, {}).get('saved_at', ''),
                    'destaques_emendas': []
                })

        logger.info(f"✅ Total final: {len(itens)} itens | PDF={len(ordem_oficial)} | API={len(api_por_codigo)}")

        c.execute('INSERT OR REPLACE INTO pauta_cache_db (evento_id, json_pauta, last_updated) VALUES (?, ?, ?)',
                  (evento_id, json.dumps(itens), now_str))
        conn.commit()
        pauta_cache[cache_key] = {'timestamp': now, 'itens': itens}
        conn.close()
        return itens, False

    except Exception as e:
        logger.warning(f"⚠️ Scraping falhou: {e}. Usando cache...")
        c.execute("SELECT json_pauta FROM pauta_cache_db WHERE evento_id = ?", (evento_id,))
        cached = c.fetchone()
        conn.close()
        if cached:
            try:
                itens = json.loads(cached[0])
                pauta_cache[cache_key] = {'timestamp': now, 'itens': itens}
                return itens, True
            except Exception:
                pass
        return [], True

# --------------------------------------------------------------------------
# FILTRO DE DATA
# --------------------------------------------------------------------------
@app.template_filter('datetimeformat')
def datetimeformat(value, format='%d/%m/%Y %H:%M'):
    try:
        dt = datetime.fromisoformat(str(value))
        # Se não tem timezone, assume que já está em Brasília
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ_BRASILIA)
        return dt.astimezone(TZ_BRASILIA).strftime(format)
    except Exception:
        return value

# --------------------------------------------------------------------------
# ROTAS
# --------------------------------------------------------------------------
@app.route('/')
@login_required
def home():
    return redirect(url_for('selecionar_data'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('selecionar_data'))
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        conn = get_conn()
        c = conn.cursor()
        c.execute('SELECT id, username, password, role, categoria FROM users WHERE username = ?', (username,))
        u = c.fetchone()
        conn.close()
        if u and bcrypt.check_password_hash(u[2], password):
            login_user(User(u[0], u[1], u[3], u[4] if len(u) > 4 else 'geral'))
            return redirect(url_for('selecionar_data'))
        flash('Usuário ou senha inválidos.', 'error')

    # Busca lista de usuários para o dropdown
    conn = get_conn()
    c = conn.cursor()
    try:
        c.execute('SELECT username, categoria FROM users ORDER BY username')
        usuarios = [{'username': r[0], 'categoria': r[1]} for r in c.fetchall()]
    except Exception:
        usuarios = []
    finally:
        conn.close()
    return render_template('login.html', usuarios=usuarios)

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

@app.route('/selecionar-data', methods=['GET', 'POST'])
@login_required
def selecionar_data():
    data = request.form.get('data', now_brasilia().strftime('%Y-%m-%d'))
    eventos = fetch_eventos_por_data(data)
    return render_template('selecionar_data.html', data_selecionada=data, eventos=eventos, user_role=current_user.role)

@app.route('/pauta/<int:evento_id>/view')
@login_required
def view_pauta(evento_id):
    force_reload = request.args.get('force_reload', 'false').lower() == 'true'
    itens, from_cache = fetch_pauta(evento_id, force_reload)
    conn = get_conn()
    c = conn.cursor()
    last_updated = None
    last_saved_user = None
    try:
        c.execute("SELECT last_updated, last_saved_by FROM pauta_cache_db WHERE evento_id = ?", (evento_id,))
        row = c.fetchone()
        if row:
            last_updated = row[0]
            last_saved_user = row[1]
    except Exception:
        pass
    finally:
        conn.close()

    try:
        r = requests.get(f"https://dadosabertos.camara.leg.br/api/v2/eventos/{evento_id}", timeout=10)
        d = r.json().get("dados", {})
        evento = {
            'id': evento_id,
            'dataHoraInicio': d.get('dataHoraInicio', 'N/D'),
            'situacao': d.get('situacao', 'N/D'),
            'descricao': d.get('descricao', 'Sessão Deliberativa'),
            'local': d.get('localCamara', {}).get('nome', 'N/D') if isinstance(d.get('localCamara'), dict) else d.get('localCamara', 'N/D')
        }
    except Exception:
        evento = {'id': evento_id, 'dataHoraInicio': 'N/D', 'situacao': 'N/D', 'descricao': 'Sessão Deliberativa', 'local': 'Plenário'}

    # Carrega assessores com foto e responsavel_pauta
    try:
        conn2 = get_conn()
        c2 = conn2.cursor()
        c2.execute('SELECT username, nome_display, foto, responsavel_pauta, categoria FROM users ORDER BY nome_display, username')
        rows_ass = c2.fetchall()
        conn2.close()
        assessores = [{'username': r[0], 'nome': r[1] or r[0], 'foto': r[2] or '', 'responsavel_pauta': bool(r[3]), 'categoria': r[4] or 'geral'} for r in rows_ass]
    except Exception as e:
        logger.warning(f"Erro ao carregar assessores: {e}")
        assessores = []
    # Verifica se usuário atual é responsável pela pauta
    eh_responsavel_pauta = any(a['username'] == current_user.username and a['responsavel_pauta'] for a in assessores) or current_user.role.lower() == 'admin'
    # Adiciona responsavel_username em cada item
    notas_db = load_notas()
    for item in itens:
        key = f"PROP_{item.get('id_principal','')}"
        item['responsavel_username'] = notas_db.get(key, {}).get('responsavel_username', '')

    # Data do evento (apenas YYYY-MM-DD)
    data_evento = ''
    try:
        dh = evento.get('dataHoraInicio', '') or ''
        if dh and dh != 'N/D':
            data_evento = str(dh)[:10]  # '2026-05-20'
    except Exception:
        pass

    # Monta set de projetos na pauta atual (códigos normalizados)
    projetos_pauta = set()
    for item in itens:
        proj = item.get('projeto_original') or item.get('projeto') or ''
        projetos_pauta.add(_normalizar_codigo(proj.split(' ao ')[0].strip()))

    # Monta índice de itens por código normalizado para herança de análise
    itens_por_codigo = {}
    for item in itens:
        proj = item.get('projeto_original') or item.get('projeto') or ''
        cod = _normalizar_codigo(proj.split(' ao ')[0].strip())
        itens_por_codigo[cod] = item

    # Herança de análise: REQ ao PL X → PL X herda análise do REQ
    # Índice secundário por número/ano (sem sigla) — cobre casos onde REQ diz "PL 139" mas é "PLP 139"
    itens_por_numano = {}
    for cod, it in itens_por_codigo.items():
        m = re.search(r'(\d+)/(\d{4})$', cod)
        if m:
            numano = f"{m.group(1)}/{m.group(2)}"
            itens_por_numano.setdefault(numano, it)

    for item in itens:
        proj = item.get('projeto') or ''
        if ' ao ' not in proj:
            continue
        resumo_req = (item.get('resumo_materia') or '').strip()
        if not resumo_req:
            continue
        ref_parte = proj.split(' ao ')[1].strip()
        ref_norm  = _normalizar_codigo(ref_parte)

        # Busca direta por código normalizado
        pl_item = itens_por_codigo.get(ref_norm)

        # Fallback: busca só por número/ano ignorando sigla (PL vs PLP vs PEC)
        if not pl_item:
            m_num = re.search(r'(\d+)/(\d{4})$', ref_norm)
            if m_num:
                numano = f"{m_num.group(1)}/{m_num.group(2)}"
                pl_item = itens_por_numano.get(numano)
                if pl_item:
                    logger.info(f"Herança via número/ano: '{ref_norm}' → '{numano}'")

        if pl_item and not (pl_item.get('resumo_materia') or '').strip():
            pl_item['resumo_materia']   = resumo_req
            pl_item['orientacao']       = item.get('orientacao') or pl_item.get('orientacao') or ''
            pl_item['resumo_parecer']   = item.get('resumo_parecer') or pl_item.get('resumo_parecer') or ''
            pl_item['saved_by']         = item.get('saved_by') or ''
            pl_item['saved_at']         = item.get('saved_at') or ''
            pl_item['req_pl_mesmo_dia'] = True
            logger.info(f"✅ Herança OK: {proj} → {ref_parte}")
        elif pl_item:
            logger.info(f"PL já tem análise própria: {ref_parte}")

    # Detecta remanescentes e REQs com PL na mesma pauta
    for item in itens:
        saved_at    = item.get('saved_at') or ''
        resumo      = item.get('resumo_materia') or ''
        tem_analise = bool(resumo.strip())
        if 'eh_remanescente' not in item:
            item['eh_remanescente'] = False
        if 'req_pl_mesmo_dia' not in item:
            item['req_pl_mesmo_dia'] = False

        if tem_analise and saved_at and data_evento:
            data_salvo = str(saved_at)[:10]
            if data_salvo and data_salvo != data_evento:
                item['eh_remanescente'] = True

        # REQ com PL referenciado na mesma pauta
        proj = item.get('projeto') or ''
        if ' ao ' in proj and tem_analise and not item['req_pl_mesmo_dia']:
            ref_parte = proj.split(' ao ')[1].strip()
            ref_norm  = _normalizar_codigo(ref_parte)
            if ref_norm in projetos_pauta:
                item['req_pl_mesmo_dia'] = True

    return render_template('pauta.html', evento_id=evento_id, evento=evento, itens=itens,
                           from_cache=from_cache, user_role=current_user.role,
                           user_categoria=current_user.categoria,
                           last_updated=last_updated, last_saved_user=last_saved_user,
                           assessores=assessores,
                           data_evento=data_evento,
                           eh_responsavel_pauta=eh_responsavel_pauta)

@app.route('/save_item', methods=['POST'])
@login_required
def save_item():
    data = request.get_json()
    evento_id    = data.get('evento_id')
    id_principal = data.get('id_principal')
    ordem        = data.get('ordem')
    orientacao   = data.get('orientacao', '') or ''
    conn = get_conn()
    c = conn.cursor()
    try:
        prop_key = f"PROP_{id_principal}"
        now_str  = now_brasilia().strftime('%Y-%m-%d %H:%M:%S')
        saved_by = current_user.display_name()
        c.execute('INSERT OR REPLACE INTO notas (item_key, evento_id, ordem, resumo_materia, orientacao, resumo_parecer, saved_by, saved_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                  (prop_key, evento_id, ordem, data.get('resumo_materia', ''), orientacao, data.get('resumo_parecer', ''), saved_by, now_str))
        conn.commit()

        # Exporta orientação para o quadro da bancada
        if orientacao:
            try:
                # Determina o grupo: assessor responsável se existir, senão usuário logado
                grupo = current_user.categoria or 'geral'

                # Tenta pegar categoria do assessor responsável pelo item
                c.execute('SELECT responsavel_username FROM notas WHERE item_key=?', (prop_key,))
                row_resp = c.fetchone()
                responsavel = (row_resp[0] if row_resp else '') or ''
                if responsavel:
                    c.execute('SELECT categoria FROM users WHERE username=?', (responsavel,))
                    row_cat = c.fetchone()
                    if row_cat and row_cat[0]:
                        grupo = row_cat[0]

                logger.info(f"Exportando orientação: grupo={grupo} id={id_principal} ori={orientacao} resp={responsavel}")
                c.execute('''INSERT OR REPLACE INTO orientacoes_grupo
                             (evento_id, id_principal, grupo, orientacao, comentario, saved_by, saved_at)
                             VALUES (?, ?, ?, ?, ?, ?, ?)''',
                          (evento_id, str(id_principal), grupo, orientacao, '', saved_by, now_str))
                conn.commit()
                logger.info(f"✅ Orientação exportada para quadro: {grupo} / {id_principal} → {orientacao}")
            except Exception as e:
                logger.error(f"Erro ao exportar orientação: {e}")

        # Atualiza o cache persistente
        c.execute("SELECT json_pauta FROM pauta_cache_db WHERE evento_id = ?", (evento_id,))
        row = c.fetchone()
        if row:
            try:
                itens = json.loads(row[0])
                for item in itens:
                    if str(item.get('id_principal')) == str(id_principal):
                        item['resumo_materia'] = data.get('resumo_materia', '')
                        item['orientacao']     = orientacao
                        item['resumo_parecer'] = data.get('resumo_parecer', '')
                c.execute('UPDATE pauta_cache_db SET json_pauta = ?, last_updated = ?, last_saved_by = ? WHERE evento_id = ?',
                          (json.dumps(itens), now_str, saved_by, evento_id))
                conn.commit()
            except Exception:
                pass

        pauta_cache.clear()
        return jsonify({'message': 'Salvo com sucesso!'})
    except Exception as e:
        conn.rollback()
        return jsonify({'message': f'Erro ao salvar: {e}'})
    finally:
        conn.close()

def _clean_html(raw):
    if raw is None:
        return ''
    s = re.sub(r'<[^>]+>', '', raw, flags=re.S | re.I)
    s = ihtml.unescape(s)
    return re.sub(r'\s+', ' ', s, flags=re.S).strip()

@app.route('/destaques/<id_proposicao>')
@login_required
def buscar_destaques(id_proposicao):
    """Busca destaques em tempo real para uma proposição via scraping do site da Câmara."""
    url = f"https://www.camara.leg.br/pplen/destaques.html?codOrgao=180&codProposicao={id_proposicao}"
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        html = r.text
        rows = re.findall(r'<tr[^>]*>(.*?)</tr>', html, flags=re.S | re.I)
        destaques = []
        for row in rows:
            cols = re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', row, flags=re.S | re.I)
            if len(cols) < 5:
                continue
            numero    = _clean_html(cols[0])
            autoria   = _clean_html(cols[1])
            descricao = _clean_html(cols[2])
            tipo      = _clean_html(cols[3])
            situacao  = _clean_html(cols[4])
            if 'DTQ' not in numero.upper():
                continue
            destaques.append({
                'numero':        numero,
                'autoria':       autoria,
                'descricao':     descricao,
                'tipo_destaque': tipo,
                'situacao':      situacao,
            })
        return jsonify({'destaques': destaques, 'total': len(destaques)})
    except Exception as e:
        logger.warning(f"Falha ao buscar destaques de {id_proposicao}: {e}")
        return jsonify({'destaques': [], 'total': 0, 'erro': str(e)})

def buscar_texto_prlp_ou_sbt(id_proposicao):
    """
    Busca o texto do último PRLP ou Substitutivo de plenário.
    Usa o Avulso e as últimas tramitações, verificando o conteúdo do PDF.
    """
    from bs4 import BeautifulSoup
    import pdfplumber
    from io import BytesIO

    headers = {'User-Agent': 'Mozilla/5.0 Chrome/124.0.0.0 Safari/537.36'}

    try:
        url_pag = f"https://www.camara.leg.br/proposicoesWeb/fichadetramitacao?idProposicao={id_proposicao}"
        r = requests.get(url_pag, headers=headers, timeout=12)
        if not r.ok:
            return None

        soup = BeautifulSoup(r.text, 'html.parser')

        # Coleta links: primeiro PRLP/SBT pelo filename, depois tramitações recentes, depois avulso
        prlp_sbt_urls = []  # [(label, url)] com PRLP/SBT no filename
        tramitacoes   = []  # [(num, url)]
        avulso_url    = None

        for a in soup.find_all('a', href=True):
            href = a['href']
            if 'codteor' not in href.lower():
                continue
            url_doc  = camara_url(href)
            m_fn     = re.search(r'filename=([^&"]+)', href)
            filename = (m_fn.group(1) if m_fn else '').upper()

            # PRLP ou SBT no filename — prioridade máxima
            # Filenames: PRLP-1-PL-X, SBT-1-PL-X, Parecer-PLEN-*, Substitutivo-*
            eh_prlp_fn = any(p in filename for p in ['PRLP', 'SBT', 'SUBSTITUT'])
            eh_parecer_plen = ('PARECER' in filename and
                               any(p in filename for p in ['PLEN', 'PLENARIO', 'PLENÁRIO']))

            if eh_prlp_fn or eh_parecer_plen:
                tipo = 'Substitutivo' if any(p in filename for p in ['SBT', 'SUBSTITUT']) else 'PRLP'
                prlp_sbt_urls.append((tipo, url_doc))
            elif 'AVULSO' in filename:
                avulso_url = url_doc
            else:
                m_tram = re.search(r'Tramitacao-(\d+)-', filename)
                if m_tram:
                    tramitacoes.append((int(m_tram.group(1)), url_doc))

        # Ordena: PRLP/SBT por filename → tramitações recentes (verifica PDF) → avulso
        tramitacoes.sort(key=lambda x: x[0], reverse=True)
        candidatos = []
        for tipo, url in reversed(prlp_sbt_urls):
            candidatos.append((tipo, url))
        for num, url in tramitacoes[:8]:  # aumenta para 8 tramitações
            candidatos.append((f'tram-{num}', url))
        if avulso_url:
            candidatos.append(('avulso', avulso_url))

        # Estratégia extra: rastreia links próximos às menções de PRLP/SBT no HTML
        # As menções são "Apresentação do PRLP n. X PLEN" mas o link está na linha anterior/posterior
        texto_html = r.text
        for m in re.finditer(r'PRLP|SUBSTITUT', texto_html, re.IGNORECASE):
            # Pega trecho ao redor da menção
            inicio = max(0, m.start() - 500)
            fim    = min(len(texto_html), m.end() + 500)
            trecho = texto_html[inicio:fim]
            # Extrai codteors do trecho
            for ct in re.findall(r'codteor=(\d+)', trecho):
                url_doc = f"https://www.camara.leg.br/proposicoesWeb/prop_mostrarintegra?codteor={ct}"
                # Evita duplicatas
                if not any(url_doc in c[1] for c in candidatos):
                    candidatos.append((f'prlp-html-{ct}', url_doc))

        # Remove duplicatas preservando ordem
        vistos = set()
        candidatos_unicos = []
        for label, url in candidatos:
            if url not in vistos:
                vistos.add(url)
                candidatos_unicos.append((label, url))
        candidatos = candidatos_unicos

        numero_ultimo_prlp = None
        url_ultimo_prlp = None
        try:
            url_pareceres = f"https://www.camara.leg.br/proposicoesWeb/prop_pareceres_substitutivos_votos?idProposicao={id_proposicao}"
            r_par = requests.get(url_pareceres, headers=headers, timeout=12)
            logger.info(f"Página pareceres: status={r_par.status_code}, tamanho={len(r_par.text)}")
            if r_par.ok:
                from bs4 import BeautifulSoup as _BS
                soup_par = _BS(r_par.text, 'html.parser')
                # Coleta todos os PRLPs com seus números e links
                prlps_encontrados = []
                for row in soup_par.find_all('tr'):
                    row_txt = row.get_text(' ', strip=True).upper()
                    if 'PRLP' not in row_txt:
                        continue
                    # Extrai número do PRLP
                    m_num = re.search(r'PRLP\s*[Nn]?[º°.\s]*(\d+)', row_txt, re.IGNORECASE)
                    if not m_num:
                        continue
                    num = int(m_num.group(1))
                    # Pega o link do PDF
                    for a in row.find_all('a', href=True):
                        href = a['href']
                        if 'codteor' in href.lower():
                            if href.startswith('http'):
                                url_doc = href
                            elif href.startswith('/'):
                                url_doc = f"https://www.camara.leg.br{href}"
                            else:
                                url_doc = f"https://www.camara.leg.br/{href}"
                            prlps_encontrados.append((num, url_doc))
                            break

                if prlps_encontrados:
                    # Pega o PRLP com maior número
                    prlps_encontrados.sort(key=lambda x: x[0], reverse=True)
                    numero_ultimo_prlp = str(prlps_encontrados[0][0])
                    url_ultimo_prlp    = prlps_encontrados[0][1]
                    logger.info(f"Último PRLP via pareceres: nº {numero_ultimo_prlp} → {url_ultimo_prlp}")
                else:
                    # Fallback: extrai só números
                    todos_prlp = re.findall(r'PRLP\s*[Nn]?[º°.\s]*(\d+)', r_par.text, re.IGNORECASE)
                    if todos_prlp:
                        numero_ultimo_prlp = str(max(int(n) for n in todos_prlp))
                    logger.info(f"PRLPs encontrados (fallback): {todos_prlp} → último: {numero_ultimo_prlp}")
        except Exception as e:
            logger.warning(f"Erro ao scrappear pareceres: {e}")

        # Se já temos a URL do último PRLP, coloca no topo dos candidatos
        if url_ultimo_prlp:
            candidatos = [(f'prlp-{numero_ultimo_prlp}', url_ultimo_prlp)] + [
                (l, u) for l, u in candidatos if u != url_ultimo_prlp
            ]

        # Se já temos URL e número do último PRLP, retorna direto sem baixar PDFs
        if url_ultimo_prlp and numero_ultimo_prlp:
            url_pdf_direto = url_ultimo_prlp + ('&' if '?' in url_ultimo_prlp else '?') + 'tipo=PDF'
            logger.info(f"✅ Retorno direto PRLP {numero_ultimo_prlp}: {url_pdf_direto}")
            # Tenta confirmar que é PDF válido
            try:
                rp_test = requests.get(url_pdf_direto, headers=headers, timeout=15)
                if rp_test.ok and 'pdf' in rp_test.headers.get('Content-Type','').lower():
                    import pdfplumber
                    with pdfplumber.open(BytesIO(rp_test.content)) as pdf:
                        texto_conf = '\n'.join(p.extract_text() or '' for p in pdf.pages).strip()
                    return {
                        'tipo': 'PRLP',
                        'numero': numero_ultimo_prlp,
                        'data': '',
                        'texto': texto_conf[:8000],
                        'url_pdf': url_pdf_direto
                    }
            except Exception as e:
                logger.warning(f"Erro ao confirmar PRLP direto: {e}")
            # Retorna mesmo sem confirmar — a URL está correta
            return {
                'tipo': 'PRLP',
                'numero': numero_ultimo_prlp,
                'data': '',
                'texto': '',
                'url_pdf': url_pdf_direto
            }

        palavras_prlp_sbt = [
            'PRLP', 'PARECER PRELIMINAR', 'SUBSTITUTIVO',
            'PARECER DE PLEN', 'PARECER DO RELATOR DE PLEN',
        ]

        for label, url_doc in candidatos:
            url_pdf = url_doc + ('&' if '?' in url_doc else '?') + 'tipo=PDF'
            try:
                rp = requests.get(url_pdf, headers=headers, timeout=20)
                if not rp.ok or 'pdf' not in rp.headers.get('Content-Type','').lower():
                    continue
                with pdfplumber.open(BytesIO(rp.content)) as pdf:
                    pag1  = (pdf.pages[0].extract_text() or '').upper()
                    texto = '\n'.join(p.extract_text() or '' for p in pdf.pages).strip()

                # PRLP/SBT identificados pelo filename: aceita sempre
                # Tramitações e Avulso: só aceita se contiver PRLP/Substitutivo
                eh_prlp_fn = label in ('PRLP', 'Substitutivo')
                eh_prlp_txt = any(p in pag1 for p in palavras_prlp_sbt)

                if not eh_prlp_fn and not eh_prlp_txt:
                    continue

                tipo = label if eh_prlp_fn else (
                    'Substitutivo' if 'SUBSTITUTIVO' in pag1 else 'PRLP'
                )

                # Número do PRLP: API tem prioridade sobre extração do PDF/HTML
                numero_prlp = numero_ultimo_prlp  # já calculado via API acima

                # Confirma/sobrescreve com busca no PDF
                for busca_num in [pag1, texto.upper()[:3000]]:
                    for pat in [r'PRLP\s*N[º°.]?\s*(\d+)', r'N\.\s*(\d+)\s*PLEN']:
                        m_num = re.search(pat, busca_num, re.IGNORECASE)
                        if m_num:
                            numero_prlp = m_num.group(1)
                            break
                    if numero_prlp:
                        break

                # Fallback: busca no HTML com janela ampla ao redor do codteor
                if not numero_prlp:
                    codteor_atual = re.search(r'codteor=(\d+)', url_doc)
                    if codteor_atual:
                        ct = codteor_atual.group(1)
                        for m_ct in re.finditer(rf'codteor={ct}', texto_html):
                            ini = max(0, m_ct.start() - 3000)
                            fim = min(len(texto_html), m_ct.end() + 3000)
                            trecho = texto_html[ini:fim]
                            logger.info(f"Trecho HTML ao redor de codteor={ct}: {trecho[:500]}")
                            m_prlp = re.search(r'PRLP\s+[Nn]?[º°.\s]*(\d+)', trecho, re.IGNORECASE)
                            if m_prlp:
                                numero_prlp = m_prlp.group(1)
                                break

                # Último fallback: maior número no HTML + 1 se o doc é mais recente
                if not numero_prlp:
                    todos_prlp = re.findall(r'PRLP\s+[Nn][º°.\s]*(\d+)', texto_html, re.IGNORECASE)
                    if todos_prlp:
                        maior = max(int(n) for n in todos_prlp)
                        # Se o documento é uma tramitação recente não listada, é maior+1
                        if label.startswith('prlp-html-') or label.startswith('tram-'):
                            numero_prlp = str(maior + 1)
                        else:
                            numero_prlp = str(maior)
                        logger.info(f"Número PRLP estimado: {numero_prlp} (maior no HTML={maior})")

                # Extrai data — busca em todo o texto do documento
                data = ''
                texto_busca_data = texto.upper()[:3000]
                for padrao_data in [
                    r'APRESENTA[CÇ][AÃ]O[:\s]+(\d{2}/\d{2}/\d{4})',
                    r'APRESENTA[CÇ][AÃ]O\s*:\s*(\d{2}/\d{2}/\d{4})',
                ]:
                    m_data = re.search(padrao_data, texto_busca_data, re.IGNORECASE)
                    if m_data:
                        data = m_data.group(1)
                        break

                if not data:
                    todas_datas = re.findall(r'(\d{2}/\d{2}/\d{4})', texto_busca_data)
                    datas_recentes = [d for d in todas_datas if int(d[6:]) >= 2024]
                    if datas_recentes:
                        data = datas_recentes[-1]
                    elif todas_datas:
                        data = todas_datas[-1]

                # Se não achou no PDF, busca na API
                if not data:
                    try:
                        r_api = requests.get(
                            f"https://dadosabertos.camara.leg.br/api/v2/proposicoes/{id_proposicao}",
                            headers=headers, timeout=8
                        )
                        if r_api.ok:
                            dh = r_api.json().get('dados', {}).get('statusProposicao', {}).get('dataHora', '')
                            if dh:
                                data = datetime.fromisoformat(str(dh)[:10]).strftime('%d/%m/%Y')
                    except Exception:
                        pass
                logger.info(f"PDF pag1 (300 chars): {pag1[:300]}")
                logger.info(f"Documento {tipo} ({label}) para {id_proposicao}: {len(texto)} chars, PRLP nº {numero_prlp}, data {data}")
                return {'tipo': tipo, 'numero': numero_prlp, 'data': data, 'texto': texto[:8000], 'url_pdf': url_pdf}
            except Exception as e:
                logger.warning(f"Erro ao processar {label}: {e}")
                continue

        return None

    except Exception as e:
        logger.warning(f"Erro buscar_texto_prlp_ou_sbt({id_proposicao}): {e}")
    return None

def _extrair_nome_doc_despacho(despacho, tipo_label):
    """Extrai nome descritivo do documento a partir do texto do despacho."""
    if not despacho:
        return tipo_label
    m = re.search(
        r'adotad[ao]\s+pel[ao]\s+(?:relator[a]?\s+)?(?:dep\.\s+)?(.{5,80}?)(?:\s*[\.\(]|$)',
        despacho, re.IGNORECASE
    )
    if m:
        quem = m.group(1).strip().rstrip('.,;')
        return f"{tipo_label} adotado por {quem}"
    return f"{tipo_label} ({despacho[:80].strip()}...)" if len(despacho) > 80 else tipo_label

def buscar_ultimo_parecer(id_proposicao):
    """
    Busca o último PRLP ou Substitutivo de Plenário via tramitações da proposição.
    Filtra apenas documentos do Plenário (PLEN, MESA, PRLP, SBT em plenário).
    """
    headers = {'Accept': 'application/json', 'User-Agent': 'Mozilla/5.0 Chrome/124.0.0.0'}

    ORGAOS_PLENARIO = {'PLEN', 'MESA', 'PRESID', 'CVT'}
    SIGLAS_DOC      = {'PRLP', 'SBT', 'SUBSTITUT', 'PARECER PRELIMINAR DE PLEN'}

    try:
        # Busca tramitações em ordem DESC (mais recente primeiro)
        url = (f"https://dadosabertos.camara.leg.br/api/v2/proposicoes"
               f"/{id_proposicao}/tramitacoes?itens=50&ordem=DESC")
        r = requests.get(url, headers=headers, timeout=12)

        if r.ok:
            trams = r.json().get('dados', [])
            for t in trams:
                sigla_orgao = (t.get('siglaOrgao') or '').upper()
                descricao   = (t.get('descricaoTramitacao') or '').upper()
                despacho    = (t.get('despacho') or '').upper()
                texto_busca = f"{descricao} {despacho}"

                # Só plenário
                eh_plenario = (sigla_orgao in ORGAOS_PLENARIO or
                               'PLEN' in sigla_orgao or
                               'PLENÁRIO' in texto_busca or
                               'PLENARIO' in texto_busca)
                if not eh_plenario:
                    continue

                # Verifica se é PRLP ou Substitutivo
                eh_prlp_ou_sbt = (
                    'PRLP' in texto_busca or
                    'SUBSTITUT' in texto_busca or
                    'PARECER PRELIMINAR' in texto_busca
                )
                if not eh_prlp_ou_sbt:
                    continue

                # Encontrou — extrai data e tipo
                data_hora = t.get('dataHora', '') or ''
                data_fmt  = ''
                if data_hora:
                    try:
                        dt = datetime.fromisoformat(str(data_hora)[:10])
                        data_fmt = dt.strftime('%d/%m/%Y')
                    except Exception:
                        data_fmt = str(data_hora)[:10]

                tipo_label = 'Substitutivo' if 'SUBSTITUT' in texto_busca else 'Parecer Preliminar de Plenário (PRLP)'
                despacho_orig = t.get('despacho', '') or ''
                nome_doc = _extrair_nome_doc_despacho(despacho_orig, tipo_label)

                logger.info(f"Parecer plenário encontrado para {id_proposicao}: {tipo_label} em {data_fmt}")
                return {
                    'tipo':  tipo_label,
                    'nome':  nome_doc,
                    'data':  data_fmt,
                    'orgao': t.get('siglaOrgao', ''),
                }

        # Fallback: usa statusProposicao do endpoint principal
        r2 = requests.get(
            f"https://dadosabertos.camara.leg.br/api/v2/proposicoes/{id_proposicao}",
            headers=headers, timeout=10
        )
        if r2.ok:
            status = r2.json().get('dados', {}).get('statusProposicao', {}) or {}
            despacho   = (status.get('despacho', '') or '').upper()
            tramitacao = (status.get('descricaoTramitacao', '') or '').upper()
            sigla_orgao = (status.get('siglaOrgao', '') or '').upper()
            data_hora   = status.get('dataHora', '') or ''

            eh_plenario   = sigla_orgao in ORGAOS_PLENARIO or 'PLEN' in sigla_orgao
            eh_prlp_ou_sbt = ('SUBSTITUT' in despacho or 'PRLP' in despacho or
                               'SUBSTITUT' in tramitacao or 'PRLP' in tramitacao)

            if eh_plenario and eh_prlp_ou_sbt:
                data_fmt = ''
                if data_hora:
                    try:
                        dt = datetime.fromisoformat(str(data_hora)[:10])
                        data_fmt = dt.strftime('%d/%m/%Y')
                    except Exception:
                        data_fmt = str(data_hora)[:10]

                despacho_orig = status.get('despacho', '') or ''
                tramitacao_orig = status.get('descricaoTramitacao', '') or ''
                texto_upper = f"{despacho_orig} {tramitacao_orig}".upper()
                tipo_label = 'Substitutivo' if 'SUBSTITUT' in texto_upper else 'PRLP'

                # Extrai nome descritivo do despacho
                nome_doc = _extrair_nome_doc_despacho(despacho_orig, tipo_label)

                return {
                    'tipo':  tipo_label,
                    'nome':  nome_doc,
                    'data':  data_fmt,
                    'orgao': status.get('siglaOrgao', ''),
                }

    except Exception as e:
        logger.warning(f"Erro ao buscar parecer de {id_proposicao}: {e}")

    return None

@app.route('/analisar_ia', methods=['POST'])
@login_required
def analisar_ia():
    data         = request.get_json()
    projeto      = data.get('projeto', '')
    ementa       = data.get('ementa', '')
    autor        = data.get('autor', '')
    relator      = data.get('relator', '')
    id_principal = data.get('id_principal', '')
    url_doc_sel  = data.get('url_documento', '')
    label_doc    = data.get('label_documento', '')

    gemini_key = os.environ.get('GEMINI_API_KEY', '')
    groq_key   = os.environ.get('GROQ_API_KEY', '')

    # ── Tratamento especial para REQ de Urgência ──────────────────────────
    # Analisa o PL referenciado, não o requerimento em si
    siglas_req = ('REQ', 'RQS', 'RQU', 'REC')
    proj_base  = projeto.split(' ao ')[0].strip()
    eh_req     = any(proj_base.upper().startswith(s) for s in siglas_req)

    if eh_req:
        # Extrai referência ao PL na ementa ou projeto
        id_pl_ref = None
        projeto_pl = projeto
        ementa_pl  = ementa
        autor_pl   = autor
        relator_pl = relator

        m_pl = None
        for txt in [ementa, projeto]:
            m_pl = re.search(r'\b(PL|PEC|PLP|MPV|PDL|PLC)\s+n[º°.]?\s*([\d.]+)[,\s/]+(?:de\s+)?(\d{4})', txt, re.IGNORECASE)
            if m_pl: break
            m_pl = re.search(r'\b(PL|PEC|PLP|MPV|PDL|PLC)\s+([\d.]+)[/\-](\d{4})', txt, re.IGNORECASE)
            if m_pl: break

        if m_pl:
            sigla_ref = m_pl.group(1).upper()
            num_ref   = m_pl.group(2).replace('.', '')
            ano_ref   = m_pl.group(3)
            try:
                r_api = requests.get(
                    f"https://dadosabertos.camara.leg.br/api/v2/proposicoes?siglaTipo={sigla_ref}&numero={num_ref}&ano={ano_ref}&itens=1",
                    headers={'Accept': 'application/json'}, timeout=8
                )
                if r_api.ok:
                    dados = r_api.json().get('dados', [])
                    if dados:
                        id_pl_ref  = str(dados[0].get('id', ''))
                        ementa_pl  = dados[0].get('ementa', ementa)
                        projeto_pl = f"{sigla_ref} {num_ref}/{ano_ref}"
                        logger.info(f"REQ → analisando {projeto_pl} (id={id_pl_ref})")
            except Exception as e:
                logger.warning(f"Erro buscar PL do REQ: {e}")

        # Usa dados do PL referenciado para a análise
        projeto      = projeto_pl
        ementa       = ementa_pl
        id_principal = id_pl_ref or id_principal
        nota_req     = f"<p><em>Este requerimento solicita <strong>urgência</strong> para o {projeto_pl}.</em></p><br>"
    else:
        nota_req = ''

    # ── Busca último documento disponível ────────────────────────────────
    doc_plenario = None
    parecer_info = None

    # Se usuário selecionou um documento específico, usa ele
    if url_doc_sel:
        texto_sel = extrair_texto_documento(url_doc_sel) or ''
        if texto_sel and not texto_sel.startswith('[PDF escaneado'):
            doc_plenario = {'tipo': label_doc or 'Documento selecionado', 'numero': '', 'data': '', 'texto': texto_sel[:8000], 'url_pdf': url_doc_sel}
    
    if not doc_plenario and id_principal:
        doc_plenario = buscar_texto_prlp_ou_sbt(id_principal)
        if not doc_plenario:
            parecer_info = buscar_ultimo_parecer(id_principal)

    # Monta contexto
    if doc_plenario and doc_plenario.get('texto'):
        num_str   = f" nº {doc_plenario['numero']}" if doc_plenario.get('numero') else ''
        ref_linha = f"{doc_plenario['tipo']}{num_str}" + (f" de {doc_plenario['data']}" if doc_plenario.get('data') else '')
        contexto_doc = f"\nDocumento de referência: {ref_linha}\n\nTEXTO:\n{doc_plenario['texto']}\n\n---\nFaça a análise com base no texto acima."
    elif parecer_info:
        ref_linha    = f"{parecer_info['tipo']} de {parecer_info.get('data','')}"
        contexto_doc = f"\nDocumento de referência: {ref_linha}"
    else:
        ref_linha    = 'texto original da proposição'
        contexto_doc = ''

    # Detecta se autoria/relator é de partido de esquerda ou governo
    PARTIDOS_ESQUERDA = {'PT', 'REDE', 'PSOL', 'PCdoB', 'PDT', 'SOLIDARIEDADE', 'GOVERNO'}
    def tem_partido_esquerda(texto):
        if not texto: return False
        for p in PARTIDOS_ESQUERDA:
            if re.search(rf'\b{re.escape(p)}\b', texto, re.IGNORECASE):
                return True
        return False

    eh_esquerda = tem_partido_esquerda(autor) or tem_partido_esquerda(relator)

    secao_criticas = ""
    if eh_esquerda:
        secao_criticas = """
<br>
<p><strong style="color:#8B0000;">⚠️ CRÍTICAS E PONTOS DE COMBATE</strong><br>
<em style="color:#8B0000;">[Autoria ou relatoria de partido de esquerda/governo]</em></p>
<ul>
<li><strong>Críticas ao mérito:</strong> [principais críticas técnicas — contradições, falhas, impactos negativos]</li>
<li><strong>Contradições com o discurso da esquerda:</strong> [onde o projeto contradiz posições históricas do PT/PSOL/PCdoB — privatizações, redução de direitos, favorecimento de setores privados]</li>
<li><strong>Discurso de combate para o Plenário:</strong> [argumento direto e combativo para pronunciamento — inclua exemplos de declarações contraditórias de parlamentares da esquerda]</li>
<li><strong>Questionamentos ao relator:</strong> [perguntas incisivas para fazer ao relator no plenário]</li>
</ul>"""

    prompt = f"""Você é um assessor legislativo especializado em análise de proposições da Câmara dos Deputados do Brasil, trabalhando para a Oposição e Minoria.

**Proposição:** {projeto}
**Autor(es):** {autor}
**Relator:** {relator}
**Ementa:** {ementa}
{contexto_doc}

Gere uma nota técnica em HTML com EXATAMENTE este formato:

<p><strong>Nota Técnica: Análise do {projeto}</strong></p>
{nota_req}<p><em style="color:red;">Análise baseada em: {ref_linha}</em></p>
<br>
<p><strong>Objetivo da Proposição</strong><br>[texto detalhado]</p>
<br>
<p><strong>Principais Alterações</strong><br><ul><li>[item]</li><li>[item]</li><li>[item]</li></ul></p>
<br>
<p><strong>Impacto Esperado</strong><br>[texto detalhado]</p>
<br>
<p><strong>Pontos de Atenção</strong><br><ul><li>[item]</li><li>[item]</li></ul></p>
{secao_criticas}

Seja detalhado e técnico. Máximo 600 palavras. Não use ### ou ** no texto, apenas HTML."""

    # Tenta Gemini primeiro, fallback para Groq
    if gemini_key:
        try:
            r = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent?key={gemini_key}",
                headers={"Content-Type": "application/json"},
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"maxOutputTokens": 1024, "temperature": 0.3}
                },
                timeout=30
            )
            r.raise_for_status()
            texto = r.json()['candidates'][0]['content']['parts'][0]['text']
            return jsonify({'resumo': texto, 'fonte': 'gemini', 'parecer': parecer_info})
        except Exception as e:
            status = getattr(getattr(e, 'response', None), 'status_code', None)
            import traceback
            logger.error(f"Gemini falhou analisar_ia status={status}: {e}\n{traceback.format_exc()[-500:]}")
            if status == 429:
                return jsonify({'error': 'Limite de requisições atingido. Aguarde alguns segundos e tente novamente.'}), 429
            return jsonify({'error': f'Erro Gemini ({status}): {str(e)}'}), 500

    return jsonify({'error': 'GEMINI_API_KEY não configurada.'}), 500

@app.route('/infografico/<int:evento_id>')
@login_required
def gerar_infografico(evento_id):
    from gerar_infografico import gerar_infografico_pdf
    itens, _ = fetch_pauta(evento_id, force_reload=False)
    try:
        r = requests.get(f"https://dadosabertos.camara.leg.br/api/v2/eventos/{evento_id}", timeout=10)
        d = r.json().get("dados", {})
        evento = {
            'id': evento_id,
            'dataHoraInicio': d.get('dataHoraInicio', ''),
            'situacao': d.get('situacao', ''),
            'descricao': d.get('descricao', 'Sessão Deliberativa'),
            'local': d.get('localCamara', {}).get('nome', 'Plenário') if isinstance(d.get('localCamara'), dict) else 'Plenário'
        }
    except Exception:
        evento = {'id': evento_id, 'dataHoraInicio': '', 'situacao': '', 'descricao': 'Sessão Deliberativa', 'local': 'Plenário'}

    static_path = os.path.join(app.root_path, 'static')
    # Carrega resumos IA
    resumos_ia = {}
    try:
        conn_ri = get_conn()
        c_ri = conn_ri.cursor()
        c_ri.execute('SELECT id_proposicao, resumo FROM resumos_ia WHERE evento_id=?', (evento_id,))
        resumos_ia = {str(r[0]): r[1] for r in c_ri.fetchall()}
        conn_ri.close()
    except Exception:
        pass
    pdf = gerar_infografico_pdf(evento, itens,
                                 os.path.join(static_path, 'logo_minoria.png'),
                                 os.path.join(static_path, 'logo_oposicao.png'),
                                 resumos_ia=resumos_ia)
    resp = make_response(pdf)
    resp.headers['Content-Type'] = 'application/pdf'
    resp.headers['Content-Disposition'] = f'inline; filename="infografico_plenario_{evento_id}.pdf"'
    return resp

@app.route('/export-resumo/<int:evento_id>')
@login_required
def export_resumo(evento_id):
    return redirect(url_for('exportar.exportar_pauta', evento_id=evento_id))

from exportar_pauta import exportar_bp
app.register_blueprint(exportar_bp)

@app.route('/trocar-senha', methods=['GET', 'POST'])
@login_required
def trocar_senha():
    if request.method == 'POST':
        data         = request.get_json()
        senha_atual  = data.get('senha_atual', '')
        nova_senha   = data.get('nova_senha', '').strip()
        confirma     = data.get('confirma', '').strip()
        if not nova_senha or len(nova_senha) < 4:
            return jsonify({'error': 'Nova senha deve ter ao menos 4 caracteres.'}), 400
        if nova_senha != confirma:
            return jsonify({'error': 'Nova senha e confirmação não coincidem.'}), 400
        conn = get_conn()
        c    = conn.cursor()
        c.execute('SELECT password FROM users WHERE id=?', (current_user.id,))
        row = c.fetchone()
        if not row or not bcrypt.check_password_hash(row[0], senha_atual):
            conn.close()
            return jsonify({'error': 'Senha atual incorreta.'}), 400
        nova_hash = bcrypt.generate_password_hash(nova_senha).decode('utf-8')
        c.execute('UPDATE users SET password=? WHERE id=?', (nova_hash, current_user.id))
        conn.commit()
        conn.close()
        return jsonify({'message': 'Senha alterada com sucesso!'})
    return render_template('trocar_senha.html')

@app.route('/admin/usuarios')
@login_required
def admin_usuarios():
    if current_user.role.lower() != 'admin':
        flash('Acesso restrito.', 'error')
        return redirect(url_for('selecionar_data'))
    conn = get_conn()
    c = conn.cursor()
    c.execute('SELECT id, username, role, categoria, nome_display, foto, responsavel_pauta FROM users ORDER BY id DESC')
    usuarios = c.fetchall()
    conn.close()
    return render_template('admin_usuarios.html', usuarios=usuarios)

@app.route('/admin/usuarios/add', methods=['POST'])
@login_required
def add_usuario():
    if current_user.role.lower() != 'admin':
        return jsonify({'error': 'Acesso negado'}), 403
    data      = request.get_json()
    username  = data.get('username', '').strip()
    password  = data.get('password', '').strip()
    role      = data.get('role', 'Assessor').strip()
    categoria = data.get('categoria', 'geral').strip()
    nome_display = data.get('nome_display', '').strip()
    if not username or not password:
        return jsonify({'error': 'Usuário e senha obrigatórios'}), 400
    hashed = bcrypt.generate_password_hash(password).decode('utf-8')
    conn = get_conn()
    c = conn.cursor()
    try:
        # Verifica se já existe
        c.execute('SELECT id FROM users WHERE username=?', (username,))
        if c.fetchone():
            return jsonify({'error': 'Usuário já existe'}), 409
        c.execute('INSERT INTO users (username, password, role, categoria, nome_display) VALUES (?, ?, ?, ?, ?)',
                  (username, hashed, role, categoria, nome_display or username))
        conn.commit()
        return jsonify({'message': 'Usuário criado!'})
    except Exception as e:
        logger.error(f"Erro add_usuario: {e}")
        if 'unique' in str(e).lower() or 'duplicate' in str(e).lower():
            return jsonify({'error': 'Usuário já existe'}), 409
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@app.route('/admin/usuarios/foto/<int:user_id>', methods=['POST'])
@login_required
def upload_foto_usuario(user_id):
    if current_user.role.lower() != 'admin':
        return jsonify({'error': 'Acesso negado'}), 403
    if 'foto' not in request.files:
        return jsonify({'error': 'Nenhum arquivo enviado'}), 400
    f = request.files['foto']
    if not f.filename:
        return jsonify({'error': 'Arquivo inválido'}), 400
    ext = f.filename.rsplit('.', 1)[-1].lower()
    if ext not in ('jpg', 'jpeg', 'png', 'gif', 'webp'):
        return jsonify({'error': 'Formato inválido'}), 400
    import base64
    data = base64.b64encode(f.read()).decode('utf-8')
    foto_data = f'data:image/{ext};base64,{data}'
    conn = get_conn()
    c = conn.cursor()
    c.execute('UPDATE users SET foto=? WHERE id=?', (foto_data, user_id))
    conn.commit()
    conn.close()
    return jsonify({'message': 'Foto atualizada!'})

@app.route('/admin/usuarios/nome_display/<int:user_id>', methods=['POST'])
@login_required
def update_nome_display(user_id):
    if current_user.role.lower() != 'admin':
        return jsonify({'error': 'Acesso negado'}), 403
    data = request.get_json()
    nome = data.get('nome_display', '').strip()
    conn = get_conn()
    c = conn.cursor()
    c.execute('UPDATE users SET nome_display=? WHERE id=?', (nome, user_id))
    conn.commit()
    conn.close()
    return jsonify({'message': 'Nome atualizado!'})

@app.route('/admin/usuarios/responsavel_pauta/<int:user_id>', methods=['POST'])
@login_required
def set_responsavel_pauta(user_id):
    if current_user.role.lower() != 'admin':
        return jsonify({'error': 'Acesso negado'}), 403
    data = request.get_json()
    ativo = 1 if data.get('ativo') else 0
    conn = get_conn()
    c = conn.cursor()
    if ativo:
        c.execute('UPDATE users SET responsavel_pauta=? WHERE id=?', (ativo, user_id))
    conn.commit()
    conn.close()
    return jsonify({'message': 'Responsável atualizado!'})

@app.route('/atribuir_responsavel', methods=['POST'])
@login_required
def atribuir_responsavel():
    """Atribui assessor a uma proposição. Admin e responsáveis pela pauta podem fazer isso."""
    conn = get_conn()
    c = conn.cursor()
    try:
        # Admin sempre pode; outros só se forem responsável pela pauta
        if current_user.role.lower() != 'admin':
            c.execute('SELECT responsavel_pauta FROM users WHERE id=?', (current_user.id,))
            row = c.fetchone()
            if not row or not row[0]:
                return jsonify({'error': 'Apenas o responsável pela pauta pode atribuir proposições'}), 403

        data = request.get_json()
        item_key             = data.get('item_key', '')
        evento_id            = data.get('evento_id', '')
        responsavel_username = data.get('responsavel_username', '')

        if not item_key:
            return jsonify({'error': 'item_key obrigatório'}), 400

        # Verifica se nota já existe
        c.execute('SELECT item_key FROM notas WHERE item_key=?', (item_key,))
        existe = c.fetchone()
        if existe:
            c.execute('UPDATE notas SET responsavel_username=? WHERE item_key=?',
                      (responsavel_username, item_key))
        else:
            c.execute('INSERT INTO notas (item_key, evento_id, responsavel_username) VALUES (?,?,?)',
                      (item_key, evento_id, responsavel_username))
        conn.commit()
        return jsonify({'message': 'Responsável atribuído!'})
    except Exception as e:
        logger.error(f"Erro atribuir_responsavel: {e}")
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@app.route('/listar_assessores')
@login_required
def listar_assessores():
    """Lista usuários disponíveis para atribuição de proposição."""
    try:
        conn = get_conn()
        c = conn.cursor()
        c.execute('SELECT username, nome_display, foto, categoria FROM users WHERE categoria != ? ORDER BY nome_display, username', ('restrito',))
        rows = c.fetchall()
        conn.close()
        assessores = [{'username': r[0], 'nome': r[1] or r[0], 'foto': r[2] or '', 'categoria': r[3] or 'geral'} for r in rows]
        return jsonify({'assessores': assessores})
    except Exception as e:
        logger.error(f"Erro listar_assessores: {e}")
        return jsonify({'assessores': [], 'error': str(e)})

@app.route('/exportar_orientacoes_pdf', methods=['POST'])
@login_required
def exportar_orientacoes_pdf():
    from io import BytesIO
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.units import cm
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.enums import TA_CENTER, TA_LEFT
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer,
                                     Table, TableStyle, HRFlowable)

    data     = request.get_json()
    itens    = data.get('itens', [])
    ori_data = data.get('orientacoes', {})
    colunas  = data.get('colunas', [])
    evento_id = data.get('evento_id', '')

    COR_VERDE  = colors.HexColor("#1A6B3A")
    COR_AZUL   = colors.HexColor("#0D2B5E")
    COR_CINZA  = colors.HexColor("#555555")
    CORES_ORI  = {
        'SIM':        colors.HexColor("#d4edda"),
        'NÃO':        colors.HexColor("#f8d7da"),
        'NEGOCIAÇÃO': colors.HexColor("#fff3cd"),
        'LIBERADO':   colors.HexColor("#cce5ff"),
        'OBSTRUÇÃO':  colors.HexColor("#ffe5d0"),
        'ABSTENÇÃO':  colors.HexColor("#e2e3e5"),
        '—':         colors.white,
        '':          colors.white,
    }
    CORES_COL = {
        'PL':       colors.HexColor("#dceefb"),
        'NOVO':     colors.HexColor("#fde8d8"),
        'oposicao': colors.HexColor("#fdeaea"),
        'minoria':  colors.HexColor("#e8f5ee"),
    }

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4),
                            leftMargin=1.2*cm, rightMargin=1.2*cm,
                            topMargin=1.5*cm, bottomMargin=1.5*cm)
    SS = getSampleStyleSheet()
    T  = ParagraphStyle("T", parent=SS["Title"],  fontSize=12, textColor=COR_VERDE, alignment=TA_CENTER)
    S  = ParagraphStyle("S", parent=SS["Normal"], fontSize=7.5, textColor=COR_CINZA, leading=10, wordWrap='CJK')
    SB = ParagraphStyle("SB",parent=SS["Normal"], fontSize=7.5, fontName="Helvetica-Bold", leading=10, wordWrap='CJK')
    SC = ParagraphStyle("SC",parent=SS["Normal"], fontSize=7,   textColor=COR_CINZA, leading=9, wordWrap='CJK', alignment=TA_CENTER)

    story = []
    story.append(Paragraph("Quadro de Orientações — Plenário da Câmara dos Deputados", T))
    story.append(Paragraph(f"Gerado em: {now_brasilia().strftime('%d/%m/%Y %H:%M')}", ParagraphStyle("sm", parent=SS["Normal"], fontSize=7.5, textColor=COR_CINZA, alignment=TA_CENTER)))
    story.append(Spacer(1, 8))
    story.append(HRFlowable(width="100%", thickness=1, color=COR_VERDE))
    story.append(Spacer(1, 6))

    # Cabeçalho
    header = [Paragraph("<b>#</b>", SC),
              Paragraph("<b>Proposição</b>", SC),
              Paragraph("<b>Ementa</b>", SC)]
    for col in colunas:
        header.append(Paragraph(f"<b>{col['label']}</b>", SC))

    rows = [header]
    col_widths = [0.7*cm, 2.5*cm, 7*cm] + [4.5*cm] * len(colunas)

    style_cmds = [
        ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#f0f0f0")),
        ("GRID",       (0,0), (-1,-1), 0.4, colors.HexColor("#CCCCCC")),
        ("VALIGN",     (0,0), (-1,-1), "TOP"),
        ("TOPPADDING", (0,0), (-1,-1), 3),
        ("BOTTOMPADDING",(0,0),(-1,-1),3),
        ("FONTSIZE",   (0,0), (-1,-1), 7.5),
    ]

    for i, item in enumerate(itens):
        row_num = i + 1
        row = [
            Paragraph(str(item.get('ordem','')), SC),
            Paragraph(f"<b>{item.get('projeto','')}</b>", SB),
            Paragraph(item.get('ementa',''), S),
        ]
        for j, col in enumerate(colunas):
            key   = f"{item.get('id_principal')}|{col['grupo']}"
            salvo = ori_data.get(key, {}) or {}
            ori   = salvo.get('orientacao', '') if isinstance(salvo, dict) else ''
            com   = salvo.get('comentario', '') if isinstance(salvo, dict) else ''
            texto = f"<b>{ori}</b>" if ori else "—"
            if com:
                texto += f"<br/><font size='6.5' color='#555555'>{com}</font>"
            row.append(Paragraph(texto, ParagraphStyle("oc", parent=S, alignment=TA_CENTER)))
            # Cor de fundo da célula
            cor_bg = CORES_ORI.get(ori, colors.white)
            col_idx = 3 + j
            style_cmds.append(("BACKGROUND", (col_idx, row_num), (col_idx, row_num), cor_bg))
        rows.append(row)

    tbl = Table(rows, colWidths=col_widths, repeatRows=1)
    tbl.setStyle(TableStyle(style_cmds))
    story.append(tbl)

    doc.build(story)
    pdf = buf.getvalue(); buf.close()

    resp = make_response(pdf)
    resp.headers["Content-Type"] = "application/pdf"
    resp.headers["Content-Disposition"] = f'attachment; filename="orientacoes_{evento_id}.pdf"'
    return resp

def buscar_documentos_disponiveis(id_proposicao):
    """
    Busca todos os documentos da página prop_pareceres_substitutivos_votos.
    Sempre inclui o Avulso (texto consolidado) no topo.
    """
    from bs4 import BeautifulSoup
    headers = {'User-Agent': 'Mozilla/5.0 Chrome/124.0.0.0 Safari/537.36'}
    docs = []
    vistos = set()

    # 1. Busca o Avulso (texto integral consolidado) da ficha de tramitação — sempre no topo
    try:
        url_tram = f"https://www.camara.leg.br/proposicoesWeb/fichadetramitacao?idProposicao={id_proposicao}"
        r_tram = requests.get(url_tram, headers=headers, timeout=12)
        if r_tram.ok:
            soup_tram = BeautifulSoup(r_tram.text, 'html.parser')
            for a in soup_tram.find_all('a', href=True):
                href = a['href']
                txt_link = a.get_text(strip=True).lower()
                if 'codteor' not in href.lower():
                    continue
                m_fn = re.search(r'filename=([^&"]+)', href)
                fn   = (m_fn.group(1) if m_fn else '').upper()
                # Pega avulso pelo texto do link OU pelo filename
                eh_avulso = ('AVULSO' in fn and 'LEGISLACAO' not in fn) or txt_link in ('avulsos', 'avulso')
                if eh_avulso:
                    url_doc = camara_url(href)
                    docs.append({
                        'label':    '📄 Avulso — Texto Integral da Proposição',
                        'url':      url_doc,
                        'filename': fn,
                        'tipo':     'Avulso',
                        'data':     '',
                    })
                    vistos.add(url_doc)
                    break
        # Fallback: se scraping falhou (403 etc), adiciona link da ficha de tramitação
        if not any(d['tipo'] == 'Avulso' for d in docs):
            docs.append({
                'label': '📄 Texto da Proposição (ficha de tramitação)',
                'url':   url_tram,
                'filename': '',
                'tipo': 'Avulso',
                'data': '',
            })
    except Exception as e:
        logger.warning(f"Erro ao buscar avulso: {e}")
        url_tram = f"https://www.camara.leg.br/proposicoesWeb/fichadetramitacao?idProposicao={id_proposicao}"
        docs.append({
            'label': '📄 Texto da Proposição (ficha de tramitação)',
            'url':   url_tram,
            'filename': '',
            'tipo': 'Avulso',
            'data': '',
        })

    # 2. Busca todos os documentos da página de pareceres/substitutivos
    try:
        url = f"https://www.camara.leg.br/proposicoesWeb/prop_pareceres_substitutivos_votos?idProposicao={id_proposicao}"
        r = requests.get(url, headers=headers, timeout=12)
        logger.info(f"Página pareceres {id_proposicao}: status={r.status_code}, size={len(r.text)}")
        if r.ok:
            soup = BeautifulSoup(r.text, 'html.parser')
            for row in soup.find_all('tr'):
                cols = row.find_all('td')
                if len(cols) < 3:
                    continue
                sigla = cols[0].get_text(strip=True)
                tipo  = cols[1].get_text(strip=True)
                data  = cols[2].get_text(strip=True)
                if not sigla:
                    continue
                for a in row.find_all('a', href=True):
                    href = a['href']
                    if 'codteor' not in href.lower():
                        continue
                    url_doc = camara_url(href)
                    if url_doc in vistos:
                        continue
                    vistos.add(url_doc)
                    label = f"📋 {sigla} — {tipo}"
                    if data:
                        label += f" ({data})"
                    docs.append({
                        'label':    label,
                        'url':      url_doc,
                        'filename': sigla,
                        'tipo':     tipo,
                        'data':     data,
                    })
    except Exception as e:
        logger.warning(f"Erro ao buscar pareceres: {e}")

    logger.info(f"Total documentos: {len(docs)} — {[d['label'] for d in docs]}")
    return docs

def extrair_texto_documento(url_doc):
    """Baixa PDF e extrai texto completo."""
    import pdfplumber
    from io import BytesIO

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36',
        'Accept': 'application/pdf,*/*',
        'Referer': 'https://www.camara.leg.br/',
    }

    # Garante URL correta
    if url_doc.startswith('http') and '/proposicoesWeb/' not in url_doc and 'codteor' in url_doc:
        m_ct = re.search(r'codteor=(\d+)', url_doc)
        if m_ct:
            codteor = m_ct.group(1)
            url_doc = f"https://www.camara.leg.br/proposicoesWeb/prop_mostrarintegra?codteor={codteor}"

    m_ct = re.search(r'codteor=(\d+)', url_doc)
    codteor = m_ct.group(1) if m_ct else None

    url_pdf = url_doc + ('&' if '?' in url_doc else '?') + 'tipo=PDF'
    urls_tentar = [url_pdf]
    if codteor:
        urls_tentar.append(
            f"https://www.camara.leg.br/proposicoesWeb/prop_mostrarintegra?codteor={codteor}&tipo=PDF"
        )

    for url in urls_tentar:
        try:
            logger.info(f"Tentando PDF: {url[:120]}")
            rp = requests.get(url, headers=headers, timeout=60, allow_redirects=True)
            logger.info(f"  → status={rp.status_code}, CT={rp.headers.get('Content-Type','')[:40]}, size={len(rp.content)}")

            if not rp.ok or len(rp.content) < 1000:
                continue
            ct = rp.headers.get('Content-Type', '').lower()
            if 'pdf' not in ct and not rp.content[:4] == b'%PDF':
                continue

            with pdfplumber.open(BytesIO(rp.content)) as pdf:
                n_pags = len(pdf.pages)
                partes = []
                for page in pdf.pages:
                    txt = page.extract_text()
                    if txt:
                        partes.append(txt)
                    else:
                        # Tenta extract_text com layout para PDFs complexos
                        txt2 = page.extract_text(layout=True)
                        if txt2:
                            partes.append(txt2)
                texto = '\n'.join(partes).strip()

            logger.info(f"  ✅ Extraído: {len(texto)} chars de {n_pags} páginas")

            if len(texto) < 100 and n_pags > 0:
                logger.warning(f"  ⚠️ PDF com poucas chars — pode ser PDF de imagem (escaneado)")
                # Retorna aviso no texto para a IA
                return f"[PDF escaneado — texto não extraível automaticamente. O documento tem {n_pags} páginas.]\n\nURL: {url}"

            return texto
        except Exception as e:
            logger.warning(f"  ❌ Erro em {url[:80]}: {e}")
            continue

    logger.warning(f"Nenhuma URL funcionou para extrair PDF")
    return None

@app.route('/extrair_texto_doc', methods=['POST'])
@login_required
def extrair_texto_doc():
    """Extrai e retorna o texto de um PDF para visualização."""
    data    = request.get_json()
    url_doc = data.get('url_documento', '')
    if not url_doc:
        return jsonify({'texto': '', 'erro': 'URL não fornecida'})
    texto = extrair_texto_documento(url_doc) or '(texto não extraído — verifique se o PDF está acessível)'
    return jsonify({'texto': texto, 'chars': len(texto)})

@app.route('/listar_documentos/<int:id_prop>')
@login_required
def listar_documentos(id_prop):
    docs = buscar_documentos_disponiveis(id_prop)
    return jsonify({'documentos': docs})

@app.route('/gerar_quadro_dtq', methods=['POST'])
@login_required
def gerar_quadro_dtq():
    """Gera conteúdo do quadro DTQ: sim/não e explicação."""
    data         = request.get_json()
    projeto      = data.get('projeto', '')
    numero       = data.get('numero', '')
    descricao    = data.get('descricao', '')
    analise_html = data.get('analise_html', '')
    url_doc_sel  = data.get('url_documento', '')
    label_doc    = data.get('label_documento', '')

    gemini_key = os.environ.get('GEMINI_API_KEY', '')
    groq_key   = os.environ.get('GROQ_API_KEY', '')

    # Detecta se é destaque de emenda
    descricao_upper = descricao.upper()
    eh_emenda = any(p in descricao_upper for p in ['EMENDA', 'EMD', 'SUBEMENDA'])

    if eh_emenda:
        regra_sim_nao = """REGRA FUNDAMENTAL para destaques de EMENDA:
- O destaque quer VOTAR A EMENDA EM SEPARADO para aprová-la
- Voto SIM = APROVA a emenda → ALTERA o texto do relator (acata a emenda)
- Voto NÃO = REJEITA a emenda → MANTÉM o texto do relator"""
        sim_label_default = "Aprova a emenda / Altera o texto do relator"
        nao_label_default = "Rejeita a emenda / Mantém o texto do relator"
    else:
        regra_sim_nao = """REGRA FUNDAMENTAL dos destaques de votação em separado:
- Voto SIM = MANTÉM o texto do relator (aprovado como está)
- Voto NÃO = ALTERA ou SUPRIME o texto do relator"""
        sim_label_default = "Mantém o texto do relator"
        nao_label_default = "Altera o texto do relator"

    prompt = f"""Você é um assessor legislativo especializado na Câmara dos Deputados do Brasil.

**Proposição:** {projeto}
**Destaque:** {numero}
**Descrição:** {descricao}
**Análise já realizada:** {analise_texto}

{regra_sim_nao}

Com base na descrição e análise acima, gere APENAS um JSON válido (sem markdown, sem explicações):

{{
  "titulo": "{projeto} – [título curto da proposição, máx 60 chars]",
  "dtq": "{numero} - [autoria resumida]",
  "descricao": "[descrição resumida do destaque, máx 120 chars]",
  "sim_label": "{sim_label_default}",
  "sim_conteudo": "[O que significa votar SIM — efeito prático em 1-2 frases curtas]",
  "nao_label": "{nao_label_default}",
  "nao_conteudo": "[O que significa votar NÃO — efeito prático em 1-2 frases curtas]",
  "explicacao": "[Explicação completa do dispositivo destacado e impacto, 3-5 frases]"
}}

Responda APENAS com o JSON, sem ```json, sem comentários."""

    if gemini_key:
        try:
            r = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent?key={gemini_key}",
                headers={"Content-Type": "application/json"},
                json={"contents": [{"parts": [{"text": prompt}]}],
                      "generationConfig": {"maxOutputTokens": 512, "temperature": 0.2}},
                timeout=30
            )
            r.raise_for_status()
            texto = r.json()['candidates'][0]['content']['parts'][0]['text']
            texto = re.sub(r'```(?:json)?|```', '', texto).strip()
            dados = json.loads(texto)
            return jsonify({'ok': True, 'dados': dados})
        except Exception as e:
            status = getattr(getattr(e, 'response', None), 'status_code', None)
            if status == 429:
                return jsonify({'ok': False, 'error': 'Limite de requisições atingido. Aguarde alguns segundos.'}), 429
            logger.warning(f"Erro ao gerar quadro DTQ: {e}")

    return jsonify({'ok': False, 'error': 'Falha na IA — verifique a chave Gemini.'}), 500

@app.route('/buscar_votos/<int:evento_id>')
@login_required
def buscar_votos(evento_id):
    """Busca resultado das votações do evento via scraping."""
    from bs4 import BeautifulSoup
    headers = {'User-Agent': 'Mozilla/5.0 Chrome/124.0.0.0 Safari/537.36'}
    votacoes = []
    try:
        # Tenta API aberta primeiro
        r = requests.get(
            f"https://dadosabertos.camara.leg.br/api/v2/votacoes?idEvento={evento_id}&itens=50&ordem=DESC",
            headers={'Accept': 'application/json', 'User-Agent': 'Mozilla/5.0 Chrome/124.0.0.0'},
            timeout=10
        )
        if r.ok:
            for v in r.json().get('dados', []):
                votacoes.append({
                    'id':         v.get('id', ''),
                    'descricao':  v.get('descricao', '') or '',
                    'proposicao': v.get('proposicaoObjeto', '') or '',
                    'sim':        v.get('totalVotosSim', ''),
                    'nao':        v.get('totalVotosNao', ''),
                    'abstencao':  v.get('totalVotosAbstencao', '') or 0,
                    'aprovado':   v.get('aprovado', None),
                })
            if votacoes:
                return jsonify({'votacoes': votacoes, 'total': len(votacoes)})
    except Exception as e:
        logger.warning(f"API votações falhou: {e}")

    # Fallback: scraping da página de votações
    try:
        url = f"https://www.camara.leg.br/presenca-comissoes/votacao-portal?reuniao={evento_id}"
        r2 = requests.get(url, headers=headers, timeout=12)
        if r2.ok:
            soup = BeautifulSoup(r2.text, 'html.parser')
            # Procura dados de votação na página
            for row in soup.find_all('tr'):
                cols = row.find_all('td')
                if len(cols) < 3:
                    continue
                texto = ' '.join(c.get_text(strip=True) for c in cols)
                # Procura padrões SIM/NAO
                m_sim = re.search(r'(\d+)\s*sim', texto, re.IGNORECASE)
                m_nao = re.search(r'(\d+)\s*n[ãa]o', texto, re.IGNORECASE)
                if m_sim or m_nao:
                    votacoes.append({
                        'descricao':  cols[0].get_text(strip=True) if cols else '',
                        'proposicao': texto[:80],
                        'sim':        m_sim.group(1) if m_sim else '',
                        'nao':        m_nao.group(1) if m_nao else '',
                        'abstencao':  '',
                    })
    except Exception as e:
        logger.warning(f"Scraping votações falhou: {e}")

    if votacoes:
        return jsonify({'votacoes': votacoes, 'total': len(votacoes)})

    # Se nada funcionar, retorna vazio para o frontend pedir manual
    return jsonify({'votacoes': [], 'total': 0, 'info': 'Votos não encontrados automaticamente — insira manualmente.'})

@app.route('/debug_destaque', methods=['POST'])
@login_required
def debug_destaque():
    """Debug: mostra texto extraído e trecho localizado para análise de destaque."""
    data         = request.get_json()
    url_doc_sel  = data.get('url_documento', '')
    descricao    = data.get('descricao', '')

    texto_doc = extrair_texto_documento(url_doc_sel) if url_doc_sel else ''

    refs_leis = re.findall(r'[Ll]ei\s+(?:n[º°.]?\s*)?([\d.]+)[/\-](\d{4})', descricao)
    texto_relevante = ''
    busca_info = []

    if texto_doc and refs_leis:
        for num_lei, ano_lei in refs_leis:
            num_limpo = num_lei.replace('.', '')
            for padrao in [num_limpo, num_lei, f"{num_limpo[:1]}.{num_limpo[1:]}"]:
                m = re.search(re.escape(padrao), texto_doc)
                if m:
                    ini = max(0, m.start() - 200)
                    fim = min(len(texto_doc), m.end() + 3000)
                    texto_relevante = texto_doc[ini:fim]
                    busca_info.append(f"✅ Encontrado '{padrao}' na posição {m.start()}")
                    break
                else:
                    busca_info.append(f"❌ Não encontrado '{padrao}'")

    return jsonify({
        'total_chars':      len(texto_doc),
        'primeiros_500':    texto_doc[:500],
        'texto_completo':   texto_doc[:50000],  # até 50k chars para o popup
        'refs_extraidas':   refs_leis,
        'busca_info':       busca_info,
        'trecho_relevante': texto_relevante[:3000] if texto_relevante else '(não localizado)',
    })

def buscar_texto_emenda(id_proposicao, descricao):
    """
    Busca o texto da emenda referenciada na descrição do destaque.
    Acessa prop_emendas e extrai o PDF da emenda específica.
    """
    from bs4 import BeautifulSoup
    import pdfplumber
    from io import BytesIO

    headers = {'User-Agent': 'Mozilla/5.0 Chrome/124.0.0.0 Safari/537.36'}

    # Extrai número da emenda da descrição
    # Ex: "Emenda nº 5", "EMD 12", "Emenda Aglutinativa nº 3"
    m_num = re.search(r'(?:EMD|Emenda|EMENDA)\s+(?:n[º°.]?\s*)?(\d+)', descricao, re.IGNORECASE)
    num_emenda = m_num.group(1) if m_num else None

    try:
        url = f"https://www.camara.leg.br/proposicoesWeb/prop_emendas?idProposicao={id_proposicao}&subst=0"
        r = requests.get(url, headers=headers, timeout=12)
        if not r.ok:
            logger.warning(f"Página de emendas retornou {r.status_code}")
            return None, None

        soup = BeautifulSoup(r.text, 'html.parser')

        # Coleta todas as emendas com seus links
        emendas = []
        for row in soup.find_all('tr'):
            txt = row.get_text(' ', strip=True)
            # Identifica linha de emenda
            m = re.search(r'(?:EMD|Emenda)\s*[Nn]?[º°.]?\s*(\d+)', txt, re.IGNORECASE)
            if not m:
                continue
            num = m.group(1)
            # Pega o link do PDF
            link = row.find('a', href=True)
            if link:
                href = link['href']
                url_doc = camara_url(href)
                emendas.append((num, url_doc, txt[:120]))

        if not emendas:
            # Tenta links diretos com codteor
            for a in soup.find_all('a', href=True):
                if 'codteor' in a['href']:
                    txt_ctx = a.get_text(strip=True)
                    m = re.search(r'(\d+)', txt_ctx)
                    num = m.group(1) if m else str(len(emendas)+1)
                    emendas.append((num, camara_url(a['href']), txt_ctx[:80]))

        logger.info(f"Emendas encontradas para {id_proposicao}: {[(e[0],e[2]) for e in emendas]}")

        # Seleciona a emenda correta pelo número
        emenda_sel = None
        if num_emenda:
            emenda_sel = next((e for e in emendas if e[0] == num_emenda), None)
        if not emenda_sel and emendas:
            emenda_sel = emendas[0]  # fallback: primeira

        if not emenda_sel:
            return None, None

        num_sel, url_emenda, label_sel = emenda_sel
        logger.info(f"Emenda selecionada: nº {num_sel} — {url_emenda}")

        # Extrai texto do PDF da emenda
        url_pdf = url_emenda + ('&' if '?' in url_emenda else '?') + 'tipo=PDF'
        rp = requests.get(url_pdf, headers=headers, timeout=20)
        if not rp.ok or 'pdf' not in rp.headers.get('Content-Type','').lower():
            # Tenta sem tipo=PDF
            rp = requests.get(url_emenda, headers=headers, timeout=20)

        if rp.ok and len(rp.content) > 500:
            with pdfplumber.open(BytesIO(rp.content)) as pdf:
                texto = '\n'.join(p.extract_text() or '' for p in pdf.pages).strip()
            if texto:
                return texto, f"Emenda nº {num_sel}"

    except Exception as e:
        logger.warning(f"Erro ao buscar emenda {id_proposicao}: {e}")

    return None, None


@app.route('/analisar_destaque', methods=['POST'])
@login_required
def analisar_destaque():
    data         = request.get_json()
    id_principal = data.get('id_principal', '')
    descricao    = data.get('descricao', '')
    numero       = data.get('numero', '')
    projeto      = data.get('projeto', '')
    url_doc_sel  = data.get('url_documento', '')
    label_doc    = data.get('label_documento', '')
    trecho_manual = data.get('trecho_manual', '')  # trecho selecionado manualmente pelo usuário

    gemini_key = os.environ.get('GEMINI_API_KEY', '')
    groq_key   = os.environ.get('GROQ_API_KEY', '')

    # ── Destaque de emenda: busca e analisa o texto da emenda diretamente ──
    descricao_upper = descricao.upper()
    eh_emenda = any(p in descricao_upper for p in ['EMENDA', 'EMD', 'SUBEMENDA'])

    if eh_emenda and id_principal and not trecho_manual:
        texto_emenda, label_emenda = buscar_texto_emenda(id_principal, descricao)
        if texto_emenda:
            tipo_doc = label_emenda or 'Emenda'
            regra = ('REGRA: Voto SIM = APROVA a emenda → altera texto do relator. '
                     'Voto NÃO = REJEITA a emenda → mantém texto do relator.')
            prompt_emenda = f"""Você é um assessor legislativo da Câmara dos Deputados.

**Proposição:** {projeto}
**Destaque:** {numero} — {descricao}
**Documento:** {tipo_doc}

TEXTO DA EMENDA:
{texto_emenda[:8000]}

{regra}

Gere análise em HTML:

<p><strong>Objeto:</strong> [o que a emenda propõe alterar em 1 frase]</p>
<br>
<p><strong>Texto da Emenda:</strong></p>
<blockquote style="border-left:3px solid #1A6B3A;padding-left:10px;color:#333;font-style:italic;">
[trecho principal da emenda, literalmente]
</blockquote>
<br>
<p><strong>Voto SIM (aprova a emenda):</strong><br>[efeito prático — o que muda. Máx 80 palavras.]</p>
<br>
<p><strong>Voto NÃO (rejeita a emenda):</strong><br>[texto do relator prevalece — impacto. Máx 60 palavras.]</p>

Não use ### ou ** fora do HTML."""

            if gemini_key:
                try:
                    r = requests.post(
                        f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent?key={gemini_key}",
                        headers={"Content-Type": "application/json"},
                        json={"contents": [{"parts": [{"text": prompt_emenda}]}],
                              "generationConfig": {"maxOutputTokens": 512, "temperature": 0.3}},
                        timeout=30
                    )
                    r.raise_for_status()
                    texto_resp = r.json()['candidates'][0]['content']['parts'][0]['text']
                    return jsonify({'resumo': texto_resp, 'doc_usado': tipo_doc})
                except Exception as e:
                    status = getattr(getattr(e, 'response', None), 'status_code', None)
                    if status == 429:
                        return jsonify({'error': 'Limite de requisições. Aguarde e tente novamente.'}), 429
                    logger.warning(f"Gemini falhou (emenda): {e}")
            return jsonify({'error': 'Falha ao analisar emenda.'}), 500

    # ── Fluxo normal (não emenda) ───────────────────────────────────────────
    if trecho_manual:
        texto_doc = trecho_manual
        tipo_doc  = f"{label_doc} (trecho selecionado manualmente)"
        texto_truncado = trecho_manual
        refs_leis = re.findall(r'[Ll]ei\s+(?:n[º°.]?\s*)?([\d.]+)[/\-](\d{4})', descricao)
        nota_refs = ''
        if refs_leis:
            nota_refs = f"\n**Leis referenciadas:** {', '.join([f'Lei {n}/{a}' for n,a in refs_leis])}"
    else:
        # Extrai texto do documento selecionado
        texto_doc = ''
        tipo_doc  = label_doc or 'documento selecionado'
        if url_doc_sel:
            texto_doc = extrair_texto_documento(url_doc_sel) or ''
            if texto_doc.startswith('[PDF escaneado') and id_principal:
                logger.info("PDF escaneado — tentando PRLP como fallback")
                doc_fb = buscar_texto_prlp_ou_sbt(id_principal)
                if doc_fb and doc_fb.get('texto'):
                    texto_doc = doc_fb['texto']
                    tipo_doc = f"{doc_fb.get('tipo','')} nº {doc_fb.get('numero','')} (fallback)"
            if not texto_doc or texto_doc.startswith('[PDF escaneado'):
                return jsonify({'error': f'O PDF selecionado não possui texto extraível. Use o botão Debug para selecionar o trecho manualmente.'}), 400
        elif id_principal:
            doc = buscar_texto_prlp_ou_sbt(id_principal)
            if doc:
                texto_doc = doc.get('texto', '')
                tipo_doc  = f"{doc.get('tipo','')} nº {doc.get('numero','')} de {doc.get('data','')}"

        # Extrai refs de leis e localiza trecho relevante
        refs_leis = re.findall(r'[Ll]ei\s+(?:n[º°.]?\s*)?([\d.]+)[/\-](\d{4})', descricao)
        nota_refs = ''
        if refs_leis:
            leis_str = ', '.join([f"Lei {n.replace('.','')}/{a}" for n, a in refs_leis])
            nota_refs = f"\n**Leis referenciadas no destaque:** {leis_str} (busque variações como 'Lei nº {refs_leis[0][0]}, de' no texto)"

        texto_relevante = ''
        if texto_doc and refs_leis:
            for num_lei, ano_lei in refs_leis:
                num_limpo = num_lei.replace('.', '')
                variacoes = list(set([num_limpo, num_lei,
                    '.'.join([num_limpo[:-3], num_limpo[-3:]]) if len(num_limpo) >= 4 else num_limpo]))
                logger.info(f"Buscando Lei variações {variacoes} em {len(texto_doc)} chars")
                melhor_pos = None
                for variacao in variacoes:
                    padrao_flex = variacao.replace('.', r'[.\s]?')
                    for m in re.finditer(padrao_flex, texto_doc):
                        pos = m.start()
                        if melhor_pos is None or pos > melhor_pos:
                            melhor_pos = pos
                if melhor_pos is not None:
                    ini = max(0, melhor_pos - 500)
                    fim = min(len(texto_doc), melhor_pos + 4000)
                    texto_relevante = texto_doc[ini:fim]
                    logger.info(f"Trecho: {ini}-{fim} ({len(texto_relevante)} chars)")
                    break

        texto_truncado = texto_relevante if texto_relevante else texto_doc[:12000]

    prompt = f"""Você é um assessor legislativo especializado na Câmara dos Deputados do Brasil.

**Proposição:** {projeto}
**Destaque:** {numero}
**Descrição do Destaque:** {descricao}{nota_refs}
**Documento analisado:** {tipo_doc}

TEXTO COMPLETO DO DOCUMENTO:
{texto_truncado if texto_truncado else '(texto não disponível)'}

---
INSTRUÇÕES PARA LOCALIZAR O TRECHO:

A descrição do destaque menciona leis, artigos ou dispositivos específicos. Para localizá-los:

1. **Matching flexível de leis**: A descrição pode mencionar "Lei 9.096/1995" mas no texto pode aparecer como "Lei nº 9.096, de 19 de setembro de 1995" ou "Lei 9.096/95". São a mesma lei — use apenas o número para localizar.

2. **Se o destaque menciona "art. X da Lei Y"**: procure no texto por:
   - O artigo que ALTERA esse dispositivo: "Art. 2º O art. X da Lei nº Y..."
   - Ou diretamente o artigo numerado no texto
   - Extraia o trecho que está sendo destacado para votação em separado

3. **Se o destaque menciona "art. X do substitutivo/texto"**: procure diretamente "Art. Xº" no texto

4. **Copie LITERALMENTE** o trecho encontrado, incluindo caput, incisos e parágrafos relevantes

Gere a análise em HTML com EXATAMENTE este formato:

<p><strong>Objeto do Destaque:</strong> [descreva em uma frase o que o destaque vota em separado]</p>
<br>
<p><strong>Trecho do Texto:</strong></p>
<blockquote style="border-left:3px solid #1A6B3A; padding-left:10px; color:#333; font-style:italic;">
[Trecho literal encontrado. Se usou matching flexível, indique: "Lei X mencionada no destaque corresponde a 'Lei nº X, de DD de mês de AAAA' no documento". Se não localizar mesmo com busca flexível, explique qual número buscou.]
</blockquote>
<br>
<p><strong>Análise:</strong><br>
[Explique o que esse trecho propõe e o impacto prático de aprovar ou rejeitar este destaque. Máx 150 palavras.]
</p>

Não use ### ou ** fora do HTML."""

    if gemini_key:
        try:
            r = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent?key={gemini_key}",
                headers={"Content-Type": "application/json"},
                json={"contents": [{"parts": [{"text": prompt}]}],
                      "generationConfig": {"maxOutputTokens": 512, "temperature": 0.3}},
                timeout=30
            )
            r.raise_for_status()
            texto = r.json()['candidates'][0]['content']['parts'][0]['text']
            return jsonify({'resumo': texto, 'doc_usado': tipo_doc})
        except Exception as e:
            status = getattr(getattr(e, 'response', None), 'status_code', None)
            if status == 429:
                return jsonify({'error': 'Limite de requisições atingido. Aguarde alguns segundos e tente novamente.'}), 429
            logger.warning(f"Gemini falhou em analisar_destaque: {e}")

    return jsonify({'error': 'Falha ao gerar análise. Tente novamente.'}), 500

@app.route('/buscar_url_prlp', methods=['POST'])
@login_required
def buscar_url_prlp():
    """Encontra URL do PDF do PRLP específico pelo número."""
    data         = request.get_json()
    id_prop      = data.get('id_proposicao', '')
    numero_prlp  = str(data.get('numero_prlp', ''))

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml',
        'Accept-Language': 'pt-BR,pt;q=0.9',
    }

    # Estratégia 1: página de pareceres (scraping)
    try:
        from bs4 import BeautifulSoup
        url_pag = f"https://www.camara.leg.br/proposicoesWeb/prop_pareceres_substitutivos_votos?idProposicao={id_prop}"
        r = requests.get(url_pag, headers=headers, timeout=15)
        if r.ok:
            soup = BeautifulSoup(r.text, 'html.parser')
            # Procura linha que menciona PRLP + número correto
            for row in soup.find_all('tr'):
                txt = row.get_text(' ', strip=True).upper()
                if f'PRLP' not in txt:
                    continue
                # Verifica se tem o número correto
                m = re.search(r'PRLP\s*[Nnº°.\s]*(\d+)', txt)
                if not m or m.group(1) != numero_prlp:
                    continue
                # Pega o link
                for a in row.find_all('a', href=True):
                    href = a['href']
                    if 'codteor' in href.lower():
                        url_doc = camara_url(href)
                        url_pdf = url_doc + ('&' if '?' in url_doc else '?') + 'tipo=PDF'
                        logger.info(f"PRLP {numero_prlp} encontrado: {url_pdf}")
                        return jsonify({'url_pdf': url_pdf})
    except Exception as e:
        logger.warning(f"Erro buscar_url_prlp estrategia1: {e}")

    # Estratégia 2: ficha de tramitação
    try:
        url_tram = f"https://www.camara.leg.br/proposicoesWeb/fichadetramitacao?idProposicao={id_prop}"
        r = requests.get(url_tram, headers=headers, timeout=15)
        if r.ok:
            soup = BeautifulSoup(r.text, 'html.parser')
            # Busca todos os links com PRLP no filename
            prlps = []
            for a in soup.find_all('a', href=True):
                href = a['href']
                fn_m = re.search(r'filename=([^&"]+)', href)
                fn = (fn_m.group(1) if fn_m else '').upper()
                if 'PRLP' in fn or 'PRLP' in href.upper():
                    m_num = re.search(r'PRLP[^\d]*(\d+)', fn or href, re.IGNORECASE)
                    num = int(m_num.group(1)) if m_num else 0
                    url_doc = camara_url(href)
                    prlps.append((num, url_doc))
            if prlps:
                # Pega o que tem o número correto, ou o maior
                alvo = [p for p in prlps if str(p[0]) == numero_prlp]
                escolhido = alvo[0] if alvo else sorted(prlps, key=lambda x: x[0], reverse=True)[0]
                url_pdf = escolhido[1] + ('&' if '?' in escolhido[1] else '?') + 'tipo=PDF'
                return jsonify({'url_pdf': url_pdf})
    except Exception as e:
        logger.warning(f"Erro buscar_url_prlp estrategia2: {e}")

    return jsonify({'url_pdf': None})

@app.route('/verificar_doc/<int:id_prop>')
@login_required
def verificar_doc(id_prop):
    """Retorna tipo, número, data e URL do último PRLP/Substitutivo de plenário."""
    try:
        doc = buscar_texto_prlp_ou_sbt(id_prop)
        if doc:
            return jsonify({
                'tipo':      doc.get('tipo'),
                'numero':    doc.get('numero'),
                'data':      doc.get('data'),
                'tem_texto': bool(doc.get('texto')),
                'url_pdf':   doc.get('url_pdf', '')
            })
        return jsonify({'tipo': None, 'data': None, 'numero': None, 'url_pdf': ''})
    except Exception as e:
        logger.error(f"Erro verificar_doc {id_prop}: {e}", exc_info=True)
        return jsonify({'tipo': None, 'data': None, 'numero': None, 'url_pdf': '', 'erro': str(e)})

@app.route('/debug_docs/<path:codigo>')
@login_required
def debug_docs(codigo):
    """Debug dos documentos. Aceita id numérico ou 'PL-1054-2019'."""
    headers = {'Accept': 'application/json', 'User-Agent': 'Mozilla/5.0 Chrome/124.0.0.0'}
    resultado = {'codigo': codigo}

    # Resolve id
    id_prop = None
    if '-' in str(codigo):
        partes = str(codigo).split('-')
        if len(partes) == 3:
            sigla, numero, ano = partes
            # Parâmetro correto é siglaTipo
            url_busca = f"https://dadosabertos.camara.leg.br/api/v2/proposicoes?siglaTipo={sigla}&numero={numero}&ano={ano}&itens=1"
            try:
                r = requests.get(url_busca, headers=headers, timeout=10)
                resultado['busca_status'] = r.status_code
                resultado['busca_url'] = url_busca
                if r.ok:
                    dados = r.json().get('dados', [])
                    resultado['busca_dados'] = dados
                    if dados:
                        id_prop = dados[0].get('id')
            except Exception as e:
                resultado['busca_erro'] = str(e)
    else:
        id_prop = int(codigo)

    resultado['id_prop'] = id_prop
    if not id_prop:
        return jsonify(resultado)

    # Testa vários endpoints
    urls = [
        f"https://dadosabertos.camara.leg.br/api/v2/proposicoes/{id_prop}/documentos?itens=10&ordem=DESC",
        f"https://dadosabertos.camara.leg.br/api/v2/proposicoes/{id_prop}/textos",
        f"https://dadosabertos.camara.leg.br/api/v2/proposicoes/{id_prop}",
    ]
    resultado['endpoints'] = []
    for url in urls:
        try:
            r = requests.get(url, headers=headers, timeout=10)
            body = r.json() if r.ok else r.text[:200]
            resultado['endpoints'].append({
                'url': url.split('camara.leg.br')[1],
                'status': r.status_code,
                'body': body if isinstance(body, dict) else body
            })
        except Exception as e:
            resultado['endpoints'].append({'url': url.split('camara.leg.br')[1], 'erro': str(e)})

    resultado['parecer'] = buscar_ultimo_parecer(id_prop)

    # Debug da página de tramitação
    headers = {'User-Agent': 'Mozilla/5.0 Chrome/124.0.0.0 Safari/537.36'}
    url_tram = f"https://www.camara.leg.br/proposicoesWeb/fichadetramitacao?idProposicao={id_prop}"
    try:
        from bs4 import BeautifulSoup
        r_tram = requests.get(url_tram, headers=headers, timeout=12)
        resultado['tram_status'] = r_tram.status_code
        resultado['tram_url'] = url_tram
        if r_tram.ok:
            soup = BeautifulSoup(r_tram.text, 'html.parser')
            # Todos os links com codteor
            links_codteor = []
            for a in soup.find_all('a', href=True):
                href = a['href']
                txt  = a.get_text(strip=True)
                if 'codteor' in href.lower() or 'mostrarintegra' in href.lower():
                    links_codteor.append({'texto': txt[:60], 'href': href[:120]})
            resultado['links_codteor'] = links_codteor[:20]
            # Texto bruto com PRLP ou SBT
            texto_pag = soup.get_text()
            prlp_mencoes = [l.strip() for l in texto_pag.split('\n') if 'PRLP' in l.upper() or 'SBT' in l.upper() or 'SUBSTITUT' in l.upper()]
            resultado['mencoes_prlp_sbt'] = prlp_mencoes[:10]
    except Exception as e:
        resultado['tram_erro'] = str(e)

    resultado['texto_prlp_sbt'] = buscar_texto_prlp_ou_sbt(id_prop)
    return jsonify(resultado)

@app.route('/debug_matching/<int:evento_id>')
@login_required
def debug_matching(evento_id):
    """Mostra exatamente como os códigos da API batem com a ordem do PDF."""
    itens, _ = fetch_pauta(evento_id)
    ordem = buscar_ordem_oficial(evento_id)
    
    resultado = []
    for item in itens:
        proj_orig = item.get('projeto_original') or item.get('projeto', '')
        proj_base = proj_orig.split(' ao ')[0].strip()
        cod_norm  = _normalizar_codigo(proj_base)
        pos_pdf   = ordem.get(cod_norm, 'NÃO ENCONTRADO')
        resultado.append({
            'ordem_app':      item.get('ordem'),
            'projeto_orig':   proj_orig,
            'projeto_base':   proj_base,
            'cod_normalizado': cod_norm,
            'posicao_pdf':    pos_pdf,
        })
    
    return jsonify({
        'ordem_pdf': ordem,
        'itens_api': resultado
    })

@app.route('/debug_ordem/<int:evento_id>')
@login_required
def debug_ordem(evento_id):
    """Debug da extração de ordem oficial do PDF por coordenadas."""
    resultado = {'evento_id': evento_id, 'etapas': []}
    try:
        from bs4 import BeautifulSoup
        import pdfplumber
        from io import BytesIO

        # 1. Página do evento
        url = f"https://www.camara.leg.br/evento-legislativo/{evento_id}"
        r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0 Chrome/124.0.0.0 Safari/537.36'}, timeout=12)
        resultado['etapas'].append({'etapa': '1_evento', 'status': r.status_code})
        if not r.ok:
            return jsonify(resultado)

        # 2. Acha PDF de Pauta
        soup = BeautifulSoup(r.text, 'html.parser')
        pdf_url = None
        for a in soup.find_all('a', href=re.compile(r'codteor=\d+', re.I)):
            if a.get_text(strip=True).lower() == 'pauta':
                href = a['href']
                pdf_url = (camara_url(href))
                pdf_url += ('&' if '?' in pdf_url else '?') + 'tipo=PDF'
                break
        resultado['etapas'].append({'etapa': '2_pdf_url', 'url': pdf_url})
        if not pdf_url:
            return jsonify(resultado)

        # 3. Baixa PDF
        rp = requests.get(pdf_url, headers={'User-Agent': 'Mozilla/5.0 Chrome/124.0.0.0 Safari/537.36'}, timeout=20)
        resultado['etapas'].append({'etapa': '3_download', 'status': rp.status_code, 'size': len(rp.content), 'ct': rp.headers.get('Content-Type','')})
        if not rp.ok:
            return jsonify(resultado)

        # 4. Extrai palavras com coordenadas
        numeros_centrais = []
        page_width = 595.0
        with pdfplumber.open(BytesIO(rp.content)) as pdf:
            page_width = float(pdf.pages[0].width) if pdf.pages else 595.0
            for pnum, page in enumerate(pdf.pages):
                words = page.extract_words(x_tolerance=3, y_tolerance=3)
                linhas = {}
                for w in words:
                    y = round(float(w['top']))
                    linhas.setdefault(y, []).append(w)
                ys = sorted(linhas.keys())
                for i, y in enumerate(ys):
                    ws = linhas[y]
                    # Procura palavra 1-2 dígitos centralizada (ignora resto da linha)
                    num_encontrado = None
                    for w in ws:
                        txt = w['text'].strip()
                        if not re.match(r'^\d{1,2}$', txt):
                            continue
                        centro_w = (float(w['x0']) + float(w['x1'])) / 2
                        if abs(centro_w - page_width / 2) <= page_width * 0.05:
                            num_encontrado = (int(txt), float(w['x0']), float(w['x1']))
                            break
                    if not num_encontrado:
                        continue
                    num, x0, x1 = num_encontrado
                    if num < 1 or num > 30:
                        continue
                    centro = (x0 + x1) / 2
                    dist_centro = abs(centro - page_width / 2)
                    margem = page_width * 0.20
                    # Próximas linhas
                    prox = ys[i+1:i+6]
                    bloco = ' '.join(' '.join(w['text'] for w in linhas[ny]) for ny in prox if ny in linhas)
                    # Mostra todas as palavras da linha (incluindo possíveis invisíveis)
                    palavras_linha_raw = [{'text': w['text'], 'x0': round(float(w['x0']),1), 'x1': round(float(w['x1']),1)} for w in ws]
                    numeros_centrais.append({
                        'num': num, 'page': pnum+1,
                        'centro_x': round(centro, 1),
                        'dist_centro': round(dist_centro, 1),
                        'margem_max': round(margem, 1),
                        'centralizado': dist_centro <= margem,
                        'palavras_na_linha': palavras_linha_raw,
                        'bloco_seguinte': bloco[:120]
                    })

        resultado['page_width'] = page_width
        resultado['numeros_encontrados'] = numeros_centrais

        # 5. Resultado final
        ordem = buscar_ordem_oficial(evento_id)
        resultado['ordem_extraida'] = ordem
        resultado['total'] = len(ordem)

    except Exception as e:
        import traceback
        resultado['erro'] = str(e)
        resultado['tb'] = traceback.format_exc()[-500:]
    return jsonify(resultado)

@app.route('/admin/limpar_todo_cache', methods=['POST'])
@login_required
def limpar_todo_cache():
    if current_user.role.lower() != 'admin':
        return jsonify({'error': 'Acesso negado'}), 403
    conn = get_conn()
    c = conn.cursor()
    c.execute('DELETE FROM pauta_cache_db')
    n = c.rowcount
    conn.commit()
    conn.close()
    pauta_cache.clear()
    return jsonify({'message': f'{n} eventos removidos do cache.'})

@app.route('/limpar_cache/<int:evento_id>', methods=['GET', 'POST'])
@login_required
def limpar_cache(evento_id):
    """Remove cache de um evento específico para forçar reprocessamento."""
    conn = get_conn()
    c = conn.cursor()
    try:
        c.execute('DELETE FROM pauta_cache_db WHERE evento_id = ?', (evento_id,))
        conn.commit()
        pauta_cache.pop(str(evento_id), None)
        pauta_cache.clear()  # limpa todo cache em memória para garantir
        logger.info(f"✅ Cache limpo para evento {evento_id}")
        if request.method == 'GET':
            return redirect(url_for('view_pauta', evento_id=evento_id, force_reload='true'))
        return jsonify({'message': f'Cache do evento {evento_id} limpo.'})
    except Exception as e:
        logger.error(f"Erro ao limpar cache: {e}")
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@app.route('/resumo_ementa', methods=['POST'])
@login_required
def resumo_ementa():
    return resumo_ementa_impl(request.get_json())

@app.route('/resumos_evento/<int:evento_id>')
@login_required
def resumos_evento(evento_id):
    conn = get_conn()
    c = conn.cursor()
    try:
        c.execute('SELECT id_proposicao, resumo FROM resumos_ia WHERE evento_id=?', (evento_id,))
        rows = c.fetchall()
        return jsonify({str(r[0]): r[1] for r in rows})
    except Exception:
        return jsonify({})
    finally:
        conn.close()

@app.route('/salvar_resumo_ia', methods=['POST'])
@login_required
def salvar_resumo_ia():
    data      = request.get_json()
    evento_id = data.get('evento_id')
    id_prop   = data.get('id_principal')
    resumo    = data.get('resumo', '')
    if not evento_id or not id_prop or not resumo:
        return jsonify({'ok': False})
    conn = get_conn()
    c = conn.cursor()
    try:
        c.execute('''CREATE TABLE IF NOT EXISTS resumos_ia (
            evento_id INTEGER, id_proposicao TEXT, resumo TEXT,
            PRIMARY KEY (evento_id, id_proposicao))''')
        c.execute('INSERT OR REPLACE INTO resumos_ia (evento_id, id_proposicao, resumo) VALUES (?,?,?)',
                  (evento_id, str(id_prop), resumo))
        conn.commit()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})
    finally:
        conn.close()

@app.route('/buscar_imagem_item', methods=['POST'])
@login_required
def buscar_imagem_item():
    """Usa IA para extrair keywords e busca imagem via Wikimedia Commons (gratuito)."""
    data      = request.get_json()
    resumo    = data.get('resumo', '')
    groq_key  = os.environ.get('GROQ_API_KEY', '')
    gemini_key = os.environ.get('GEMINI_API_KEY', '')

    # Extrai 2-3 palavras-chave do resumo para busca de imagem
    keywords = ''
    prompt_kw = f"""Extraia 2 ou 3 palavras-chave em inglês para buscar uma imagem que ilustre o tema desta proposição legislativa brasileira.
Responda APENAS com as palavras separadas por espaço, sem explicação.
Proposição: {resumo[:300]}"""

    for key, url, body_fn in [
        (gemini_key,
         f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent?key={gemini_key}",
         lambda: {"contents":[{"parts":[{"text":prompt_kw}]}],"generationConfig":{"maxOutputTokens":20,"temperature":0.1}}),
        (groq_key,
         "https://api.groq.com/openai/v1/chat/completions",
         lambda: {"model":"llama-3.3-70b-versatile","messages":[{"role":"user","content":prompt_kw}],"max_tokens":20,"temperature":0.1}),
    ]:
        if not key: continue
        try:
            headers = {"Content-Type": "application/json"}
            if 'groq' in url: headers["Authorization"] = f"Bearer {key}"
            r = requests.post(url, headers=headers, json=body_fn(), timeout=8)
            if r.ok:
                if 'generativelanguage' in url:
                    keywords = r.json()['candidates'][0]['content']['parts'][0]['text'].strip()
                else:
                    keywords = r.json()['choices'][0]['message']['content'].strip()
                keywords = re.sub(r'[^\w\s]', '', keywords).strip()
                break
        except Exception as e:
            logger.warning(f"Erro keywords imagem: {e}")

    if not keywords:
        keywords = 'brazil congress law'

    # Busca no Wikimedia Commons (API gratuita, sem key)
    try:
        r = requests.get(
            'https://en.wikipedia.org/api/rest_v1/page/summary/' + keywords.replace(' ', '_'),
            headers={'User-Agent': 'PlenarioApp/1.0'},
            timeout=6
        )
        if r.ok:
            thumb = r.json().get('thumbnail', {}).get('source', '')
            if thumb:
                return jsonify({'imagem_url': thumb, 'keywords': keywords})
    except Exception:
        pass

    # Fallback: Wikimedia Commons search
    try:
        r = requests.get(
            f"https://commons.wikimedia.org/w/api.php?action=query&generator=search&gsrsearch={requests.utils.quote(keywords)}&gsrlimit=1&prop=imageinfo&iiprop=url|mime&iiurlwidth=400&format=json",
            headers={'User-Agent': 'PlenarioApp/1.0'},
            timeout=8
        )
        if r.ok:
            pages = r.json().get('query', {}).get('pages', {})
            for page in pages.values():
                imgs = page.get('imageinfo', [])
                if imgs:
                    url_img = imgs[0].get('thumburl') or imgs[0].get('url', '')
                    if url_img:
                        return jsonify({'imagem_url': url_img, 'keywords': keywords})
    except Exception:
        pass

    return jsonify({'imagem_url': None, 'keywords': keywords})

def resumo_ementa_impl(data):
    """Gera resumo de até 3 linhas da ementa. Para REQ busca dados do PL na web."""
    projeto      = data.get('projeto', '')
    ementa       = data.get('ementa', '')
    autor        = data.get('autor', '')
    id_principal = data.get('id_principal', '')
    groq_key     = os.environ.get('GROQ_API_KEY', '')
    gemini_key   = os.environ.get('GEMINI_API_KEY', '')

    if not groq_key and not gemini_key:
        return jsonify({'resumo': ementa[:200]})

    # Para REQ ou qualquer proposição que mencione outro PL na ementa:
    # busca dados do PL referenciado para enriquecer o resumo
    contexto_pl = ''
    proj_base = projeto.split(' ao ')[0].strip()
    siglas_req = ('REQ', 'RQS', 'RQU', 'REC')
    eh_req = any(proj_base.upper().startswith(s) for s in siglas_req)

    # Busca referência ao PL na ementa ou no projeto
    m_pl = None
    num_ref = ano_ref = sigla_ref = ''

    # Padrão com ano: "PL nº 4.215/2023" ou "PL 4215, de 2023"
    for txt_busca in [ementa, projeto]:
        m_pl = re.search(r'\b(PL|PEC|PLP|MPV|PDL|PLC)\s+n[º°.]?\s*([\d.]+)[,\s/]+(?:de\s+)?(\d{4})', txt_busca, re.IGNORECASE)
        if m_pl: break
        m_pl = re.search(r'\b(PL|PEC|PLP|MPV|PDL|PLC)\s+([\d.]+)[/\-](\d{4})', txt_busca, re.IGNORECASE)
        if m_pl: break

    # Padrão sem ano: "PL nº 4.215" ou "Projeto de Lei nº 4.215"
    if not m_pl:
        for txt_busca in [ementa, projeto]:
            m_pl2 = re.search(r'\b(?:Projeto de Lei|PL|PEC|PLP|MPV|PDL)\s+n[º°.]?\s*([\d.]+)\b', txt_busca, re.IGNORECASE)
            if m_pl2:
                num_ref = m_pl2.group(1).replace('.', '').replace(',', '')
                sigla_ref = 'PL'
                ano_ref = ''
                break

    if m_pl:
        sigla_ref = m_pl.group(1).upper()
        num_ref   = m_pl.group(2).replace('.', '')
        ano_ref   = m_pl.group(3)

    if num_ref and (eh_req or sigla_ref):
        try:
            # Busca com ano se disponível, senão busca só pelo número
            if ano_ref:
                url_api = f"https://dadosabertos.camara.leg.br/api/v2/proposicoes?siglaTipo={sigla_ref or 'PL'}&numero={num_ref}&ano={ano_ref}&itens=1"
            else:
                url_api = f"https://dadosabertos.camara.leg.br/api/v2/proposicoes?numero={num_ref}&itens=1&ordem=DESC&ordenarPor=ano"
            r_api = requests.get(url_api, headers={'Accept': 'application/json'}, timeout=8)
            if r_api.ok:
                dados = r_api.json().get('dados', [])
                if dados:
                    ementa_pl  = dados[0].get('ementa', '')
                    sigla_real = dados[0].get('siglaTipo', sigla_ref)
                    num_real   = dados[0].get('numero', num_ref)
                    ano_real   = dados[0].get('ano', ano_ref)
                    if ementa_pl:
                        contexto_pl = f"\nO {sigla_real} {num_real}/{ano_real} (referenciado) trata de: {ementa_pl}"
                        logger.info(f"Contexto PL encontrado: {sigla_real} {num_real}/{ano_real}")
            if not contexto_pl:
                # API indisponível — usa a referência extraída da ementa para orientar a IA
                ref_str = f"{sigla_ref} {num_ref}" + (f"/{ano_ref}" if ano_ref else "")
                contexto_pl = f"\n(Referência ao {ref_str} — busque na ementa o contexto do projeto referenciado)"
        except Exception as e:
            logger.warning(f"Erro buscar PL referenciado: {e}")
            if num_ref:
                ref_str = f"{sigla_ref} {num_ref}" + (f"/{ano_ref}" if ano_ref else "")
                contexto_pl = f"\n(Referência ao {ref_str})"

    if contexto_pl and eh_req:
        prompt = f"""Você é um assessor legislativo da Câmara dos Deputados do Brasil.
Gere um resumo MUITO CURTO (máximo 2-3 linhas, máximo 180 caracteres) do que este REQUERIMENTO pede.
- Se for urgência: comece com "Urgência para o PL que [explique o PL em poucas palavras]"
- Se for adiamento/retirada: comece com "Adiamento/Retirada do PL que..."
- Use a ementa do requerimento e o contexto do PL referenciado
- Seja direto. Não repita número da proposição.

Requerimento: {projeto}
Ementa: {ementa}{contexto_pl}

Responda APENAS com o resumo, sem introdução, sem aspas."""
    else:
        prompt = f"""Você é um assessor legislativo da Câmara dos Deputados do Brasil.
Gere um resumo MUITO CURTO (máximo 2-3 linhas, máximo 180 caracteres) do que esta proposição trata na prática.
- Seja direto e claro para parlamentares
- Não repita o número da proposição
- Use linguagem simples e objetiva

Proposição: {projeto}
Autor: {autor}
Ementa: {ementa}

Responda APENAS com o resumo, sem introdução, sem aspas."""

    # Tenta Gemini primeiro, depois Groq
    for key, url, body_fn in [
        (gemini_key,
         f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent?key={gemini_key}",
         lambda: {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"maxOutputTokens": 100, "temperature": 0.2}}),
        (groq_key,
         "https://api.groq.com/openai/v1/chat/completions",
         lambda: {"model": "llama-3.3-70b-versatile", "messages": [{"role": "user", "content": prompt}], "max_tokens": 100, "temperature": 0.2}),
    ]:
        if not key:
            continue
        try:
            headers = {"Content-Type": "application/json"}
            if 'groq' in url:
                headers["Authorization"] = f"Bearer {key}"
            r = requests.post(url, headers=headers, json=body_fn(), timeout=15)
            if r.ok:
                if 'generativelanguage' in url:
                    texto = r.json()['candidates'][0]['content']['parts'][0]['text'].strip()
                else:
                    texto = r.json()['choices'][0]['message']['content'].strip()
                return jsonify({'resumo': texto})
        except Exception as e:
            logger.warning(f"Erro resumo_ementa: {e}")

    return jsonify({'resumo': ementa[:200]})

@app.route('/enriquecer_ementa', methods=['POST'])
@login_required
def enriquecer_ementa():
    """Retorna ementa original + complemento IA. Para REQ, busca ementa do PL referenciado."""
    data     = request.get_json()
    projeto  = data.get('projeto', '')
    ementa   = data.get('ementa', '').strip()
    autor    = data.get('autor', '')
    groq_key = os.environ.get('GROQ_API_KEY')

    # Para REQ/RQS/RQU/REC: busca ementa do PL referenciado
    siglas_req = ('REQ', 'RQS', 'RQU', 'REC')
    proj_base = projeto.split(' ao ')[0].strip()
    if any(proj_base.upper().startswith(s) for s in siglas_req):
        # Extrai referência ao PL na ementa
        m_pl = re.search(
            r'\b(PL|PEC|PLP|MPV|PDL)\s+n[º°.]?\s*([\d.]+)[,\s/]+(?:de\s+)?(\d{4})',
            ementa, re.IGNORECASE
        )
        if not m_pl:
            m_pl = re.search(r'\b(PL|PEC|PLP|MPV|PDL)\s+([\d.]+)[/\-](\d{4})', projeto, re.IGNORECASE)
        if m_pl:
            sigla_ref = m_pl.group(1).upper()
            num_ref   = m_pl.group(2).replace('.', '')
            ano_ref   = m_pl.group(3)
            ementa_pl = ''
            try:
                r_api = requests.get(
                    f"https://dadosabertos.camara.leg.br/api/v2/proposicoes?siglaTipo={sigla_ref}&numero={num_ref}&ano={ano_ref}&itens=1",
                    headers={'Accept': 'application/json'}, timeout=8
                )
                if r_api.ok:
                    dados = r_api.json().get('dados', [])
                    if dados:
                        ementa_pl = dados[0].get('ementa', '')
            except Exception:
                pass

            if ementa_pl and groq_key:
                prompt = f"""Você é um especialista legislativo. Explique em UMA frase direta (máx 20 palavras) o objeto deste requerimento para os parlamentares.

Requerimento: {projeto}
Ementa do requerimento: {ementa}
Ementa do {sigla_ref} {num_ref}/{ano_ref} referenciado: {ementa_pl}

Responda APENAS com a frase, sem introdução, sem aspas, sem ponto final."""
                try:
                    r = requests.post(
                        "https://api.groq.com/openai/v1/chat/completions",
                        headers={"Authorization": f"Bearer {groq_key}", "Content-Type": "application/json"},
                        json={"model": "llama-3.3-70b-versatile",
                              "messages": [{"role": "user", "content": prompt}],
                              "max_tokens": 60, "temperature": 0.2},
                        timeout=10
                    )
                    if r.ok:
                        comp = r.json()['choices'][0]['message']['content'].strip().rstrip('.')
                        return jsonify({'ementa_enriquecida': f"{ementa} ({comp})", 'complemento': comp})
                except Exception as e:
                    logger.warning(f"Erro enriquecer REQ: {e}")
            elif ementa_pl:
                comp = ementa_pl[:120].rstrip('.')
                return jsonify({'ementa_enriquecida': f"{ementa} ({comp})", 'complemento': comp})

        return jsonify({'ementa_enriquecida': ementa, 'complemento': ''})

    # Lógica original para não-REQ
    def ementa_e_vaga(txt):
        txt_lower = txt.lower()
        # Padrões de ementa vaga: só referencia lei sem explicar o que faz
        padroes_vagos = [
            r'^altera\s+.{0,80}lei\s+n[º°.]?\s*[\d\.]+.*?(e\s+dá\s+outras\s+providências\.?)?$',
            r'^acrescenta\s+(artigo|inciso|parágrafo).{0,80}(e\s+dá\s+outras\s+providências\.?)?$',
            r'^revoga\s+.{0,80}(e\s+dá\s+outras\s+providências\.?)?$',
            r'^dá\s+nova\s+redação.{0,80}(e\s+dá\s+outras\s+providências\.?)?$',
        ]
        # Se ementa é muito curta ou só faz referência formal
        if len(txt) < 60:
            return True
        for padrao in padroes_vagos:
            if re.match(padrao, txt_lower, re.IGNORECASE | re.DOTALL):
                return True
        # Se contém palavras que explicam o conteúdo, não é vaga
        palavras_explicativas = [
            'para', 'visando', 'com o objetivo', 'com a finalidade',
            'destinado', 'dispõe sobre', 'institui', 'cria', 'estabelece',
            'regulamenta', 'define', 'determina', 'proíbe', 'autoriza a',
            'concede', 'assegura', 'garante', 'prevê'
        ]
        if any(p in txt_lower for p in palavras_explicativas) and len(txt) > 80:
            return False
        return len(txt) < 120

    if not ementa_e_vaga(ementa):
        return jsonify({'ementa_enriquecida': ementa, 'complemento': ''})

    groq_key = os.environ.get('GROQ_API_KEY')
    if not groq_key:
        return jsonify({'ementa_enriquecida': ementa, 'complemento': ''})

    prompt = f"""Você é um especialista legislativo. Em UMA frase direta (máximo 20 palavras), \
explique de forma simples o que esta proposição trata na prática para os cidadãos.
Não repita o número da lei. Use linguagem clara e objetiva.

Proposição: {projeto}
Autor: {autor}
Ementa: {ementa}

Responda APENAS com a frase explicativa, sem introdução, sem aspas, sem ponto final."""

    try:
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {groq_key}", "Content-Type": "application/json"},
            json={"model": "llama-3.3-70b-versatile",
                  "messages": [{"role": "user", "content": prompt}],
                  "max_tokens": 60, "temperature": 0.2},
            timeout=10
        )
        if r.ok:
            comp = r.json()['choices'][0]['message']['content'].strip().rstrip('.')
            ementa_enriquecida = f"{ementa} ({comp})"
            return jsonify({'ementa_enriquecida': ementa_enriquecida, 'complemento': comp})
    except Exception as e:
        logger.warning(f"Erro enriquecer ementa: {e}")

    return jsonify({'ementa_enriquecida': ementa, 'complemento': ''})

@app.route('/complementar_ementa', methods=['POST'])
@login_required
def complementar_ementa():
    """Usa Groq para complementar ementa vaga com resumo do que se trata."""
    data    = request.get_json()
    projeto = data.get('projeto', '')
    ementa  = data.get('ementa', '')
    autor   = data.get('autor', '')

    groq_key = os.environ.get('GROQ_API_KEY')
    if not groq_key:
        return jsonify({'complemento': ementa})

    prompt = f"""Você é um especialista legislativo. Sobre a proposição abaixo, escreva em UMA frase direta (máximo 30 palavras) o que ela trata, de forma clara para leigos.
Se a ementa já for clara, retorne ela resumida.

Proposição: {projeto}
Autor: {autor}
Ementa: {ementa}

Responda APENAS com a frase descritiva, sem introdução, sem aspas."""

    try:
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {groq_key}", "Content-Type": "application/json"},
            json={"model": "llama-3.3-70b-versatile", "messages": [{"role": "user", "content": prompt}],
                  "max_tokens": 80, "temperature": 0.2},
            timeout=10
        )
        if r.ok:
            complemento = r.json()['choices'][0]['message']['content'].strip()
            return jsonify({'complemento': complemento})
    except Exception as e:
        logger.warning(f"Erro ao complementar ementa: {e}")

    return jsonify({'complemento': ementa})

@app.route('/salvar_orientacoes', methods=['POST'])
@login_required
def salvar_orientacoes():
    """Salva orientações por grupo (PL, NOVO, oposicao, minoria) para cada item."""
    data      = request.get_json()
    evento_id = data.get('evento_id')
    orientacoes = data.get('orientacoes', [])  # [{id_principal, grupo, orientacao, comentario}]

    conn = get_conn()
    c    = conn.cursor()

    # Cria tabela se não existir
    c.execute('''CREATE TABLE IF NOT EXISTS orientacoes_grupo (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        evento_id INTEGER,
        id_principal TEXT,
        grupo TEXT,
        orientacao TEXT,
        comentario TEXT,
        saved_by TEXT,
        saved_at TEXT,
        UNIQUE(evento_id, id_principal, grupo))''')

    now_str  = now_brasilia().strftime('%Y-%m-%d %H:%M:%S')
    saved_by = current_user.display_name()

    for ori in orientacoes:
        c.execute('''INSERT OR REPLACE INTO orientacoes_grupo
                     (evento_id, id_principal, grupo, orientacao, comentario, saved_by, saved_at)
                     VALUES (?, ?, ?, ?, ?, ?, ?)''',
                  (evento_id, ori.get('id_principal'), ori.get('grupo'),
                   ori.get('orientacao'), ori.get('comentario', ''), saved_by, now_str))
    conn.commit()
    conn.close()
    return jsonify({'message': 'Orientações salvas!'})

@app.route('/get_orientacoes/<int:evento_id>')
@login_required
def get_orientacoes(evento_id):
    """Retorna orientações salvas para um evento."""
    conn = get_conn()
    c    = conn.cursor()
    try:
        c.execute('''SELECT id_principal, grupo, orientacao, comentario, saved_by, saved_at
                     FROM orientacoes_grupo WHERE evento_id=?''', (evento_id,))
        rows = c.fetchall()
        result = [{'id_principal': str(r[0]), 'grupo': r[1], 'orientacao': r[2],
                   'comentario': r[3], 'saved_by': r[4], 'saved_at': r[5]} for r in rows]
    except Exception as e:
        logger.warning(f"Erro get_orientacoes: {e}")
        result = []
    conn.close()
    return jsonify(result)

@app.route('/admin/reset_todas_senhas', methods=['POST'])
@login_required
def reset_todas_senhas():
    if current_user.role.lower() != 'admin':
        return jsonify({'error': 'Acesso negado'}), 403
    nova_hash = bcrypt.generate_password_hash('123').decode('utf-8')
    conn = get_conn()
    c    = conn.cursor()
    c.execute('UPDATE users SET password=?', (nova_hash,))
    affected = c.rowcount
    conn.commit()
    conn.close()
    return jsonify({'message': f'Senha 123 definida para {affected} usuários.'})

@app.route('/admin/usuarios/reset_senha', methods=['POST'])
@login_required
def reset_senha():
    if current_user.role.lower() != 'admin':
        return jsonify({'error': 'Acesso negado'}), 403
    data       = request.get_json()
    user_id    = data.get('user_id')
    nova_senha = data.get('nova_senha', '').strip()
    if not nova_senha or len(nova_senha) < 3:
        return jsonify({'error': 'Senha deve ter ao menos 3 caracteres.'}), 400
    nova_hash = bcrypt.generate_password_hash(nova_senha).decode('utf-8')
    conn = get_conn()
    c    = conn.cursor()
    c.execute('UPDATE users SET password=? WHERE id=?', (nova_hash, user_id))
    conn.commit()
    conn.close()
    return jsonify({'message': 'Senha redefinida!'})

@app.route('/admin/usuarios/update_categoria', methods=['POST'])
@login_required
def update_categoria():
    if current_user.role.lower() != 'admin':
        return jsonify({'error': 'Acesso negado'}), 403
    data      = request.get_json()
    user_id   = data.get('user_id')
    categoria = data.get('categoria', 'geral')
    conn = get_conn()
    c = conn.cursor()
    c.execute('UPDATE users SET categoria=? WHERE id=?', (categoria, user_id))
    conn.commit()
    conn.close()
    return jsonify({'message': 'Categoria atualizada!'})

@app.route('/admin/usuarios/delete/<int:user_id>', methods=['POST'])
@login_required
def delete_usuario(user_id):
    if current_user.role.lower() != 'admin':
        return jsonify({'error': 'Acesso negado'}), 403
    if user_id == current_user.id:
        return jsonify({'error': 'Não pode excluir sua própria conta'}), 400
    try:
        conn = get_conn()
        c = conn.cursor()
        # Busca username antes de deletar
        c.execute('SELECT username FROM users WHERE id=?', (user_id,))
        row = c.fetchone()
        if not row:
            return jsonify({'error': 'Usuário não encontrado'}), 404
        username = row[0]
        c.execute('DELETE FROM users WHERE id = ?', (user_id,))
        # Registra na tabela de deletados para não recriar no próximo startup
        if USE_POSTGRES:
            c.execute('INSERT INTO usuarios_deletados (username) VALUES (%s) ON CONFLICT DO NOTHING', (username,))
        else:
            c.execute('INSERT OR IGNORE INTO usuarios_deletados (username) VALUES (?)', (username,))
        conn.commit()
        conn.close()
        return jsonify({'message': f'Usuário {username} removido.'})
    except Exception as e:
        logger.error(f"Erro delete_usuario: {e}")
        return jsonify({'error': str(e)}), 500

@app.errorhandler(500)
def handle_500(e):
    logger.error(f"500 error: {e}")
    if request.is_json or request.path.startswith('/admin') or request.path.startswith('/atribuir'):
        return jsonify({'error': str(e)}), 500
    return str(e), 500

@app.errorhandler(Exception)
def handle_exception(e):
    logger.error(f"Unhandled exception: {e}", exc_info=True)
    if request.is_json:
        return jsonify({'error': str(e)}), 500
    return str(e), 500

@app.route('/diagnostico')
@login_required
def diagnostico():
    """Diagnóstico do banco de dados."""
    conn = get_conn()
    c = conn.cursor()
    resultado = {
        'use_postgres': USE_POSTGRES,
        'database_url_set': bool(os.environ.get('DATABASE_URL')),
        'pg_params_host': PG_PARAMS.get('host', 'N/A') if USE_POSTGRES else 'SQLite'
    }
    try:
        # Colunas da tabela orientacoes_grupo
        if USE_POSTGRES:
            c.execute("SELECT column_name FROM information_schema.columns WHERE table_name='orientacoes_grupo' ORDER BY ordinal_position")
        else:
            c.execute("PRAGMA table_info(orientacoes_grupo)")
        cols = c.fetchall()
        resultado['orientacoes_colunas'] = [r[0] for r in cols]

        # Últimas orientações salvas
        c.execute("SELECT * FROM orientacoes_grupo ORDER BY id DESC LIMIT 5")
        rows = c.fetchall()
        resultado['orientacoes_ultimas'] = [list(r) for r in rows]

        # Contagem
        c.execute("SELECT COUNT(*) FROM orientacoes_grupo")
        resultado['orientacoes_total'] = c.fetchone()[0]
    except Exception as e:
        resultado['erro_diagnostico'] = str(e)
    finally:
        conn.close()
    return jsonify(resultado)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
