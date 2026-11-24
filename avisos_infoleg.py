"""Avisos do Infoleg em tempo real.

Como funciona
-------------
O Infoleg só manda notificação para o próprio aplicativo dele. Um celular Android com o Infoleg logado
roda o MacroDroid (ou Tasker), que lê cada notificação do Infoleg no instante em que ela chega e faz um
POST em /api/infoleg-aviso. Este módulo grava o aviso, mostra na tela /avisos e dispara Web Push para
todos os aparelhos que ativaram as notificações no app.

Chaves
------
- Chave do webhook: variável INFOLEG_CHAVE no Railway; se não existir, é gerada uma vez e guardada no banco
  (aparece para o admin na tela /avisos).
- Chaves VAPID do Web Push: variáveis VAPID_PRIVATE_KEY / VAPID_PUBLIC_KEY (base64url); se não existirem,
  são geradas uma vez e guardadas no banco.
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
from datetime import datetime, timedelta, timezone

from flask import Blueprint, jsonify, render_template, request
from flask_login import current_user, login_required

logger = logging.getLogger(__name__)

avisos_bp = Blueprint('avisos', __name__)

_get_conn = None
_use_pg = False
_ultimo_id = 0            # id do aviso mais recente (só 1 worker no gunicorn → memória é confiável)
_lock = threading.Lock()
_cfg_cache = {}

_BRT = timezone(timedelta(hours=-3))


def _agora():
    return datetime.now(_BRT)


# ─── Banco ───────────────────────────────────────────────────────────────────
def _criar_tabelas():
    conn = _get_conn()
    c = conn.cursor()
    try:
        if _use_pg:
            c.execute('''CREATE TABLE IF NOT EXISTS infoleg_avisos (
                id SERIAL PRIMARY KEY, recebido_em TEXT, titulo TEXT, texto TEXT, hash TEXT)''')
        else:
            c.execute('''CREATE TABLE IF NOT EXISTS infoleg_avisos (
                id INTEGER PRIMARY KEY AUTOINCREMENT, recebido_em TEXT, titulo TEXT, texto TEXT, hash TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS push_inscricoes (
            endpoint TEXT PRIMARY KEY, p256dh TEXT, auth TEXT, username TEXT, criado_em TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS app_config (
            chave TEXT PRIMARY KEY, valor TEXT)''')
        conn.commit()
        c.execute('SELECT MAX(id) FROM infoleg_avisos')
        row = c.fetchone()
        global _ultimo_id
        _ultimo_id = int(row[0] or 0) if row else 0
    finally:
        conn.close()


def _cfg(chave, gerar):
    """Lê uma configuração do banco; se não existir, gera com `gerar()` e grava."""
    if chave in _cfg_cache:
        return _cfg_cache[chave]
    conn = _get_conn()
    c = conn.cursor()
    try:
        c.execute('SELECT valor FROM app_config WHERE chave = ?', (chave,))
        row = c.fetchone()
        if row and row[0]:
            valor = row[0]
        else:
            valor = gerar()
            if _use_pg:
                c.execute('INSERT INTO app_config (chave, valor) VALUES (?, ?) ON CONFLICT (chave) DO NOTHING',
                          (chave, valor))
            else:
                c.execute('INSERT OR IGNORE INTO app_config (chave, valor) VALUES (?, ?)', (chave, valor))
            conn.commit()
            c.execute('SELECT valor FROM app_config WHERE chave = ?', (chave,))
            valor = c.fetchone()[0]
        _cfg_cache[chave] = valor
        return valor
    finally:
        conn.close()


def chave_webhook():
    return os.environ.get('INFOLEG_CHAVE') or _cfg('infoleg_chave', lambda: secrets.token_urlsafe(24))


def _b64url(b):
    return base64.urlsafe_b64encode(b).rstrip(b'=').decode()


def _chaves_vapid():
    """Retorna (privada_b64url_raw, publica_b64url)."""
    priv = os.environ.get('VAPID_PRIVATE_KEY')
    pub = os.environ.get('VAPID_PUBLIC_KEY')
    if priv and pub:
        return priv, pub

    def gerar():
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives import serialization
        k = ec.generate_private_key(ec.SECP256R1())
        raw_priv = k.private_numbers().private_value.to_bytes(32, 'big')
        raw_pub = k.public_key().public_bytes(serialization.Encoding.X962,
                                              serialization.PublicFormat.UncompressedPoint)
        return json.dumps({'priv': _b64url(raw_priv), 'pub': _b64url(raw_pub)})

    d = json.loads(_cfg('vapid', gerar))
    return d['priv'], d['pub']


# ─── Web Push ────────────────────────────────────────────────────────────────
def _enviar_push(titulo, texto, aviso_id):
    try:
        from pywebpush import webpush, WebPushException
        from py_vapid import Vapid
    except ImportError:
        logger.warning('[avisos] pywebpush não instalado — push desativado')
        return
    try:
        priv, _ = _chaves_vapid()
        vapid = Vapid.from_raw(priv.encode())
    except Exception as e:
        logger.error(f'[avisos] chave VAPID inválida: {e}')
        return

    conn = _get_conn()
    c = conn.cursor()
    try:
        c.execute('SELECT endpoint, p256dh, auth FROM push_inscricoes')
        inscricoes = c.fetchall()
    finally:
        conn.close()

    payload = json.dumps({'title': titulo or 'Infoleg', 'body': texto or '',
                          'tag': f'infoleg-{aviso_id}', 'url': '/avisos'}, ensure_ascii=False)
    mortos = []
    enviados = 0
    for endpoint, p256dh, auth in inscricoes:
        try:
            webpush(subscription_info={'endpoint': endpoint, 'keys': {'p256dh': p256dh, 'auth': auth}},
                    data=payload, vapid_private_key=vapid,
                    vapid_claims={'sub': 'mailto:marcelo.uvara@camara.leg.br'},
                    ttl=300, headers={'Urgency': 'high'}, timeout=10)
            enviados += 1
        except WebPushException as e:
            status = getattr(getattr(e, 'response', None), 'status_code', None)
            if status in (404, 410):
                mortos.append(endpoint)
            else:
                logger.warning(f'[avisos] push falhou ({status}): {e}')
        except Exception as e:
            logger.warning(f'[avisos] push falhou: {e}')

    if mortos:
        conn = _get_conn()
        c = conn.cursor()
        try:
            for ep in mortos:
                c.execute('DELETE FROM push_inscricoes WHERE endpoint = ?', (ep,))
            conn.commit()
        finally:
            conn.close()
    logger.info(f'[avisos] push #{aviso_id}: {enviados} enviados, {len(mortos)} inscrições removidas')


# ─── Recebimento ─────────────────────────────────────────────────────────────
def _ler_campos():
    """Aceita query string, formulário, JSON ou texto puro — o MacroDroid pode mandar de vários jeitos."""
    titulo = request.values.get('titulo') or request.values.get('title') or ''
    texto = request.values.get('texto') or request.values.get('text') or ''
    if not (titulo or texto):
        dados = request.get_json(silent=True, force=True)
        if isinstance(dados, dict):
            titulo = str(dados.get('titulo') or dados.get('title') or '')
            texto = str(dados.get('texto') or dados.get('text') or '')
        elif request.data:
            texto = request.data.decode('utf-8', 'replace')
    # Campos extras (ex.: texto grande, subtexto, ticker da notificação): junta os que vierem preenchidos.
    # Assim dá para mandar vários "textos mágicos" do MacroDroid sem precisar mexer no app.
    partes = [texto.strip()] if texto.strip() else []
    for k in request.values.keys():
        if k.lower() in ('chave', 'titulo', 'title', 'texto', 'text'):
            continue
        for v in request.values.getlist(k):
            v = (v or '').strip()
            if v and v != titulo.strip() and not v.startswith('{') and all(v not in p for p in partes):
                partes = [p for p in partes if p not in v]  # se o novo contém um anterior, fica só o maior
                partes.append(v)
    if not titulo.strip() and len(partes) > 1:
        titulo = partes.pop(0)
    texto = '\n'.join(partes)
    return titulo.strip()[:300], texto.strip()[:4000]


def registrar_aviso(titulo, texto):
    """Grava o aviso (ignorando repetição do mesmo texto em 2 min) e dispara o push. Retorna id ou None."""
    global _ultimo_id
    h = hashlib.sha1(f'{titulo}\n{texto}'.encode()).hexdigest()
    agora = _agora()
    limite = (agora - timedelta(minutes=2)).strftime('%Y-%m-%d %H:%M:%S')
    with _lock:
        conn = _get_conn()
        c = conn.cursor()
        try:
            c.execute('SELECT id FROM infoleg_avisos WHERE hash = ? AND recebido_em >= ?', (h, limite))
            if c.fetchone():
                return None
            valores = (agora.strftime('%Y-%m-%d %H:%M:%S'), titulo, texto, h)
            if _use_pg:
                c.execute('INSERT INTO infoleg_avisos (recebido_em, titulo, texto, hash) '
                          'VALUES (?, ?, ?, ?) RETURNING id', valores)
                novo_id = c.fetchone()[0]
            else:
                c.execute('INSERT INTO infoleg_avisos (recebido_em, titulo, texto, hash) VALUES (?, ?, ?, ?)',
                          valores)
                novo_id = c.lastrowid
            conn.commit()
        finally:
            conn.close()
        _ultimo_id = max(_ultimo_id, int(novo_id))
    threading.Thread(target=_enviar_push, args=(titulo, texto, novo_id), daemon=True).start()
    return novo_id


@avisos_bp.route('/api/infoleg-aviso', methods=['POST', 'GET'])
def receber_aviso():
    chave = request.values.get('chave') or request.headers.get('X-Chave') or ''
    if not hmac.compare_digest(chave, chave_webhook()):
        return jsonify({'ok': False, 'erro': 'chave inválida'}), 403
    titulo, texto = _ler_campos()
    logger.info(f'[avisos] recebido: campos={[k for k in request.values.keys() if k != "chave"]} '
                f'titulo={len(titulo)}c texto={len(texto)}c corpo={len(request.data or b"")}b')
    if not (titulo or texto):
        # Chega vazio quando a ação é testada à mão no MacroDroid (sem notificação real, o texto mágico fica vazio).
        # Registra mesmo assim, para confirmar que a ligação celular → app está funcionando.
        logger.info(f'[avisos] aviso vazio recebido ({request.method}, {request.content_type})')
        titulo, texto = 'Conexão com o celular OK', '(chegou sem texto — normal ao testar a ação à mão no MacroDroid)'
    novo_id = registrar_aviso(titulo, texto)
    return jsonify({'ok': True, 'id': novo_id, 'duplicado': novo_id is None})


# ─── Consulta e tela ─────────────────────────────────────────────────────────
@avisos_bp.route('/api/avisos')
@login_required
def listar_avisos():
    try:
        apos = int(request.args.get('apos', 0))
    except ValueError:
        apos = 0
    # Atalho barato para o polling: nada novo → nem abre conexão
    if apos >= _ultimo_id:
        return jsonify({'ultimo_id': _ultimo_id, 'avisos': []})
    limite = min(int(request.args.get('limite', 100) or 100), 300)
    conn = _get_conn()
    c = conn.cursor()
    try:
        c.execute('SELECT id, recebido_em, titulo, texto FROM infoleg_avisos WHERE id > ? '
                  'ORDER BY id DESC LIMIT ?', (apos, limite))
        avisos = [{'id': r[0], 'recebido_em': r[1], 'titulo': r[2] or '', 'texto': r[3] or ''}
                  for r in c.fetchall()]
    finally:
        conn.close()
    return jsonify({'ultimo_id': _ultimo_id, 'avisos': avisos})


@avisos_bp.route('/avisos')
@login_required
def tela_avisos():
    eh_admin = (current_user.role or '').lower() == 'admin'
    webhook = ''
    if eh_admin:
        base = request.url_root.rstrip('/').replace('http://', 'https://')
        webhook = f'{base}/api/infoleg-aviso?chave={chave_webhook()}'
    return render_template('avisos.html', eh_admin=eh_admin, webhook=webhook)


@avisos_bp.route('/api/avisos/teste', methods=['POST'])
@login_required
def aviso_teste():
    if (current_user.role or '').lower() != 'admin':
        return jsonify({'ok': False}), 403
    novo_id = registrar_aviso('Teste — Pauta Plenário',
                              f'Aviso de teste enviado às {_agora().strftime("%H:%M:%S")}')
    return jsonify({'ok': True, 'id': novo_id})


# ─── Inscrição de aparelhos para Web Push ────────────────────────────────────
@avisos_bp.route('/api/push/chave')
@login_required
def push_chave():
    return jsonify({'publica': _chaves_vapid()[1]})


@avisos_bp.route('/api/push/inscrever', methods=['POST'])
@login_required
def push_inscrever():
    d = request.get_json(silent=True) or {}
    endpoint = d.get('endpoint')
    keys = d.get('keys') or {}
    if not (endpoint and keys.get('p256dh') and keys.get('auth')):
        return jsonify({'ok': False, 'erro': 'inscrição incompleta'}), 400
    conn = _get_conn()
    c = conn.cursor()
    try:
        c.execute('DELETE FROM push_inscricoes WHERE endpoint = ?', (endpoint,))
        c.execute('INSERT INTO push_inscricoes (endpoint, p256dh, auth, username, criado_em) VALUES (?, ?, ?, ?, ?)',
                  (endpoint, keys['p256dh'], keys['auth'], current_user.username,
                   _agora().strftime('%Y-%m-%d %H:%M:%S')))
        conn.commit()
    finally:
        conn.close()
    return jsonify({'ok': True})


@avisos_bp.route('/api/push/cancelar', methods=['POST'])
@login_required
def push_cancelar():
    endpoint = (request.get_json(silent=True) or {}).get('endpoint')
    if endpoint:
        conn = _get_conn()
        c = conn.cursor()
        try:
            c.execute('DELETE FROM push_inscricoes WHERE endpoint = ?', (endpoint,))
            conn.commit()
        finally:
            conn.close()
    return jsonify({'ok': True})


def init_avisos(app, get_conn, use_postgres):
    global _get_conn, _use_pg
    _get_conn = get_conn
    _use_pg = use_postgres
    try:
        _criar_tabelas()
        logger.info('✅ Avisos do Infoleg prontos')
    except Exception as e:
        logger.error(f'❌ Avisos do Infoleg: erro ao criar tabelas: {e}')
    app.register_blueprint(avisos_bp)
