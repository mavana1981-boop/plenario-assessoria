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
from io import BytesIO
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
                    sql += (' ON CONFLICT (evento_id, id_principal, grupo) DO UPDATE SET '
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

        c.execute('''CREATE TABLE IF NOT EXISTS resumos_ia (
            evento_id INTEGER,
            id_proposicao TEXT,
            resumo TEXT,
            PRIMARY KEY (evento_id, id_proposicao))''')

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
        # Atualiza categoria SOMENTE se ainda estiver como 'geral' (padrão inicial)
        # Não sobrescreve categorias editadas manualmente pelo admin
        for _un, _cat in _cats.items():
            try:
                c.execute(
                    f'UPDATE users SET categoria={_p} WHERE username={_p} AND (categoria={_p} OR categoria IS NULL)',
                    (_cat, _un, 'geral')
                )
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
        # Sigla curta com número: "PLP nº 221/2024", "PL nº 1811/2026"
        (r'\b(PLP|PLC|PEC|MPV|PDL|PLV|PDS|PRS|PL)\s+n[º°.]?\s*(\d+)[,\s/]+(?:de\s+)?(\d{4})\b', 3, None),
        (r'\b(PLP|PLC|PEC|MPV|PDL|PLV|PDS|PRS|PL)\s+(\d+),?\s*de\s+(\d{4})\b', 3, None),
        (r'\b(PLP|PLC|PEC|MPV|PDL|PLV|PDS|PRS|PL)\s*(\d{3,5})[/\-](\d{4})\b', 3, None),
        # Texto por extenso — ordem importa: Complementar antes de Lei simples
        (r'Projeto\s+de\s+Lei\s+Complementar\s+n[º°.]?\s*(\d+)[,\s/]+(?:de\s+)?(\d{4})', 2, 'PLP'),
        (r'Proposta\s+de\s+Emenda\s+[AÀ]\s+Constitui[cç][aã]o\s+n[º°.]?\s*(\d+)[,\s/]+(?:de\s+)?(\d{4})', 2, 'PEC'),
        (r'Medida\s+Provis[oó]ria\s+n[º°.]?\s*(\d+)[,\s/]+(?:de\s+)?(\d{4})', 2, 'MPV'),
        (r'Projeto\s+de\s+Decreto\s+Legislativo\s+n[º°.]?\s*(\d+)[,\s/]+(?:de\s+)?(\d{4})', 2, 'PDL'),
        (r'Projeto\s+de\s+Lei\s+n[º°.]?\s*(\d+)[,\s/]+(?:de\s+)?(\d{4})', 2, 'PL'),
    ]

    for item in padroes:
        padrao, n_grupos, sigla_fixa = item
        m = re.search(padrao, ementa_str, re.IGNORECASE)
        if m:
            if n_grupos == 3:
                sigla, num, ano = m.group(1).upper(), m.group(2), m.group(3)
            else:
                sigla, num, ano = sigla_fixa, m.group(1), m.group(2)
            return f"{projeto_base} ao {sigla} {num}/{ano}"

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
                    logger.info(f"  Item {num} (centralizado): bloco='{bloco[:200]}' → codigo={codigo}")
                    if codigo:
                        chave = _normalizar_codigo(codigo)
                        posicoes_usadas = {v: k for k, v in ordem.items()}
                        if num in posicoes_usadas:
                            del ordem[posicoes_usadas[num]]
                            logger.info(f"  Posição {num} sobrescrita: {posicoes_usadas[num]} → {chave}")
                        ordem[chave] = num
                    else:
                        logger.warning(f"  Item {num}: NÃO extraiu código do bloco: '{bloco[:300]}'")
                        # Marca posição como pendente para o fallback resolver
                        chave_pend = f"PEND{num}"
                        if num not in ordem.values():
                            ordem[chave_pend] = num

        # Texto bruto já extraído acima

        # Extrai REQ do texto bruto (formato com ponto, não centralizado)
        with pdfplumber.open(BytesIO(rp.content)) as pdf:
            texto_total = '\n'.join(p.extract_text() or '' for p in pdf.pages)

        # REQ com número: "N. Requerimento nº X.XXX, de AAAA"
        for m in re.finditer(
            r'^(\d+)\.\s+Requerimento\s+n\.?[º°oa]?\.?\s*([\d.]+),\s*de\s+(\d{4})',
            texto_total, re.MULTILINE | re.IGNORECASE
        ):
            num   = int(m.group(1))
            num_p = m.group(2).replace('.', '')
            ano   = m.group(3)
            chave = _normalizar_codigo(f"REQ {num_p}/{ano}")
            if chave not in ordem and num <= 30:
                ordem[chave] = num
                logger.info(f"  Item {num} (REQ texto): REQ {num_p}/{ano} → {chave}")

        # Fallback robusto: extrai ordem de qualquer linha "N. TIPO Nº X/ANO" no texto
        # Sobrescreve posições PEND (não identificadas pelo parser centralizado)
        posicoes_usadas = {v: k for k, v in ordem.items()}
        for m in re.finditer(
            r'^(\d{1,2})\.\s+(' +
            r'(?:PROJETO\s+DE\s+LEI(?:\s+COMPLEMENTAR)?|' +
            r'PROPOSTA\s+DE\s+EMENDA\s+[AÀ]\s+CONSTITUI[CÇ][AÃ]O|' +
            r'MEDIDA\s+PROVIS[OÓ]RIA|' +
            r'PROJETO\s+DE\s+DECRETO\s+LEGISLATIVO|' +
            r'PROJETO\s+DE\s+RESOLU[CÇ][AÃ]O|' +
            r'Requerimento)' +
            r'\s+[Nn][º°.]?\s*([\d.]+)(?:-[A-Z])?,?\s*[Dd][Ee]\s+(\d{4}))',
            texto_total, re.MULTILINE | re.IGNORECASE
        ):
            num  = int(m.group(1))
            if num > 30:
                continue
            tipo_txt = m.group(2).upper()
            num_p = m.group(3).replace('.', '')
            ano   = m.group(4)
            if 'COMPLEMENTAR' in tipo_txt:   sigla = 'PLP'
            elif 'EMENDA' in tipo_txt:       sigla = 'PEC'
            elif 'PROVIS' in tipo_txt:       sigla = 'MPV'
            elif 'DECRETO' in tipo_txt:      sigla = 'PDL'
            elif 'RESOLU' in tipo_txt:       sigla = 'PRC'
            elif 'REQUERIMENTO' in tipo_txt: sigla = 'REQ'
            else:                            sigla = 'PL'
            chave = _normalizar_codigo(f"{sigla} {num_p}/{ano}")
            chave_pend = f"PEND{num}"
            # Sobrescreve se: chave nova não existe OU posição tem uma chave PEND/REQSN
            chave_atual = posicoes_usadas.get(num, '')
            if chave not in ordem and (num not in posicoes_usadas or
                    chave_atual.startswith('PEND') or chave_atual.startswith('REQSN')):
                if chave_atual:
                    del ordem[chave_atual]
                ordem[chave] = num
                posicoes_usadas[num] = chave
                logger.info(f"  Item {num} (fallback texto): {sigla} {num_p}/{ano} → {chave}")

        # REQ sem número (s/n ou s/nº)
        # Extrai o PL referenciado da ementa do PDF para matching com a API
        posicoes_usadas_final = {v: k for k, v in ordem.items()}
        req_sn_count = 0
        for m in re.finditer(
            r'^(\d+)\.\s+Requerimento\s+s/n[º°]?',
            texto_total, re.MULTILINE | re.IGNORECASE
        ):
            num = int(m.group(1))
            if num > 30:
                continue
            chave_atual = posicoes_usadas_final.get(num, '')
            if chave_atual.startswith('PEND') or num not in posicoes_usadas_final:
                if chave_atual:
                    del ordem[chave_atual]
                # Extrai PL referenciado do bloco do PDF para uso no matching
                bloco_req = texto_total[m.start():m.start()+500]
                m_pl = re.search(
                    r'Projeto\s+de\s+Lei(?:\s+Complementar)?\s+n[º°.]?\s*([\d.]+),\s*de\s+(\d{4})',
                    bloco_req, re.IGNORECASE
                )
                if m_pl:
                    num_pl = m_pl.group(1).replace('.', '')
                    ano_pl = m_pl.group(2)
                    sigla_pl = 'PLP' if 'Complementar' in m_pl.group(0) else 'PL'
                    chave = f"REQSN_{sigla_pl}{num_pl}/{ano_pl}"
                    logger.info(f"  Item {num} (REQ s/nº → {sigla_pl} {num_pl}/{ano_pl}): chave={chave}")
                else:
                    chave = f"REQSN{req_sn_count}"
                    logger.info(f"  Item {num} (REQ s/nº): chave={chave}")
                req_sn_count += 1
                ordem[chave] = num
                posicoes_usadas_final[num] = chave

        # Preenche gaps de posição usando cabeçalhos de PL/PLP/PEC no texto
        # Ex: "PROJETO DE LEI Nº 5.868, DE 2025" aparece entre pos 18 e 20 → pos 19
        posicoes_usadas_set = set(ordem.values())
        gaps = sorted(set(range(1, max(posicoes_usadas_set)+1)) - posicoes_usadas_set)
        logger.info(f"Gaps de posição no PDF: {gaps}")

        if gaps:
            # Coleta cabeçalhos de proposição no texto (sem número sequencial na frente)
            cabecalhos = []
            # Usa re.search sem ^ para pegar cabeçalhos em qualquer posição da linha
            for m in re.finditer(
                r'(?:^|\n)\s*(PROJETO\s+DE\s+LEI(?:\s+COMPLEMENTAR)?'
                r'|PROPOSTA\s+DE\s+EMENDA\s+[AÀ]\s+CONSTITUI[CÇ][AÃ]O)'
                r'\s+N[º°.]?\s*([\d.]+(?:-[A-Z])?),?\s*DE\s+(\d{4})',
                texto_total, re.IGNORECASE | re.MULTILINE
            ):
                tipo_txt = m.group(1).upper()
                num_p = re.sub(r'-[A-Z]$', '', m.group(2).replace('.', ''))
                ano   = m.group(3)
                if 'COMPLEMENTAR' in tipo_txt: sigla = 'PLP'
                elif 'EMENDA' in tipo_txt:     sigla = 'PEC'
                else:                          sigla = 'PL'
                chave = _normalizar_codigo(f"{sigla} {num_p}/{ano}")
                if chave not in ordem:
                    cabecalhos.append((m.start(), chave, sigla, num_p, ano))
                    logger.info(f"  Cabeçalho encontrado: {sigla} {num_p}/{ano} → {chave}")

            cabecalhos.sort(key=lambda x: x[0])
            for gap, (_, chave, sigla, num_p, ano) in zip(gaps, cabecalhos):
                ordem[chave] = gap
                posicoes_usadas_set.add(gap)
                logger.info(f"  Gap {gap} preenchido: {sigla} {num_p}/{ano} → {chave}")

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
    if 'prop_mostrarintegra' in url and '/proposicoesWeb/' not in url:
        url = url.replace('camara.leg.br/prop_mostrarintegra', 'camara.leg.br/proposicoesWeb/prop_mostrarintegra')
    return url

