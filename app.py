# -*- coding: utf-8 -*-
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

# ── Helper Gemini com detecção automática de modelo ─────────────────────────
GEMINI_MODEL = "gemini-2.0-flash"  # fallback fixo
_gemini_modelo_cache = {"modelo": None}  # cache em memória

GEMINI_PREFERENCIA = [
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
    "gemini-1.5-flash",
    "gemini-1.5-flash-8b",
]

def detectar_modelo_gemini(key):
    """Consulta API Gemini e retorna o melhor modelo disponível.
    Resultado cacheado em memória para não repetir a chamada a cada request."""
    if _gemini_modelo_cache["modelo"]:
        return _gemini_modelo_cache["modelo"]
    try:
        r = requests.get(
            "https://generativelanguage.googleapis.com/v1beta/models?key=" + key,
            timeout=8
        )
        if not r.ok:
            logger.warning(f"detectar_modelo_gemini: HTTP {r.status_code}")
            return GEMINI_MODEL
        modelos_disponiveis = [
            m["name"].split("/")[-1]
            for m in r.json().get("models", [])
            if "generateContent" in m.get("supportedGenerationMethods", [])
        ]
        logger.info(f"Modelos Gemini disponíveis: {modelos_disponiveis}")
        for preferido in GEMINI_PREFERENCIA:
            if preferido in modelos_disponiveis:
                _gemini_modelo_cache["modelo"] = preferido
                logger.info(f"Modelo Gemini selecionado: {preferido}")
                return preferido
        # Se nenhum da lista, usa o primeiro disponível
        if modelos_disponiveis:
            _gemini_modelo_cache["modelo"] = modelos_disponiveis[0]
            return modelos_disponiveis[0]
    except Exception as e:
        logger.warning(f"detectar_modelo_gemini falhou: {e}")
    return GEMINI_MODEL

def gemini_post(key, prompt, max_tokens=1500, temperatura=0.3, tentativas=3):
    """Chama a API Gemini com retry automático em caso de rate limit (429).
    Detecta automaticamente o modelo ativo via API."""
    import time
    modelo = detectar_modelo_gemini(key)
    url = "https://generativelanguage.googleapis.com/v1beta/models/" + modelo + ":generateContent?key=" + key
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": max_tokens, "temperature": temperatura}
    }
    for i in range(tentativas):
        try:
            r = requests.post(url, headers={"Content-Type": "application/json"},
                              json=payload, timeout=15)
            if r.status_code == 429:
                espera = 5 * (i + 1)
                logger.warning(f"Gemini 429 — aguardando {espera}s (tentativa {i+1}/{tentativas})")
                time.sleep(espera)
                continue
            if r.status_code == 404:
                # Modelo não existe mais — limpa cache e tenta novamente
                logger.warning(f"Gemini 404 — modelo {modelo} não encontrado, limpando cache")
                _gemini_modelo_cache["modelo"] = None
                modelo = detectar_modelo_gemini(key)
                url = "https://generativelanguage.googleapis.com/v1beta/models/" + modelo + ":generateContent?key=" + key
                continue
            r.raise_for_status()
            return r.json()['candidates'][0]['content']['parts'][0]['text']
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 429 and i < tentativas - 1:
                espera = 5 * (i + 1)
                logger.warning(f"Gemini HTTPError 429 — aguardando {espera}s")
                time.sleep(espera)
                continue
            raise
    raise Exception("Gemini indisponível após retries.")


def groq_post(prompt, max_tokens=1500, temperatura=0.3):
    """Chama Groq como fallback. Retorna texto ou lança exceção."""
    groq_key = os.environ.get('GROQ_API_KEY', '')
    if not groq_key:
        raise Exception("GROQ_API_KEY não configurada.")
    r = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {groq_key}", "Content-Type": "application/json"},
        json={"model": "llama-3.3-70b-versatile",
              "messages": [{"role": "user", "content": prompt}],
              "max_tokens": max_tokens, "temperature": temperatura},
        timeout=30
    )
    r.raise_for_status()
    return r.json()['choices'][0]['message']['content']


def cloudflare_post(prompt, max_tokens=1500, temperatura=0.3):
    """Chama Cloudflare Workers AI (gratuito, 500 RPM). Retorna texto ou lança exceção."""
    cf_account = os.environ.get('CF_ACCOUNT_ID', '')
    cf_token   = os.environ.get('CF_API_TOKEN', '')
    if not cf_account or not cf_token:
        raise Exception("CF_ACCOUNT_ID ou CF_API_TOKEN não configurados.")
    url = f"https://api.cloudflare.com/client/v4/accounts/{cf_account}/ai/run/@cf/meta/llama-3.1-70b-instruct"
    r = requests.post(url,
        headers={"Authorization": f"Bearer {cf_token}", "Content-Type": "application/json"},
        json={"messages": [{"role": "user", "content": prompt}],
              "max_tokens": max_tokens, "temperature": temperatura},
        timeout=30
    )
    r.raise_for_status()
    return r.json()['result']['response']


def ia_chain(prompt, max_tokens=1500, temperatura=0.3, contexto=""):
    """Cadeia tripla: Gemini → Groq → Cloudflare AI. Máx 15s por tentativa.
    Retorna (texto, fonte) ou lança Exception se todos falharem."""
    erros = []

    # Log diagnóstico
    groq_ok = bool(os.environ.get('GROQ_API_KEY', ''))
    cf_ok   = bool(os.environ.get('CF_ACCOUNT_ID', '') and os.environ.get('CF_API_TOKEN', ''))
    gem_ok  = bool(os.environ.get('GEMINI_API_KEY', ''))
    logger.info(f"ia_chain [{contexto}] chaves: GEMINI={'✅' if gem_ok else '❌'} GROQ={'✅' if groq_ok else '❌'} CF={'✅' if cf_ok else '❌'}")

    # 1. Groq (llama-3.3-70b, 30 RPM gratuito) — primeiro por ser mais rápido e confiável
    groq_key = os.environ.get('GROQ_API_KEY', '')
    if groq_key:
        try:
            texto = groq_post(prompt, max_tokens=max_tokens, temperatura=temperatura)
            if texto and texto.strip():
                logger.info(f"ia_chain [{contexto}]: Groq OK")
                return texto, 'groq'
        except Exception as e:
            logger.warning(f"ia_chain [{contexto}]: Groq falhou — {e}")
            erros.append(f"Groq: {e}")

    # 2. Gemini (fallback)
    gemini_key = os.environ.get('GEMINI_API_KEY', '')
    if gemini_key:
        try:
            texto = gemini_post(gemini_key, prompt, max_tokens=max_tokens,
                                temperatura=temperatura, tentativas=1)
            if texto and texto.strip():
                logger.info(f"ia_chain [{contexto}]: Gemini OK ({_gemini_modelo_cache.get('modelo','?')})")
                return texto, 'gemini'
        except Exception as e:
            logger.warning(f"ia_chain [{contexto}]: Gemini falhou — {e}")
            _gemini_modelo_cache["modelo"] = None
            erros.append(f"Gemini: {e}")

    # 3. Cloudflare Workers AI (llama-3.1-70b, 500 RPM gratuito)
    cf_account = os.environ.get('CF_ACCOUNT_ID', '')
    cf_token   = os.environ.get('CF_API_TOKEN', '')
    if cf_account and cf_token:
        try:
            texto = cloudflare_post(prompt, max_tokens=max_tokens, temperatura=temperatura)
            if texto and texto.strip():
                logger.info(f"ia_chain [{contexto}]: Cloudflare OK")
                return texto, 'cloudflare'
        except Exception as e:
            logger.warning(f"ia_chain [{contexto}]: Cloudflare falhou — {e}")
            erros.append(f"Cloudflare: {e}")

    raise Exception("Todos os provedores falharam: " + " | ".join(erros))


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
        }
        # SSL: usa apenas se não for rede interna do Railway
        if _parsed.hostname and 'railway.internal' not in _parsed.hostname:
            PG_PARAMS['ssl_context'] = True
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
                    # Upsert manual: tenta UPDATE primeiro, depois INSERT se não existir
                    # Extrai os valores dos params: (evento_id, id_principal, grupo, orientacao, comentario, saved_by, saved_at)
                    if params and len(params) >= 7:
                        p = list(params)
                        ev_id, id_pr, grp, ori, com, sb, sa = p[0], p[1], p[2], p[3], p[4], p[5], p[6]
                        _orig_exec(
                            'UPDATE orientacoes_grupo SET orientacao=%s, comentario=%s, saved_by=%s, saved_at=%s '
                            'WHERE evento_id=%s AND id_principal=%s AND grupo=%s',
                            [ori, com, sb, sa, ev_id, id_pr, grp]
                        )
                        _orig_exec(
                            'INSERT INTO orientacoes_grupo (evento_id, id_principal, grupo, orientacao, comentario, saved_by, saved_at) '
                            'SELECT %s,%s,%s,%s,%s,%s,%s WHERE NOT EXISTS '
                            '(SELECT 1 FROM orientacoes_grupo WHERE evento_id=%s AND id_principal=%s AND grupo=%s)',
                            [ev_id, id_pr, grp, ori, com, sb, sa, ev_id, id_pr, grp]
                        )
                    return
                    sql = sql  # nunca chega aqui
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

        c.execute('''CREATE TABLE IF NOT EXISTS extra_pauta (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            evento_id INTEGER NOT NULL,
            id_proposicao TEXT NOT NULL,
            projeto TEXT,
            ementa TEXT,
            autor TEXT,
            relator TEXT,
            created_by TEXT,
            created_at TEXT,
            UNIQUE(evento_id, id_proposicao))''')

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
    def __init__(self, id, username, role, categoria='geral', nome_display=None, foto=None):
        self.id = id; self.username = username; self.role = role; self.categoria = categoria
        self.nome_display = nome_display or username
        self.foto = foto or ''

    def display_name(self):
        """Nome de exibição com categoria."""
        if self.categoria in ('oposicao', 'minoria'):
            return f"{self.username} - {self.categoria}"
        return self.username

