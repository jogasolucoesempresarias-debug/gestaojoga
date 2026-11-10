"""
Gestão JOGA — Sistema financeiro (Receitas, Despesas, Impostos, Caixa)
Rode: python -X utf8 server.py
Acesse: http://localhost:5000
"""

import os
import io
import csv
import json
import time
import secrets
import logging
import calendar
import functools
import threading
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from urllib.parse import urlparse

import psycopg2
from psycopg2.pool import ThreadedConnectionPool
from psycopg2.extras import RealDictCursor, Json
from flask import (
    Flask, jsonify, send_from_directory, request, session, redirect, Response, has_request_context
)
from werkzeug.exceptions import HTTPException
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv

import comercial_agente
import schema

load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('gestao-joga')

DEV = bool(os.getenv('DEV')) or os.getenv('FLASK_ENV') == 'development'

app = Flask(__name__, static_folder=None)

_secret = os.getenv('SECRET_KEY', '').strip()
if not _secret:
    if DEV:
        _secret = 'dev-secret-change-me'
    else:
        # Nunca cair numa chave conhecida em produção: sem SECRET_KEY, gera uma aleatória
        # (o efeito colateral é derrubar as sessões a cada reinício, o que avisa o problema).
        _secret = secrets.token_hex(32)
        log.warning('SECRET_KEY não definida — usando chave aleatória; as sessões caem a cada reinício.')
app.secret_key = _secret
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=os.getenv('COOKIE_SECURE', '0' if DEV else '1') == '1',
    MAX_CONTENT_LENGTH=2 * 1024 * 1024,
)


# ══════════════════════════════════════════════════════════════════════
# Banco de dados (pool de conexões)
# ══════════════════════════════════════════════════════════════════════
def _dsn():
    return dict(
        host=os.getenv('DB_HOST', 'localhost'),
        port=os.getenv('DB_PORT', '5432'),
        dbname=os.getenv('DB_NAME', 'gestao_joga'),
        user=os.getenv('DB_USER', 'postgres'),
        password=os.getenv('DB_PASSWORD', ''),
        connect_timeout=10,
    )


_pool = None
_pool_lock = threading.Lock()


def _get_pool():
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = ThreadedConnectionPool(1, int(os.getenv('DB_POOL_MAX', '10')), **_dsn())
    return _pool


@contextmanager
def db():
    """Conexão do pool numa transação: commit na saída normal, rollback em erro."""
    pool = _get_pool()
    conn = pool.getconn()
    if conn.closed:
        pool.putconn(conn, close=True)
        conn = pool.getconn()
    descartar = False
    try:
        yield conn
        conn.commit()
    except Exception as e:
        if isinstance(e, (psycopg2.OperationalError, psycopg2.InterfaceError)):
            descartar = True
        try:
            conn.rollback()
        except Exception:
            descartar = True
        raise
    finally:
        pool.putconn(conn, close=descartar or bool(conn.closed))


