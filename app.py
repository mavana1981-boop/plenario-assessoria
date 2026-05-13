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
            role TEXT NOT NULL)''')
        _c.execute('''CREATE TABLE IF NOT EXISTS notas (
            item_key TEXT PRIMARY KEY,
            evento_id INTEGER,
            ordem TEXT,
            resumo_materia TEXT,
            orientacao TEXT,
            resumo_parecer TEXT)''')
        _c.execute('''CREATE TABLE IF NOT EXISTS pauta_cache_db (
            evento_id INTEGER PRIMARY KEY,
            json_pauta TEXT,
            last_updated TEXT)''')
        _conn.commit()
        _bcrypt = _Bc()
        for _u in [
            ('admin',             _bcrypt.generate_password_hash('123').decode('utf-8'), 'Admin'),
            ('assessor_plenario', _bcrypt.generate_password_hash('123').decode('utf-8'), 'Assessor Plenário'),
            ('assessor',          _bcrypt.generate_password_hash('123').decode('utf-8'), 'Assessor'),
        ]:
            try:
                _c.execute('INSERT INTO users (username, password, role) VALUES (?, ?, ?)', _u)
            except _sq.IntegrityError:
                pass
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
        c.execute('SELECT item_key, resumo_materia, orientacao, resumo_parecer FROM notas')
        notas = {row[0]: {'resumo_materia': row[1], 'orientacao': row[2], 'resumo_parecer': row[3]} for row in c.fetchall()}
    except Exception:
        notas = {}
    finally:
        conn.close()
    return notas

# --------------------------------------------------------------------------
# LOGIN
# --------------------------------------------------------------------------
class User(UserMixin):
    def __init__(self, id, username, role):
        self.id = id; self.username = username; self.role = role

@login_manager.user_loader
def load_user(user_id):
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute('SELECT id, username, role FROM users WHERE id = ?', (user_id,))
    u = c.fetchone()
    conn.close()
    return User(u[0], u[1], u[2]) if u else None

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
        c.execute('SELECT id, username, password, role FROM users WHERE username = ?', (username,))
        u = c.fetchone()
        conn.close()
        if u and bcrypt.check_password_hash(u[2], password):
            login_user(User(u[0], u[1], u[3]))
            return redirect(url_for('selecionar_data'))
        flash('Usuário ou senha inválidos.', 'error')
    return render_template('login.html')

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
    last_updated = None
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    try:
        c.execute("SELECT last_updated FROM pauta_cache_db WHERE evento_id = ?", (evento_id,))
        row = c.fetchone()
        if row:
            last_updated = row[0]
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
                           from_cache=from_cache, user_role=current_user.role, last_updated=last_updated)

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
        c.execute('INSERT OR REPLACE INTO notas (item_key, evento_id, ordem, resumo_materia, orientacao, resumo_parecer) VALUES (?, ?, ?, ?, ?, ?)',
                  (prop_key, evento_id, ordem, data.get('resumo_materia', ''), data.get('orientacao', ''), data.get('resumo_parecer', '')))
        conn.commit()

        # Atualiza o cache persistente com as notas salvas
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
                now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                c.execute('UPDATE pauta_cache_db SET json_pauta = ?, last_updated = ? WHERE evento_id = ?',
                          (json.dumps(itens), now_str, evento_id))
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

@app.route('/admin/usuarios')
@login_required
def admin_usuarios():
    if current_user.role != 'Admin':
        flash('Acesso restrito.', 'error')
        return redirect(url_for('selecionar_data'))
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute('SELECT id, username, role FROM users')
    usuarios = c.fetchall()
    conn.close()
    return render_template('admin_usuarios.html', usuarios=usuarios)

@app.route('/admin/usuarios/add', methods=['POST'])
@login_required
def add_usuario():
    if current_user.role != 'Admin':
        return jsonify({'error': 'Acesso negado'}), 403
    data = request.get_json()
    username = data.get('username', '').strip()
    password = data.get('password', '').strip()
    role     = data.get('role', 'Assessor').strip()
    if not username or not password:
        return jsonify({'error': 'Usuário e senha obrigatórios'}), 400
    hashed = bcrypt.generate_password_hash(password).decode('utf-8')
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    try:
        c.execute('INSERT INTO users (username, password, role) VALUES (?, ?, ?)', (username, hashed, role))
        conn.commit()
        return jsonify({'message': 'Usuário criado!'})
    except sqlite3.IntegrityError:
        return jsonify({'error': 'Usuário já existe'}), 409
    finally:
        conn.close()

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