@login_manager.user_loader
def load_user(user_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute('SELECT id, username, role, categoria, nome_display, foto FROM users WHERE id = ?', (user_id,))
    u = c.fetchone()
    conn.close()
    if not u: return None
    return User(u[0], u[1], u[2],
                u[3] if len(u) > 3 else 'geral',
                u[4] if len(u) > 4 else None,
                u[5] if len(u) > 5 else None)

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

def _extrair_sigla_num_ano(texto):
    """Extrai (sigla_curta, numero, ano) de QUALQUER formato de menção a projeto
    legislativo. Cobre siglas por extenso e abreviadas, com ou sem nº, com
    barra, vírgula, espaço ou 'de' como separador."""
    PADROES = [
        # Complementar (extenso)
        (r'Projeto\s+de\s+Lei\s+Complementar\s+n[º°.]?\s*(\d[\d.]*)\s*[,/]\s*(?:de\s+)?(\d{4})', 'PLP'),
        (r'Projeto\s+de\s+Lei\s+Complementar\s+n[º°.]?\s*(\d[\d.]*)\s+(?:de\s+)?(\d{4})', 'PLP'),
        (r'Projeto\s+de\s+Lei\s+Complementar\s+(\d[\d.]*)\s*[,/]\s*(?:de\s+)?(\d{4})', 'PLP'),
        (r'Projeto\s+de\s+Lei\s+Complementar\s+(\d[\d.]*)\s+(?:de\s+)?(\d{4})\b', 'PLP'),
        # PEC (extenso)
        (r'Proposta\s+de\s+Emenda\s+[AÀ]\s+Constitui[cç][aã]o\s+n[º°.]?\s*(\d[\d.]*)\s*[,/]\s*(?:de\s+)?(\d{4})', 'PEC'),
        (r'Proposta\s+de\s+Emenda\s+[AÀ]\s+Constitui[cç][aã]o\s+n[º°.]?\s*(\d[\d.]*)\s+(?:de\s+)?(\d{4})', 'PEC'),
        (r'Proposta\s+de\s+Emenda\s+[AÀ]\s+Constitui[cç][aã]o\s+(\d[\d.]*)\s*[,/]\s*(?:de\s+)?(\d{4})', 'PEC'),
        (r'Proposta\s+de\s+Emenda\s+[AÀ]\s+Constitui[cç][aã]o\s+(\d[\d.]*)\s+(?:de\s+)?(\d{4})\b', 'PEC'),
        # MPV (extenso)
        (r'Medida\s+Provis[oó]ria\s+n[º°.]?\s*(\d[\d.]*)\s*[,/]\s*(?:de\s+)?(\d{4})', 'MPV'),
        (r'Medida\s+Provis[oó]ria\s+n[º°.]?\s*(\d[\d.]*)\s+(?:de\s+)?(\d{4})\b', 'MPV'),
        (r'Medida\s+Provis[oó]ria\s+(\d[\d.]*)\s*[,/]\s*(?:de\s+)?(\d{4})', 'MPV'),
        (r'Medida\s+Provis[oó]ria\s+(\d[\d.]*)\s+(?:de\s+)?(\d{4})\b', 'MPV'),
        # PDL (extenso)
        (r'Projeto\s+de\s+Decreto\s+Legislativo\s+n[º°.]?\s*(\d[\d.]*)\s*[,/]\s*(?:de\s+)?(\d{4})', 'PDL'),
        (r'Projeto\s+de\s+Decreto\s+Legislativo\s+n[º°.]?\s*(\d[\d.]*)\s+(?:de\s+)?(\d{4})\b', 'PDL'),
        (r'Projeto\s+de\s+Decreto\s+Legislativo\s+(\d[\d.]*)\s*[,/]\s*(?:de\s+)?(\d{4})', 'PDL'),
        (r'Projeto\s+de\s+Decreto\s+Legislativo\s+(\d[\d.]*)\s+(?:de\s+)?(\d{4})\b', 'PDL'),
        # PL (extenso) — com nº
        (r'Projeto\s+de\s+Lei\s+n[º°.]\s*(\d[\d.]*)\s*[,/]\s*(?:de\s+)?(\d{4})', 'PL'),
        (r'Projeto\s+de\s+Lei\s+n[º°.]\s*(\d[\d.]*)\s+(?:de\s+)?(\d{4})\b', 'PL'),
        # PL (extenso) — sem nº
        (r'Projeto\s+de\s+Lei\s+(\d[\d.]*)\s*/\s*(\d{4})\b', 'PL'),
        (r'Projeto\s+de\s+Lei\s+(\d[\d.]*)\s*,\s*(?:de\s+)?(\d{4})\b', 'PL'),
        (r'Projeto\s+de\s+Lei\s+(\d[\d.]*)\s+(?:de\s+)?(\d{4})\b', 'PL'),
        # Siglas curtas — com nº
        (r'\b(PLP|PLC|PEC|MPV|PDL|PLV|PDS|PRS|PLS|PL)\s+n[º°.]\s*(\d[\d.]*)\s*[,/]\s*(?:de\s+)?(\d{4})\b', None),
        (r'\b(PLP|PLC|PEC|MPV|PDL|PLV|PDS|PRS|PLS|PL)\s+n[º°.]\s*(\d[\d.]*)\s+(?:de\s+)?(\d{4})\b', None),
        # Siglas curtas — sem nº (barra, vírgula, espaço)
        (r'\b(PLP|PLC|PEC|MPV|PDL|PLV|PDS|PRS|PLS|PL)\s+(\d[\d.]*)/( \d{4})\b', None),
        (r'\b(PLP|PLC|PEC|MPV|PDL|PLV|PDS|PRS|PLS|PL)\s+(\d[\d.]*)\s*,\s*(?:de\s+)?(\d{4})\b', None),
        (r'\b(PLP|PLC|PEC|MPV|PDL|PLV|PDS|PRS|PLS|PL)\s+(\d[\d.]*)/(\d{4})\b', None),
        (r'\b(PLP|PLC|PEC|MPV|PDL|PLV|PDS|PRS|PLS|PL)\s+(\d[\d.]*)\s+(?:de\s+)?(\d{4})\b', None),
    ]
    for item in PADROES:
        padrao, sigla_fixa = item
        m = re.search(padrao, texto, re.IGNORECASE)
        if not m:
            continue
        if sigla_fixa:
            return sigla_fixa, m.group(1).replace('.', ''), m.group(2)
        else:
            return m.group(1).upper(), m.group(2).replace('.', ''), m.group(3)
    return '', '', ''

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
    sigla, num, ano = _extrair_sigla_num_ano(ementa_str)
    if sigla and num and ano:
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

        # Redação Final: "N. Redação Final ao Projeto de Lei nº X.XXX, de AAAA"
        # Estes itens têm número à esquerda (não centralizado) — parser específico
        for m in re.finditer(
            r'^(\d+)\.\s+Redação\s+Final\s+ao\s+'
            r'(PROJETO\s+DE\s+LEI(?:\s+COMPLEMENTAR)?'
            r'|PROPOSTA\s+DE\s+EMENDA\s+[AÀ]\s+CONSTITUI[CÇ][AÃ]O'
            r'|PROJETO\s+DE\s+DECRETO\s+LEGISLATIVO)'
            r'\s+N[º°.]?\s*([\d.]+(?:-[A-Z])?),?\s*[Dd][Ee]\s+(\d{4})',
            texto_total, re.MULTILINE | re.IGNORECASE
        ):
            num      = int(m.group(1))
            tipo_txt = m.group(2).upper()
            num_p    = re.sub(r'[-–][A-Z]$', '', m.group(3).replace('.', ''))
            ano      = m.group(4)
            if 'COMPLEMENTAR' in tipo_txt:          sigla = 'PLP'
            elif 'EMENDA' in tipo_txt:              sigla = 'PEC'
            elif 'DECRETO LEGISLATIVO' in tipo_txt: sigla = 'PDL'
            else:                                   sigla = 'PL'
            chave = _normalizar_codigo(f"{sigla} {num_p}/{ano}")
            posicoes_usadas = {v: k for k, v in ordem.items()}
            if num <= 30:
                if num in posicoes_usadas and posicoes_usadas[num].startswith('PEND'):
                    del ordem[posicoes_usadas[num]]
                if chave not in ordem:
                    ordem[chave] = num
                    logger.info(f"  Item {num} (Redação Final): {sigla} {num_p}/{ano} → {chave}")

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
            for m in re.finditer(
                r'(?:^|\n)\s*(?:Redação Final ao\s+)?(PROJETO\s+DE\s+LEI(?:\s+COMPLEMENTAR)?'
                r'|PROJETO\s+DE\s+DECRETO\s+LEGISLATIVO'
                r'|PROPOSTA\s+DE\s+EMENDA\s+[AÀ]\s+CONSTITUI[CÇ][AÃ]O'
                r'|MEDIDA\s+PROVIS[OÓ]RIA'
                r'|PROJETO\s+DE\s+DECRETO'
                r'|PROJETO\s+DE\s+RESOLU[CÇ][AÃ]O)'
                r'\s+N[º°.]?\s*([\d.]+(?:-[A-Z])?),?\s*DE\s+(\d{4})',
                texto_total, re.IGNORECASE | re.MULTILINE
            ):
                tipo_txt = m.group(1).upper()
                num_p = re.sub(r'-[A-Z]$', '', m.group(2).replace('.', ''))
                ano   = m.group(3)
                if 'COMPLEMENTAR' in tipo_txt:       sigla = 'PLP'
                elif 'EMENDA' in tipo_txt:           sigla = 'PEC'
                elif 'DECRETO LEGISLATIVO' in tipo_txt: sigla = 'PDL'
                elif 'DECRETO' in tipo_txt:          sigla = 'PDC'
                elif 'MEDIDA' in tipo_txt:           sigla = 'MPV'
                elif 'RESOLUÇÃO' in tipo_txt:        sigla = 'PRC'
                else:                                sigla = 'PL'
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

    # Dados do usuário logado para o quadro de boas-vindas
    user_nome_display = current_user.nome_display or current_user.username
    user_foto = current_user.foto or ''
    # Itens atribuídos ao usuário logado
    itens_atribuidos = [item for item in itens if item.get('responsavel_username') == current_user.username]

    return render_template('pauta.html', evento_id=evento_id, evento=evento, itens=itens,
                           from_cache=from_cache, user_role=current_user.role,
                           user_categoria=current_user.categoria,
                           last_updated=last_updated, last_saved_user=last_saved_user,
                           assessores=assessores,
                           data_evento=data_evento,
                           eh_responsavel_pauta=eh_responsavel_pauta,
                           user_nome_display=user_nome_display,
                           user_foto=user_foto,
                           itens_atribuidos=itens_atribuidos)

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
                GRUPOS_VALIDOS = ['oposicao', 'minoria', 'PL', 'NOVO']
                grupo = None

                # 1. Tenta categoria do usuário logado
                cat_logado = (current_user.categoria or '').strip()
                if cat_logado and cat_logado.lower() in [g.lower() for g in GRUPOS_VALIDOS]:
                    grupo = cat_logado

                # 2. Se não (admin/geral), tenta responsavel_username da nota
                if not grupo:
                    c.execute('SELECT responsavel_username FROM notas WHERE item_key=? AND responsavel_username IS NOT NULL AND responsavel_username != ""', (prop_key,))
                    row_resp = c.fetchone()
                    if row_resp and row_resp[0]:
                        c.execute('SELECT categoria FROM users WHERE username=?', (row_resp[0],))
                        row_cat = c.fetchone()
                        if row_cat and row_cat[0] and row_cat[0].lower() in [g.lower() for g in GRUPOS_VALIDOS]:
                            grupo = row_cat[0]

                # 3. Se ainda não, tenta usuário que salvou por nome
                if not grupo:
                    c.execute('SELECT categoria FROM users WHERE username=? OR nome_display=?', (saved_by, saved_by))
                    row_cat = c.fetchone()
                    if row_cat and row_cat[0] and row_cat[0].lower() in [g.lower() for g in GRUPOS_VALIDOS]:
                        grupo = row_cat[0]

                if not grupo:
                    logger.warning(f"Orientação NÃO exportada: usuário '{saved_by}' categoria='{cat_logado}' não tem grupo válido")
                    grupo_exportado = None
                else:
                    c.execute('''INSERT OR REPLACE INTO orientacoes_grupo
                                 (evento_id, id_principal, grupo, orientacao, comentario, saved_by, saved_at)
                                 VALUES (?, ?, ?, ?, ?, ?, ?)''',
                              (evento_id, str(id_principal), grupo, orientacao, '', saved_by, now_str))
                    conn.commit()
                    logger.info(f"✅ Orientação exportada: grupo={grupo} id={id_principal} ori={orientacao} by={saved_by}")
                    grupo_exportado = grupo

            except Exception as e:
                logger.error(f"Erro ao exportar orientação: {e}")
                grupo_exportado = None
        else:
            grupo_exportado = None

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
        return jsonify({'message': 'Salvo com sucesso!', 'grupo_exportado': grupo_exportado})
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
        secao_criticas = "\n\nCRITICAS E PONTOS DE COMBATE (autoria ou relatoria de partido de esquerda/governo):\n- Criticas ao merito: principais criticas tecnicas, contradicoes, falhas, impactos negativos\n- Contradicoes com o discurso da esquerda: onde o projeto contradiz posicoes historicas do PT/PSOL/PCdoB\n- Discurso de combate para o Plenario: argumento direto e combativo para pronunciamento\n- Questionamentos ao relator: perguntas incisivas para fazer ao relator no plenario\n"

    estrutura = "📘 Resumo tecnico\n\ntexto do resumo\n\n🟢 Pontos positivos\n\ntexto dos pontos positivos\n\n🔴 Pontos negativos\n\ntexto dos pontos negativos\n\n⚖️ Riscos politicos e de imagem\n\ntexto dos riscos\n\n↔️ Orientacao sugerida\n\ntexto da orientacao\n\n"

    cabecalho = "Voce e um assessor legislativo da Camara dos Deputados, trabalhando para a Oposicao e Minoria.\n\n"
    dados = "Proposicao: %s\nAutores: %s\nRelator: %s\nEmenta: %s\n%s\n\n" % (str(projeto), str(autor), str(relator), str(ementa), str(contexto_doc))
    instrucao = "Gere uma nota tecnica em texto puro com esta estrutura. Cada titulo com emoji deve estar sozinho na linha, seguido de linha em branco.\n\n"
    regras = "Regras: sem HTML, sem asteriscos, sem markdown. Seja completo e nao corte a analise.\n"

    prompt = cabecalho + dados + instrucao + estrutura + regras + secao_criticas

    # ── Cadeia tripla: Groq → Cloudflare → Gemini ───────────────────────────
    try:
        # Para análise técnica completa, usa Gemini primeiro (melhor para textos longos)
        # Groq pode truncar por limite de tokens/minuto no plano gratuito
        texto = None
        fonte = None
        _erros_ia = []

        # 1. Gemini
        _gkey = os.environ.get('GEMINI_API_KEY', '')
        if _gkey:
            try:
                texto = gemini_post(_gkey, prompt, max_tokens=1500, temperatura=0.3, tentativas=2)
                if texto and texto.strip():
                    fonte = 'gemini'
            except Exception as _e:
                _erros_ia.append(f'Gemini: {_e}')
                logger.warning(f'analisar_ia: Gemini falhou — {_e}')

        # 2. Groq
        if not texto:
            _grok = os.environ.get('GROQ_API_KEY', '')
            if _grok:
                try:
                    texto = groq_post(prompt, max_tokens=1500, temperatura=0.3)
                    if texto and texto.strip():
                        fonte = 'groq'
                except Exception as _e:
                    _erros_ia.append(f'Groq: {_e}')
                    logger.warning(f'analisar_ia: Groq falhou — {_e}')

        # 3. Cloudflare
        if not texto:
            try:
                texto, fonte = ia_chain(prompt, max_tokens=1500, contexto="analisar_ia")
            except Exception as _e:
                _erros_ia.append(f'Cloudflare: {_e}')

        if not texto:
            raise Exception('; '.join(_erros_ia))
    except Exception as e:
        import traceback
        logger.error(f"ia_chain falhou em analisar_ia: {e}\n{traceback.format_exc()[-300:]}")
        return jsonify({'error': 'Serviço de IA indisponível. Tente novamente.'}), 503

    # Pós-processamento: garante que cada título fique sozinho na linha
    titulos_secao = ['📘', '🟢', '🔴', '⚖️', '↔️', '⚠️']
    linhas = texto.split('\n')
    linhas_corrigidas = []
    for linha in linhas:
        linha_strip = linha.strip()
        separado = False
        for emoji in titulos_secao:
            if linha_strip.startswith(emoji) and len(linha_strip) > len(emoji) + 30:
                partes = linha_strip.split(None, 5)
                if len(partes) >= 3:
                    titulo_palavras = [partes[0]]
                    i = 1
                    while i < len(partes) and i < 4:
                        titulo_palavras.append(partes[i])
                        i += 1
                        if partes[i-1][-1:] in '.,:' or len(' '.join(titulo_palavras)) > 30:
                            break
                    titulo = ' '.join(titulo_palavras)
                    resto = linha_strip[len(titulo):].strip()
                    if resto:
                        linhas_corrigidas.append(titulo)
                        linhas_corrigidas.append('')
                        linhas_corrigidas.append(resto)
                        separado = True
                        break
        if not separado:
            linhas_corrigidas.append(linha)
    texto = '\n'.join(linhas_corrigidas)

    TITULOS_MAP = {
        '📘': ('📘 Resumo Técnico',                '#0D2B5E'),
        '🟢': ('🟢 Pontos Positivos',              '#1A6B3A'),
        '🔴': ('🔴 Pontos Negativos',              '#8B0000'),
        '⚖️': ('⚖️ Riscos Políticos e de Imagem', '#7B5C00'),
        '↔️': ('↔️ Orientação Sugerida',           '#0D2B5E'),
        '⚠️': ('⚠️ Críticas e Pontos de Combate', '#8B0000'),
    }
    html_linhas = []
    for linha in texto.split('\n'):
        emoji_enc = next((e for e in TITULOS_MAP if linha.strip().startswith(e)), None)
        if emoji_enc:
            titulo_padrao, cor = TITULOS_MAP[emoji_enc]
            html_linhas.append(f'<p><strong><span style="font-size:16px;color:{cor};">{titulo_padrao}</span></strong></p>')
        elif linha.strip():
            html_linhas.append(f'<p>{linha}</p>')
        else:
            html_linhas.append('<p><br></p>')

    html_final = '\n'.join(html_linhas)
    if nota_req:
        html_final = nota_req + html_final
    return jsonify({'resumo': html_final, 'fonte': fonte, 'parecer': parecer_info})

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

    # 3. Busca emendas via tramitações da API (fonte alternativa quando fichadetramitacao dá 403)
    try:
        r_tram = requests.get(
            f"https://dadosabertos.camara.leg.br/api/v2/proposicoes/{id_proposicao}/tramitacoes?itens=50&ordem=DESC",
            headers={'Accept': 'application/json', 'User-Agent': 'Mozilla/5.0'}, timeout=10
        )
        if r_tram.ok:
            for t in r_tram.json().get('dados', []):
                despacho = (t.get('despacho', '') or '').upper()
                # Detecta menção de emenda no despacho
                m_emd = re.search(r'EMENDA\s*(?:N[Âº°.]?\s*)?(\d+)', despacho)
                if m_emd:
                    num_emd = m_emd.group(1)
                    label = f"📋 EMD nº {num_emd} — mencionada em tramitação"
                    # Sem URL direta — será resolvida manualmente
                    chave = f"EMD{num_emd}"
                    if chave not in vistos:
                        vistos.add(chave)
                        docs.append({
                            'label':    label,
                            'url':      '',
                            'filename': f'EMD{num_emd}',
                            'tipo':     'Emenda',
                            'data':     t.get('dataHora', '')[:10],
                        })
    except Exception as e:
        logger.warning(f"Erro busca emendas tramitações: {e}")

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

@app.route('/comparar_documentos', methods=['POST'])
@login_required
def comparar_documentos():
    """Extrai dois PDFs e pede à IA para comparar as diferenças."""
    data    = request.get_json()
    url1    = data.get('url_doc1','')
    label1  = data.get('label_doc1','Documento 1')
    url2    = data.get('url_doc2','')
    label2  = data.get('label_doc2','Documento 2')
    if not url1 or not url2:
        return jsonify({'error': 'Duas URLs são necessárias.'})
    try:
        texto1 = (extrair_texto_documento(url1) or '')[:8000]
        texto2 = (extrair_texto_documento(url2) or '')[:8000]
        if not texto1 or not texto2:
            return jsonify({'error': 'Não foi possível extrair o texto de um ou ambos os documentos.'})

        prompt = f"""Você é um assessor parlamentar especialista em análise de textos legislativos.
Compare os dois documentos abaixo e liste de forma clara e estruturada as principais diferenças entre eles.
Destaque: (1) O que foi adicionado, (2) O que foi removido, (3) O que foi modificado.
Seja objetivo e use linguagem técnica parlamentar.

DOCUMENTO 1 — {label1}:
{texto1}

DOCUMENTO 2 — {label2}:
{texto2}

Liste as diferenças de forma clara e numerada:"""

        gemini_key = os.environ.get('GEMINI_API_KEY','')
        try:
            texto_ia, fonte = ia_chain(prompt, max_tokens=2000, contexto="comparar_documentos")
        except Exception as e:
            logger.error(f"ia_chain falhou em comparar_documentos: {e}")
            return jsonify({'error': 'Serviço de IA indisponível. Tente novamente.'})
        aviso = '⚠️ Gemini indisponível, analisando via fallback...\n\n' if fonte != 'gemini' else ''
        return jsonify({'comparacao': aviso + texto_ia, 'fonte': fonte})
    except Exception as e:
        logger.error(f"Erro ao comparar documentos: {e}")
        return jsonify({'error': str(e)})


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

    # Extrai texto limpo da análise já feita
    analise_texto = re.sub(r'<[^>]+>', ' ', analise_html).strip()

    # Detecta tipo de destaque pela descrição
    descricao_upper = descricao.upper()

    # DESTAQUE DE EMENDA ou DESTAQUE DE PREFERÊNCIA:
    #   SIM = aprova a emenda/preferência → ALTERA o texto do relator
    #   NÃO = rejeita → MANTÉM o texto do relator
    eh_emenda_ou_preferencia = (
        'DESTAQUE DE EMENDA' in descricao_upper or
        'DESTAQUE DE PREFERÊNCIA' in descricao_upper or
        'DESTAQUE DE PREFERENCIA' in descricao_upper or
        any(p in descricao_upper for p in ['EMD ', 'SUBEMENDA', 'EMENDA AGLUTINATIVA'])
    )

    # DESTAQUE (em separado, supressivo, etc.) — lógica inversa:
    #   SIM = mantém o texto do relator
    #   NÃO = altera o texto do relator
    eh_destaque_separado = not eh_emenda_ou_preferencia

    if eh_emenda_ou_preferencia:
        regra_sim_nao = """REGRA FUNDAMENTAL para DESTAQUE DE EMENDA e DESTAQUE DE PREFERÊNCIA:
- O destaque quer votar a emenda/preferência em separado para aprová-la
- Voto SIM = APROVA a emenda/preferência → ALTERA o texto do relator (acata a mudança)
- Voto NÃO = REJEITA a emenda/preferência → MANTÉM o texto do relator"""
        sim_label_default = "Aprova / Altera o texto do relator"
        nao_label_default = "Rejeita / Mantém o texto do relator"
    else:
        regra_sim_nao = """REGRA FUNDAMENTAL para DESTAQUE (em separado, supressivo, etc.):
- O destaque quer votar um trecho do texto do relator em separado
- Voto SIM = MANTÉM o texto do relator (aprovado como está)
- Voto NÃO = ALTERA ou SUPRIME o texto do relator"""
        sim_label_default = "Mantém o texto do relator"
        nao_label_default = "Altera / Suprime o texto do relator"

    prompt = f"""Você é um assessor legislativo especializado na Câmara dos Deputados do Brasil.

**Proposição:** {projeto}
**Destaque:** {numero}
**Descrição:** {descricao}
**Análise já realizada:** {analise_texto}

{regra_sim_nao}

Com base na descrição e análise acima, gere APENAS um JSON válido (sem markdown, sem explicações):

{{
  "titulo": "{projeto} – titulo curto da proposicao max 60 chars",
  "dtq": "{numero} - autoria resumida",
  "descricao": "(descrição resumida do destaque, máx 120 chars)",
  "sim_label": "{sim_label_default}",
  "sim_conteudo": "(O que significa votar SIM — efeito prático em 1-2 frases curtas)",
  "nao_label": "{nao_label_default}",
  "nao_conteudo": "(O que significa votar NÃO — efeito prático em 1-2 frases curtas)",
  "explicacao": "(Explicação completa do dispositivo destacado e impacto, 3-5 frases)"
}}

Responda APENAS com o JSON, sem ```json, sem comentários."""

    try:
        texto, fonte = ia_chain(prompt, temperatura=0.2, contexto="gerar_quadro_dtq")
        texto = re.sub(r'```(?:json)?|```', '', texto).strip()
        dados = json.loads(texto)
        return jsonify({'ok': True, 'dados': dados, 'fonte': fonte})
    except json.JSONDecodeError as e:
        logger.warning(f"gerar_quadro_dtq: JSON inválido: {e}")
        return jsonify({'ok': False, 'error': 'Resposta da IA não é JSON válido.'}), 500
    except Exception as e:
        logger.error(f"gerar_quadro_dtq: todos os provedores falharam: {e}")
        return jsonify({'ok': False, 'error': 'Serviço de IA indisponível. Tente novamente.'}), 500

@app.route('/monitor_status/<int:evento_id>')
@login_required
def monitor_status(evento_id):
    """
    Retorna status atual dos itens da pauta para o agente de monitoramento.
    Tenta múltiplas fontes: API votações, API pauta, página HTML.
    """
    resultado = {'evento_id': evento_id, 'itens': [], 'texto': '', 'fonte': '', 'fontes_status': {}}

    headers_camara = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36',
        'Accept': 'application/json, text/html, */*',
        'Accept-Language': 'pt-BR,pt;q=0.9',
        'Referer': 'https://www.camara.leg.br/',
    }

    # Fonte 1: API de votações
    try:
        r = requests.get(
            f'https://dadosabertos.camara.leg.br/api/v2/votacoes?idEvento={evento_id}&itens=10&ordem=DESC',
            headers={**headers_camara, 'Accept': 'application/json'}, timeout=8
        )
        resultado['fontes_status']['api_votacoes'] = r.status_code
        if r.ok:
            votacoes = r.json().get('dados', [])
            for v in votacoes:
                resultado['itens'].append({
                    'proposicao': v.get('proposicaoObjeto', '') or v.get('descricao', ''),
                    'situacao': 'Em Votação' if v.get('dataHoraRegistro') else '',
                    'aprovado': v.get('aprovado'),
                    'sim': v.get('totalVotosSim', ''),
                    'nao': v.get('totalVotosNao', ''),
                })
            resultado['fonte'] = 'api_votacoes'
    except Exception as e:
        resultado['fontes_status']['api_votacoes'] = str(e)

    # Fonte 2: Página HTML do evento
    try:
        r2 = requests.get(
            f'https://www.camara.leg.br/evento-legislativo/{evento_id}',
            headers=headers_camara, timeout=12
        )
        resultado['fontes_status']['html_evento'] = r2.status_code
        if r2.ok and len(r2.text) > 100:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(r2.text, 'html.parser')
            for tag in soup(['script', 'style', 'noscript']):
                tag.decompose()
            texto = ' '.join(soup.get_text(' ').split())
            resultado['texto'] = texto[:5000]
            resultado['fonte'] += '+html'
    except Exception as e:
        resultado['fontes_status']['html_evento'] = str(e)

    # Fonte 3: API de pauta
    try:
        r3 = requests.get(
            f'https://dadosabertos.camara.leg.br/api/v2/eventos/{evento_id}/pauta',
            headers={**headers_camara, 'Accept': 'application/json'}, timeout=8
        )
        resultado['fontes_status']['api_pauta'] = r3.status_code
        if r3.ok:
            pauta = r3.json().get('dados', [])
            for item in pauta:
                sit = item.get('situacaoItem', '') or ''
                if sit:
                    resultado['itens'].append({
                        'proposicao': item.get('proposicao', {}).get('siglaTipo', '') + ' ' + str(item.get('proposicao', {}).get('numero', '')),
                        'situacao': sit,
                        'aprovado': None,
                    })
            if pauta:
                resultado['fonte'] += '+api_pauta'
    except Exception as e:
        logger.warning(f"monitor_status pauta: {e}")

    return jsonify(resultado)

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