def _serialize(value):
    """Converte tipos do Postgres (Decimal, date) em JSON-friendly."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _clean(row):
    if row is None:
        return None
    return {k: _serialize(v) for k, v in row.items()}


def _ler(fn):
    """Leitura com 1 nova tentativa se a conexão do pool tiver caído (ex.: Postgres reiniciou)."""
    for tentativa in (1, 2):
        try:
            with db() as conn:
                return fn(conn)
        except (psycopg2.OperationalError, psycopg2.InterfaceError):
            if tentativa == 2:
                raise


def q_all(sql, params=None):
    def f(conn):
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params or ())
            return [_clean(r) for r in cur.fetchall()]
    return _ler(f)


def q_one(sql, params=None):
    def f(conn):
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params or ())
            return _clean(cur.fetchone())
    return _ler(f)


def scalar(sql, params=None):
    def f(conn):
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            row = cur.fetchone()
        v = row[0] if row else None
        return float(v) if isinstance(v, Decimal) else v
    return _ler(f)


def execute(sql, params=None, returning=False):
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, params or ())
        return _clean(cur.fetchone()) if returning else cur.rowcount


def registrar(cur, acao, entidade, entidade_id, detalhe=None):
    """Grava no histórico quem fez o quê (na mesma transação da alteração)."""
    uid = session.get('user_id') if has_request_context() else None
    cur.execute(
        "INSERT INTO historico (usuario_id, acao, entidade, entidade_id, detalhe) VALUES (%s,%s,%s,%s,%s)",
        (uid, acao, entidade, entidade_id,
         Json(detalhe, dumps=lambda o: json.dumps(o, default=str, ensure_ascii=False)) if detalhe else None)
    )


def _update_parcial(cur, tabela, id_, campos):
    """UPDATE só dos campos enviados. As chaves vêm sempre de listas fixas no código."""
    if not campos:
        return
    sets = ', '.join(f"{k} = %s" for k in campos)
    cur.execute(f"UPDATE {tabela} SET {sets} WHERE id = %s", (*campos.values(), id_))


# ══════════════════════════════════════════════════════════════════════
# Erros e validação
# ══════════════════════════════════════════════════════════════════════
class ErroApi(Exception):
    def __init__(self, msg, status=400, **extra):
        super().__init__(msg)
        self.status = status
        self.extra = extra


@app.errorhandler(ErroApi)
def _erro_api(e):
    return jsonify({'ok': False, 'error': str(e), **e.extra}), e.status


@app.errorhandler(Exception)
def _erro_geral(e):
    if isinstance(e, HTTPException):
        if request.path.startswith('/api/'):
            return jsonify({'ok': False, 'error': e.description or e.name}), e.code
        return e
    log.exception('Erro em %s %s', request.method, request.path)
    if request.path.startswith('/api/'):
        return jsonify({'ok': False, 'error': 'Erro interno — tente de novo. Se continuar, avise o suporte.'}), 500
    return 'Erro interno', 500


def corpo():
    d = request.get_json(silent=True)
    return d if isinstance(d, dict) else {}


def parse_date(s, default=None):
    if not s:
        return default
    try:
        return date.fromisoformat(str(s)[:10])
    except (ValueError, TypeError):
        return default


def parse_valor(v, campo='Valor', permitir_zero=False):
    try:
        d = Decimal(str(v)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError):
        raise ErroApi(f'{campo} inválido')
    if not d.is_finite() or d < 0 or (d == 0 and not permitir_zero):
        raise ErroApi(f'{campo} deve ser maior que zero')
    return d


# Conversores usados nos PUT parciais: cada um valida e normaliza um campo
def _txt_req(nome, maximo=255):
    def f(v):
        s = v.strip() if isinstance(v, str) else ''
        if not s:
            raise ErroApi(f'{nome} obrigatório')
        return s[:maximo]
    return f


def _txt_opt(maximo=255):
    def f(v):
        if v is None:
            return None
        s = str(v).strip()
        return s[:maximo] or None
    return f


def _id_opt(v):
    if v in (None, '', 0, '0'):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        raise ErroApi('Identificador inválido')


def _data_req(nome):
    def f(v):
        d = parse_date(v)
        if not d:
            raise ErroApi(f'{nome} inválida')
        return d
    return f


def _data_opt(v):
    if v in (None, ''):
        return None
    d = parse_date(v)
    if not d:
        raise ErroApi('Data inválida')
    return d


def _valor_req(nome):
    return lambda v: parse_valor(v, nome)


def _valor_zero_ok(nome):
    def f(v):
        try:
            return Decimal(str(v if v not in (None, '') else 0)).quantize(Decimal('0.01'))
        except (InvalidOperation, ValueError, TypeError):
            raise ErroApi(f'{nome} inválido')
    return f


def _int_faixa(nome, minimo, maximo):
    def f(v):
        try:
            n = int(v)
        except (TypeError, ValueError):
            raise ErroApi(f'{nome} inválido')
        if not minimo <= n <= maximo:
            raise ErroApi(f'{nome} deve ficar entre {minimo} e {maximo}')
        return n
    return f


def _enum(nome, opcoes):
    def f(v):
        if v not in opcoes:
            raise ErroApi(f'{nome} inválido')
        return v
    return f


def _campos(d, regras):
    """Aplica os conversores só aos campos que vieram no corpo (PUT parcial de verdade)."""
    return {k: conv(d[k]) for k, conv in regras.items() if k in d}


# ── Helpers financeiros ───────────────────────────────────────────────
def add_months(d: date, n: int) -> date:
    """Soma n meses a uma data, ajustando dia pro fim do mês quando necessário."""
    m = d.month - 1 + n
    y = d.year + m // 12
    m = m % 12 + 1
    day = min(d.day, calendar.monthrange(y, m)[1])
    return date(y, m, day)


def fim_do_mes(d: date) -> date:
    return date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])


def split_valor(total, n: int):
    """Divide um total em n parcelas de 2 casas; última absorve o arredondamento."""
    total = Decimal(str(total)).quantize(Decimal('0.01'))
    n = max(1, int(n))
    base = (total / n).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    parts = [base] * (n - 1)
    parts.append(total - base * (n - 1))
    return parts


def parse_competencia(comp, default: date):
    """'YYYY-MM' → date(ano, mes, 1)."""
    if not comp:
        return default.replace(day=1)
    try:
        return date(int(comp[:4]), int(comp[5:7]), 1)
    except (ValueError, IndexError, TypeError):
        raise ErroApi('Competência inválida (use AAAA-MM)')


def status_efetivo(p):
    """Deriva 'atrasado' de parcelas previstas vencidas (não armazenado)."""
    venc = p.get('data_vencimento')
    if p.get('status') == 'previsto' and venc:
        if date.fromisoformat(venc[:10]) < date.today():
            return 'atrasado'
    return p.get('status')


def conta_padrao(cur):
    cur.execute("SELECT id FROM contas WHERE ativo ORDER BY id LIMIT 1")
    row = cur.fetchone()
    if not row:
        raise ErroApi('Cadastre uma conta/caixa antes de dar baixa', 409)
    return row['id'] if isinstance(row, dict) else row[0]


def _recalcular_total(cur, lancamento_id):
    """valor_total do lançamento = soma das parcelas não canceladas."""
    cur.execute("""
        UPDATE lancamentos SET valor_total = (
            SELECT COALESCE(SUM(valor), 0) FROM parcelas
            WHERE lancamento_id = %s AND status <> 'cancelado')
        WHERE id = %s
    """, (lancamento_id, lancamento_id))


# ══════════════════════════════════════════════════════════════════════
# Segurança: origem das requisições, headers, tentativas de login
# ══════════════════════════════════════════════════════════════════════
@app.before_request
def _checar_origem():
    """Bloqueia alteração disparada de outro site (o navegador manda o Origin em POST/PUT/DELETE)."""
    if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
        origin = request.headers.get('Origin')
        if origin and urlparse(origin).netloc != request.host:
            return jsonify({'ok': False, 'error': 'Origem não permitida'}), 403
    return None


@app.after_request
def _headers_seguranca(resp):
    resp.headers.setdefault('X-Content-Type-Options', 'nosniff')
    resp.headers.setdefault('X-Frame-Options', 'DENY')
    resp.headers.setdefault('Referrer-Policy', 'same-origin')
    return resp


LOGIN_JANELA_S = 15 * 60
LOGIN_MAX_POR_IP = 5
LOGIN_MAX_POR_EMAIL = 10
_falhas_login = {}
_falhas_lock = threading.Lock()


def _ip_cliente():
    fwd = request.headers.get('X-Forwarded-For', '')
    return (fwd.split(',')[0].strip() if fwd else '') or (request.remote_addr or '?')


def _qtd_falhas(chave):
    agora = time.time()
    with _falhas_lock:
        recentes = [t for t in _falhas_login.get(chave, []) if agora - t < LOGIN_JANELA_S]
        if recentes:
            _falhas_login[chave] = recentes
        else:
            _falhas_login.pop(chave, None)
        return len(recentes)


def _registrar_falha(*chaves):
    agora = time.time()
    with _falhas_lock:
        for c in chaves:
            _falhas_login.setdefault(c, []).append(agora)


def _limpar_falhas(*chaves):
    with _falhas_lock:
        for c in chaves:
            _falhas_login.pop(c, None)


# ── Autenticação ──────────────────────────────────────────────────────
def login_required(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            if request.path.startswith('/api/'):
                return jsonify({'ok': False, 'error': 'Não autenticado'}), 401
            return redirect('/login')
        if session.get('must_change_password') and request.path not in ('/trocar-senha', '/api/trocar-senha'):
            if request.path.startswith('/api/'):
                return jsonify({'ok': False, 'error': 'Troque a senha antes de continuar', 'redirect': '/trocar-senha'}), 403
            return redirect('/trocar-senha')
        return f(*args, **kwargs)
    return decorated


# ── Páginas estáticas ─────────────────────────────────────────────────
PAGINAS = {
    '/':             'index.html',
    '/lancamentos':  'lancamentos.html',
    '/contas':       'contas.html',
    '/recorrencias': 'recorrencias.html',
    '/impostos':     'impostos.html',
    '/comercial':    'comercial.html',
    '/cadastros':    'cadastros.html',
}
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


@app.route('/static/<path:filename>')
def static_assets(filename):
    return send_from_directory(os.path.join(BASE_DIR, 'static'), filename)


@app.route('/health')
def health():
    return jsonify({'ok': True, 'service': 'gestao-joga'}), 200


@app.route('/login', methods=['GET'])
def login_page():
    if 'user_id' in session and not session.get('must_change_password'):
        return redirect('/')
    return send_from_directory(BASE_DIR, 'login.html')


@app.route('/trocar-senha', methods=['GET'])
def trocar_senha_page():
    if 'user_id' not in session:
        return redirect('/login')
    return send_from_directory(BASE_DIR, 'trocar-senha.html')


@app.route('/<path:rota>')
@app.route('/', endpoint='root')
@login_required
def pagina(rota=''):
    arquivo = PAGINAS.get('/' + rota if rota else '/')
    if not arquivo:
        return jsonify({'ok': False, 'error': 'Página não encontrada'}), 404
    return send_from_directory(BASE_DIR, arquivo)


# ── Auth API ──────────────────────────────────────────────────────────
@app.route('/api/login', methods=['POST'])
def login_post():
    data = corpo()
    email = str(data.get('email') or '').strip().lower()
    senha = str(data.get('senha') or '')
    if not email or not senha:
        return jsonify({'ok': False, 'error': 'Preencha e-mail e senha'}), 400

    chave_ip, chave_email = ('ip', _ip_cliente()), ('email', email)
    if _qtd_falhas(chave_ip) >= LOGIN_MAX_POR_IP or _qtd_falhas(chave_email) >= LOGIN_MAX_POR_EMAIL:
        return jsonify({'ok': False, 'error': 'Muitas tentativas erradas. Aguarde 15 minutos e tente de novo.'}), 429

    user = q_one(
        "SELECT id, nome, password_hash, role, ativo, must_change_password "
        "FROM usuarios WHERE email = %s", (email,)
    )
    if not user or not check_password_hash(user['password_hash'], senha):
        _registrar_falha(chave_ip, chave_email)
        return jsonify({'ok': False, 'error': 'E-mail ou senha inválidos'}), 401
    if not user['ativo']:
        return jsonify({'ok': False, 'error': 'Conta desativada'}), 403

    _limpar_falhas(chave_ip, chave_email)
    session.clear()
    session['user_id'] = user['id']
    session['nome'] = user['nome']
    session['role'] = user['role']
    session['must_change_password'] = bool(user['must_change_password'])
    if user['must_change_password']:
        return jsonify({'ok': True, 'redirect': '/trocar-senha'})
    return jsonify({'ok': True, 'redirect': '/'})


SENHA_MINIMA = 8


@app.route('/api/trocar-senha', methods=['POST'])
@login_required
def trocar_senha_post():
    data = corpo()
    nova = str(data.get('nova') or '')
    confirma = str(data.get('confirma') or '')
    if len(nova) < SENHA_MINIMA:
        return jsonify({'ok': False, 'error': f'Senha deve ter no mínimo {SENHA_MINIMA} caracteres'}), 400
    if nova != confirma:
        return jsonify({'ok': False, 'error': 'Confirmação não bate com a nova senha'}), 400
    execute(
        "UPDATE usuarios SET password_hash = %s, must_change_password = false WHERE id = %s",
        (generate_password_hash(nova), session['user_id'])
    )
    session['must_change_password'] = False
    return jsonify({'ok': True, 'redirect': '/'})


@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')


@app.route('/api/me')
@login_required
def me():
    return jsonify({'ok': True, 'nome': session.get('nome'), 'role': session.get('role')})


# ══════════════════════════════════════════════════════════════════════
# Cadastros: Categorias, Contatos, Contas, Transferências
# ══════════════════════════════════════════════════════════════════════
@app.route('/api/categorias', methods=['GET'])
@login_required
def categorias_list():
    tipo = request.args.get('tipo')
    if tipo in ('receita', 'despesa'):
        rows = q_all("SELECT * FROM categorias WHERE ativo AND tipo=%s ORDER BY nome", (tipo,))
    else:
        rows = q_all("SELECT * FROM categorias WHERE ativo ORDER BY tipo, nome")
    return jsonify({'ok': True, 'rows': rows})


REGRAS_CATEGORIA = {'nome': _txt_req('Nome', 120), 'cor': _txt_opt(9)}


@app.route('/api/categorias', methods=['POST'])
@login_required
def categorias_create():
    d = corpo()
    nome = _txt_req('Nome', 120)(d.get('nome'))
    tipo = _enum('Tipo (receita/despesa)', ('receita', 'despesa'))(d.get('tipo'))
    row = execute(
        "INSERT INTO categorias (nome, tipo, cor) VALUES (%s,%s,%s) RETURNING *",
        (nome, tipo, _txt_opt(9)(d.get('cor')) or '#38bdf8'), returning=True
    )
    return jsonify({'ok': True, 'row': row})


@app.route('/api/categorias/<int:cid>', methods=['PUT'])
@login_required
def categorias_update(cid):
    campos = _campos(corpo(), REGRAS_CATEGORIA)
    with db() as conn, conn.cursor() as cur:
        _update_parcial(cur, 'categorias', cid, campos)
    return jsonify({'ok': True})


@app.route('/api/categorias/<int:cid>', methods=['DELETE'])
@login_required
def categorias_delete(cid):
    execute("UPDATE categorias SET ativo=false WHERE id=%s", (cid,))
    return jsonify({'ok': True})


@app.route('/api/contatos', methods=['GET'])
@login_required
def contatos_list():
    rows = q_all("SELECT * FROM contatos WHERE ativo ORDER BY nome")
    return jsonify({'ok': True, 'rows': rows})


REGRAS_CONTATO = {
    'nome': _txt_req('Nome'),
    'tipo': _enum('Tipo', ('cliente', 'fornecedor', 'ambos')),
    'documento': _txt_opt(20),
    'email': _txt_opt(255),
    'telefone': _txt_opt(30),
}


@app.route('/api/contatos', methods=['POST'])
@login_required
def contatos_create():
    d = corpo()
    d.setdefault('tipo', 'cliente')
    if d.get('tipo') not in ('cliente', 'fornecedor', 'ambos'):
        d['tipo'] = 'cliente'
    if 'nome' not in d:
        raise ErroApi('Nome obrigatório')
    c = _campos(d, REGRAS_CONTATO)
    row = execute(
        "INSERT INTO contatos (nome, tipo, documento, email, telefone) "
        "VALUES (%s,%s,%s,%s,%s) RETURNING *",
        (c['nome'], c['tipo'], c.get('documento'), c.get('email'), c.get('telefone')), returning=True
    )
    return jsonify({'ok': True, 'row': row})


@app.route('/api/contatos/<int:cid>', methods=['PUT'])
@login_required
def contatos_update(cid):
    campos = _campos(corpo(), REGRAS_CONTATO)
    with db() as conn, conn.cursor() as cur:
        _update_parcial(cur, 'contatos', cid, campos)
    return jsonify({'ok': True})


@app.route('/api/contatos/<int:cid>', methods=['DELETE'])
@login_required
def contatos_delete(cid):
    execute("UPDATE contatos SET ativo=false WHERE id=%s", (cid,))
    return jsonify({'ok': True})


# Saldo de cada conta = inicial + baixas (pelo valor efetivamente pago) ± transferências
SQL_CONTAS_SALDO = """
    SELECT c.*,
        c.saldo_inicial
        + COALESCE((
            SELECT SUM(CASE WHEN l.tipo='receita' THEN COALESCE(p.valor_pago, p.valor)
                            ELSE -COALESCE(p.valor_pago, p.valor) END)
            FROM parcelas p JOIN lancamentos l ON l.id = p.lancamento_id
            WHERE p.conta_id = c.id AND p.status='pago'), 0)
        + COALESCE((SELECT SUM(t.valor) FROM transferencias t WHERE t.conta_destino = c.id), 0)
        - COALESCE((SELECT SUM(t.valor) FROM transferencias t WHERE t.conta_origem  = c.id), 0)
        AS saldo_atual
    FROM contas c
