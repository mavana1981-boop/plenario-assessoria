from flask import Flask, jsonify, request, render_template, redirect, url_for, flash, make_response
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from flask_bcrypt import Bcrypt
import sqlite3
import requests
import json
import logging
from datetime import datetime, timedelta
import os
import re
import html as ihtml
from scraper_camara import obter_itens_pauta

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s", handlers=[logging.StreamHandler()])
logger = logging.getLogger(__name__)
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
        import sqlite3 as _sq
        from flask_bcrypt import Bcrypt as _Bc
        _conn = _sq.connect(DB)
        _c = _conn.cursor()
        _c.execute('''CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            role TEXT NOT NULL,
            categoria TEXT NOT NULL DEFAULT 'geral')''')
        # Migração: adiciona coluna categoria se não existir
        try:
            _c.execute('ALTER TABLE users ADD COLUMN categoria TEXT NOT NULL DEFAULT "geral"')
            _conn.commit()
        except Exception:
            pass
        _c.execute('''CREATE TABLE IF NOT EXISTS notas (
            item_key TEXT PRIMARY KEY,
            evento_id INTEGER,
            ordem TEXT,
            resumo_materia TEXT,
            orientacao TEXT,
            resumo_parecer TEXT,
            saved_by TEXT,
            saved_at TEXT)''')
        _c.execute('''CREATE TABLE IF NOT EXISTS pauta_cache_db (
            evento_id INTEGER PRIMARY KEY,
            json_pauta TEXT,
            last_updated TEXT,
            last_saved_by TEXT)''')
        _conn.commit()
        _bcrypt = _Bc()
        _pw123 = _bcrypt.generate_password_hash('123').decode('utf-8')

        # usuarios padrão + novos — INSERT OR IGNORE (não sobrescreve existentes)
        _usuarios = [
            ('admin',             'Admin',            'admin'),
            ('assessor_plenario', 'Assessor Plenário','minoria'),
            ('assessor',          'Assessor',         'geral'),
            ('PL',                'Orientação',       'restrito'),
            ('NOVO',              'Orientação',       'restrito'),
            ('marcelo.oliveira',  'Assessor Plenário','minoria'),
        ]
        for _un, _role, _cat in _usuarios:
            try:
                _c.execute('INSERT INTO users (username, password, role, categoria) VALUES (?, ?, ?, ?)',
                           (_un, _pw123, _role, _cat))
            except _sq.IntegrityError:
                pass

        # Redefine senha 123 para TODOS os usuários existentes
        _c.execute('UPDATE users SET password=?', (_pw123,))

        # Atualiza categorias dos usuários existentes
        _cats = {
            'vinicius.scheffel': 'oposicao', 'lianna.barros': 'oposicao',
            'marcelo.uvara': 'oposicao', 'elyesley.silva': 'oposicao',
            'pedro.chaves': 'oposicao',
            'ulisses.branco': 'minoria', 'eduardo.borba': 'minoria',
            'luisa.marreco': 'minoria', 'luiz.garibaldi': 'minoria',
            'luiz.garibaldi': 'minoria', 'assessor_plenario': 'minoria',
            'marcelo.oliveira': 'minoria',
        }
        for _un, _cat in _cats.items():
            _c.execute('UPDATE users SET categoria=? WHERE username=?', (_cat, _un))
        _conn.commit()
        _conn.close()
        logger.info('✅ Banco inicializado.')
    except Exception as _e:
        logger.error(f'❌ Erro banco: {_e}')