def buscar_texto_emenda(id_proposicao, descricao, num_emenda=None):
    """
    Busca o texto da emenda referenciada na descrição do destaque.
    Acessa prop_emendas e extrai o PDF da emenda específica.
    """
    from bs4 import BeautifulSoup
    import pdfplumber
    from io import BytesIO

    headers = {'User-Agent': 'Mozilla/5.0 Chrome/124.0.0.0 Safari/537.36'}

    # Usa o número passado diretamente; só extrai da descrição como fallback
    if not num_emenda:
        m_num = re.search(r'(\d+)\s*$', descricao.strip())
        if not m_num:
            m_num = re.search(r'(?:EMD|Emenda|EMENDA)\s*(?:[^\d]*)(\d+)', descricao, re.IGNORECASE)
        num_emenda = m_num.group(1) if m_num else None

    logger.info(f"buscar_texto_emenda: id={id_proposicao} num_emenda={num_emenda}")

    try:
        url = f"https://www.camara.leg.br/proposicoesWeb/prop_emendas?idProposicao={id_proposicao}&subst=0"
        r = requests.get(url, headers=headers, timeout=12)
        if not r.ok:
            logger.warning(f"Página de emendas retornou {r.status_code}")
            return None, None

        soup = BeautifulSoup(r.text, 'html.parser')

        # Coleta todas as emendas com seus links
        emendas = []
        vistos = set()
        for a in soup.find_all('a', href=True):
            href = a['href']
            if 'codteor' not in href and 'mostrarintegra' not in href:
                continue
            # Extrai número EXCLUSIVAMENTE pelo EMP+N no parâmetro filename
            # Ex: filename=EMP+3+%3D%3E → num='3'
            # Ex: filename=EMP+2+%3D%3E → num='2'  
            m_emp = re.search(r'[?&]filename=EMP[+](\d+)', href)
            if not m_emp:
                # Fallback: qualquer EMP+N no href
                m_emp = re.search(r'\bEMP[+](\d+)\b', href)
            if not m_emp:
                continue
            num = m_emp.group(1)
            if num in vistos:
                continue
            vistos.add(num)
            url_doc = camara_url(href)
            # Pega texto da linha para contexto
            row = a.find_parent('tr')
            txt_row = (row.get_text(' ', strip=True) if row else a.get_text(strip=True))[:120]
            emendas.append((num, url_doc, txt_row))
            logger.info(f"Emenda EMP+{num}: {href[:80]}")

        if not emendas:
            # Tenta links diretos com codteor
            for a in soup.find_all('a', href=True):
                if 'codteor' in a['href']:
                    txt_ctx = a.get_text(strip=True)
                    m = re.search(r'(\d+)', txt_ctx)
                    num = m.group(1) if m else str(len(emendas)+1)
                    emendas.append((num, camara_url(a['href']), txt_ctx[:80]))

        logger.info(f"Emendas encontradas para {id_proposicao}: {[(e[0],e[2][:60]) for e in emendas]}")

        # Seleciona a emenda correta
        emenda_sel = None
        if num_emenda:
            # 1. Tenta pelo número exato do EMP (EMP+3 → num='3')
            emenda_sel = next((e for e in emendas if e[0] == num_emenda), None)

            # 2. Se não encontrou EMP+N, pega a N-ésima da lista (contagem ordinal)
            # Ex: "Emenda n. 3" → 3ª emenda disponível (índice 2)
            if not emenda_sel:
                idx = int(num_emenda) - 1
                if 0 <= idx < len(emendas):
                    emenda_sel = emendas[idx]
                    logger.info(f"EMP+{num_emenda} não encontrado — usando {idx+1}ª emenda da lista: EMP+{emenda_sel[0]}")
                else:
                    logger.warning(f"Emenda n. {num_emenda} não encontrada. Disponíveis: {[e[0] for e in emendas]}")

        if not emenda_sel and emendas:
            emenda_sel = emendas[0]

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


@app.route('/listar_emendas/<int:id_prop>')
@login_required
def listar_emendas(id_prop):
    """Lista todas as emendas disponíveis para uma proposição."""
    from bs4 import BeautifulSoup
    headers = {'User-Agent': 'Mozilla/5.0 Chrome/124.0.0.0 Safari/537.36'}
    try:
        url = f"https://www.camara.leg.br/proposicoesWeb/prop_emendas?idProposicao={id_prop}&subst=0"
        r = requests.get(url, headers=headers, timeout=12)
        if not r.ok:
            return jsonify({'emendas': [], 'erro': f'HTTP {r.status_code}'})

        soup = BeautifulSoup(r.text, 'html.parser')
        emendas = []
        vistos = set()

        for row in soup.find_all('tr'):
            txt = row.get_text(' ', strip=True)
            m = re.search(r'(?:EMD|Emenda)\s*[Nn]?[º°.]?\s*(\d+)', txt, re.IGNORECASE)
            if not m:
                continue
            num = m.group(1)
            if num in vistos:
                continue
            vistos.add(num)
            # Pega o link do PDF
            link = row.find('a', href=True)
            if link:
                url_doc = camara_url(link['href'])
                label = f"Emenda nº {num}"
                # Tenta extrair mais info (autor, tipo)
                m_tipo = re.search(r'(Aglutinativa|Substitutiva|de Plenário)', txt, re.IGNORECASE)
                if m_tipo:
                    label = f"Emenda {m_tipo.group(1)} nº {num}"
                emendas.append({'num': num, 'label': label, 'url': url_doc, 'txt': txt[:100]})

        # Ordena por número
        emendas.sort(key=lambda e: int(e['num']) if e['num'].isdigit() else 0)
        logger.info(f"Emendas para {id_prop}: {[e['label'] for e in emendas]}")
        return jsonify({'emendas': emendas, 'total': len(emendas)})
    except Exception as e:
        logger.warning(f"listar_emendas {id_prop}: {e}")
        return jsonify({'emendas': [], 'erro': str(e)})

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

    # ── Destaque de emenda: usa URL passada diretamente pelo frontend ──
    url_emenda_sel = data.get('url_emenda', '')  # URL específica da emenda selecionada
    num_emenda_sel = data.get('num_emenda', '')

    if eh_emenda and id_principal and not trecho_manual:
        # Usa o número enviado pelo frontend (já extraído corretamente do título do DTQ)
        num_emenda_desc = num_emenda_sel or ''

        # Se não veio do frontend, tenta extrair da descrição
        if not num_emenda_desc:
            m_num_emd = re.search(r'(\d+)\s*$', descricao.strip())
            if not m_num_emd:
                m_num_emd = re.search(r'(?:EMD|Emenda)\s*(?:[^\d]*)(\d+)', descricao, re.IGNORECASE)
            num_emenda_desc = m_num_emd.group(1) if m_num_emd else ''

        logger.info(f"Analisando emenda nº '{num_emenda_desc}' (frontend enviou: '{num_emenda_sel}') | descrição: {descricao}")

        # Se frontend passou URL específica, usa ela
        if url_emenda_sel:
            texto_emenda = extrair_texto_documento(url_emenda_sel) or ''
            label_emenda = f"Emenda nº {num_emenda_desc}" if num_emenda_desc else 'Emenda selecionada'
        else:
            # Busca PDF pelo número da emenda
            texto_emenda, label_emenda = buscar_texto_emenda(id_principal, descricao, num_emenda_desc)

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

<p><strong>Objeto:</strong> (o que a emenda propõe alterar em 1 frase)</p>
<br>
<p><strong>Texto da Emenda:</strong></p>
<blockquote style="border-left:3px solid #1A6B3A;padding-left:10px;color:#333;font-style:italic;">
(trecho principal da emenda, literalmente)
</blockquote>
<br>
<p><strong>Voto SIM (aprova a emenda):</strong><br>(efeito prático — o que muda. Máx 80 palavras.)</p>
<br>
<p><strong>Voto NÃO (rejeita a emenda):</strong><br>(texto do relator prevalece — impacto. Máx 60 palavras.)</p>