"""


@app.route('/api/contas', methods=['GET'])
@login_required
def contas_list():
    rows = q_all(SQL_CONTAS_SALDO + " WHERE c.ativo ORDER BY c.id")
    return jsonify({'ok': True, 'rows': rows})


REGRAS_CONTA = {'nome': _txt_req('Nome', 120), 'saldo_inicial': _valor_zero_ok('Saldo inicial')}


@app.route('/api/contas', methods=['POST'])
@login_required
def contas_create():
    d = corpo()
    nome = _txt_req('Nome', 120)(d.get('nome'))
    saldo = _valor_zero_ok('Saldo inicial')(d.get('saldo_inicial'))
    row = execute("INSERT INTO contas (nome, saldo_inicial) VALUES (%s,%s) RETURNING *",
                  (nome, saldo), returning=True)
    return jsonify({'ok': True, 'row': row})


@app.route('/api/contas/<int:cid>', methods=['PUT'])
@login_required
def contas_update(cid):
    campos = _campos(corpo(), REGRAS_CONTA)
    with db() as conn, conn.cursor() as cur:
        _update_parcial(cur, 'contas', cid, campos)
    return jsonify({'ok': True})


@app.route('/api/contas/<int:cid>', methods=['DELETE'])
@login_required
def contas_delete(cid):
    conta = q_one(SQL_CONTAS_SALDO + " WHERE c.id = %s AND c.ativo", (cid,))
    if not conta:
        raise ErroApi('Conta não encontrada', 404)
    if abs(conta['saldo_atual'] or 0) >= 0.005:
        raise ErroApi('A conta ainda tem saldo. Transfira o saldo para outra conta antes de excluir.', 409)
    if scalar("SELECT COUNT(*) FROM contas WHERE ativo") <= 1:
        raise ErroApi('Esta é a única conta ativa — cadastre outra antes de excluir.', 409)
    execute("UPDATE contas SET ativo=false WHERE id=%s", (cid,))
    return jsonify({'ok': True})


@app.route('/api/transferencias', methods=['GET'])
@login_required
def transferencias_list():
    rows = q_all("""
        SELECT t.*, o.nome AS origem_nome, d.nome AS destino_nome
        FROM transferencias t
        JOIN contas o ON o.id = t.conta_origem
        JOIN contas d ON d.id = t.conta_destino
        ORDER BY t.data DESC, t.id DESC LIMIT 200
    """)
    return jsonify({'ok': True, 'rows': rows})


@app.route('/api/transferencias', methods=['POST'])
@login_required
def transferencias_create():
    d = corpo()
    origem, destino = _id_opt(d.get('conta_origem')), _id_opt(d.get('conta_destino'))
    if not origem or not destino:
        raise ErroApi('Escolha a conta de origem e a de destino')
    if origem == destino:
        raise ErroApi('Origem e destino precisam ser contas diferentes')
    valor = parse_valor(d.get('valor'))
    data_t = parse_date(d.get('data'), date.today())
    ativas = scalar("SELECT COUNT(*) FROM contas WHERE ativo AND id IN (%s, %s)", (origem, destino))
    if ativas != 2:
        raise ErroApi('Conta inválida ou inativa')
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            INSERT INTO transferencias (conta_origem, conta_destino, valor, data, descricao, criado_por)
            VALUES (%s,%s,%s,%s,%s,%s) RETURNING id
        """, (origem, destino, valor, data_t, _txt_opt()(d.get('descricao')), session['user_id']))
        tid = cur.fetchone()['id']
    return jsonify({'ok': True, 'id': tid})


@app.route('/api/transferencias/<int:tid>', methods=['DELETE'])
@login_required
def transferencias_delete(tid):
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("DELETE FROM transferencias WHERE id=%s RETURNING *", (tid,))
        row = cur.fetchone()
        if not row:
            raise ErroApi('Transferência não encontrada', 404)
        registrar(cur, 'excluir', 'transferencia', tid, _clean(row))
    return jsonify({'ok': True})


# ══════════════════════════════════════════════════════════════════════
# Lançamentos (+ geração de parcelas)
# ══════════════════════════════════════════════════════════════════════
@app.route('/api/lancamentos', methods=['GET'])
@login_required
def lancamentos_list():
    tipo = request.args.get('tipo')
    de = parse_date(request.args.get('de'))
    ate = parse_date(request.args.get('ate'))
    busca = (request.args.get('q') or '').strip()

    where = ["1=1"]
    params = []
    if tipo in ('receita', 'despesa', 'imposto'):
        where.append("l.tipo = %s")
        params.append(tipo)
    if de:
        where.append("l.data_competencia >= %s")
        params.append(de)
    if ate:
        where.append("l.data_competencia <= %s")
        params.append(ate)
    if busca:
        where.append("(l.descricao ILIKE %s OR ct.nome ILIKE %s OR l.num_nf ILIKE %s)")
        like = f"%{busca}%"
        params += [like, like, like]

    rows = q_all(f"""
        SELECT l.*,
            cat.nome AS categoria_nome, cat.cor AS categoria_cor,
            ct.nome AS contato_nome, co.descricao AS contrato_descricao,
            (SELECT COUNT(*) FROM parcelas p WHERE p.lancamento_id = l.id AND p.status <> 'cancelado') AS qtd_parcelas,
            (SELECT COUNT(*) FROM parcelas p WHERE p.lancamento_id = l.id AND p.status='pago') AS parcelas_pagas,
            (SELECT COALESCE(SUM(COALESCE(p.valor_pago, p.valor)),0) FROM parcelas p
              WHERE p.lancamento_id = l.id AND p.status='pago') AS valor_pago
        FROM lancamentos l
        LEFT JOIN categorias cat ON cat.id = l.categoria_id
        LEFT JOIN contatos  ct  ON ct.id  = l.contato_id
        LEFT JOIN contratos co  ON co.id  = l.contrato_id
        WHERE {' AND '.join(where)}
        ORDER BY l.data_competencia DESC, l.id DESC
        LIMIT 1000
    """, params)
    return jsonify({'ok': True, 'rows': rows})