def _buscar_ordem_html(evento_id):
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

    def eh_req_sn(item):
        proj = (item.get('projeto_original') or item.get('projeto', '')).upper()
        return any(proj.startswith(s) for s in ('REQ','RQS','RQU','REC')) and \
               not re.search(r'\d{2,}', proj.split('/')[0])

    # Casa REQ s/n da API com chaves REQSN do PDF (por ordem de aparição)
    chaves_reqsn = sorted([k for k in ordem_oficial if k.startswith('REQSN')],
                          key=lambda k: ordem_oficial[k])
    req_sn_itens = [it for it in itens if eh_req_sn(it) and norm(it) not in ordem_oficial]
    ordem_extra = {}
    for i, (chave, item) in enumerate(zip(chaves_reqsn, req_sn_itens)):
        ordem_extra[id(item)] = ordem_oficial[chave]
        logger.info(f"REQ s/n match: {item.get('projeto','')} → posição {ordem_oficial[chave]}")

    encontrados = []
    nao_encontrados = []
    for i, it in enumerate(itens):
        n = norm(it)
        if n in ordem_oficial:
            encontrados.append((i, it, ordem_oficial[n]))
        elif id(it) in ordem_extra:
            encontrados.append((i, it, ordem_extra[id(it)]))
        else:
            nao_encontrados.append((i, it))

    cobertura = len(encontrados) / len(itens)
    logger.info(f"Cobertura PDF: {len(encontrados)}/{len(itens)} ({cobertura:.0%})")

    if not encontrados or cobertura < 0.30:
        logger.warning("Cobertura insuficiente — mantendo ordem da API.")
        return itens

    encontrados.sort(key=lambda x: x[2])

    resultado = list(encontrados)
    for api_idx, item in sorted(nao_encontrados, key=lambda x: x[0]):
        pos = 0
        for j, (enc_idx, _, _) in enumerate(resultado):
            if enc_idx < api_idx:
                pos = j + 1
        resultado.insert(pos, (api_idx, item, -1))
        logger.info(f"Não encontrado no PDF: {item.get('projeto_original','')[:20]} → inserido em pos {pos+1}")

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

            # Mapeamento REQSN → item da API
            # REQSN_PL3839/2023 → busca na API o REQ cuja ementa menciona "3.839" ou "3839"
            reqsn_para_api = {}
            for chave_pdf in sorted(
                [k for k in ordem_oficial if k.startswith('REQSN')],
                key=lambda k: ordem_oficial[k]
            ):
                m_pl = re.search(r'REQSN_(?:PL|PLP)(\d+)/(\d{4})', chave_pdf)
                if m_pl:
                    num_pl, ano_pl = m_pl.group(1), m_pl.group(2)
                    # Busca REQ na API cuja ementa menciona esse número
                    for item in (itens_raw or []):
                        if not re.match(r'RE[QCS]', item['codigo'].upper()):
                            continue
                        ementa = item.get('ementa', '')
                        num_fmt1 = num_pl  # "3839"
                        num_fmt2 = f"{int(num_pl):,}".replace(',', '.')  # "3.839"
                        if (num_fmt1 in ementa or num_fmt2 in ementa) and item not in reqsn_para_api.values():
                            reqsn_para_api[chave_pdf] = item
                            logger.info(f"REQSN match por PL: {chave_pdf} → {item['codigo']}")
                            break
                if chave_pdf not in reqsn_para_api:
                    logger.warning(f"REQSN sem match: {chave_pdf}")

            codigos_pdf = sorted(ordem_oficial.keys(), key=lambda k: ordem_oficial[k])

            for cod_pdf in codigos_pdf:
                # Para REQSN, usa o item da API mapeado
                if cod_pdf.startswith('REQSN') and cod_pdf in reqsn_para_api:
                    item_api = reqsn_para_api[cod_pdf]
                else:
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
                elif cod_pdf.startswith('REQSN_'):
                    # REQ s/n sem match na API — busca o PL referenciado diretamente
                    m_ref = re.search(r'REQSN_((?:PL|PLP)(\d+)/(\d{4}))', cod_pdf)
                    if m_ref:
                        sigla_ref = 'PLP' if 'PLP' in m_ref.group(1) else 'PL'
                        num_ref   = m_ref.group(2)
                        ano_ref   = m_ref.group(3)
                        try:
                            r_pl = requests.get(
                                f"https://dadosabertos.camara.leg.br/api/v2/proposicoes"
                                f"?siglaTipo={sigla_ref}&numero={num_ref}&ano={ano_ref}&itens=1",
                                headers={'Accept': 'application/json'}, timeout=8
                            )
                            dados_pl = r_pl.json().get('dados', []) if r_pl.ok else []
                            if dados_pl:
                                pl = dados_pl[0]
                                id_pl = str(pl.get('id', ''))
                                if id_pl in vistos_ids:
                                    continue
                                vistos_ids.add(id_pl)
                                key = f"PROP_{id_pl}"
                                projeto_req = f"REQ s/nº ao {sigla_ref} {num_ref}/{ano_ref}"
                                itens.append({
                                    'ordem':            str(len(itens) + 1),
                                    'id_principal':     id_pl,
                                    'projeto':          projeto_req,
                                    'projeto_original': projeto_req,
                                    'ementa':           pl.get('ementa', ''),
                                    'autor':            'Líderes',
                                    'relator':          'Não atribuído',
                                    'situacao':         pl.get('statusProposicao', {}).get('descricaoSituacao', 'N/D') if isinstance(pl.get('statusProposicao'), dict) else 'N/D',
                                    'secao':            'N/D',
                                    'resumo_materia':   notas.get(key, {}).get('resumo_materia', ''),
                                    'orientacao':       notas.get(key, {}).get('orientacao', ''),
                                    'resumo_parecer':   notas.get(key, {}).get('resumo_parecer', ''),
                                    'saved_by':         notas.get(key, {}).get('saved_by', ''),
                                    'saved_at':         notas.get(key, {}).get('saved_at', ''),
                                    'destaques_emendas': []
                                })
                                logger.info(f"REQSN resolvido via API: {cod_pdf} → {sigla_ref} {num_ref}/{ano_ref} id={id_pl}")
                            else:
                                logger.warning(f"REQSN: PL {num_ref}/{ano_ref} não encontrado na API")
                        except Exception as e:
                            logger.warning(f"Erro buscar PL para REQSN {cod_pdf}: {e}")
                else:
                    # Item do PDF não encontrado na API — ignora
                    logger.warning(f"PDF item '{cod_pdf}' não na API — ignorado")

            # Itens da API não encontrados no PDF — insere na posição correta
            # A posição é inferida pela sequência relativa na API
            nao_encontrados_api = []
            for item in (itens_raw or []):
                id_p = item.get('id_principal')
                if not id_p or id_p in vistos_ids:
                    continue
                nao_encontrados_api.append(item)

            if nao_encontrados_api:
                # Para cada item não encontrado, determina posição na lista
                # baseado na posição do item anterior na API
                for item in nao_encontrados_api:
                    id_p = item.get('id_principal')
                    vistos_ids.add(id_p)
                    key = f"PROP_{id_p}"
                    # Acha índice do item na lista raw da API
                    idx_api = next((i for i, it in enumerate(itens_raw) if it.get('id_principal') == id_p), len(itens_raw))
                    # Acha o item anterior da API que já está na lista
                    pos_inserir = len(itens)  # default: no final
                    for idx_prev in range(idx_api - 1, -1, -1):
                        id_prev = itens_raw[idx_prev].get('id_principal')
                        for j, it_existente in enumerate(itens):
                            if it_existente.get('id_principal') == id_prev:
                                pos_inserir = j + 1
                                break
                        else:
                            continue
                        break

                    novo_item = {
                        'ordem':            '',  # será renumerado
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
                    }
                    itens.insert(pos_inserir, novo_item)
                    logger.info(f"Inserindo '{item['codigo']}' na posição {pos_inserir + 1}")

                # Renumera todos
                for i, it in enumerate(itens, start=1):
                    it['ordem'] = str(i)

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
@app.template_filter('truncar_autores')
def truncar_autores(value, max_autores=2):
    """Limita a exibição a max_autores, acrescentando 'e outros.' se necessário."""
    if not value:
        return value
    # Separa por vírgula, respeitando parênteses ex: "João (PL-RJ), Maria (PT-SP)"
    import re as _re
    partes = _re.split(r',\s*(?![^()]*\))', str(value))
    partes = [p.strip() for p in partes if p.strip()]
    # Remove " e outros." do final se já existir
    if partes and 'e outros' in partes[-1].lower():
        partes = partes[:-1]
    if len(partes) <= max_autores:
        return ', '.join(partes)
    return ', '.join(partes[:max_autores]) + ' e outros.'

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
                grupo = current_user.categoria or 'geral'
                c.execute('SELECT responsavel_username FROM notas WHERE item_key=?', (prop_key,))
                row_resp = c.fetchone()
                responsavel = (row_resp[0] if row_resp else '') or ''
                if responsavel:
                    c.execute('SELECT categoria FROM users WHERE username=?', (responsavel,))
                    row_cat = c.fetchone()
                    if row_cat and row_cat[0]:
                        grupo = row_cat[0]
                logger.info(f"Exportando orientação: grupo={grupo} id={id_principal} ori={orientacao}")
                c.execute('''INSERT OR REPLACE INTO orientacoes_grupo
                             (evento_id, id_principal, grupo, orientacao, comentario, saved_by, saved_at)
                             VALUES (?, ?, ?, ?, ?, ?, ?)''',
                          (evento_id, str(id_principal), grupo, orientacao, '', saved_by, now_str))
                conn.commit()
                logger.info(f"✅ Orientação exportada: {grupo} / {id_principal} → {orientacao}")
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

    prompt = f"""Você é um assessor legislativo especializado em análise de proposições da Câmara dos Deputados do Brasil, trabalhando para a Oposição e Minoria (bancada do PL e aliados).

**Proposição:** {projeto}
**Autor(es):** {autor}
**Relator:** {relator}
**Ementa:** {ementa}
{contexto_doc}

Gere uma nota técnica em texto puro seguindo EXATAMENTE esta estrutura.
REGRA CRÍTICA: cada título de seção (com emoji) deve estar SOZINHO em sua linha, seguido de linha em branco, depois o texto. Nunca coloque texto na mesma linha do emoji/título.

Formato obrigatório:

📘 Resumo técnico

[parágrafo 1]

[parágrafo 2]

🟢 Pontos positivos

[parágrafo 1]

[parágrafo 2]

🔴 Pontos negativos

[parágrafo 1]

[parágrafo 2]

⚖️ Riscos políticos e de imagem

[parágrafo 1]

[parágrafo 2]

↔️ Orientação sugerida

[parágrafo 1]

[parágrafo 2]

Regras de estilo:
- Sem HTML, sem asteriscos, sem ###
- Apenas os emojis dos títulos, sem outros emojis no texto
- Detalhado, estratégico e político
- Máximo 500 palavras no total
{secao_criticas}"""

    # Tenta Gemini primeiro, fallback para Groq
    if gemini_key:
        try:
            r = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent?key={gemini_key}",
                headers={"Content-Type": "application/json"},
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"maxOutputTokens": 1500, "temperature": 0.3}
                },
                timeout=30
            )
            r.raise_for_status()
            texto = r.json()['candidates'][0]['content']['parts'][0]['text']

            # Pós-processamento: garante que cada título de seção fique sozinho na linha
            titulos_secao = ['📘', '🟢', '🔴', '⚖️', '↔️', '⚠️']
            linhas = texto.split('\n')
            linhas_corrigidas = []
            for linha in linhas:
                linha_strip = linha.strip()
                # Se a linha começa com emoji de seção mas tem texto depois
                for emoji in titulos_secao:
                    if linha_strip.startswith(emoji) and len(linha_strip) > len(emoji) + 30:
                        # Separa o título do texto
                        # Encontra onde termina o título (até o primeiro ponto ou dois pontos longo)
                        idx_sep = linha_strip.find('\n')
                        # Tenta separar após o nome da seção (ex: "📘 Resumo técnico\nTexto...")
                        partes = linha_strip.split(None, 5)
                        if len(partes) >= 3:
                            # Heurística: título são as primeiras 2-4 palavras com o emoji
                            titulo_palavras = [partes[0]]  # emoji
                            i = 1
                            while i < len(partes) and i < 4:
                                titulo_palavras.append(partes[i])
                                i += 1
                                # Para quando encontrar palavra que parece início de parágrafo
                                if partes[i-1][-1:] in '.,:' or len(' '.join(titulo_palavras)) > 30:
                                    break
                            titulo = ' '.join(titulo_palavras)
                            resto = linha_strip[len(titulo):].strip()
                            if resto:
                                linhas_corrigidas.append(titulo)
                                linhas_corrigidas.append('')
                                linhas_corrigidas.append(resto)
                                break
                else:
                    linhas_corrigidas.append(linha)

            texto = '\n'.join(linhas_corrigidas)
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
            return jsonify({'error': 'Nova senha 