Não use ### ou ** fora do HTML."""

            try:
                    texto_resp, fonte = ia_chain(prompt_emenda, contexto="destaque_emenda_pdf")
                    aviso = '<p><em style="color:#cc6600;">⚠️ Analisado via fallback IA.</em></p><br>' if fonte != 'groq' else ''
                    return jsonify({'resumo': aviso + texto_resp, 'doc_usado': tipo_doc})
            except Exception as e:
                    logger.error(f"ia_chain falhou (destaque emenda pdf): {e}")
            return jsonify({'error': 'Falha ao analisar emenda.'}), 500

        # ── Bloco else: PDF não disponível, usa avulso/PRLP ──────────────
        else:
            logger.info(f"PDF da emenda não disponível — usando avulso com contexto do número {num_emenda_desc}")
            doc_base = buscar_texto_prlp_ou_sbt(id_principal)
            texto_base = doc_base.get('texto', '') if doc_base else ''
            label_base = f"{doc_base.get('tipo','')} nº {doc_base.get('numero','')}" if doc_base else 'texto da proposição'

            if not texto_base:
                return jsonify({'error': f'Texto da Emenda nº {num_emenda_desc} não disponível. Use o botão Debug para colar o texto manualmente.'}), 400

            tipo_doc = f"Emenda nº {num_emenda_desc} — {numero}" if num_emenda_desc else descricao
            regra = ('REGRA: Voto SIM = APROVA a emenda → altera texto do relator. '
                     'Voto NÃO = REJEITA a emenda → mantém texto do relator.')
            prompt_emenda = f"""Você é um assessor legislativo da Câmara dos Deputados.

**Proposição:** {projeto}
**Destaque:** {numero}
**Descrição completa do destaque:** {descricao}
**Emenda objeto do destaque:** nº {num_emenda_desc}

ATENÇÃO: Este é especificamente o destaque referente à **Emenda nº {num_emenda_desc}**.
O texto integral desta emenda não está disponível, mas abaixo está o texto base ({label_base}).
Baseie sua análise na descrição do destaque acima e no número da emenda.

TEXTO BASE ({label_base}):
{texto_base[:6000]}

{regra}

Gere análise HTML específica para a **Emenda nº {num_emenda_desc}** ({numero}):

<p><strong>Emenda nº {num_emenda_desc}:</strong> descreva o que esta emenda propoe baseado no contexto</p>
<br>
<p><strong>Voto SIM — aprova a Emenda nº {num_emenda_desc}:</strong><br>descreva a consequencia de aprovar esta emenda. Max 80 palavras.</p>
<br>
<p><strong>Voto NÃO — rejeita a Emenda nº {num_emenda_desc}:</strong><br>o texto do relator prevalece. Max 60 palavras.</p>

Não use ### ou ** fora do HTML. Não repita análise de outras emendas."""

            try:
                    texto_resp, fonte = ia_chain(prompt_emenda, contexto="destaque_emenda_sem_pdf")
                    aviso = '<p><em style="color:#cc6600;">⚠️ Analisado via fallback IA.</em></p><br>' if fonte != 'groq' else ''
                    return jsonify({'resumo': aviso + texto_resp, 'doc_usado': tipo_doc})
            except Exception as e:
                    logger.error(f"ia_chain falhou (destaque emenda sem pdf): {e}")
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

<p><strong>Objeto do Destaque:</strong> (descreva em uma frase o que o destaque vota em separado)</p>
<br>
<p><strong>Trecho do Texto:</strong></p>
<blockquote style="border-left:3px solid #1A6B3A; padding-left:10px; color:#333; font-style:italic;">
(Trecho literal encontrado. Se usou matching flexível, indique: "Lei X mencionada no destaque corresponde a 'Lei nº X, de DD de mês de AAAA' no documento". Se não localizar mesmo com busca flexível, explique qual número buscou.)
</blockquote>
<br>
<p><strong>Análise:</strong><br>
(Explique o que esse trecho propõe e o impacto prático de aprovar ou rejeitar este destaque. Máx 150 palavras.)
</p>

Não use ### ou ** fora do HTML."""

    try:
        texto, fonte = ia_chain(prompt, contexto="destaque_normal")
        aviso = '<p><em style="color:#cc6600;">⚠️ Analisado via fallback IA.</em></p><br>' if fonte != 'groq' else ''
        return jsonify({'resumo': aviso + texto, 'doc_usado': tipo_doc})
    except Exception as e:
        logger.error(f"ia_chain falhou em analisar_destaque: {e}")
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

@app.route('/debug_pdf_texto/<int:evento_id>')
@login_required
def debug_pdf_texto(evento_id):
    """Mostra o texto bruto extraído do PDF de pauta."""
    try:
        from bs4 import BeautifulSoup
        import pdfplumber
        url = f"https://www.camara.leg.br/evento-legislativo/{evento_id}"
        r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=12)
        soup = BeautifulSoup(r.text, 'html.parser')
        pdf_url = None
        for a in soup.find_all('a', href=True):
            txt = a.get_text(strip=True).lower()
            href = a['href']
            if 'codteor' in href and txt == 'pauta':
                pdf_url = camara_url(href)
                pdf_url += ('&' if '?' in pdf_url else '?') + 'tipo=PDF'
                break
        if not pdf_url:
            return jsonify({'erro': 'PDF não encontrado'})
        rp = requests.get(pdf_url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=20)
        with pdfplumber.open(BytesIO(rp.content)) as pdf:
            texto = '\n'.join(p.extract_text() or '' for p in pdf.pages)
        # Filtra só linhas relevantes (com número no início ou tipo de proposição)
        linhas = texto.split('\n')
        relevantes = []
        for i, l in enumerate(linhas):
            l2 = l.strip()
            if re.match(r'^\d{1,2}\.', l2) or re.search(r'PROJETO|REQUERIMENTO|PROPOSTA|MEDIDA|PL |PEC |PLP |REQ ', l2, re.I):
                relevantes.append({'i': i, 'txt': l2[:200]})
        return jsonify({'total_linhas': len(linhas), 'relevantes': relevantes, 'primeiras_100': linhas[:100]})
    except Exception as e:
        import traceback
        return jsonify({'erro': str(e), 'tb': traceback.format_exc()[-1000:]})

@app.route('/debug_pauta_full/<int:evento_id>')
@login_required
def debug_pauta_full(evento_id):
    """Debug completo: ordem PDF, itens API, matching e problemas."""
    try:
        from scraper_camara import obter_itens_pauta as _oip
        import traceback

        out = {'evento_id': evento_id, 'problemas': [], 'pdf': {}, 'api': {}, 'matching': []}

        # 1. Extrai ordem do PDF
        try:
            ordem = buscar_ordem_oficial(evento_id)
            out['pdf']['ordem'] = ordem
            out['pdf']['total'] = len(ordem)
        except Exception as e:
            out['pdf']['erro'] = str(e)
            ordem = {}

        # 2. Itens da API
        try:
            itens_raw = _oip(evento_id)
            out['api']['total'] = len(itens_raw)
            out['api']['itens'] = [{'codigo': it['codigo'], 'norm': _normalizar_codigo(it['codigo']),
                                    'id': it.get('id_principal',''), 'ementa': it.get('ementa','')[:60]} for it in itens_raw]
        except Exception as e:
            out['api']['erro'] = str(e)
            itens_raw = []

        # 3. Matching
        api_por_codigo = {_normalizar_codigo(it['codigo']): it for it in itens_raw}

        for cod_pdf, pos in sorted(ordem.items(), key=lambda x: x[1]):
            item_api = api_por_codigo.get(cod_pdf)
            # Fuzzy match por número
            m_num = re.search(r'(\d{4,})', cod_pdf)
            fuzzy = None
            if not item_api and m_num:
                for cod_api, it_api in api_por_codigo.items():
                    if m_num.group(1) in cod_api:
                        fuzzy = cod_api
                        item_api = it_api
                        break
            out['matching'].append({
                'pos': pos, 'cod_pdf': cod_pdf,
                'match_exato': api_por_codigo.get(cod_pdf) is not None,
                'match_fuzzy': fuzzy,
                'cod_api': item_api['codigo'] if item_api else None,
                'id': item_api.get('id_principal','') if item_api else None,
                'status': 'OK' if item_api else '❌ SEM MATCH'
            })
            if not item_api:
                out['problemas'].append(f"Pos {pos}: '{cod_pdf}' sem match na API → vai aparecer como 'dados não disponíveis'")

        # 4. Itens da API não encontrados no PDF
        for it in itens_raw:
            cod = _normalizar_codigo(it['codigo'])
            if cod not in ordem:
                m_num = re.search(r'(\d{4,})', cod)
                no_pdf = any(m_num and m_num.group(1) in k for k in ordem) if m_num else False
                out['problemas'].append(
                    f"'{it['codigo']}' (norm={cod}) não está no PDF → " +
                    (f"fuzzy match possível" if no_pdf else "vai para o FIM da lista")
                )

        # 5. REQ s/n
        req_sn_pdf = [(k,v) for k,v in ordem.items() if k.startswith('REQSN')]
        req_sn_api = [it for it in itens_raw
                      if re.match(r'(REQ|RQS|RQU|REC)', it['codigo'].upper())
                      and not re.search(r'\d{2,}', it['codigo'].split('/')[0])]
        out['req_sn'] = {
            'no_pdf': req_sn_pdf,
            'na_api': [{'codigo': it['codigo'], 'ementa': it.get('ementa','')[:80]} for it in req_sn_api],
            'match': list(zip([k for k,v in req_sn_pdf], [it['codigo'] for it in req_sn_api]))
        }

        return jsonify(out), 200, {'Content-Type': 'application/json; charset=utf-8'}

    except Exception as e:
        return jsonify({'erro': str(e), 'tb': traceback.format_exc()}), 500

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
        c.execute('DELETE FROM resumos_ia WHERE evento_id = ?', (evento_id,))
        conn.commit()
        pauta_cache.pop(str(evento_id), None)
        pauta_cache.clear()
        logger.info(f"✅ Cache e resumos IA limpos para evento {evento_id}")
        if request.method == 'GET':
            return redirect(url_for('view_pauta', evento_id=evento_id, force_reload='true'))
        return jsonify({'message': f'Cache e resumos IA do evento {evento_id} limpos.'})
    except Exception as e:
        logger.error(f"Erro ao limpar cache: {e}")
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