@app.route('/api/lancamentos/<int:lid>', methods=['GET'])
@login_required
def lancamento_detail(lid):
    lanc = q_one("""
        SELECT l.*, cat.nome AS categoria_nome, ct.nome AS contato_nome, co.descricao AS contrato_descricao
        FROM lancamentos l
        LEFT JOIN categorias cat ON cat.id = l.categoria_id
        LEFT JOIN contatos  ct  ON ct.id  = l.contato_id
        LEFT JOIN contratos co  ON co.id  = l.contrato_id
        WHERE l.id = %s
    """, (lid,))
    if not lanc:
        raise ErroApi('Lançamento não encontrado', 404)
    parcelas = q_all("SELECT * FROM parcelas WHERE lancamento_id=%s ORDER BY numero", (lid,))
    for p in parcelas:
        p['status_efetivo'] = status_efetivo(p)
    return jsonify({'ok': True, 'lancamento': lanc, 'parcelas': parcelas})


def _inserir_parcelas(cur, lid, valor_total, num_parcelas, primeiro_venc, conta_id, forma):
    for i, v in enumerate(split_valor(valor_total, num_parcelas)):
        cur.execute("""
            INSERT INTO parcelas
                (lancamento_id, numero, valor, data_vencimento, conta_id, forma_pagamento, status)
            VALUES (%s,%s,%s,%s,%s,%s,'previsto')
        """, (lid, i + 1, v, add_months(primeiro_venc, i), conta_id, forma))