# --------------------------------------------------------------------------
# HELPERS DB
# --------------------------------------------------------------------------
def load_notas():
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    try:
        c.execute('SELECT item_key, resumo_materia, orientacao, resumo_parecer, saved_by, saved_at FROM notas')
        notas = {row[0]: {'resumo_materia': row[1], 'orientacao': row[2], 'resumo_parecer': row[3],
                          'saved_by': row[4] or '', 'saved_at': row[5] or ''}
                 for row in c.fetchall()}
    except Exception:
        # Coluna pode não existir ainda — tenta sem saved_by/saved_at
        try:
            c.execute('SELECT item_key, resumo_materia, orientacao, resumo_parecer FROM notas')
            notas = {row[0]: {'resumo_materia': row[1], 'orientacao': row[2], 'resumo_parecer': row[3],
                              'saved_by': '', 'saved_at': ''}
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
    conn = sqlite3.connect(DB)
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

def fetch_pauta(evento_id, force_reload=False):
    now = datetime.now()
    cache_key = str(evento_id)
    notas = load_notas()

    if not force_reload and cache_key in pauta_cache:
        cached = pauta_cache[cache_key]
        if now - cached['timestamp'] < CACHE_DURATION:
            return cached['itens'], False

    conn = sqlite3.connect(DB)
    c = conn.cursor()

    if not force_reload:
        try:
            c.execute("SELECT json_pauta, last_updated FROM pauta_cache_db WHERE evento_id = ?", (evento_id,))
            row = c.fetchone()
            if row:
                itens = json.loads(row[0])
                # Reaplica notas salvas sobre o cache
                for item in itens:
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
        itens_raw = obter_itens_pauta(evento_id)
        if not itens_raw:
            raise ValueError("Scraper sem itens")

        itens = []
        vistos = set()
        for ordem, item in enumerate(itens_raw, start=1):
            id_p = item.get('id_principal')
            if not id_p or id_p in vistos:
                continue
            vistos.add(id_p)
            key = f"PROP_{id_p}"
            itens.append({
                'ordem': str(ordem),
                'id_principal': id_p,
                'projeto': item['codigo'],
                'ementa': item['ementa'],
                'autor': item.get('autores', 'N/D'),
                'relator': item.get('relator', 'Não atribuído'),
                'situacao': item.get('situacao', 'N/D'),
                'secao': item.get('secao', 'N/D'),
                'resumo_materia': notas.get(key, {}).get('resumo_materia', ''),
                'orientacao':     notas.get(key, {}).get('orientacao', ''),
                'resumo_parecer': notas.get(key, {}).get('resumo_parecer', ''),
                'saved_by':       notas.get(key, {}).get('saved_by', ''),
                'saved_at':       notas.get(key, {}).get('saved_at', ''),
                'destaques_emendas': []
            })

        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
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
        return datetime.fromisoformat(value).strftime(format)
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
        conn = sqlite3.connect(DB)
        c = conn.cursor()
        c.execute('SELECT id, username, password, role, categoria FROM users WHERE username = ?', (username,))
        u = c.fetchone()
        conn.close()
        if u and bcrypt.check_password_hash(u[2], password):
            login_user(User(u[0], u[1], u[3], u[4] if len(u) > 4 else 'geral'))
            return redirect(url_for('selecionar_data'))
        flash('Usuário ou senha inválidos.', 'error')

    # Busca lista de usuários para o dropdown
    conn = sqlite3.connect(DB)
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
    data = request.form.get('data', datetime.now().strftime('%Y-%m-%d'))
    eventos = fetch_eventos_por_data(data)
    return render_template('selecionar_data.html', data_selecionada=data, eventos=eventos, user_role=current_user.role)

@app.route('/pauta/<int:evento_id>/view')
@login_required
def view_pauta(evento_id):
    force_reload = request.args.get('force_reload', 'false').lower() == 'true'
    itens, from_cache = fetch_pauta(evento_id, force_reload)
    conn = sqlite3.connect(DB)
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

    return render_template('pauta.html', evento_id=evento_id, evento=evento, itens=itens,
                           from_cache=from_cache, user_role=current_user.role,
                           user_categoria=current_user.categoria,
                           last_updated=last_updated, last_saved_user=last_saved_user)

@app.route('/save_item', methods=['POST'])
@login_required
def save_item():
    data = request.get_json()
    evento_id   = data.get('evento_id')
    id_principal = data.get('id_principal')
    ordem       = data.get('ordem')
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    try:
        prop_key = f"PROP_{id_principal}"
        now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        saved_by = current_user.display_name()
        c.execute('INSERT OR REPLACE INTO notas (item_key, evento_id, ordem, resumo_materia, orientacao, resumo_parecer, saved_by, saved_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                  (prop_key, evento_id, ordem, data.get('resumo_materia', ''), data.get('orientacao', ''), data.get('resumo_parecer', ''), saved_by, now_str))
        conn.commit()

        # Atualiza o cache persistente com as notas salvas e usuário
        c.execute("SELECT json_pauta FROM pauta_cache_db WHERE evento_id = ?", (evento_id,))
        row = c.fetchone()
        if row:
            try:
                itens = json.loads(row[0])
                for item in itens:
                    if str(item.get('id_principal')) == str(id_principal):
                        item['resumo_materia'] = data.get('resumo_materia', '')
                        item['orientacao']     = data.get('orientacao', '')
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

@app.route('/analisar_ia', methods=['POST'])
@login_required
def analisar_ia():
    data    = request.get_json()
    projeto = data.get('projeto', '')
    ementa  = data.get('ementa', '')
    autor   = data.get('autor', '')
    relator = data.get('relator', '')

    groq_key = os.environ.get('GROQ_API_KEY', '')
    if not groq_key:
        return jsonify({'error': 'Chave Groq não configurada.'}), 500

    prompt = f"""Você é um assessor legislativo especializado em análise de proposições da Câmara dos Deputados do Brasil.

Analise a seguinte proposição e gere uma nota técnica objetiva em português:

**Proposição:** {projeto}
**Autor(es):** {autor}
**Relator:** {relator}
**Ementa:** {ementa}

Gere a nota técnica com os seguintes tópicos em HTML simples (use <strong>, <br>, <ul>, <li>):

1. <strong>Objetivo da Proposição</strong> — o que a proposta pretende fazer
2. <strong>Principais Alterações</strong> — mudanças concretas propostas
3. <strong>Impacto Esperado</strong> — efeitos práticos para a sociedade
4. <strong>Pontos de Atenção</strong> — aspectos relevantes para o parlamentar

Seja objetivo e técnico. Máximo 300 palavras."""

    try:
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {groq_key}", "Content-Type": "application/json"},
            json={"model": "llama-3.3-70b-versatile", "messages": [{"role": "user", "content": prompt}], "max_tokens": 1024, "temperature": 0.3},
            timeout=30
        )
        r.raise_for_status()
        return jsonify({'resumo': r.json()['choices'][0]['message']['content']})
    except Exception as e:
        logger.error(f"Erro Groq: {e}")
        return jsonify({'error': str(e)}), 500

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
    pdf = gerar_infografico_pdf(evento, itens,
                                 os.path.join(static_path, 'logo_minoria.png'),
                                 os.path.join(static_path, 'logo_oposicao.png'))
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
        conn = sqlite3.connect(DB)
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
    if current_user.role != 'Admin':
        flash('Acesso restrito.', 'error')
        return redirect(url_for('selecionar_data'))
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute('SELECT id, username, role, categoria FROM users ORDER BY id DESC')
    usuarios = c.fetchall()
    conn.close()
    return render_template('admin_usuarios.html', usuarios=usuarios)

@app.route('/admin/usuarios/add', methods=['POST'])
@login_required
def add_usuario():
    if current_user.role != 'Admin':
        return jsonify({'error': 'Acesso negado'}), 403
    data      = request.get_json()
    username  = data.get('username', '').strip()
    password  = data.get('password', '').strip()
    role      = data.get('role', 'Assessor').strip()
    categoria = data.get('categoria', 'geral').strip()
    if not username or not password:
        return jsonify({'error': 'Usuário e senha obrigatórios'}), 400
    hashed = bcrypt.generate_password_hash(password).decode('utf-8')
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    try:
        c.execute('INSERT INTO users (username, password, role, categoria) VALUES (?, ?, ?, ?)',
                  (username, hashed, role, categoria))
        conn.commit()
        return jsonify({'message': 'Usuário criado!'})
    except sqlite3.IntegrityError:
        return jsonify({'error': 'Usuário já existe'}), 409
    finally:
        conn.close()

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
        'a favor':   colors.HexColor("#d4edda"),
        'contra':    colors.HexColor("#f8d7da"),
        'obstrução': colors.HexColor("#f8d7da"),
        'liberado':  colors.HexColor("#fff3cd"),
        'abstenção': colors.HexColor("#e2e3e5"),
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
    story.append(Paragraph(f"Gerado em: {datetime.now().strftime('%d/%m/%Y %H:%M')}", ParagraphStyle("sm", parent=SS["Normal"], fontSize=7.5, textColor=COR_CINZA, alignment=TA_CENTER)))
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

@app.route('/salvar_orientacoes', methods=['POST'])
@login_required
def salvar_orientacoes():
    """Salva orientações por grupo (PL, NOVO, oposicao, minoria) para cada item."""
    data      = request.get_json()
    evento_id = data.get('evento_id')
    orientacoes = data.get('orientacoes', [])  # [{id_principal, grupo, orientacao, comentario}]

    conn = sqlite3.connect(DB)
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

    now_str  = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
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
    conn = sqlite3.connect(DB)
    c    = conn.cursor()
    try:
        c.execute('''SELECT id_principal, grupo, orientacao, comentario, saved_by, saved_at
                     FROM orientacoes_grupo WHERE evento_id=?''', (evento_id,))
        rows = c.fetchall()
        result = [{'id_principal': r[0], 'grupo': r[1], 'orientacao': r[2],
                   'comentario': r[3], 'saved_by': r[4], 'saved_at': r[5]} for r in rows]
    except Exception:
        result = []
    conn.close()
    return jsonify(result)

@app.route('/admin/reset_todas_senhas', methods=['POST'])
@login_required
def reset_todas_senhas():
    if current_user.role != 'Admin':
        return jsonify({'error': 'Acesso negado'}), 403
    nova_hash = bcrypt.generate_password_hash('123').decode('utf-8')
    conn = sqlite3.connect(DB)
    c    = conn.cursor()
    c.execute('UPDATE users SET password=?', (nova_hash,))
    affected = c.rowcount
    conn.commit()
    conn.close()
    return jsonify({'message': f'Senha 123 definida para {affected} usuários.'})

@app.route('/admin/usuarios/reset_senha', methods=['POST'])
@login_required
def reset_senha():
    if current_user.role != 'Admin':
        return jsonify({'error': 'Acesso negado'}), 403
    data       = request.get_json()
    user_id    = data.get('user_id')
    nova_senha = data.get('nova_senha', '').strip()
    if not nova_senha or len(nova_senha) < 3:
        return jsonify({'error': 'Senha deve ter ao menos 3 caracteres.'}), 400
    nova_hash = bcrypt.generate_password_hash(nova_senha).decode('utf-8')
    conn = sqlite3.connect(DB)
    c    = conn.cursor()
    c.execute('UPDATE users SET password=? WHERE id=?', (nova_hash, user_id))
    conn.commit()
    conn.close()
    return jsonify({'message': 'Senha redefinida!'})

@app.route('/admin/usuarios/update_categoria', methods=['POST'])
@login_required
def update_categoria():
    if current_user.role != 'Admin':
        return jsonify({'error': 'Acesso negado'}), 403
    data      = request.get_json()
    user_id   = data.get('user_id')
    categoria = data.get('categoria', 'geral')
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute('UPDATE users SET categoria=? WHERE id=?', (categoria, user_id))
    conn.commit()
    conn.close()
    return jsonify({'message': 'Categoria atualizada!'})

@app.route('/admin/usuarios/delete/<int:user_id>', methods=['POST'])
@login_required
def delete_usuario(user_id):
    if current_user.role != 'Admin':
        return jsonify({'error': 'Acesso negado'}), 403
    if user_id == current_user.id:
        return jsonify({'error': 'Não pode excluir sua própria conta'}), 400
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute('DELETE FROM users WHERE id = ?', (user_id,))
    conn.commit()
    conn.close()
    return jsonify({'message': 'Usuário removido.'})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