@app.route('/limpar_resumos_ia/<int:evento_id>', methods=['POST'])
@login_required
def limpar_resumos_ia(evento_id):
    """Remove resumos IA salvos para forçar regeração."""
    conn = get_conn()
    c = conn.cursor()
    try:
        c.execute('DELETE FROM resumos_ia WHERE evento_id=?', (evento_id,))
        n = c.rowcount
        conn.commit()
        return jsonify({'message': f'{n} resumos removidos. Recarregue a pauta.'})
    except Exception as e:
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
    data       = request.get_json()
    resumo     = data.get('resumo', '')
    groq_key   = os.environ.get('GROQ_API_KEY', '')
    gemini_key = os.environ.get('GEMINI_API_KEY', '')

    resumo_trunc = str(resumo)[:300]
    prompt_kw = ("Extraia 2 ou 3 palavras-chave em inglês para buscar uma imagem que ilustre "
                 "o tema desta proposição legislativa brasileira.\n"
                 "Responda APENAS com as palavras separadas por espaço, sem explicação.\n"
                 "Proposição: " + resumo_trunc)

    keywords = ''

    # Tenta Gemini
    if gemini_key and not keywords:
        try:
            r = requests.post(
                "https://generativelanguage.googleapis.com/v1beta/models/" + detectar_modelo_gemini(gemini_key) + ":generateContent?key=" + gemini_key,
                headers={"Content-Type": "application/json"},
                json={"contents": [{"parts": [{"text": prompt_kw}]}],
                      "generationConfig": {"maxOutputTokens": 20, "temperature": 0.1}},
                timeout=8
            )
            if r.ok:
                keywords = r.json()['candidates'][0]['content']['parts'][0]['text'].strip()
                keywords = re.sub(r'[^\w\s]', '', keywords).strip()
        except Exception as e:
            logger.warning(f"Erro keywords imagem (Gemini): {e}")

    # Tenta Groq
    if groq_key and not keywords:
        try:
            r = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": "Bearer " + groq_key, "Content-Type": "application/json"},
                json={"model": "llama-3.3-70b-versatile",
                      "messages": [{"role": "user", "content": prompt_kw}],
                      "max_tokens": 20, "temperature": 0.1},
                timeout=8
            )
            if r.ok:
                keywords = r.json()['choices'][0]['message']['content'].strip()
                keywords = re.sub(r'[^\w\s]', '', keywords).strip()
        except Exception as e:
            logger.warning(f"Erro keywords imagem (Groq): {e}")

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
        return jsonify({'resumo': ''})

    proj_base = projeto.split(' ao ')[0].strip()
    siglas_req = ('REQ', 'RQS', 'RQU', 'REC')
    eh_req = any(proj_base.upper().startswith(s) for s in siglas_req)

    # ── Para REQ: busca ementa completa e PL referenciado ────────────────────
    contexto_pl = ''
    if eh_req:
        try:
            # Passo 1: busca ementa completa do REQ (API retorna truncada)
            ementa_completa = ementa or ''
            if id_principal:
                try:
                    r_req = requests.get(
                        f"https://dadosabertos.camara.leg.br/api/v2/proposicoes/{id_principal}",
                        headers={'Accept': 'application/json'}, timeout=6
                    )
                    if r_req.ok:
                        ementa_api = r_req.json().get('dados', {}).get('ementa', '')
                        if ementa_api and len(ementa_api) > len(ementa_completa):
                            ementa_completa = ementa_api
                            logger.info(f"Ementa completa REQ {id_principal}: {ementa_completa[:80]}")
                except Exception as e:
                    logger.warning(f"Erro buscar ementa completa: {e}")

            # Passo 2: extrai sigla+número+ano do PL referenciado na ementa completa
            # Suporta tanto sigla curta (PLP 221/2024) quanto texto por extenso
            # ("Projeto de Lei Complementar nº 221, de 2024")
            sigla_ref = num_ref = ano_ref = ''

            for txt in [ementa_completa, projeto]:
                sigla_ref, num_ref, ano_ref = _extrair_sigla_num_ano(txt)
                if sigla_ref and num_ref and ano_ref:
                    logger.info(f"REQ {id_principal}: PL extraído de '{txt[:60]}' → {sigla_ref} {num_ref}/{ano_ref}")
                    break

            # Passo 3: se não achou sigla+número+ano completos → retorna vazio
            # Nunca faz busca sem ano para evitar pegar PL errado
            if not (sigla_ref and num_ref and ano_ref):
                logger.warning(f"REQ {id_principal}: não encontrou PL com sigla+num+ano — retornando vazio")
                return jsonify({'resumo': ''})

            logger.info(f"REQ referencia: {sigla_ref} {num_ref}/{ano_ref}")

            # Passo 4: busca ementa do PL referenciado com sigla+número+ano exatos
            r_pl = requests.get(
                f"https://dadosabertos.camara.leg.br/api/v2/proposicoes"
                f"?siglaTipo={sigla_ref}&numero={num_ref}&ano={ano_ref}&itens=1",
                headers={'Accept': 'application/json'}, timeout=8
            )
            if not r_pl.ok:
                logger.warning(f"API PL {sigla_ref} {num_ref}/{ano_ref}: HTTP {r_pl.status_code}")
                return jsonify({'resumo': ''})

            dados_pl = r_pl.json().get('dados', [])
            if not dados_pl:
                logger.warning(f"PL {sigla_ref} {num_ref}/{ano_ref} não encontrado na API")
                return jsonify({'resumo': ''})

            ementa_pl  = dados_pl[0].get('ementa', '')
            sigla_real = dados_pl[0].get('siglaTipo', sigla_ref)
            num_real   = dados_pl[0].get('numero', num_ref)
            ano_real   = dados_pl[0].get('ano', ano_ref)

            if not ementa_pl:
                logger.warning(f"PL {sigla_ref} {num_ref}/{ano_ref} sem ementa")
                return jsonify({'resumo': ''})

            contexto_pl = f"\nO {sigla_real} {num_real}/{ano_real} (referenciado) trata de: {ementa_pl}"
            logger.info(f"PL referenciado encontrado: {sigla_real} {num_real}/{ano_real}")

        except Exception as e:
            logger.error(f"Erro ao buscar PL do REQ: {e}")
            return jsonify({'resumo': ''})

    # ── Para não-REQ: busca PL mencionado na ementa se houver ────────────────
    elif not contexto_pl:
        try:
            m_pl = None
            for txt in [ementa, projeto]:
                for padrao in [
                    r'\b(PLP|PLC|PEC|MPV|PDL|PL)\s+n[º°.]?\s*([\d.]+)[,\s/]+(?:de\s+)?(\d{4})',
                    r'\b(PLP|PLC|PEC|MPV|PDL|PL)\s+([\d.]+)[/\-](\d{4})',
                ]:
                    m_pl = re.search(padrao, txt, re.IGNORECASE)
                    if m_pl: break
                if m_pl: break
            if m_pl:
                sigla_ref = m_pl.group(1).upper()
                num_ref   = m_pl.group(2).replace('.', '')
                ano_ref   = m_pl.group(3)
                r_api = requests.get(
                    f"https://dadosabertos.camara.leg.br/api/v2/proposicoes"
                    f"?siglaTipo={sigla_ref}&numero={num_ref}&ano={ano_ref}&itens=1",
                    headers={'Accept': 'application/json'}, timeout=8
                )
                if r_api.ok:
                    dados = r_api.json().get('dados', [])
                    if dados and dados[0].get('ementa'):
                        ementa_pl  = dados[0]['ementa']
                        sigla_real = dados[0].get('siglaTipo', sigla_ref)
                        num_real   = dados[0].get('numero', num_ref)
                        ano_real   = dados[0].get('ano', ano_ref)
                        contexto_pl = f"\nO {sigla_real} {num_real}/{ano_real} (referenciado) trata de: {ementa_pl}"
        except Exception as e:
            logger.warning(f"Erro ao buscar PL mencionado: {e}")

    if contexto_pl and eh_req:
        prompt = f"""Você é um assessor legislativo da Câmara dos Deputados do Brasil.
Gere um resumo PRÓPRIO do que este REQUERIMENTO pede em UMA linha (máximo 120 caracteres). Seja direto e objetivo.
NÃO copie a ementa. Escreva com suas próprias palavras.
- Se for urgência: comece com "Urgência para o PL que (explique o PL em poucas palavras)"
- Se for adiamento/retirada: comece com "Adiamento/Retirada do PL que..."
- Seja direto. Não repita número da proposição.

Requerimento: {projeto}
Ementa: {ementa}{contexto_pl}

Responda APENAS com o resumo, sem introdução, sem aspas."""
    else:
        prompt = f"""Você é um assessor legislativo da Câmara dos Deputados do Brasil.
Gere um resumo PRÓPRIO do que esta proposição trata na prática em UMA linha (máximo 120 caracteres). Seja direto e objetivo.
NÃO copie a ementa. Escreva com suas próprias palavras, de forma simples e direta.
- Explique o efeito prático para o cidadão ou para o parlamento
- Não repita o número da proposição

Proposição: {projeto}
Autor: {autor}
Ementa: {ementa}

Responda APENAS com o resumo, sem introdução, sem aspas."""

    # Usa ia_chain: Groq → Cloudflare → Gemini
    try:
        texto, fonte = ia_chain(prompt, max_tokens=80, temperatura=0.4, contexto="resumo_ementa")
        texto = texto.strip()
        # Rejeita se for igual ou muito similar à ementa
        ementa_norm = re.sub(r'\s+', ' ', ementa.strip().lower())
        texto_norm  = re.sub(r'\s+', ' ', texto.lower())
        if (texto_norm == ementa_norm or
            ementa_norm[:80] in texto_norm or
            texto_norm[:80] in ementa_norm):
            logger.warning(f"Resumo igual à ementa — descartando")
            return jsonify({'resumo': ''})
        return jsonify({'resumo': texto})
    except Exception as e:
        logger.warning(f"resumo_ementa: todos provedores falharam: {e}")
        return jsonify({'resumo': ''})

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

            if ementa_pl:
                prompt = f"""Você é um especialista legislativo. Explique em UMA frase direta o objeto deste requerimento para os parlamentares.

Requerimento: {projeto}
Ementa do requerimento: {ementa}
Ementa do {sigla_ref} {num_ref}/{ano_ref} referenciado: {ementa_pl}

Responda APENAS com a frase, sem introdução, sem aspas, sem ponto final."""
                try:
                    comp, _ = ia_chain(prompt, max_tokens=60, temperatura=0.2, contexto="enriquecer_req")
                    comp = comp.strip().rstrip('.')
                    return jsonify({'ementa_enriquecida': f"{ementa} ({comp})", 'complemento': comp})
                except Exception as e:
                    logger.warning(f"Erro enriquecer REQ: {e}")
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

    prompt = f"""Você é um especialista legislativo. Em UMA frase direta, explique de forma simples o que esta proposição trata na prática para os cidadãos.
Não repita o número da lei. Use linguagem clara e objetiva.

Proposição: {projeto}
Autor: {autor}
Ementa: {ementa}

Responda APENAS com a frase explicativa, sem introdução, sem aspas, sem ponto final."""

    try:
        comp, _ = ia_chain(prompt, max_tokens=60, temperatura=0.2, contexto="enriquecer_ementa")
        comp = comp.strip().rstrip('.')
        return jsonify({'ementa_enriquecida': f"{ementa} ({comp})", 'complemento': comp})
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

    prompt = f"""Você é um especialista legislativo. Sobre a proposição abaixo, escreva em UMA frase direta o que ela trata, de forma clara para leigos.
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
    finally:
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



@app.route('/extra_pauta/<int:evento_id>', methods=['GET'])
@login_required
def listar_extra_pauta(evento_id):
    """Lista todas as proposições extra pauta de um evento."""
    conn = get_conn()
    c = conn.cursor()
    try:
        c.execute('''CREATE TABLE IF NOT EXISTS extra_pauta (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            evento_id INTEGER NOT NULL,
            id_proposicao TEXT NOT NULL,
            projeto TEXT, ementa TEXT, autor TEXT, relator TEXT,
            created_by TEXT, created_at TEXT,
            UNIQUE(evento_id, id_proposicao))''')
        c.execute('SELECT id_proposicao, projeto, ementa, autor, relator, created_by, created_at FROM extra_pauta WHERE evento_id=? ORDER BY created_at', (evento_id,))
        rows = c.fetchall()
        return jsonify([{
            'id_principal': r[0], 'projeto': r[1], 'ementa': r[2],
            'autor': r[3], 'relator': r[4],
            'created_by': r[5], 'created_at': r[6], 'eh_extra_pauta': True,
        } for r in rows])
    except Exception as e:
        logger.error(f'listar_extra_pauta: {e}')
        return jsonify([])
    finally:
        conn.close()


@app.route('/extra_pauta/<int:evento_id>', methods=['POST'])
@login_required
def salvar_extra_pauta(evento_id):
    """Salva (ou atualiza) uma proposição extra pauta."""
    data = request.get_json()
    id_prop = str(data.get('id_principal', ''))
    if not id_prop:
        return jsonify({'error': 'id_principal obrigatório'}), 400
    conn = get_conn()
    c = conn.cursor()
    try:
        c.execute('''CREATE TABLE IF NOT EXISTS extra_pauta (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            evento_id INTEGER NOT NULL,
            id_proposicao TEXT NOT NULL,
            projeto TEXT, ementa TEXT, autor TEXT, relator TEXT,
            created_by TEXT, created_at TEXT,
            UNIQUE(evento_id, id_proposicao))''')
        now_str = now_brasilia().strftime('%Y-%m-%d %H:%M:%S')
        if USE_POSTGRES:
            c.execute('''INSERT INTO extra_pauta (evento_id, id_proposicao, projeto, ementa, autor, relator, created_by, created_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (evento_id, id_proposicao) DO UPDATE SET
                  projeto=EXCLUDED.projeto, ementa=EXCLUDED.ementa,
                  autor=EXCLUDED.autor, relator=EXCLUDED.relator''',
                (evento_id, id_prop, data.get('projeto',''), data.get('ementa',''),
                 data.get('autor',''), data.get('relator',''),
                 current_user.display_name(), now_str))
        else:
            c.execute('''INSERT OR REPLACE INTO extra_pauta
                (evento_id, id_proposicao, projeto, ementa, autor, relator, created_by, created_at)
                VALUES (?,?,?,?,?,?,?,?)''',
                (evento_id, id_prop, data.get('projeto',''), data.get('ementa',''),
                 data.get('autor',''), data.get('relator',''),
                 current_user.display_name(), now_str))
        conn.commit()
        return jsonify({'ok': True, 'created_at': now_str})
    except Exception as e:
        logger.error(f'salvar_extra_pauta: {e}')
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@app.route('/extra_pauta/<int:evento_id>/<id_proposicao>', methods=['DELETE'])
@login_required
def excluir_extra_pauta(evento_id, id_proposicao):
    """Remove uma proposição extra pauta."""
    conn = get_conn()
    c = conn.cursor()
    try:
        c.execute('DELETE FROM extra_pauta WHERE evento_id=? AND id_proposicao=?', (evento_id, id_proposicao))
        conn.commit()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()



@app.route('/nota_proposicao/<id_proposicao>', methods=['GET'])
@login_required
def get_nota_proposicao(id_proposicao):
    """Busca a nota técnica salva de uma proposição, por id_principal.
    Tenta também ids de REQs de urgência relacionados ao mesmo PL."""
    conn = get_conn()
    c = conn.cursor()
    try:
        # Busca direta pelo id
        prop_key = f"PROP_{id_proposicao}"
        c.execute(
            "SELECT resumo_materia, orientacao, saved_by, saved_at FROM notas "
            "WHERE item_key=? AND (resumo_materia IS NOT NULL AND resumo_materia != '') "
            "ORDER BY saved_at DESC LIMIT 1",
            (prop_key,)
        )
        row = c.fetchone()
        if row:
            return jsonify({
                'found': True,
                'resumo': row[0], 'orientacao': row[1],
                'saved_by': row[2], 'saved_at': row[3],
                'fonte': 'banco'
            })

        # Não encontrou — retorna not found (sem erro)
        return jsonify({'found': False})
    except Exception as e:
        logger.error(f'get_nota_proposicao: {e}')
        return jsonify({'found': False, 'error': str(e)})
    finally:
        conn.close()


@app.route('/notas_por_proposicao/<id_proposicao>', methods=['GET'])
@login_required
def notas_por_proposicao(id_proposicao):
    """Busca nota de um PL E de REQs de urgência relacionados, em qualquer evento.
    Query params opcionais: projeto (ex: PL 1234/2023), numero, ano, sigla."""
    conn = get_conn()
    c = conn.cursor()
    try:
        prop_key = f"PROP_{id_proposicao}"

        # 1. Busca direta pela chave PROP_{id} em TODOS os eventos
        c.execute(
            "SELECT n.resumo_materia, n.orientacao, n.saved_by, n.saved_at, n.evento_id "
            "FROM notas n "
            "WHERE n.item_key = ? "
            "AND n.resumo_materia IS NOT NULL AND TRIM(n.resumo_materia) != '' "
            "ORDER BY n.saved_at DESC LIMIT 5",
            (prop_key,)
        )
        rows = c.fetchall()
        resultado = [{'resumo': r[0], 'orientacao': r[1], 'saved_by': r[2],
                      'saved_at': r[3], 'tipo': 'nota_direta'} for r in rows]

        # 2. Fallback: LIKE no item_key (cobre variações int/str do id)
        if not resultado:
            c.execute(
                "SELECT n.resumo_materia, n.orientacao, n.saved_by, n.saved_at "
                "FROM notas n "
                "WHERE n.item_key LIKE ? "
                "AND n.resumo_materia IS NOT NULL AND TRIM(n.resumo_materia) != '' "
                "ORDER BY n.saved_at DESC LIMIT 5",
                (f'%{id_proposicao}%',)
            )
            rows2 = c.fetchall()
            resultado = [{'resumo': r[0], 'orientacao': r[1], 'saved_by': r[2],
                          'saved_at': r[3], 'tipo': 'nota_like'} for r in rows2]

        # 3. REQs de urgência: busca notas de outros itens que mencionam este id
        #    A nota do REQ pode referenciar o PL no texto, então buscamos pelo id E pelo nome
        if not resultado:
            c.execute(
                "SELECT n.resumo_materia, n.orientacao, n.saved_by, n.saved_at "
                "FROM notas n "
                "WHERE (n.resumo_materia LIKE ? OR n.resumo_parecer LIKE ?) "
                "AND n.resumo_materia IS NOT NULL AND TRIM(n.resumo_materia) != '' "
                "ORDER BY n.saved_at DESC LIMIT 3",
                (f'%{id_proposicao}%', f'%{id_proposicao}%')
            )
            rows3 = c.fetchall()
            resultado += [{'resumo': r[0], 'orientacao': r[1], 'saved_by': r[2],
                           'saved_at': r[3], 'tipo': 'nota_req'} for r in rows3]

        # 4. Busca REQs de urgência via pauta_cache_db
        #    Estratégia: varre itens da pauta buscando REQs cujo campo ementa/projeto
        #    menciona o numero+ano do PL buscado, obtém o id_principal do REQ
        #    e busca a nota desse REQ no banco.
        #    Também aceita nome do projeto via query param (ex: ?projeto=PL+1234/2023)
        projeto_nome = request.args.get('projeto', '')  # ex: "PL 1234/2023"
        numero_pl    = request.args.get('numero', '')
        ano_pl       = request.args.get('ano', '')

        if not resultado:
            try:
                import json as _json
                c.execute(
                    "SELECT json_pauta FROM pauta_cache_db ORDER BY last_updated DESC LIMIT 30"
                )
                ids_req_candidatos = []
                _ids_verificados_api = set()
                for (jp,) in c.fetchall():
                    if not jp: continue
                    try:
                        itens = _json.loads(jp) if isinstance(jp, str) else jp
                    except Exception:
                        continue
                    if not isinstance(itens, list): continue
                    for it in itens:
                        ref_id  = str(it.get('id_principal') or '')
                        ementa  = str(it.get('ementa') or '')
                        projeto = str(it.get('projeto_original') or it.get('projeto') or '')

                        if ref_id == str(id_proposicao):
                            continue  # é o próprio PL, não o REQ

                        # Condições de match: id do PL na ementa/projeto do REQ
                        # OU nome do projeto na ementa do REQ
                        # OU numero+ano na ementa do REQ
                        match = False
                        if str(id_proposicao) in ementa:
                            match = True
                        if projeto_nome and projeto_nome in ementa:
                            match = True
                        if numero_pl and ano_pl:
                            if numero_pl in ementa and ano_pl in ementa:
                                match = True
                        # REQ de urgência: projeto começa com REQ/RQS/RQU
                        eh_req = bool(re.match(r'^REQ|^RQS|^RQU|^REC', projeto, re.I))
                        if match and eh_req and ref_id and ref_id not in ids_req_candidatos:
                            ids_req_candidatos.append(ref_id)

                        # REQ sem referência explícita ao PL na ementa:
                        # verifica via API /relacionadas se este REQ aponta para o PL buscado
                        if eh_req and not match and ref_id and ref_id not in ids_req_candidatos:
                            _ids_verificados_api.add(ref_id)

                # Busca notas para cada REQ candidato
                for req_id in ids_req_candidatos[:5]:
                    req_key = f"PROP_{req_id}"
                    c.execute(
                        "SELECT resumo_materia, orientacao, saved_by, saved_at FROM notas "
                        "WHERE item_key=? AND resumo_materia IS NOT NULL AND TRIM(resumo_materia) != '' "
                        "ORDER BY saved_at DESC LIMIT 1",
                        (req_key,)
                    )
                    row_req = c.fetchone()
                    if row_req:
                        resultado.append({
                            'resumo': row_req[0], 'orientacao': row_req[1],
                            'saved_by': row_req[2], 'saved_at': row_req[3],
                            'tipo': 'nota_req_pauta'
                        })
                # Para REQs sem referência explícita na ementa,
                # consulta a API da Câmara /proposicoes/{id_req}/relacionadas
                for req_id_check in list(_ids_verificados_api)[:10]:
                    if req_id_check in ids_req_candidatos:
                        continue
                    try:
                        r_rel = requests.get(
                            f'https://dadosabertos.camara.leg.br/api/v2/proposicoes/{req_id_check}/relacionadas',
                            headers={'Accept': 'application/json'}, timeout=5
                        )
                        if r_rel.ok:
                            for rel in (r_rel.json().get('dados') or []):
                                if str(rel.get('id')) == str(id_proposicao):
                                    ids_req_candidatos.append(req_id_check)
                                    break
                    except Exception:
                        pass

                if ids_req_candidatos:
                    logger.info(f'notas_por_proposicao: REQ candidatos para {id_proposicao}: {ids_req_candidatos}')
            except Exception as _e:
                logger.warning(f'Busca REQ via pauta_cache_db falhou: {_e}')

        # Debug: mostra TODAS as chaves quando não achar
        try:
            c.execute("SELECT COUNT(*) FROM notas WHERE item_key LIKE 'PROP_%'")
            total = c.fetchone()[0]
            c.execute("SELECT item_key, saved_at FROM notas WHERE item_key LIKE ? ORDER BY saved_at DESC LIMIT 5",
                      (f'%{id_proposicao}%',))
            matches = c.fetchall()
            if not resultado and not matches:
                # Mostra TODAS as chaves para diagnóstico
                c.execute("SELECT item_key FROM notas WHERE item_key LIKE 'PROP_%' ORDER BY saved_at DESC")
                todas = [r[0] for r in c.fetchall()]
                logger.info(f'notas_por_proposicao id={id_proposicao}: NÃO ENCONTRADO. '
                            f'Todas as {total} chaves: {todas}')
            else:
                logger.info(f'notas_por_proposicao id={id_proposicao}: {len(resultado)} nota(s). '
                            f'Matches: {matches}')
        except Exception as _de:
            logger.info(f'notas_por_proposicao id={id_proposicao}: {len(resultado)} nota(s). debug_err={_de}')
        return jsonify({'found': bool(resultado), 'notas': resultado})
    except Exception as e:
        logger.error(f'notas_por_proposicao: {e}')
        return jsonify({'found': False, 'notas': [], 'error': str(e)})
    finally:
        conn.close()



@app.route('/buscar_proposicao', methods=['GET'])
@login_required
def buscar_proposicao():
    """Busca proposição na API da Câmara com fallbacks.
    Params: sigla, numero, ano
    Tenta: busca exata → busca sem sigla → busca por número próximo"""
    sigla  = request.args.get('sigla', 'PL').upper().strip()
    numero = request.args.get('numero', '').strip()
    ano    = request.args.get('ano', '').strip()

    if not numero or not ano:
        return jsonify({'found': False, 'error': 'número e ano obrigatórios'}), 400

    import requests as _req

    def _buscar(sig, num, an):
        url = (f'https://dadosabertos.camara.leg.br/api/v2/proposicoes'
               f'?siglaTipo={sig}&numero={num}&ano={an}&itens=1')
        r = _req.get(url, headers={'Accept': 'application/json'}, timeout=10)
        if r.ok:
            dados = r.json().get('dados', [])
            if dados:
                return dados[0]
        return None

    prop = None

    # 1. Busca exata
    prop = _buscar(sigla, numero, ano)

    # 2. Tenta siglas alternativas comuns
    if not prop:
        for sig_alt in ['PL', 'PLP', 'PEC', 'MPV', 'PDL', 'PLC']:
            if sig_alt == sigla:
                continue
            prop = _buscar(sig_alt, numero, ano)
            if prop:
                break

    # 3. Busca pelo número na pauta_cache_db (o número na pauta pode ser diferente da API)
    if not prop:
        try:
            import json as _json
            conn = get_conn()
            c = conn.cursor()
            c.execute("SELECT json_pauta FROM pauta_cache_db ORDER BY last_updated DESC LIMIT 10")
            for (jp,) in c.fetchall():
                if not jp: continue
                itens = _json.loads(jp) if isinstance(jp, str) else jp
                if not isinstance(itens, list): continue
                for it in itens:
                    proj = str(it.get('projeto', '') or it.get('projeto_original', ''))
                    # Verifica se o número e ano batem
                    if numero in proj and ano in proj:
                        id_p = it.get('id_principal')
                        if id_p:
                            r2 = _req.get(
                                f'https://dadosabertos.camara.leg.br/api/v2/proposicoes/{id_p}',
                                headers={'Accept': 'application/json'}, timeout=10
                            )
                            if r2.ok:
                                prop = r2.json().get('dados')
                                if prop:
                                    break
                if prop:
                    break
            conn.close()
        except Exception as _e:
            logger.warning(f'buscar_proposicao fallback pauta_cache: {_e}')

    if not prop:
        return jsonify({'found': False, 'error': f'{sigla} {numero}/{ano} não encontrado'})

    # Busca autores
    autor = 'N/D'
    try:
        ra = _req.get(
            f'https://dadosabertos.camara.leg.br/api/v2/proposicoes/{prop["id"]}/autores',
            headers={'Accept': 'application/json'}, timeout=8
        )
        if ra.ok:
            autores = ra.json().get('dados', [])
            autor = ', '.join(a['nome'] for a in autores[:2])
            if len(autores) > 2: autor += ' e outros'
    except Exception:
        pass

    # Busca relator (última tramitação com "Relator")
    relator = 'Não atribuído'
    try:
        rt = _req.get(
            f'https://dadosabertos.camara.leg.br/api/v2/proposicoes/{prop["id"]}/tramitacoes?itens=20&ordem=DESC',
            headers={'Accept': 'application/json'}, timeout=8
        )
        if rt.ok:
            for t in rt.json().get('dados', []):
                despacho = t.get('despacho', '') or ''
                if 'relator' in despacho.lower():
                    m = re.search(r'Dep\.?\s+\w+(?:\s+\w+)?', despacho)
                    if m: relator = m.group(0); break
    except Exception:
        pass

    status = (prop.get('statusProposicao') or {})
    return jsonify({
        'found': True,
        'id': str(prop['id']),
        'projeto': f"{prop.get('siglaTipo','?')} {prop.get('numero','?')}/{prop.get('ano','?')}",
        'sigla': prop.get('siglaTipo', sigla),
        'numero': str(prop.get('numero', numero)),
        'ano': str(prop.get('ano', ano)),
        'ementa': prop.get('ementa', ''),
        'autor': autor,
        'relator': relator,
        'situacao': status.get('descricaoSituacao', 'N/D'),
    })

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



@app.route('/gerar_banner_proposicao', methods=['POST'])
@login_required
def gerar_banner_proposicao():
    """Gera banner HTML estilo panfleto jornalistico — layout fiel ao modelo de referencia."""
    import base64 as _b64
    from datetime import datetime as _dt

    proposicao   = request.form.get('proposicao', '')
    ementa       = request.form.get('ementa', '')
    autor        = request.form.get('autor', '')
    relator      = request.form.get('relator', '')
    regime       = request.form.get('regime', 'Ordinario')
    comissoes    = request.form.get('comissoes', 'Plenario')
    orientacao   = request.form.get('orientacao', 'SIM')
    nota_tecnica = request.form.get('nota_tecnica', '')
    resumo_extra = request.form.get('resumo_extra', '')

    # Imagem de fundo do cabecalho — guarda base64+mime separados para usar via <img> real
    # (CSS background com data URL longa falha silenciosamente no html2canvas)
    imagem_b64 = ''
    imagem_mime = ''
    if 'imagem' in request.files:
        f = request.files['imagem']
        if f and f.filename:
            raw  = f.read()
            ext  = f.filename.rsplit('.', 1)[-1].lower()
            imagem_mime = {'jpg':'image/jpeg','jpeg':'image/jpeg','png':'image/png','webp':'image/webp'}.get(ext,'image/jpeg')
            imagem_b64  = _b64.b64encode(raw).decode('utf-8')

    def _logo(nome):
        try:
            with open(os.path.join(app.root_path, 'static', nome), 'rb') as fh:
                return 'data:image/png;base64,' + _b64.b64encode(fh.read()).decode('utf-8')
        except Exception:
            return ''

    logo_min = _logo('logo_minoria.png')
    logo_opo = _logo('logo_oposicao.png')

    # Cores por orientacao
    ORI_CFG = {
        'SIM':        {'cor':'#1B6B3A','txt':'#fff','label':'SIM',        'emoji':'&#9989;'},
        'NAO':        {'cor':'#C9111E','txt':'#fff','label':'NAO',        'emoji':'&#10060;'},
        'NÃO':        {'cor':'#C9111E','txt':'#fff','label':'NÃO',        'emoji':'&#10060;'},
        'NEGOCIACAO': {'cor':'#D4A017','txt':'#1a1a1a','label':'NEGOCIAÇÃO','emoji':'&#129309;'},
        'NEGOCIAÇÃO': {'cor':'#D4A017','txt':'#1a1a1a','label':'NEGOCIAÇÃO','emoji':'&#129309;'},
        'LIBERADO':   {'cor':'#0B5394','txt':'#fff','label':'LIBERADO',   'emoji':'&#128275;'},
        'OBSTRUCAO':  {'cor':'#C9111E','txt':'#fff','label':'OBSTRUÇÃO',  'emoji':'&#128683;'},
        'OBSTRUÇÃO':  {'cor':'#C9111E','txt':'#fff','label':'OBSTRUÇÃO',  'emoji':'&#128683;'},
        'ABSTENCAO':  {'cor':'#555555','txt':'#fff','label':'ABSTENÇÃO',  'emoji':'&#8866;'},
        'ABSTENÇÃO':  {'cor':'#555555','txt':'#fff','label':'ABSTENÇÃO',  'emoji':'&#8866;'},
    }
    cfg = ORI_CFG.get(orientacao.upper(), {'cor':'#1B6B3A','txt':'#fff','label':orientacao.upper(),'emoji':'&#128203;'})
    ORI_COR   = cfg['cor']
    ORI_TXT   = cfg['txt']
    ORI_LABEL = cfg['label']
    ORI_EMOJI = cfg['emoji']

    # Prompt IA
    ctx = ''
    if nota_tecnica and len(nota_tecnica.strip()) > 40:
        ctx += '\n\nNOTA TECNICA:\n' + nota_tecnica[:4000]
    if resumo_extra:
        ctx += '\n\nCONTEXTO ADICIONAL:\n' + resumo_extra

    prompt = (
        'Voce e um assessor legislativo senior da Oposicao e Minoria na Camara dos Deputados, '
        'com perfil ideologico conservador: defende liberalismo economico, pautas de seguranca '
        'publica e combate ao crime, e costuma se posicionar contra agendas progressistas.\n\n'
        'PROPOSICAO: ' + proposicao + '\nEMENTA: ' + ementa + '\nAUTOR: ' + autor + '\n'
        'RELATOR: ' + relator + '\nREGIME: ' + regime + '\nCOMISSOES: ' + comissoes + '\nORIENTACAO: ' + orientacao + ctx + '\n\n'
        'Gere APENAS um JSON valido, sem markdown:\n'
        '{\n'
        '  "titulo_curto": "(sigla+numero+ano, ex: PL 1838/2026, max 18 chars)",\n'
        '  "subtitulo": "(frase de impacto em MAIUSCULAS, max 8 palavras)",\n'
        '  "descricao_curta": "(1 frase clara do que o projeto faz, max 120 chars)",\n'
        '  "dado_chave_label": "(label do dado mais impactante, ex: JORNADA MAXIMA, max 20 chars)",\n'
        '  "dado_chave_valor": "(valor do dado, ex: 40 HORAS SEMANAIS, max 25 chars)",\n'
        '  "dado_chave_detalhe": "(complemento, ex: 2 DESCANSOS SEMANAIS REMUNERADOS, max 35 chars)",\n'
        '  "resumo_executivo": "(3-4 frases claras, max 200 palavras)",\n'
        '  "o_que_preve": [\n'
        '    {"texto": "descricao completa do item, 1-2 frases"},\n'
        '    {"texto": "..."},\n'
        '    {"texto": "..."},\n'
        '    {"texto": "..."},\n'
        '    {"texto": "..."}\n'
        '  ],\n'
        '  "criticas": [\n'
        '    {"titulo": "Critica 1", "detalhe": "explicacao 1-2 frases"},\n'
        '    {"titulo": "Critica 2", "detalhe": "explicacao 1-2 frases"},\n'
        '    {"titulo": "Critica 3", "detalhe": "explicacao 1-2 frases"},\n'
        '    {"titulo": "Critica 4", "detalhe": "explicacao 1-2 frases"},\n'
        '    {"titulo": "Critica 5", "detalhe": "explicacao 1-2 frases"}\n'
        '  ],\n'
        '  "justificativa_oficial": "(3-4 frases sobre a justificativa formal, max 120 palavras)",\n'
        '  "argumento_chave": "(discurso pronto para o deputado ler em tribuna, em PRIMEIRA PESSOA, tom firme e direto, alinhado a posicao conservadora/liberal na economia e favoravel a seguranca publica quando pertinente ao tema. OBRIGATORIO: o discurso deve ser logicamente coerente com o campo ORIENTACAO informado acima — se SIM ou LIBERADO, defenda o voto favoravel ao projeto; se NAO ou OBSTRUCAO, ataque e justifique o voto contrario ao projeto; se NEGOCIACAO ou ABSTENCAO, adote tom ponderado pedindo ajustes antes de apoiar. Nunca contradiga a orientacao informada. Comece com uma frase de impacto, max 60 palavras, sem aspas internas)",\n'
        '  "na_pratica": ["efeito 1","efeito 2","efeito 3","efeito 4","efeito 5"]\n'
        '}\nResponda APENAS com o JSON.'
    )

    # Gemini primeiro, depois Groq, depois Cloudflare
    fonte = 'ia'
    texto_ia = None

    gemini_key = os.environ.get('GEMINI_API_KEY', '')
    if gemini_key:
        for modelo_gem in GEMINI_PREFERENCIA:
            try:
                url_gem = ('https://generativelanguage.googleapis.com/v1beta/models/'
                           + modelo_gem + ':generateContent?key=' + gemini_key)
                r_gem = requests.post(url_gem,
                    headers={'Content-Type': 'application/json'},
                    json={'contents':[{'parts':[{'text':prompt}]}],
                          'generationConfig':{'maxOutputTokens':1500,'temperature':0.3}},
                    timeout=20)
                if r_gem.status_code == 503:
                    import time as _t; _t.sleep(3); continue
                if r_gem.status_code in (404, 429):
                    continue
                r_gem.raise_for_status()
                texto_ia = r_gem.json()['candidates'][0]['content']['parts'][0]['text']
                fonte = 'gemini/' + modelo_gem
                logger.info('gerar_banner: Gemini OK — ' + modelo_gem)
                break
            except Exception as e:
                logger.warning('gerar_banner: Gemini ' + modelo_gem + ' falhou — ' + str(e))
                continue

    if not texto_ia:
        groq_key = os.environ.get('GROQ_API_KEY', '')
        if groq_key:
            import time as _t2
            for _tentativa in range(2):
                try:
                    texto_ia = groq_post(prompt, max_tokens=1500, temperatura=0.3)
                    fonte = 'groq'
                    break
                except Exception as e:
                    logger.warning('gerar_banner: Groq falhou (tentativa ' + str(_tentativa+1) + ') — ' + str(e))
                    if '429' in str(e) and _tentativa == 0:
                        _t2.sleep(8)
                        continue
                    break

    if not texto_ia:
        try:
            texto_ia = cloudflare_post(prompt, max_tokens=1500, temperatura=0.3)
            fonte = 'cloudflare'
        except Exception as e:
            logger.warning('gerar_banner: Cloudflare falhou — ' + str(e))

    if not texto_ia:
        return jsonify({'success': False, 'error': 'Servico de IA indisponivel. Tente novamente.'}), 503

    try:
        texto_ia = re.sub(r'```(?:json)?|```', '', texto_ia).strip()
        d = json.loads(texto_ia)
    except json.JSONDecodeError as e:
        logger.warning('gerar_banner: JSON invalido: ' + str(e))
        d = {
            'titulo_curto': proposicao[:18], 'subtitulo': ementa[:60].upper(),
            'descricao_curta': ementa[:120], 'resumo_executivo': ementa,
            'dado_chave_label': '', 'dado_chave_valor': '', 'dado_chave_detalhe': '',
            'o_que_preve': [{'texto':'Consulte a ementa completa'}],
            'criticas': [{'titulo':'Analise pendente','detalhe':'Gere novamente'}],
            'justificativa_oficial': 'Nao disponivel.',
            'argumento_chave': '', 'na_pratica': ['Consulte a nota tecnica'],
        }

    def _e(s):
        return str(s or '').replace('&','&amp;').replace('<','&lt;').replace('>','&gt;').replace('"','&quot;')

    # SVG ícones inline
    def ico_check():
        return ('<svg width="18" height="18" viewBox="0 0 18 18" fill="none">'
                '<circle cx="9" cy="9" r="9" fill="' + ORI_COR + '"/>'
                '<path d="M5 9.5l2.5 2.5 5.5-5.5" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>'
                '</svg>')

    CRITICA_ICOS = [
        # dolar
        '<svg viewBox="0 0 32 32" fill="none"><circle cx="16" cy="16" r="16" fill="#C9111E"/><text x="16" y="21" text-anchor="middle" font-size="16" fill="#fff" font-family="Arial">$</text></svg>',
        # grafico
        '<svg viewBox="0 0 32 32" fill="none"><circle cx="16" cy="16" r="16" fill="#C9111E"/><path d="M8 22l5-6 4 3 5-8" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
        # tendencia
        '<svg viewBox="0 0 32 32" fill="none"><circle cx="16" cy="16" r="16" fill="#C9111E"/><path d="M8 20l4-4 3 3 7-7" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
        # grupo
        '<svg viewBox="0 0 32 32" fill="none"><circle cx="16" cy="16" r="16" fill="#C9111E"/><circle cx="12" cy="13" r="3" fill="#fff"/><circle cx="20" cy="13" r="3" fill="#fff"/><path d="M6 23c0-3 3-5 6-5h8c3 0 6 2 6 5" stroke="#fff" stroke-width="2" fill="none"/></svg>',
        # balanca
        '<svg viewBox="0 0 32 32" fill="none"><circle cx="16" cy="16" r="16" fill="#C9111E"/><rect x="15" y="8" width="2" height="14" fill="#fff"/><path d="M8 12l4 4-4 4M24 12l-4 4 4 4" stroke="#fff" stroke-width="1.5" fill="none" stroke-linecap="round"/></svg>',
    ]

    # Monta HTML dos itens "o que preve"
    def _preve_items(itens):
        rows = []
        for it in (itens or []):
            texto = it.get('texto','') if isinstance(it,dict) else str(it)
            rows.append(
                '<div class="preve-item">'
                + ico_check() +
                '<span>' + _e(texto) + '</span>'
                '</div>'
            )
        return ''.join(rows)

    # Monta HTML das criticas
    def _critica_items(itens):
        rows = []
        for i, it in enumerate(itens or []):
            tit = _e(it.get('titulo','') if isinstance(it,dict) else str(it))
            det = _e(it.get('detalhe','') if isinstance(it,dict) else '')
            ico = CRITICA_ICOS[i % len(CRITICA_ICOS)]
            rows.append(
                '<div class="critica-item">'
                '<div class="critica-ico">' + ico + '</div>'
                '<div>'
                '<p class="critica-titulo">' + tit + '</p>'
                '<p class="critica-detalhe">' + det + '</p>'
                '</div>'
                '</div>'
            )
        return ''.join(rows)

    # Monta HTML da lista "na pratica"
    def _pratica_items(itens):
        rows = []
        for it in (itens or []):
            rows.append('<div class="pratica-item">' + ico_check() + '<span>' + _e(str(it)) + '</span></div>')
        return ''.join(rows)

    # Logos para o cabecalho
    logos_cab = ''
    if logo_min:
        logos_cab += '<img src="' + logo_min + '" style="height:44px;object-fit:contain;">'
    if logo_opo:
        logos_cab += '<img src="' + logo_opo + '" style="height:44px;object-fit:contain;">'

    # Dado-chave badge (canto direito do cabecalho)
    dado_label  = _e(d.get('dado_chave_label',''))
    dado_valor  = _e(d.get('dado_chave_valor',''))
    dado_detalhe= _e(d.get('dado_chave_detalhe',''))
    dado_block  = ''
    if dado_label or dado_valor:
        dado_block = (
            '<div class="dado-chave">'
            '<div class="dado-label">' + dado_label + '</div>'
            '<div class="dado-valor">' + dado_valor + '</div>'
            + ('<div class="dado-detalhe">' + dado_detalhe + '</div>' if dado_detalhe else '') +
            '</div>'
        )

    # Cabecalho — fundo: <img> real se houver foto (mais confiavel que CSS background
    # com data URL longa, que falha silenciosamente no html2canvas), senao gradiente escuro
    cab_bg_color = 'background:linear-gradient(135deg,#0A1628 0%,#1B3A5C 100%);'
    if imagem_b64:
        cab_foto_html = ('<img src="data:' + imagem_mime + ';base64,' + imagem_b64 + '" '
                          'style="position:absolute;top:0;left:0;width:100%;height:100%;'
                          'object-fit:cover;object-position:center;display:block;z-index:0;">')
    else:
        cab_foto_html = ''

    data_hoje = _dt.now().strftime('%d/%m/%Y')

    # CSS
    css = (
        '@import url("https://fonts.googleapis.com/css2?family=Barlow+Condensed:wght@700;800;900&family=Source+Sans+3:wght@400;600;700&display=swap");'
        '*,*::before,*::after{box-sizing:border-box;margin:0;padding:0;}'
        'body{background:#D8DCE0;display:flex;flex-direction:column;align-items:center;padding:20px;font-family:"Source Sans 3",Arial,sans-serif;}'
        '.banner{width:794px;background:#F5F5F5;box-shadow:0 6px 32px rgba(0,0,0,.22);overflow:hidden;border-radius:8px;}'

        # Cabecalho
        '.cab{position:relative;overflow:hidden;min-height:200px;}'
        '.cab-overlay{position:absolute;inset:0;background:rgba(10,22,40,0.62);z-index:1;}'
        '.cab-inner{position:relative;z-index:2;padding:22px 24px 0;display:flex;flex-direction:column;gap:0;}'
        '.cab-top{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;}'
        '.cab-logos{display:flex;gap:6px;align-items:center;flex-shrink:0;background:rgba(255,255,255,0.12);border-radius:6px;padding:5px 8px;}'
        '.cab-titulo{font-family:"Barlow Condensed",sans-serif;font-size:58px;font-weight:900;color:#fff;line-height:.92;letter-spacing:0;margin-top:8px;}'
        '.cab-subtitulo{font-family:"Barlow Condensed",sans-serif;font-size:20px;font-weight:800;color:' + ORI_COR + ';text-transform:uppercase;letter-spacing:.5px;margin:6px 0 4px;line-height:1.1;}'
        '.cab-desc{font-size:13px;color:rgba(255,255,255,.85);line-height:1.5;margin-bottom:16px;max-width:480px;}'

        # Dado-chave badge
        '.dado-chave{flex-shrink:0;background:rgba(15,30,55,0.85);border:2px solid ' + ORI_COR + ';border-radius:8px;padding:10px 14px;text-align:center;min-width:130px;}'
        '.dado-label{font-size:8.5px;font-weight:700;letter-spacing:2px;text-transform:uppercase;color:rgba(255,255,255,.6);margin-bottom:4px;}'
        '.dado-valor{font-family:"Barlow Condensed",sans-serif;font-size:22px;font-weight:900;color:#fff;line-height:1;}'
        '.dado-detalhe{font-size:8px;font-weight:700;color:rgba(255,255,255,.7);text-transform:uppercase;letter-spacing:1px;margin-top:4px;line-height:1.2;}'

        # Meta bar (autor/regime/comissoes/relator)
        '.meta-bar{display:grid;grid-template-columns:repeat(4,1fr);background:#fff;border-bottom:2px solid #E0E0E0;}'
        '.mc{display:flex;align-items:center;gap:10px;padding:12px 14px;border-right:1px solid #E8E8E8;}'
        '.mc:last-child{border-right:none;}'
        '.mc-ico{width:32px;height:32px;background:' + ORI_COR + ';border-radius:50%;display:flex;align-items:center;justify-content:center;flex-shrink:0;}'
        '.mc-ico svg{width:16px;height:16px;fill:#fff;}'
        '.mc-label{font-size:8.5px;font-weight:700;letter-spacing:1.8px;color:#888;text-transform:uppercase;margin-bottom:2px;}'
        '.mc-val{font-size:11.5px;font-weight:700;color:#1A1A1A;line-height:1.3;}'

        # Resumo
        '.resumo{padding:16px 20px;background:#fff;font-size:13px;color:#222;line-height:1.7;border-bottom:1px solid #E0E0E0;}'

        # Grade 2 colunas
        '.grade{display:grid;grid-template-columns:1fr 1fr;gap:0;border-top:1px solid #E0E0E0;}'

        # Card generico
        '.card{margin:14px;background:#fff;border-radius:10px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,.08);}'
        '.card-header{display:flex;align-items:center;gap:10px;padding:10px 14px;}'
        '.card-header-ico{width:28px;height:28px;border-radius:50%;background:rgba(255,255,255,0.25);display:flex;align-items:center;justify-content:center;flex-shrink:0;font-size:14px;}'
        '.card-header-txt{font-family:"Barlow Condensed",sans-serif;font-size:14px;font-weight:800;letter-spacing:1.5px;text-transform:uppercase;color:#fff;}'
        '.card-body{padding:12px 14px;}'

        # O que preve
        '.card-verde .card-header{background:' + ORI_COR + ';}'
        '.preve-item{display:flex;align-items:flex-start;gap:8px;margin-bottom:10px;font-size:12px;color:#222;line-height:1.45;}'
        '.preve-item:last-child{margin-bottom:0;}'
        '.preve-item svg{flex-shrink:0;margin-top:2px;}'

        # Criticas
        '.card-verm .card-header{background:#C9111E;}'
        '.critica-item{display:flex;align-items:flex-start;gap:10px;margin-bottom:12px;}'
        '.critica-item:last-child{margin-bottom:0;}'
        '.critica-ico{width:32px;height:32px;flex-shrink:0;}'
        '.critica-ico svg{width:32px;height:32px;}'
        '.critica-titulo{font-size:12px;font-weight:700;color:#1A1A1A;margin-bottom:2px;line-height:1.3;}'
        '.critica-detalhe{font-size:11px;color:#555;line-height:1.4;}'

        # Justificativa
        '.card-escuro .card-header{background:#1A2A3A;}'
        '.just-inner{display:flex;align-items:flex-start;gap:12px;}'
        '.just-ico{flex-shrink:0;opacity:.15;}'
        '.just-txt{font-size:12px;color:#333;line-height:1.6;}'

        # Orientacao
        '.card-ori .card-header{background:#1A2A3A;}'
        '.ori-badge{margin:12px 14px 8px;background:' + ORI_COR + ';border-radius:8px;padding:14px;text-align:center;}'
        '.ori-badge-txt{font-family:"Barlow Condensed",sans-serif;font-size:40px;font-weight:900;color:' + ORI_TXT + ';letter-spacing:3px;text-transform:uppercase;}'
        '.arg-header{background:#1A2A3A;margin:0 14px 0;border-radius:6px 6px 0 0;padding:7px 12px;}'
        '.arg-header-txt{font-size:8.5px;font-weight:700;letter-spacing:2px;text-transform:uppercase;color:rgba(255,255,255,.65);}'
        '.arg-body{background:#243040;margin:0 14px 14px;border-radius:0 0 6px 6px;padding:10px 12px;}'
        '.arg-body-txt{font-size:12px;color:rgba(255,255,255,.9);line-height:1.6;}'

        # Na pratica
        '.np-card{margin:14px;background:#fff;border-radius:10px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,.08);}'
        '.np-header{background:' + ORI_COR + ';display:flex;align-items:center;gap:10px;padding:10px 14px;}'
        '.np-header-txt{font-family:"Barlow Condensed",sans-serif;font-size:14px;font-weight:800;letter-spacing:1.5px;text-transform:uppercase;color:#fff;}'
        '.np-ico{width:28px;height:28px;border-radius:50%;background:rgba(255,255,255,.25);display:flex;align-items:center;justify-content:center;}'
        '.np-grid{display:grid;grid-template-columns:1fr 1fr;gap:2px 16px;padding:12px 14px;}'
        '.pratica-item{display:flex;align-items:flex-start;gap:7px;font-size:12px;color:#222;line-height:1.4;padding:4px 0;}'
        '.pratica-item svg{flex-shrink:0;margin-top:2px;}'

        # Rodape
        '.rodape{background:#1A2A3A;padding:8px 20px;display:flex;justify-content:flex-end;}'
        '.rodape-txt{font-size:10px;color:rgba(255,255,255,.5);}'

        '@media print{body{background:#fff;padding:0;}.banner{box-shadow:none;border-radius:0;width:100%;}.np-btn{display:none!important;}@page{size:A4 portrait;margin:0;}}'
    )

    # SVG ícones para meta bar
    ico_autor    = '<svg viewBox="0 0 24 24"><path d="M12 12c2.7 0 5-2.3 5-5s-2.3-5-5-5-5 2.3-5 5 2.3 5 5 5zm0 2c-3.3 0-10 1.7-10 5v2h20v-2c0-3.3-6.7-5-10-5z"/></svg>'
    ico_regime   = '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9" stroke="#fff" stroke-width="2" fill="none"/><path d="M12 7v5l3 3" stroke="#fff" stroke-width="2" stroke-linecap="round" fill="none"/></svg>'
    ico_comissao = '<svg viewBox="0 0 24 24"><path d="M17 20H7a2 2 0 01-2-2V6a2 2 0 012-2h10a2 2 0 012 2v12a2 2 0 01-2 2zM9 10h6M9 14h4" stroke="#fff" stroke-width="2" fill="none" stroke-linecap="round"/></svg>'
    ico_relator  = '<svg viewBox="0 0 24 24"><path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8l-6-6zM9 13h6M9 17h4" stroke="#fff" stroke-width="1.8" fill="none" stroke-linecap="round"/></svg>'

    # SVG balanca para justificativa
    ico_balanca = (
        '<svg width="64" height="64" viewBox="0 0 24 24" fill="none">'
        '<path d="M12 3v18M3 9l4 4-4 4M21 9l-4 4 4 4M5 7h14" stroke="#1A2A3A" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>'
        '</svg>'
    )

    # SVG grupo para na pratica
    ico_grupo = (
        '<svg width="20" height="20" viewBox="0 0 24 24" fill="none">'
        '<circle cx="9" cy="7" r="3" fill="#fff"/>'
        '<circle cx="15" cy="7" r="3" fill="#fff"/>'
        '<path d="M3 19c0-3 2.7-5 6-5h6c3.3 0 6 2 6 5" stroke="#fff" stroke-width="2" fill="none" stroke-linecap="round"/>'
        '</svg>'
    )

    # Fragmento style+div, sem doctype/head/scripts — mesmo padrao do Pauta Banner.
    # E isso que o frontend injeta na preview e captura com html2canvas para o PNG.
    banner_html_fragmento = (
        '<style>' + css + '</style>'
        '<div class="banner">'

        # CABECALHO
        '<div class="cab" style="' + cab_bg_color + '">'
        + cab_foto_html +
        '<div class="cab-overlay"></div>'
        '<div class="cab-inner">'
        '<div class="cab-top">'
        '<div class="cab-logos">' + logos_cab + '</div>'
        '</div>'
        '<div style="display:flex;align-items:flex-end;justify-content:space-between;gap:16px;">'
        '<div style="flex:1;">'
        '<div class="cab-titulo">' + _e(d.get('titulo_curto', proposicao)) + '</div>'
        '<div class="cab-subtitulo">' + _e(d.get('subtitulo','')) + '</div>'
        '<div class="cab-desc">' + _e(d.get('descricao_curta', ementa[:120])) + '</div>'
        '</div>'
        + dado_block +
        '</div>'
        '</div>'
        '</div>'

        # META BAR
        '<div class="meta-bar">'
        '<div class="mc"><div class="mc-ico"><svg viewBox="0 0 24 24">' + ico_autor[23:] + '</div>'
        '<div><div class="mc-label">Autor</div><div class="mc-val">' + _e(autor[:55] or '—') + '</div></div></div>'

        '<div class="mc"><div class="mc-ico"><svg viewBox="0 0 24 24" fill="none">' + ico_regime[28:] + '</div>'
        '<div><div class="mc-label">Regime</div><div class="mc-val">' + _e(regime or 'Ordinario') + '</div></div></div>'

        '<div class="mc"><div class="mc-ico"><svg viewBox="0 0 24 24" fill="none">' + ico_comissao[28:] + '</div>'
        '<div><div class="mc-label">Comissões</div><div class="mc-val">' + _e(comissoes or 'Plenario') + '</div></div></div>'

        '<div class="mc"><div class="mc-ico"><svg viewBox="0 0 24 24" fill="none">' + ico_relator[28:] + '</div>'
        '<div><div class="mc-label">Relator</div><div class="mc-val">' + _e(relator[:55] or '—') + '</div></div></div>'
        '</div>'

        # RESUMO
        '<div class="resumo">' + _e(d.get('resumo_executivo', ementa)) + '</div>'

        # GRADE: O QUE PREVE | CRITICAS
        '<div class="grade">'

        # O que preve
        '<div style="background:#F0F0F0;">'
        '<div class="card card-verde">'
        '<div class="card-header">'
        '<div class="card-header-ico">&#10003;</div>'
        '<div class="card-header-txt">O que o projeto prev&ecirc;</div>'
        '</div>'
        '<div class="card-body">'
        + _preve_items(d.get('o_que_preve',[])) +
        '</div></div></div>'

        # Criticas
        '<div style="background:#F0F0F0;">'
        '<div class="card card-verm">'
        '<div class="card-header">'
        '<div class="card-header-ico">&#10007;</div>'
        '<div class="card-header-txt">Cr&iacute;ticas e pontos de aten&ccedil;&atilde;o</div>'
        '</div>'
        '<div class="card-body">'
        + _critica_items(d.get('criticas',[])) +
        '</div></div></div>'
        '</div>'

        # GRADE: JUSTIFICATIVA | ORIENTACAO
        '<div class="grade" style="border-top:1px solid #E0E0E0;">'

        # Justificativa
        '<div style="background:#F0F0F0;">'
        '<div class="card card-escuro">'
        '<div class="card-header">'
        '<div class="card-header-ico">&#9878;</div>'
        '<div class="card-header-txt">Justificativa oficial</div>'
        '</div>'
        '<div class="card-body">'
        '<div class="just-inner">'
        '<div class="just-ico">' + ico_balanca + '</div>'
        '<div class="just-txt">' + _e(d.get('justificativa_oficial','')) + '</div>'
        '</div>'
        '</div></div></div>'

        # Orientacao
        '<div style="background:#F0F0F0;">'
        '<div class="card card-ori">'
        '<div class="card-header">'
        '<div class="card-header-ico">&#127885;</div>'
        '<div class="card-header-txt">Orienta&ccedil;&atilde;o da Minoria</div>'
        '</div>'
        '<div class="ori-badge"><div class="ori-badge-txt">' + ORI_EMOJI + ' ' + ORI_LABEL + '</div></div>'
        + (
            '<div class="arg-header"><div class="arg-header-txt">Discurso Sugerido — Tribuna (30 segundos)</div></div>'
            '<div class="arg-body"><div class="arg-body-txt">' + _e(d.get('argumento_chave','')) + '</div></div>'
            if d.get('argumento_chave') else ''
        ) +
        '</div></div>'
        '</div>'

        # NA PRATICA
        '<div style="background:#F0F0F0;padding:0 0 14px;">'
        '<div class="np-card">'
        '<div class="np-header">'
        '<div class="np-ico">' + ico_grupo + '</div>'
        '<div class="np-header-txt">Na pr&aacute;tica</div>'
        '</div>'
        '<div class="np-grid">'
        + _pratica_items(d.get('na_pratica',[])) +
        '</div></div></div>'

        # RODAPE
        '<div class="rodape"><span class="rodape-txt">Publicado em: ' + data_hoje + '</span></div>'

        '</div>'
    )

    html = (
        '<!DOCTYPE html><html lang="pt-BR"><head><meta charset="UTF-8"><title>Banner ' + _e(proposicao) + '</title>'
        '<style>' + css + '</style></head><body>'
        '<div class="banner">'

        # CABECALHO
        '<div class="cab" style="' + cab_bg_color + '">'
        + cab_foto_html +
        '<div class="cab-overlay"></div>'
        '<div class="cab-inner">'
        '<div class="cab-top">'
        '<div class="cab-logos">' + logos_cab + '</div>'
        '</div>'
        '<div style="display:flex;align-items:flex-end;justify-content:space-between;gap:16px;">'
        '<div style="flex:1;">'
        '<div class="cab-titulo">' + _e(d.get('titulo_curto', proposicao)) + '</div>'
        '<div class="cab-subtitulo">' + _e(d.get('subtitulo','')) + '</div>'
        '<div class="cab-desc">' + _e(d.get('descricao_curta', ementa[:120])) + '</div>'
        '</div>'
        + dado_block +
        '</div>'
        '</div>'
        '</div>'

        # META BAR
        '<div class="meta-bar">'
        '<div class="mc"><div class="mc-ico"><svg viewBox="0 0 24 24">' + ico_autor[23:] + '</div>'
        '<div><div class="mc-label">Autor</div><div class="mc-val">' + _e(autor[:55] or '—') + '</div></div></div>'

        '<div class="mc"><div class="mc-ico"><svg viewBox="0 0 24 24" fill="none">' + ico_regime[28:] + '</div>'
        '<div><div class="mc-label">Regime</div><div class="mc-val">' + _e(regime or 'Ordinario') + '</div></div></div>'

        '<div class="mc"><div class="mc-ico"><svg viewBox="0 0 24 24" fill="none">' + ico_comissao[28:] + '</div>'
        '<div><div class="mc-label">Comissões</div><div class="mc-val">' + _e(comissoes or 'Plenario') + '</div></div></div>'

        '<div class="mc"><div class="mc-ico"><svg viewBox="0 0 24 24" fill="none">' + ico_relator[28:] + '</div>'
        '<div><div class="mc-label">Relator</div><div class="mc-val">' + _e(relator[:55] or '—') + '</div></div></div>'
        '</div>'

        # RESUMO
        '<div class="resumo">' + _e(d.get('resumo_executivo', ementa)) + '</div>'

        # GRADE: O QUE PREVE | CRITICAS
        '<div class="grade">'

        # O que preve
        '<div style="background:#F0F0F0;">'
        '<div class="card card-verde">'
        '<div class="card-header">'
        '<div class="card-header-ico">&#10003;</div>'
        '<div class="card-header-txt">O que o projeto prev&ecirc;</div>'
        '</div>'
        '<div class="card-body">'
        + _preve_items(d.get('o_que_preve',[])) +
        '</div></div></div>'

        # Criticas
        '<div style="background:#F0F0F0;">'
        '<div class="card card-verm">'
        '<div class="card-header">'
        '<div class="card-header-ico">&#10007;</div>'
        '<div class="card-header-txt">Cr&iacute;ticas e pontos de aten&ccedil;&atilde;o</div>'
        '</div>'
        '<div class="card-body">'
        + _critica_items(d.get('criticas',[])) +
        '</div></div></div>'
        '</div>'

        # GRADE: JUSTIFICATIVA | ORIENTACAO
        '<div class="grade" style="border-top:1px solid #E0E0E0;">'

        # Justificativa
        '<div style="background:#F0F0F0;">'
        '<div class="card card-escuro">'
        '<div class="card-header">'
        '<div class="card-header-ico">&#9878;</div>'
        '<div class="card-header-txt">Justificativa oficial</div>'
        '</div>'
        '<div class="card-body">'
        '<div class="just-inner">'
        '<div class="just-ico">' + ico_balanca + '</div>'
        '<div class="just-txt">' + _e(d.get('justificativa_oficial','')) + '</div>'
        '</div>'
        '</div></div></div>'

        # Orientacao
        '<div style="background:#F0F0F0;">'
        '<div class="card card-ori">'
        '<div class="card-header">'
        '<div class="card-header-ico">&#127885;</div>'
        '<div class="card-header-txt">Orienta&ccedil;&atilde;o da Minoria</div>'
        '</div>'
        '<div class="ori-badge"><div class="ori-badge-txt">' + ORI_EMOJI + ' ' + ORI_LABEL + '</div></div>'
        + (
            '<div class="arg-header"><div class="arg-header-txt">Discurso Sugerido — Tribuna (30 segundos)</div></div>'
            '<div class="arg-body"><div class="arg-body-txt">' + _e(d.get('argumento_chave','')) + '</div></div>'
            if d.get('argumento_chave') else ''
        ) +
        '</div></div>'
        '</div>'

        # NA PRATICA
        '<div style="background:#F0F0F0;padding:0 0 14px;">'
        '<div class="np-card">'
        '<div class="np-header">'
        '<div class="np-ico">' + ico_grupo + '</div>'
        '<div class="np-header-txt">Na pr&aacute;tica</div>'
        '</div>'
        '<div class="np-grid">'
        + _pratica_items(d.get('na_pratica',[])) +
        '</div></div></div>'

        # RODAPE
        '<div class="rodape"><span class="rodape-txt">Publicado em: ' + data_hoje + '</span></div>'

        '</div>'  # /banner

        # Script de edição inline no banner
        '<script>'
        'document.addEventListener("DOMContentLoaded",function(){'
        '  var editables=['
        '    ".cab-titulo",".cab-subtitulo",".cab-desc",'
        '    ".dado-valor",".dado-label",".dado-detalhe",'
        '    ".mc-val",'
        '    ".resumo",'
        '    ".preve-item span",'
        '    ".critica-titulo",".critica-detalhe",'
        '    ".just-txt",'
        '    ".ori-badge-txt",'
        '    ".arg-body-txt",'
        '    ".pratica-item span",'
        '    ".rodape-txt"'
        '  ];'
        '  editables.forEach(function(sel){'
        '    document.querySelectorAll(sel).forEach(function(el){'
        '      el.contentEditable="true";'
        '      el.style.outline="none";'
        '      el.style.cursor="text";'
        '      el.addEventListener("focus",function(){this.style.background="rgba(255,255,0,0.18)";});'
        '      el.addEventListener("blur",function(){this.style.background="";});'
        '    });'
        '  });'
        '  // Barra de edição flutuante'
        '  var bar=document.createElement("div");'
        '  bar.id="edit-bar";'
        '  bar.style.cssText="position:fixed;top:0;left:0;right:0;background:#1A2A3A;color:#fff;'
        '    padding:8px 16px;display:flex;align-items:center;gap:12px;z-index:9999;'
        '    font-family:Arial,sans-serif;font-size:12px;";'
        '  bar.innerHTML="<span style=color:#E9B847;font-weight:700>&#9998; Modo Edição</span>'
        '    <span style=opacity:.6>Clique em qualquer campo para editar</span>";'
        '  document.body.insertBefore(bar,document.body.firstChild);'
        '  document.body.style.paddingTop="40px";'
        '});'
        '</script>'

        '<div class="np-btn" style="margin-top:16px;display:flex;gap:10px;justify-content:center;">'
        '<button onclick="window.close()" style="background:#555;color:#fff;border:none;padding:9px 16px;border-radius:4px;cursor:pointer;font-size:13px;">&#10005; Fechar</button>'
        '</div>'
        '</body></html>'
    )

    return jsonify({'success': True, 'html': html, 'banner_html_fragmento': banner_html_fragmento, 'fonte': fonte})







# PATH explícito para garantir que os binários do sistema sejam encontrados
# mesmo quando o processo Flask roda sem /usr/bin no PATH (ex: Railway)
_BIN_PATH_ENV = dict(__import__('os').environ)
_BIN_PATH_ENV['PATH'] = '/usr/bin:/bin:/usr/local/bin:' + _BIN_PATH_ENV.get('PATH', '')



@app.route('/exportar_banner_png', methods=['POST'])
@login_required
def exportar_banner_png():
    """Devolve o HTML para o frontend renderizar e capturar como PNG."""
    data = request.get_json()
    html = data.get('html', '')
    if not html:
        return jsonify({'error': 'HTML nao fornecido.'}), 400
    # O frontend usa html2canvas para capturar o banner como PNG
    return jsonify({'success': True, 'html': html})


@app.route('/exportar_banner_pdf', methods=['POST'])
@login_required
def exportar_banner_pdf():
    """Devolve o HTML para o frontend abrir e imprimir como PDF."""
    data = request.get_json()
    html = data.get('html', '')
    if not html:
        return jsonify({'error': 'HTML nao fornecido.'}), 400
    return jsonify({'success': True, 'html': html})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