@app.route('/api/lancamentos', methods=['POST'])
@login_required
def lancamento_create():
    d = corpo()
    tipo = _enum('Tipo', ('receita', 'despesa', 'imposto'))(d.get('tipo'))
    descricao = _txt_req('Descrição')(d.get('descricao'))
    valor_total = parse_valor(d.get('valor_total'))
    competencia = parse_date(d.get('data_competencia'), date.today())
    num_parcelas = _int_faixa('Nº de parcelas', 1, 120)(d.get('num_parcelas') or 1)
    primeiro_venc = parse_date(d.get('primeiro_vencimento'), competencia)

    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            INSERT INTO lancamentos
                (tipo, descricao, contato_id, categoria_id, valor_total,
                 data_competencia, num_nf, nf_obs, num_parcelas, criado_por)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
        """, (
            tipo, descricao, _id_opt(d.get('contato_id')), _id_opt(d.get('categoria_id')),
            valor_total, competencia, _txt_opt(60)(d.get('num_nf')), _txt_opt(5000)(d.get('nf_obs')),
            num_parcelas, session['user_id']
        ))
        lid = cur.fetchone()['id']
        _inserir_parcelas(cur, lid, valor_total, num_parcelas, primeiro_venc,
                          _id_opt(d.get('conta_id')), _txt_opt(40)(d.get('forma_pagamento')))
    return jsonify({'ok': True, 'id': lid})


REGRAS_LANC_META = {
    'descricao': _txt_req('Descrição'),
    'contato_id': _id_opt,
    'categoria_id': _id_opt,
    'num_nf': _txt_opt(60),
    'nf_obs': _txt_opt(5000),
    'data_competencia': _data_req('Competência'),
}


@app.route('/api/lancamentos/<int:lid>', methods=['PUT'])
@login_required
def lancamento_update(lid):
    """Edita o lançamento. Valor, nº de parcelas, 1º vencimento e tipo só mudam se nenhuma
    parcela estiver paga — nesse caso as parcelas são refeitas."""
    d = corpo()
    meta = _campos(d, REGRAS_LANC_META)

    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM lancamentos WHERE id=%s FOR UPDATE", (lid,))
        lanc = cur.fetchone()
        if not lanc:
            raise ErroApi('Lançamento não encontrado', 404)
        cur.execute("SELECT * FROM parcelas WHERE lancamento_id=%s ORDER BY numero", (lid,))
        parcelas = cur.fetchall()
        pagas = sum(1 for p in parcelas if p['status'] == 'pago')

        if 'tipo' in d and d['tipo'] != lanc['tipo']:
            if pagas:
                raise ErroApi('Há parcelas pagas — estorne-as antes de mudar o tipo.', 409)
            meta['tipo'] = _enum('Tipo', ('receita', 'despesa', 'imposto'))(d['tipo'])

        # Estrutura das parcelas: refaz só se algo mudou de fato
        novo_total = parse_valor(d['valor_total']) if 'valor_total' in d else lanc['valor_total']
        novo_n = _int_faixa('Nº de parcelas', 1, 120)(d['num_parcelas']) if 'num_parcelas' in d else lanc['num_parcelas']
        venc_atual = parcelas[0]['data_vencimento'] if parcelas else lanc['data_competencia']
        novo_venc = _data_req('1º vencimento')(d['primeiro_vencimento']) if d.get('primeiro_vencimento') else venc_atual
        mudou_estrutura = (Decimal(str(novo_total)) != Decimal(str(lanc['valor_total']))
                           or int(novo_n) != int(lanc['num_parcelas'] or 1)
                           or novo_venc != venc_atual)
        if mudou_estrutura and pagas:
            raise ErroApi('Há parcelas pagas — estorne-as antes de mudar valor, parcelas ou vencimento. '
                          'Para ajustar uma parcela só, edite-a em "A pagar / receber".', 409)

        registrar(cur, 'editar', 'lancamento', lid, {'antes': _clean(lanc), 'mudancas': d})
        if mudou_estrutura:
            conta_id = _id_opt(d['conta_id']) if 'conta_id' in d else (parcelas[0]['conta_id'] if parcelas else None)
            forma = _txt_opt(40)(d['forma_pagamento']) if 'forma_pagamento' in d else (parcelas[0]['forma_pagamento'] if parcelas else None)
            cur.execute("DELETE FROM parcelas WHERE lancamento_id=%s", (lid,))
            _inserir_parcelas(cur, lid, novo_total, novo_n, novo_venc, conta_id, forma)
            meta['valor_total'] = novo_total
            meta['num_parcelas'] = novo_n
        else:
            # Conta / forma padrão aplicadas às parcelas ainda em aberto
            extra = _campos(d, {'conta_id': _id_opt, 'forma_pagamento': _txt_opt(40)})
            if extra:
                sets = ', '.join(f"{k} = %s" for k in extra)
                cur.execute(f"UPDATE parcelas SET {sets} WHERE lancamento_id=%s AND status <> 'pago'",
                            (*extra.values(), lid))
        _update_parcial(cur, 'lancamentos', lid, meta)
    return jsonify({'ok': True})


@app.route('/api/lancamentos/<int:lid>', methods=['DELETE'])
@login_required
def lancamento_delete(lid):
    """Exclui o lançamento e as parcelas. Se houver parcela paga, exige ?forcar=1 (o caixa muda).
    Se veio de um contrato, marca o mês como pulado para a recorrência não gerar de novo."""
    forcar = request.args.get('forcar') == '1'
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM lancamentos WHERE id=%s FOR UPDATE", (lid,))
        lanc = cur.fetchone()
        if not lanc:
            raise ErroApi('Lançamento não encontrado', 404)
        cur.execute("SELECT * FROM parcelas WHERE lancamento_id=%s ORDER BY numero", (lid,))
        parcelas = cur.fetchall()
        pagas = [p for p in parcelas if p['status'] == 'pago']
        if pagas and not forcar:
            total_pago = sum(Decimal(str(p['valor_pago'] if p['valor_pago'] is not None else p['valor'])) for p in pagas)
            raise ErroApi(
                f'Este lançamento tem {len(pagas)} parcela(s) paga(s) ({float(total_pago):.2f}). '
                'Excluir tira esse dinheiro do saldo das contas.', 409,
                precisa_confirmar=True, pagas=len(pagas), valor_pago=float(total_pago))
        if lanc['contrato_id']:
            cur.execute("""
                INSERT INTO contrato_pulos (contrato_id, mes) VALUES (%s, date_trunc('month', %s::date)::date)
                ON CONFLICT DO NOTHING
            """, (lanc['contrato_id'], lanc['data_competencia']))
        registrar(cur, 'excluir', 'lancamento', lid,
                  {'lancamento': _clean(lanc), 'parcelas': [_clean(p) for p in parcelas]})
        cur.execute("DELETE FROM lancamentos WHERE id=%s", (lid,))  # cascade nas parcelas
    return jsonify({'ok': True})


# ══════════════════════════════════════════════════════════════════════
# Parcelas (contas a pagar / a receber)
# ══════════════════════════════════════════════════════════════════════
@app.route('/api/parcelas', methods=['GET'])
@login_required
def parcelas_list():
    direcao = request.args.get('direcao')   # receber | pagar
    status = request.args.get('status')     # previsto | pago | atrasado | cancelado
    de = parse_date(request.args.get('de'))
    ate = parse_date(request.args.get('ate'))

    where = ["1=1"]
    params = []
    if direcao == 'receber':
        where.append("l.tipo = 'receita'")
    elif direcao == 'pagar':
        where.append("l.tipo IN ('despesa','imposto')")
    if status in ('previsto', 'pago', 'cancelado'):
        where.append("p.status = %s")
        params.append(status)
    elif status == 'atrasado':
        where.append("p.status = 'previsto' AND p.data_vencimento < CURRENT_DATE")
    if de:
        where.append("p.data_vencimento >= %s")
        params.append(de)
    if ate:
        where.append("p.data_vencimento <= %s")
        params.append(ate)

    rows = q_all(f"""
        SELECT p.*, l.tipo AS lanc_tipo, l.descricao, l.num_nf, l.num_parcelas,
               ct.nome AS contato_nome, cat.nome AS categoria_nome,
               co.nome AS conta_nome
        FROM parcelas p
        JOIN lancamentos l ON l.id = p.lancamento_id
        LEFT JOIN contatos  ct  ON ct.id  = l.contato_id
        LEFT JOIN categorias cat ON cat.id = l.categoria_id
        LEFT JOIN contas    co  ON co.id  = p.conta_id
        WHERE {' AND '.join(where)}
        ORDER BY p.data_vencimento ASC, p.id ASC
        LIMIT 2000
    """, params)
    for p in rows:
        p['status_efetivo'] = status_efetivo(p)
    return jsonify({'ok': True, 'rows': rows})


def _parcela_lock(cur, pid):
    cur.execute("SELECT * FROM parcelas WHERE id=%s FOR UPDATE", (pid,))
    p = cur.fetchone()
    if not p:
        raise ErroApi('Parcela não encontrada', 404)
    return p


@app.route('/api/parcelas/<int:pid>/pagar', methods=['POST'])
@login_required
def parcela_pagar(pid):
    """Baixa. valor_pago diferente do previsto: juros/desconto, ou — com restante_em_aberto —
    pagamento parcial (a diferença vira uma nova parcela em aberto)."""
    d = corpo()
    data_pag = parse_date(d.get('data_pagamento'), date.today())
    forma = _txt_opt(40)(d.get('forma_pagamento'))
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        p = _parcela_lock(cur, pid)
        if p['status'] != 'previsto':
            raise ErroApi(f"Parcela já está {p['status']}", 409)
        conta_id = _id_opt(d.get('conta_id')) or p['conta_id'] or conta_padrao(cur)
        valor = Decimal(str(p['valor']))
        pago = parse_valor(d['valor_pago'], 'Valor pago') if d.get('valor_pago') not in (None, '') else valor

        if pago < valor and d.get('restante_em_aberto'):
            cur.execute("SELECT COALESCE(MAX(numero),0) + 1 AS n FROM parcelas WHERE lancamento_id=%s",
                        (p['lancamento_id'],))
            prox = cur.fetchone()['n']
            cur.execute("""
                INSERT INTO parcelas (lancamento_id, numero, valor, data_vencimento, conta_id,
                                      forma_pagamento, status, observacao)
                VALUES (%s,%s,%s,%s,%s,%s,'previsto',%s)
            """, (p['lancamento_id'], prox, valor - pago, p['data_vencimento'], p['conta_id'],
                  p['forma_pagamento'], f"Restante da parcela {p['numero']}"))
            cur.execute("""
                UPDATE parcelas SET status='pago', valor=%s, valor_pago=NULL, data_pagamento=%s,
                    conta_id=%s, forma_pagamento=COALESCE(%s, forma_pagamento)
                WHERE id=%s
            """, (pago, data_pag, conta_id, forma, pid))
        else:
            cur.execute("""
                UPDATE parcelas SET status='pago', valor_pago=%s, data_pagamento=%s,
                    conta_id=%s, forma_pagamento=COALESCE(%s, forma_pagamento)
                WHERE id=%s
            """, (None if pago == valor else pago, data_pag, conta_id, forma, pid))
        registrar(cur, 'pagar', 'parcela', pid, {'valor': valor, 'valor_pago': pago, 'data': data_pag})
    return jsonify({'ok': True})


@app.route('/api/parcelas/<int:pid>/estornar', methods=['POST'])
@login_required
def parcela_estornar(pid):
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        p = _parcela_lock(cur, pid)
        if p['status'] != 'pago':
            raise ErroApi('Só dá para estornar parcela paga', 409)
        cur.execute("UPDATE parcelas SET status='previsto', data_pagamento=NULL, valor_pago=NULL WHERE id=%s", (pid,))
        registrar(cur, 'estornar', 'parcela', pid, _clean(p))
    return jsonify({'ok': True})


REGRAS_PARCELA = {
    'valor': _valor_req('Valor'),
    'data_vencimento': _data_req('Vencimento'),
    'conta_id': _id_opt,
    'forma_pagamento': _txt_opt(40),
    'observacao': _txt_opt(2000),
}


@app.route('/api/parcelas/<int:pid>', methods=['PUT'])
@login_required
def parcela_update(pid):
    campos = _campos(corpo(), REGRAS_PARCELA)
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        p = _parcela_lock(cur, pid)
        if p['status'] == 'pago':
            raise ErroApi('Parcela paga — estorne antes de editar.', 409)
        registrar(cur, 'editar', 'parcela', pid, {'antes': _clean(p), 'mudancas': campos})
        _update_parcial(cur, 'parcelas', pid, campos)
        if 'valor' in campos:
            _recalcular_total(cur, p['lancamento_id'])
    return jsonify({'ok': True})


@app.route('/api/parcelas/<int:pid>/cancelar', methods=['POST'])
@login_required
def parcela_cancelar(pid):
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        p = _parcela_lock(cur, pid)
        if p['status'] != 'previsto':
            raise ErroApi('Só dá para cancelar parcela em aberto (estorne antes se já foi paga).', 409)
        cur.execute("UPDATE parcelas SET status='cancelado' WHERE id=%s", (pid,))
        _recalcular_total(cur, p['lancamento_id'])
        registrar(cur, 'cancelar', 'parcela', pid, _clean(p))
    return jsonify({'ok': True})


@app.route('/api/parcelas/<int:pid>/reativar', methods=['POST'])
@login_required
def parcela_reativar(pid):
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        p = _parcela_lock(cur, pid)
        if p['status'] != 'cancelado':
            raise ErroApi('A parcela não está cancelada', 409)
        cur.execute("UPDATE parcelas SET status='previsto' WHERE id=%s", (pid,))
        _recalcular_total(cur, p['lancamento_id'])
        registrar(cur, 'reativar', 'parcela', pid, None)
    return jsonify({'ok': True})


# ══════════════════════════════════════════════════════════════════════
# Impostos / Parâmetros
# ══════════════════════════════════════════════════════════════════════
REGIMES = ('MEI', 'ME-Simples')

REGRAS_PARAMETROS = {
    'regime': _enum('Regime', REGIMES),
    'das_mei_valor': _valor_zero_ok('Valor do DAS'),
    'das_vencimento_dia': _int_faixa('Dia de vencimento', 1, 28),
    'aliquota_simples': lambda v: None if v in (None, '') else _valor_zero_ok('Alíquota')(v),
    'simples_base': _enum('Base do Simples', ('competencia', 'caixa')),
    'recorrencia_modo': _enum('Modo da recorrência', ('manual', 'automatico')),
}


@app.route('/api/parametros', methods=['GET'])
@login_required
def parametros_get():
    row = q_one("SELECT * FROM parametros WHERE id=1")
    return jsonify({'ok': True, 'parametros': row})


@app.route('/api/parametros', methods=['PUT'])
@login_required
def parametros_update():
    campos = _campos(corpo(), REGRAS_PARAMETROS)
    ligou_auto = False
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM parametros WHERE id=1 FOR UPDATE")
        atual = cur.fetchone()
        if campos.get('recorrencia_modo') == 'automatico' and atual['recorrencia_modo'] != 'automatico':
            campos['recorrencia_auto_desde'] = date.today().replace(day=1)
            ligou_auto = True
        _update_parcial(cur, 'parametros', 1, campos)
        registrar(cur, 'editar', 'parametros', 1, {'mudancas': campos})
    gerados = rodar_automaticos() if ligou_auto else 0
    return jsonify({'ok': True, 'gerados': gerados})


def calcular_das(ano, mes):
    """Calcula o DAS de uma competência conforme o regime configurado."""
    par = q_one("SELECT * FROM parametros WHERE id=1") or {}
    regime = par.get('regime') or 'MEI'
    dia = int(par.get('das_vencimento_dia') or 20)
    competencia = date(ano, mes, 1)
    ult = fim_do_mes(competencia)
    out = {'regime': regime, 'competencia': competencia.isoformat()}

    if regime == 'MEI':
        valor = Decimal(str(par.get('das_mei_valor') or 0)).quantize(Decimal('0.01'))
        mes_venc = competencia  # mantém o comportamento original do DAS-MEI
        out.update(base=None, aliquota=None, descricao=f"DAS-MEI {mes:02d}/{ano}")
    else:
        aliquota = par.get('aliquota_simples')
        base_tipo = par.get('simples_base') or 'competencia'
        if base_tipo == 'caixa':
            base = scalar("""
                SELECT COALESCE(SUM(COALESCE(p.valor_pago, p.valor)),0)
                FROM parcelas p JOIN lancamentos l ON l.id = p.lancamento_id
                WHERE l.tipo='receita' AND p.status='pago'
                  AND p.data_pagamento >= %s AND p.data_pagamento <= %s
            """, (competencia, ult)) or 0
        else:
            base = scalar("""
                SELECT COALESCE(SUM(valor_total),0) FROM lancamentos
                WHERE tipo='receita' AND data_competencia >= %s AND data_competencia <= %s
            """, (competencia, ult)) or 0
        valor = (Decimal(str(base)) * Decimal(str(aliquota or 0)) / 100).quantize(Decimal('0.01'), ROUND_HALF_UP)
        mes_venc = add_months(competencia, 1)  # DAS do Simples vence no mês seguinte
        out.update(base=round(float(base), 2), base_tipo=base_tipo, aliquota=aliquota,
                   descricao=f"DAS-Simples {mes:02d}/{ano}")

    venc = date(mes_venc.year, mes_venc.month, min(dia, calendar.monthrange(mes_venc.year, mes_venc.month)[1]))
    ja = scalar("""
        SELECT COUNT(*) FROM lancamentos
        WHERE tipo='imposto' AND date_trunc('month', data_competencia) = %s AND descricao ILIKE 'DAS%%'
    """, (competencia,))
    out.update(valor=float(valor), vencimento=venc.isoformat(), ja_lancado=bool(ja))
    return out


def _competencia_das_padrao():
    """MEI: mês atual (comportamento original). Simples: mês anterior (o que vence agora)."""
    regime = scalar("SELECT regime FROM parametros WHERE id=1") or 'MEI'
    hoje = date.today()
    return hoje.replace(day=1) if regime == 'MEI' else add_months(hoje.replace(day=1), -1)


@app.route('/api/impostos/das/previa')
@login_required
def das_previa():
    comp = parse_competencia(request.args.get('competencia'), _competencia_das_padrao())
    return jsonify({'ok': True, 'previa': calcular_das(comp.year, comp.month),
                    'competencia_padrao': _competencia_das_padrao().strftime('%Y-%m')})


@app.route('/api/impostos/das', methods=['POST'])
@login_required
def lancar_das():
    """Lança o DAS de um mês como imposto + 1 parcela. Não duplica."""
    comp = parse_competencia(corpo().get('competencia'), _competencia_das_padrao())
    calc = calcular_das(comp.year, comp.month)
    if calc['regime'] != 'MEI' and not calc.get('aliquota'):
        raise ErroApi('Configure a alíquota efetiva do Simples antes de lançar')
    if calc['valor'] <= 0:
        if calc['regime'] == 'MEI':
            raise ErroApi('Configure o valor do DAS-MEI antes de lançar')
        raise ErroApi(f"Sem faturamento em {comp:%m/%Y} — não há DAS a lançar")
    if calc['ja_lancado']:
        raise ErroApi(f'DAS de {comp:%m/%Y} já foi lançado', 409)

    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT id FROM categorias WHERE tipo='despesa' AND nome ILIKE 'Impostos' AND ativo LIMIT 1")
        cat = cur.fetchone()
        cur.execute("""
            INSERT INTO lancamentos (tipo, descricao, categoria_id, valor_total,
                data_competencia, num_parcelas, criado_por)
            VALUES ('imposto', %s, %s, %s, %s, 1, %s) RETURNING id
        """, (calc['descricao'], cat['id'] if cat else None, calc['valor'], comp, session['user_id']))
        lid = cur.fetchone()['id']
        cur.execute("""
            INSERT INTO parcelas (lancamento_id, numero, valor, data_vencimento, status)
            VALUES (%s, 1, %s, %s, 'previsto')
        """, (lid, calc['valor'], calc['vencimento']))
    return jsonify({'ok': True, 'id': lid, 'descricao': calc['descricao'], 'valor': calc['valor']})


# ══════════════════════════════════════════════════════════════════════
# Contratos / Recorrências (receita ou despesa fixa mensal)
# ══════════════════════════════════════════════════════════════════════
LOCK_RECORRENCIA = 7_310_002


def gerar_recorrencias(ano, mes, user_id=None, contrato_id=None):
    """Gera lançamento + parcela dos contratos ativos que cobrem o mês e ainda não foram gerados
    nem pulados nele. Idempotente; o advisory lock impede geração dupla em paralelo."""
    primeiro_dia = date(ano, mes, 1)
    ultimo_dia = fim_do_mes(primeiro_dia)
    filtro = "AND c.id = %s" if contrato_id else ""
    params = [ultimo_dia, primeiro_dia, primeiro_dia, primeiro_dia] + ([contrato_id] if contrato_id else [])

    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_RECORRENCIA,))
        cur.execute(f"""
            SELECT c.* FROM contratos c
            WHERE c.status='ativo'
              AND c.data_inicio <= %s AND (c.data_fim IS NULL OR c.data_fim >= %s)
              AND NOT EXISTS (SELECT 1 FROM lancamentos l
                              WHERE l.contrato_id = c.id
                                AND date_trunc('month', l.data_competencia) = %s)
              AND NOT EXISTS (SELECT 1 FROM contrato_pulos cp
                              WHERE cp.contrato_id = c.id AND cp.mes = %s)
              {filtro}
        """, params)
        pendentes = cur.fetchall()
        for c in pendentes:
            dia = min(int(c.get('dia_vencimento') or 10), ultimo_dia.day)
            # 1º mês: a cobrança não vence antes do início do contrato (senão já nasce atrasada)
            vencimento = max(date(ano, mes, dia), c['data_inicio'])
            cur.execute("""
                INSERT INTO lancamentos (tipo, descricao, contato_id, categoria_id,
                    valor_total, data_competencia, num_parcelas, contrato_id, criado_por)
                VALUES (%s,%s,%s,%s,%s,%s,1,%s,%s) RETURNING id
            """, (c['tipo'], f"{c['descricao']} {mes:02d}/{ano}", c.get('contato_id'), c.get('categoria_id'),
                  c['valor_mensal'], primeiro_dia, c['id'], user_id))
            lid = cur.fetchone()['id']
            cur.execute("""
                INSERT INTO parcelas (lancamento_id, numero, valor, data_vencimento, status)
                VALUES (%s, 1, %s, %s, 'previsto')
            """, (lid, c['valor_mensal'], vencimento))
        return len(pendentes)


MAX_MESES_RETROATIVOS = 24


def rodar_automaticos():
    """Modo automático: gera todos os meses desde que o modo foi ligado até o mês atual."""
    par = q_one("SELECT recorrencia_modo, recorrencia_auto_desde FROM parametros WHERE id=1")
    if not par or par.get('recorrencia_modo') != 'automatico':
        return 0
    hoje = date.today().replace(day=1)
    desde = (parse_date(par.get('recorrencia_auto_desde')) or hoje).replace(day=1)
    desde = max(desde, add_months(hoje, -MAX_MESES_RETROATIVOS))
    total = 0
    m = desde
    while m <= hoje:
        total += gerar_recorrencias(m.year, m.month)
        m = add_months(m, 1)
    if total:
        log.info('[recorrência automática] %s lançamento(s) gerado(s)', total)
    return total


_auto_dia = {'ultimo': None}
_auto_lock = threading.Lock()


@app.before_request
def _recorrencia_automatica():
    """Uma vez por dia, na primeira requisição de alguém logado, roda o modo automático."""
    if 'user_id' not in session or request.path.startswith('/static/'):
        return None
    hoje = date.today()
    if _auto_dia['ultimo'] == hoje:
        return None
    with _auto_lock:
        if _auto_dia['ultimo'] == hoje:
            return None
        _auto_dia['ultimo'] = hoje
        try:
            rodar_automaticos()
        except Exception:
            log.exception('Falha na recorrência automática')
    return None


@app.route('/api/contratos', methods=['GET'])
@login_required
def contratos_list():
    primeiro_dia = date.today().replace(day=1)
    todos = request.args.get('todos') == '1'
    rows = q_all(f"""
        SELECT c.*, ct.nome AS contato_nome, cat.nome AS categoria_nome,
            EXISTS (SELECT 1 FROM lancamentos l
                    WHERE l.contrato_id = c.id
                      AND date_trunc('month', l.data_competencia) = %s) AS gerado_mes_atual,
            EXISTS (SELECT 1 FROM contrato_pulos cp
                    WHERE cp.contrato_id = c.id AND cp.mes = %s) AS pulado_mes_atual,
            (c.data_inicio <= %s AND (c.data_fim IS NULL OR c.data_fim >= %s)) AS vigente_mes_atual,
            (SELECT COUNT(*) FROM lancamentos l WHERE l.contrato_id = c.id) AS qtd_lancamentos
        FROM contratos c
        LEFT JOIN contatos  ct  ON ct.id  = c.contato_id
        LEFT JOIN categorias cat ON cat.id = c.categoria_id
        {'' if todos else "WHERE c.status <> 'cancelado'"}
        ORDER BY CASE c.status WHEN 'ativo' THEN 0 WHEN 'pausado' THEN 1 ELSE 2 END, c.descricao
    """, (primeiro_dia, primeiro_dia, fim_do_mes(primeiro_dia), primeiro_dia))
    return jsonify({'ok': True, 'rows': rows})


REGRAS_CONTRATO = {
    'descricao': _txt_req('Descrição'),
    'contato_id': _id_opt,
    'categoria_id': _id_opt,
    'valor_mensal': _valor_req('Valor mensal'),
    'dia_vencimento': _int_faixa('Dia de vencimento', 1, 28),
    'data_inicio': _data_req('Início'),
    'data_fim': _data_opt,
    'status': _enum('Status', ('ativo', 'pausado', 'cancelado')),
}


@app.route('/api/contratos', methods=['POST'])
@login_required
def contratos_create():
    d = corpo()
    tipo = _enum('Tipo (receita/despesa)', ('receita', 'despesa'))(d.get('tipo'))
    if d.get('dia_vencimento') in (None, ''):
        d['dia_vencimento'] = 10
    d['data_inicio'] = d.get('data_inicio') or date.today().isoformat()
    if 'descricao' not in d:
        raise ErroApi('Descrição obrigatória')
    if 'valor_mensal' not in d:
        raise ErroApi('Valor mensal obrigatório')
    c = _campos(d, REGRAS_CONTRATO)
    if c.get('data_fim') and c['data_fim'] < c['data_inicio']:
        raise ErroApi('O fim não pode ser antes do início')
    row = execute("""
        INSERT INTO contratos
            (contato_id, descricao, tipo, categoria_id, valor_mensal,
             dia_vencimento, data_inicio, data_fim, criado_por)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *
    """, (c.get('contato_id'), c['descricao'], tipo, c.get('categoria_id'),
          c['valor_mensal'], c['dia_vencimento'], c['data_inicio'], c.get('data_fim'),
          session['user_id']), returning=True)
    gerados = rodar_automaticos()
    return jsonify({'ok': True, 'row': row, 'gerados': gerados})


@app.route('/api/contratos/<int:cid>', methods=['PUT'])
@login_required
def contratos_update(cid):
    """Edição parcial: só muda o que veio no corpo (pausar não apaga cliente/categoria/fim).
    aplicar_mes_atual=true leva valor/dia novos ao lançamento já gerado no mês, se ainda em aberto."""
    d = corpo()
    campos = _campos(d, REGRAS_CONTRATO)
    hoje = date.today()
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM contratos WHERE id=%s FOR UPDATE", (cid,))
        atual = cur.fetchone()
        if not atual:
            raise ErroApi('Contrato não encontrado', 404)
        inicio = campos.get('data_inicio', atual['data_inicio'])
        fim = campos['data_fim'] if 'data_fim' in campos else atual['data_fim']
        if fim and fim < inicio:
            raise ErroApi('O fim não pode ser antes do início')
        registrar(cur, 'editar', 'contrato', cid, {'antes': _clean(atual), 'mudancas': campos})
        _update_parcial(cur, 'contratos', cid, campos)

        if d.get('aplicar_mes_atual') and ('valor_mensal' in campos or 'dia_vencimento' in campos):
            cur.execute("""
                SELECT p.id AS parcela_id, l.id AS lancamento_id
                FROM lancamentos l JOIN parcelas p ON p.lancamento_id = l.id
                WHERE l.contrato_id = %s AND date_trunc('month', l.data_competencia) = %s
                  AND p.status = 'previsto'
            """, (cid, hoje.replace(day=1)))
            for r in cur.fetchall():
                valor = campos.get('valor_mensal', atual['valor_mensal'])
                dia = min(int(campos.get('dia_vencimento', atual['dia_vencimento']) or 10), fim_do_mes(hoje).day)
                venc = max(date(hoje.year, hoje.month, dia), inicio)
                cur.execute("UPDATE parcelas SET valor=%s, data_vencimento=%s WHERE id=%s",
                            (valor, venc, r['parcela_id']))
                _recalcular_total(cur, r['lancamento_id'])
    gerados = rodar_automaticos()
    return jsonify({'ok': True, 'gerados': gerados})


@app.route('/api/contratos/<int:cid>', methods=['DELETE'])
@login_required
def contratos_delete(cid):
    """Padrão: cancela (soft) — lançamentos já gerados ficam.
    ?definitivo=1: apaga de vez, só se o contrato nunca gerou lançamento (criado por engano)."""
    with db() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM contratos WHERE id=%s FOR UPDATE", (cid,))
        atual = cur.fetchone()
        if not atual:
            raise ErroApi('Contrato não encontrado', 404)
        if request.args.get('definitivo') == '1':
            cur.execute("SELECT COUNT(*) AS n FROM lancamentos WHERE contrato_id=%s", (cid,))
            if cur.fetchone()['n']:
                raise ErroApi('O contrato já gerou lançamentos — só dá para cancelar.', 409)
            registrar(cur, 'excluir', 'contrato', cid, _clean(atual))
            cur.execute("DELETE FROM contratos WHERE id=%s", (cid,))
        else:
            registrar(cur, 'cancelar', 'contrato', cid, None)
            cur.execute("UPDATE contratos SET status='cancelado' WHERE id=%s", (cid,))
    return jsonify({'ok': True})


@app.route('/api/contratos/gerar', methods=['POST'])
@login_required
def contratos_gerar():
    """Gera os lançamentos+parcelas dos contratos ativos para um mês (default: atual)."""
    comp = parse_competencia(corpo().get('competencia'), date.today())
    gerados = gerar_recorrencias(comp.year, comp.month, session['user_id'])
    pulados = scalar("SELECT COUNT(*) FROM contrato_pulos cp JOIN contratos c ON c.id = cp.contrato_id "
                     "WHERE c.status='ativo' AND cp.mes = %s", (comp,))
    return jsonify({'ok': True, 'gerados': gerados, 'pulados': pulados, 'competencia': f"{comp:%m/%Y}"})


@app.route('/api/contratos/<int:cid>/gerar', methods=['POST'])
@login_required
def contrato_gerar_um(cid):
    """Gera o mês de UM contrato — inclusive um mês que estava pulado (desfaz o pulo)."""
    comp = parse_competencia(corpo().get('competencia'), date.today())
    execute("DELETE FROM contrato_pulos WHERE contrato_id=%s AND mes=%s", (cid, comp))
    gerados = gerar_recorrencias(comp.year, comp.month, session['user_id'], contrato_id=cid)
    if not gerados:
        raise ErroApi(f'Nada a gerar em {comp:%m/%Y}: o contrato não está ativo nesse mês ou já foi gerado.', 409)
    return jsonify({'ok': True, 'gerados': gerados, 'competencia': f"{comp:%m/%Y}"})


# ══════════════════════════════════════════════════════════════════════
# Dashboard
# ══════════════════════════════════════════════════════════════════════
def _saldo_atual():
    """Soma das contas ativas (+ baixas antigas sem conta, se ainda existir alguma)."""
    contas = scalar(f"SELECT COALESCE(SUM(saldo_atual),0) FROM ({SQL_CONTAS_SALDO} WHERE c.ativo) s") or 0
    sem_conta = scalar("""
        SELECT COALESCE(SUM(CASE WHEN l.tipo='receita' THEN COALESCE(p.valor_pago, p.valor)
                                 ELSE -COALESCE(p.valor_pago, p.valor) END),0)
        FROM parcelas p JOIN lancamentos l ON l.id = p.lancamento_id
        WHERE p.status='pago' AND p.conta_id IS NULL
    """) or 0
    return round(contas + sem_conta, 2)


def _previstas(tipos, ini, fim):
    """Soma de parcelas previstas (não pagas/canceladas) por janela de vencimento."""
    placeholders = ','.join(['%s'] * len(tipos))
    return scalar(f"""
        SELECT COALESCE(SUM(p.valor),0)
        FROM parcelas p JOIN lancamentos l ON l.id = p.lancamento_id
        WHERE p.status='previsto' AND l.tipo IN ({placeholders})
          AND p.data_vencimento >= %s AND p.data_vencimento <= %s
    """, (*tipos, ini, fim)) or 0


def _projecao_contratos(ini, fim):
    """Cobranças de contratos ativos AINDA NÃO GERADAS nos meses entre ini e fim.
    Cada item: tipo, valor, vencimento, mes. Meses pulados não entram."""
    contratos = q_all("""
        SELECT id, tipo, valor_mensal, dia_vencimento, data_inicio, data_fim
        FROM contratos WHERE status='ativo'
    """)
    if not contratos:
        return []
    m_ini = ini.replace(day=1)
    feitos = {(r['contrato_id'], r['mes']) for r in q_all("""
        SELECT DISTINCT contrato_id, to_char(data_competencia, 'YYYY-MM') AS mes
        FROM lancamentos WHERE contrato_id IS NOT NULL AND data_competencia >= %s AND data_competencia <= %s
        UNION
        SELECT contrato_id, to_char(mes, 'YYYY-MM') FROM contrato_pulos WHERE mes >= %s AND mes <= %s
    """, (m_ini, fim, m_ini, fim))}
    out = []
    m = m_ini
    while m <= fim:
        ult = fim_do_mes(m)
        chave_mes = f"{m:%Y-%m}"
        for c in contratos:
            inicio, termino = parse_date(c['data_inicio']), parse_date(c['data_fim'])
            if inicio > ult or (termino and termino < m) or (c['id'], chave_mes) in feitos:
                continue
            venc = max(date(m.year, m.month, min(int(c['dia_vencimento'] or 10), ult.day)), inicio)
            out.append({'tipo': c['tipo'], 'valor': float(c['valor_mensal']), 'vencimento': venc, 'mes': chave_mes})
        m = add_months(m, 1)
    return out


def _soma(itens, tipo, cond=lambda i: True):
    return sum(i['valor'] for i in itens if i['tipo'] == tipo and cond(i))


def _mrr(tipo):
    """Receita/despesa recorrente mensal: soma de contratos ativos que cobrem hoje."""
    hoje = date.today()
    return scalar("""
        SELECT COALESCE(SUM(valor_mensal),0) FROM contratos
        WHERE status='ativo' AND tipo=%s
          AND data_inicio <= %s AND (data_fim IS NULL OR data_fim >= %s)
    """, (tipo, hoje, hoje)) or 0


@app.route('/api/dashboard/resumo')
@login_required
def dashboard_resumo():
    hoje = date.today()
    ontem = hoje - timedelta(days=1)
    saldo = _saldo_atual()
    inicio_mes = hoje.replace(day=1)
    fim_mes = fim_do_mes(hoje)
    proj = _projecao_contratos(inicio_mes, add_months(hoje, 3))

    def receber(fim):
        return round(_previstas(['receita'], hoje, fim)
                     + _soma(proj, 'receita', lambda i: hoje <= i['vencimento'] <= fim), 2)

    def pagar(fim):
        return round(_previstas(['despesa', 'imposto'], hoje, fim)
                     + _soma(proj, 'despesa', lambda i: hoje <= i['vencimento'] <= fim), 2)

    receita_lancada = scalar("""
        SELECT COALESCE(SUM(valor_total),0) FROM lancamentos
        WHERE tipo='receita' AND data_competencia >= %s AND data_competencia <= %s
    """, (inicio_mes, fim_mes)) or 0
    despesa_lancada = scalar("""
        SELECT COALESCE(SUM(valor_total),0) FROM lancamentos
        WHERE tipo IN ('despesa','imposto') AND data_competencia >= %s AND data_competencia <= %s
    """, (inicio_mes, fim_mes)) or 0
    mes_atual = f"{hoje:%Y-%m}"
    rec_pendente = _soma(proj, 'receita', lambda i: i['mes'] == mes_atual)
    desp_pendente = _soma(proj, 'despesa', lambda i: i['mes'] == mes_atual)
    receita_mes = receita_lancada + rec_pendente
    despesa_mes = despesa_lancada + desp_pendente

    mrr_receita = round(_mrr('receita'), 2)
    mrr_despesa = round(_mrr('despesa'), 2)

    return jsonify({'ok': True, 'resumo': {
        'saldo_atual': saldo,
        'a_receber_30': receber(add_months(hoje, 1)),
        'a_receber_60': receber(add_months(hoje, 2)),
        'a_receber_90': receber(add_months(hoje, 3)),
        'a_pagar_30': pagar(add_months(hoje, 1)),
        'a_pagar_60': pagar(add_months(hoje, 2)),
        'a_pagar_90': pagar(add_months(hoje, 3)),
        'vencidos_receber': round(_previstas(['receita'], date(2000, 1, 1), ontem), 2),
        'vencidos_pagar': round(_previstas(['despesa', 'imposto'], date(2000, 1, 1), ontem), 2),
        'receita_mes': round(receita_mes, 2),
        'despesa_mes': round(despesa_mes, 2),
        'resultado_mes': round(receita_mes - despesa_mes, 2),
        'recorrencia_pendente_mes': round(rec_pendente - desp_pendente, 2),
        'recorrencias_nao_geradas_mes': sum(1 for i in proj if i['mes'] == mes_atual),
        'mrr_receita': mrr_receita,
        'mrr_despesa': mrr_despesa,
        'mrr_liquido': round(mrr_receita - mrr_despesa, 2),
    }})


@app.route('/api/dashboard/fluxo')
@login_required
def dashboard_fluxo():
    try:
        meses = max(3, min(int(request.args.get('meses', 12)), 24))
    except (TypeError, ValueError):
        meses = 12
    saldo = _saldo_atual()
    inicio = date.today().replace(day=1)
    proj = _projecao_contratos(inicio, fim_do_mes(add_months(inicio, meses - 1)))
    rows = []
    acumulado = saldo
    for i in range(meses):
        mref = add_months(inicio, i)
        ult = fim_do_mes(mref)
        chave = f"{mref:%Y-%m}"
        # Parcelas já geradas no mês + contratos ativos ainda não gerados (sem contar em dobro)
        entradas = round(_previstas(['receita'], mref, ult) + _soma(proj, 'receita', lambda x: x['mes'] == chave), 2)
        saidas = round(_previstas(['despesa', 'imposto'], mref, ult) + _soma(proj, 'despesa', lambda x: x['mes'] == chave), 2)
        liquido = round(entradas - saidas, 2)
        acumulado = round(acumulado + liquido, 2)
        rows.append({'mes': chave, 'entradas': entradas, 'saidas': saidas,
                     'liquido': liquido, 'acumulado': acumulado})
    return jsonify({'ok': True, 'saldo_inicial': saldo, 'rows': rows})


# ══════════════════════════════════════════════════════════════════════
# Agente de IA Comercial
# ══════════════════════════════════════════════════════════════════════
@app.route('/api/comercial/chat', methods=['POST'])
@login_required
def comercial_chat():
    messages = corpo().get('messages') or []
    try:
        out = comercial_agente.chat(messages)
    except Exception:
        log.exception('Falha no agente comercial')
        return jsonify({'ok': False, 'error': 'O agente não respondeu agora — tente de novo em instantes.'}), 502
    # Persiste a estimativa gerada (histórico), sem derrubar a resposta em caso de erro
    if out.get('estimativa'):
        try:
            execute("INSERT INTO orcamentos (criado_por, tipo_servico, payload) VALUES (%s,%s,%s)",
                    (session['user_id'], out['estimativa'].get('tipo_servico'), Json(out['estimativa'])))
        except Exception:
            log.exception('Falha ao gravar orçamento')
    return jsonify({'ok': True, 'reply': out.get('reply'),
                    'estimativa': out.get('estimativa'), 'mock': out.get('mock', False)})


@app.route('/api/comercial/orcamentos', methods=['GET'])
@login_required
def comercial_orcamentos():
    rows = q_all("""
        SELECT o.id, o.tipo_servico, o.payload, o.criado_em, u.nome AS criado_por_nome
        FROM orcamentos o LEFT JOIN usuarios u ON u.id = o.criado_por
        ORDER BY o.id DESC LIMIT 50
    """)
    return jsonify({'ok': True, 'rows': rows})


# ══════════════════════════════════════════════════════════════════════
# Export CSV
# ══════════════════════════════════════════════════════════════════════
@app.route('/api/export/lancamentos.csv')
@login_required
def export_csv():
    rows = q_all("""
        SELECT l.data_competencia, l.tipo, l.descricao, cat.nome AS categoria,
               ct.nome AS contato, l.num_nf, p.numero, l.num_parcelas,
               p.valor, p.valor_pago, p.data_vencimento, p.data_pagamento, p.status, co.nome AS conta
        FROM parcelas p
        JOIN lancamentos l ON l.id = p.lancamento_id
        LEFT JOIN categorias cat ON cat.id = l.categoria_id
        LEFT JOIN contatos  ct  ON ct.id  = l.contato_id
        LEFT JOIN contas    co  ON co.id  = p.conta_id
        ORDER BY l.data_competencia DESC, l.id DESC, p.numero ASC
    """)
    buf = io.StringIO()
    buf.write('﻿')  # BOM pro Excel
    w = csv.writer(buf, delimiter=';')
    w.writerow(['Competencia', 'Tipo', 'Descricao', 'Categoria', 'Contato', 'NF',
                'Parcela', 'TotalParcelas', 'Valor', 'ValorPago', 'Vencimento', 'Pagamento', 'Status', 'Conta'])

    def br(v):
        return f"{v:.2f}".replace('.', ',') if v is not None else ''

    for r in rows:
        pago = r['valor_pago'] if r['valor_pago'] is not None else (r['valor'] if r['status'] == 'pago' else None)
        w.writerow([
            r['data_competencia'], r['tipo'], r['descricao'], r['categoria'] or '',
            r['contato'] or '', r['num_nf'] or '', r['numero'], r['num_parcelas'],
            br(r['valor']), br(pago), r['data_vencimento'],
            r['data_pagamento'] or '', r['status'], r['conta'] or '',
        ])
    return Response(
        buf.getvalue(), mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=lancamentos_gestao_joga.csv'}
    )


# ══════════════════════════════════════════════════════════════════════
# Boot
# ══════════════════════════════════════════════════════════════════════
def iniciar():
    """Migra o banco e roda a recorrência automática. Uma vez por processo."""
    try:
        with db() as conn:
            schema.migrar(conn, log.info)
    except Exception:
        log.exception('Falha na migração do banco — o app sobe, mas pode faltar coluna/tabela nova')
    try:
        rodar_automaticos()
    except Exception:
        log.exception('Falha na recorrência automática do boot')


if __name__ == '__main__':
    porta = int(os.getenv('PORT', '5000'))
    iniciar()
    if DEV:
        app.run(host='0.0.0.0', port=porta, debug=True)
    else:
        from waitress import serve
        log.info('[Gestão JOGA] Servindo em http://0.0.0.0:%s', porta)
        serve(app, host='0.0.0.0', port=porta, threads=8)
