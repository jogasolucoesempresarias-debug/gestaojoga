"""
Gestão JOGA — Sistema financeiro (Receitas, Despesas, Impostos, Caixa)
Rode: python -X utf8 server.py
Acesse: http://localhost:5000
"""

import os
import io
import csv
import calendar
import functools
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP

import psycopg2
from psycopg2.extras import RealDictCursor
from flask import (
    Flask, jsonify, send_from_directory, request, session, redirect, Response
)
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__, static_folder=None)
app.secret_key = os.getenv('SECRET_KEY', 'dev-secret-change-me')
CORS(app, supports_credentials=True)


# ── Banco de dados ────────────────────────────────────────────────────
def get_db():
    return psycopg2.connect(
        host=os.getenv('DB_HOST', 'localhost'),
        port=os.getenv('DB_PORT', '5432'),
        dbname=os.getenv('DB_NAME', 'gestao_joga'),
        user=os.getenv('DB_USER', 'postgres'),
        password=os.getenv('DB_PASSWORD', ''),
    )


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


def q_all(sql, params=None):
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute(sql, params or ())
    rows = [_clean(r) for r in cur.fetchall()]
    cur.close()
    conn.close()
    return rows


def q_one(sql, params=None):
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute(sql, params or ())
    row = cur.fetchone()
    cur.close()
    conn.close()
    return _clean(row)


def execute(sql, params=None, returning=False):
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute(sql, params or ())
    out = _clean(cur.fetchone()) if returning else None
    conn.commit()
    cur.close()
    conn.close()
    return out


def scalar(sql, params=None):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(sql, params or ())
    row = cur.fetchone()
    cur.close()
    conn.close()
    v = row[0] if row else None
    return float(v) if isinstance(v, Decimal) else v


# ── Helpers financeiros ───────────────────────────────────────────────
def add_months(d: date, n: int) -> date:
    """Soma n meses a uma data, ajustando dia pro fim do mês quando necessário."""
    m = d.month - 1 + n
    y = d.year + m // 12
    m = m % 12 + 1
    day = min(d.day, calendar.monthrange(y, m)[1])
    return date(y, m, day)


def split_valor(total, n: int):
    """Divide um total em n parcelas de 2 casas; última absorve o arredondamento."""
    total = Decimal(str(total)).quantize(Decimal('0.01'))
    n = max(1, int(n))
    base = (total / n).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    parts = [base] * (n - 1)
    parts.append(total - base * (n - 1))
    return parts


def parse_date(s, default=None):
    if not s:
        return default
    try:
        return date.fromisoformat(str(s)[:10])
    except (ValueError, TypeError):
        return default


def status_efetivo(p):
    """Deriva 'atrasado' de parcelas previstas vencidas (não armazenado)."""
    venc = p.get('data_vencimento')
    if p.get('status') == 'previsto' and venc:
        if date.fromisoformat(venc[:10]) < date.today():
            return 'atrasado'
    return p.get('status')


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
    '/impostos':     'impostos.html',
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
    data = request.get_json() or {}
    email = data.get('email', '').strip().lower()
    senha = data.get('senha', '')
    if not email or not senha:
        return jsonify({'ok': False, 'error': 'Preencha e-mail e senha'}), 400
    user = q_one(
        "SELECT id, nome, password_hash, role, ativo, must_change_password "
        "FROM usuarios WHERE email = %s", (email,)
    )
    if not user:
        return jsonify({'ok': False, 'error': 'E-mail ou senha inválidos'}), 401
    if not user['ativo']:
        return jsonify({'ok': False, 'error': 'Conta desativada'}), 403
    if not check_password_hash(user['password_hash'], senha):
        return jsonify({'ok': False, 'error': 'E-mail ou senha inválidos'}), 401
    session['user_id'] = user['id']
    session['nome'] = user['nome']
    session['role'] = user['role']
    session['must_change_password'] = bool(user['must_change_password'])
    if user['must_change_password']:
        return jsonify({'ok': True, 'redirect': '/trocar-senha'})
    return jsonify({'ok': True, 'redirect': '/'})


@app.route('/api/trocar-senha', methods=['POST'])
@login_required
def trocar_senha_post():
    data = request.get_json() or {}
    nova = data.get('nova', '')
    confirma = data.get('confirma', '')
    if len(nova) < 6:
        return jsonify({'ok': False, 'error': 'Senha deve ter no mínimo 6 caracteres'}), 400
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
# Cadastros: Categorias, Contatos, Contas
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


@app.route('/api/categorias', methods=['POST'])
@login_required
def categorias_create():
    d = request.get_json() or {}
    nome = (d.get('nome') or '').strip()
    tipo = d.get('tipo')
    if not nome or tipo not in ('receita', 'despesa'):
        return jsonify({'ok': False, 'error': 'Nome e tipo (receita/despesa) obrigatórios'}), 400
    row = execute(
        "INSERT INTO categorias (nome, tipo, cor) VALUES (%s,%s,%s) RETURNING *",
        (nome, tipo, d.get('cor') or '#38bdf8'), returning=True
    )
    return jsonify({'ok': True, 'row': row})


@app.route('/api/categorias/<int:cid>', methods=['PUT'])
@login_required
def categorias_update(cid):
    d = request.get_json() or {}
    execute(
        "UPDATE categorias SET nome=COALESCE(%s,nome), cor=COALESCE(%s,cor) WHERE id=%s",
        (d.get('nome'), d.get('cor'), cid)
    )
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


@app.route('/api/contatos', methods=['POST'])
@login_required
def contatos_create():
    d = request.get_json() or {}
    nome = (d.get('nome') or '').strip()
    tipo = d.get('tipo') if d.get('tipo') in ('cliente', 'fornecedor', 'ambos') else 'cliente'
    if not nome:
        return jsonify({'ok': False, 'error': 'Nome obrigatório'}), 400
    row = execute(
        "INSERT INTO contatos (nome, tipo, documento, email, telefone) "
        "VALUES (%s,%s,%s,%s,%s) RETURNING *",
        (nome, tipo, d.get('documento'), d.get('email'), d.get('telefone')), returning=True
    )
    return jsonify({'ok': True, 'row': row})


@app.route('/api/contatos/<int:cid>', methods=['PUT'])
@login_required
def contatos_update(cid):
    d = request.get_json() or {}
    execute(
        "UPDATE contatos SET nome=COALESCE(%s,nome), tipo=COALESCE(%s,tipo), "
        "documento=%s, email=%s, telefone=%s WHERE id=%s",
        (d.get('nome'), d.get('tipo'), d.get('documento'), d.get('email'), d.get('telefone'), cid)
    )
    return jsonify({'ok': True})


@app.route('/api/contatos/<int:cid>', methods=['DELETE'])
@login_required
def contatos_delete(cid):
    execute("UPDATE contatos SET ativo=false WHERE id=%s", (cid,))
    return jsonify({'ok': True})


@app.route('/api/contas', methods=['GET'])
@login_required
def contas_list():
    """Lista contas com saldo atual = saldo_inicial +/- parcelas pagas na conta."""
    rows = q_all("""
        SELECT c.*,
            c.saldo_inicial + COALESCE((
                SELECT SUM(CASE WHEN l.tipo='receita' THEN p.valor ELSE -p.valor END)
                FROM parcelas p JOIN lancamentos l ON l.id = p.lancamento_id
                WHERE p.conta_id = c.id AND p.status='pago'
            ), 0) AS saldo_atual
        FROM contas c WHERE c.ativo ORDER BY c.nome
    """)
    return jsonify({'ok': True, 'rows': rows})


@app.route('/api/contas', methods=['POST'])
@login_required
def contas_create():
    d = request.get_json() or {}
    nome = (d.get('nome') or '').strip()
    if not nome:
        return jsonify({'ok': False, 'error': 'Nome obrigatório'}), 400
    row = execute(
        "INSERT INTO contas (nome, saldo_inicial) VALUES (%s,%s) RETURNING *",
        (nome, d.get('saldo_inicial') or 0), returning=True
    )
    return jsonify({'ok': True, 'row': row})


@app.route('/api/contas/<int:cid>', methods=['PUT'])
@login_required
def contas_update(cid):
    d = request.get_json() or {}
    execute(
        "UPDATE contas SET nome=COALESCE(%s,nome), saldo_inicial=COALESCE(%s,saldo_inicial) WHERE id=%s",
        (d.get('nome'), d.get('saldo_inicial'), cid)
    )
    return jsonify({'ok': True})


@app.route('/api/contas/<int:cid>', methods=['DELETE'])
@login_required
def contas_delete(cid):
    execute("UPDATE contas SET ativo=false WHERE id=%s", (cid,))
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
            ct.nome AS contato_nome,
            (SELECT COUNT(*) FROM parcelas p WHERE p.lancamento_id = l.id) AS qtd_parcelas,
            (SELECT COUNT(*) FROM parcelas p WHERE p.lancamento_id = l.id AND p.status='pago') AS parcelas_pagas,
            (SELECT COALESCE(SUM(p.valor),0) FROM parcelas p WHERE p.lancamento_id = l.id AND p.status='pago') AS valor_pago
        FROM lancamentos l
        LEFT JOIN categorias cat ON cat.id = l.categoria_id
        LEFT JOIN contatos  ct  ON ct.id  = l.contato_id
        WHERE {' AND '.join(where)}
        ORDER BY l.data_competencia DESC, l.id DESC
        LIMIT 1000
    """, params)
    return jsonify({'ok': True, 'rows': rows})


@app.route('/api/lancamentos/<int:lid>', methods=['GET'])
@login_required
def lancamento_detail(lid):
    lanc = q_one("""
        SELECT l.*, cat.nome AS categoria_nome, ct.nome AS contato_nome
        FROM lancamentos l
        LEFT JOIN categorias cat ON cat.id = l.categoria_id
        LEFT JOIN contatos  ct  ON ct.id  = l.contato_id
        WHERE l.id = %s
    """, (lid,))
    if not lanc:
        return jsonify({'ok': False, 'error': 'Lançamento não encontrado'}), 404
    parcelas = q_all("SELECT * FROM parcelas WHERE lancamento_id=%s ORDER BY numero", (lid,))
    for p in parcelas:
        p['status_efetivo'] = status_efetivo(p)
    return jsonify({'ok': True, 'lancamento': lanc, 'parcelas': parcelas})


@app.route('/api/lancamentos', methods=['POST'])
@login_required
def lancamento_create():
    d = request.get_json() or {}
    tipo = d.get('tipo')
    descricao = (d.get('descricao') or '').strip()
    if tipo not in ('receita', 'despesa', 'imposto'):
        return jsonify({'ok': False, 'error': 'Tipo inválido'}), 400
    if not descricao:
        return jsonify({'ok': False, 'error': 'Descrição obrigatória'}), 400
    try:
        valor_total = Decimal(str(d.get('valor_total'))).quantize(Decimal('0.01'))
    except Exception:
        return jsonify({'ok': False, 'error': 'Valor inválido'}), 400
    if valor_total <= 0:
        return jsonify({'ok': False, 'error': 'Valor deve ser maior que zero'}), 400

    competencia = parse_date(d.get('data_competencia'), date.today())
    num_parcelas = max(1, int(d.get('num_parcelas') or 1))
    primeiro_venc = parse_date(d.get('primeiro_vencimento'), competencia)
    forma = d.get('forma_pagamento')
    conta_id = d.get('conta_id') or None

    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("""
            INSERT INTO lancamentos
                (tipo, descricao, contato_id, categoria_id, valor_total,
                 data_competencia, num_nf, nf_obs, num_parcelas, criado_por)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id
        """, (
            tipo, descricao, d.get('contato_id') or None, d.get('categoria_id') or None,
            valor_total, competencia, d.get('num_nf'), d.get('nf_obs'),
            num_parcelas, session['user_id']
        ))
        lid = cur.fetchone()['id']

        valores = split_valor(valor_total, num_parcelas)
        for i, v in enumerate(valores):
            cur.execute("""
                INSERT INTO parcelas
                    (lancamento_id, numero, valor, data_vencimento, conta_id, forma_pagamento, status)
                VALUES (%s,%s,%s,%s,%s,%s,'previsto')
            """, (lid, i + 1, v, add_months(primeiro_venc, i), conta_id, forma))
        conn.commit()
    except Exception as e:
        conn.rollback()
        cur.close()
        conn.close()
        return jsonify({'ok': False, 'error': str(e)}), 500
    cur.close()
    conn.close()
    return jsonify({'ok': True, 'id': lid})


@app.route('/api/lancamentos/<int:lid>', methods=['PUT'])
@login_required
def lancamento_update(lid):
    """Edita só os metadados (não recalcula parcelas)."""
    d = request.get_json() or {}
    execute("""
        UPDATE lancamentos SET
            descricao = COALESCE(%s, descricao),
            contato_id = %s, categoria_id = %s,
            num_nf = %s, nf_obs = %s
        WHERE id = %s
    """, (d.get('descricao'), d.get('contato_id') or None, d.get('categoria_id') or None,
          d.get('num_nf'), d.get('nf_obs'), lid))
    return jsonify({'ok': True})


@app.route('/api/lancamentos/<int:lid>', methods=['DELETE'])
@login_required
def lancamento_delete(lid):
    execute("DELETE FROM lancamentos WHERE id=%s", (lid,))  # cascade nas parcelas
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
    if de:
        where.append("p.data_vencimento >= %s")
        params.append(de)
    if ate:
        where.append("p.data_vencimento <= %s")
        params.append(ate)

    rows = q_all(f"""
        SELECT p.*, l.tipo AS lanc_tipo, l.descricao, l.num_nf,
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
    # filtro 'atrasado' aplicado pós-derivação
    if status == 'atrasado':
        rows = [p for p in rows if p['status_efetivo'] == 'atrasado']
    return jsonify({'ok': True, 'rows': rows})


@app.route('/api/parcelas/<int:pid>/pagar', methods=['POST'])
@login_required
def parcela_pagar(pid):
    d = request.get_json() or {}
    data_pag = parse_date(d.get('data_pagamento'), date.today())
    execute("""
        UPDATE parcelas SET status='pago', data_pagamento=%s,
            conta_id=COALESCE(%s, conta_id), forma_pagamento=COALESCE(%s, forma_pagamento)
        WHERE id=%s
    """, (data_pag, d.get('conta_id') or None, d.get('forma_pagamento'), pid))
    return jsonify({'ok': True})


@app.route('/api/parcelas/<int:pid>/estornar', methods=['POST'])
@login_required
def parcela_estornar(pid):
    execute("UPDATE parcelas SET status='previsto', data_pagamento=NULL WHERE id=%s", (pid,))
    return jsonify({'ok': True})


# ══════════════════════════════════════════════════════════════════════
# Impostos / Parâmetros
# ══════════════════════════════════════════════════════════════════════
@app.route('/api/parametros', methods=['GET'])
@login_required
def parametros_get():
    row = q_one("SELECT * FROM parametros WHERE id=1")
    return jsonify({'ok': True, 'parametros': row})


@app.route('/api/parametros', methods=['PUT'])
@login_required
def parametros_update():
    d = request.get_json() or {}
    execute("""
        UPDATE parametros SET
            regime = COALESCE(%s, regime),
            das_mei_valor = COALESCE(%s, das_mei_valor),
            das_vencimento_dia = COALESCE(%s, das_vencimento_dia),
            aliquota_simples = %s
        WHERE id = 1
    """, (d.get('regime'), d.get('das_mei_valor'),
          d.get('das_vencimento_dia'), d.get('aliquota_simples')))
    return jsonify({'ok': True})


@app.route('/api/impostos/das', methods=['POST'])
@login_required
def lancar_das():
    """Lança o DAS-MEI de um mês (default: mês atual) como despesa/imposto + 1 parcela."""
    d = request.get_json() or {}
    comp = d.get('competencia')  # 'YYYY-MM'
    hoje = date.today()
    if comp:
        try:
            ano, mes = int(comp[:4]), int(comp[5:7])
        except (ValueError, IndexError):
            return jsonify({'ok': False, 'error': 'Competência inválida (use YYYY-MM)'}), 400
    else:
        ano, mes = hoje.year, hoje.month

    par = q_one("SELECT * FROM parametros WHERE id=1")
    valor = Decimal(str(par.get('das_mei_valor') or 0))
    if valor <= 0:
        return jsonify({'ok': False, 'error': 'Configure o valor do DAS-MEI antes de lançar'}), 400
    dia = min(int(par.get('das_vencimento_dia') or 20), calendar.monthrange(ano, mes)[1])
    competencia = date(ano, mes, 1)
    vencimento = date(ano, mes, dia)

    # Evita duplicar o DAS do mesmo mês
    existe = scalar("""
        SELECT COUNT(*) FROM lancamentos
        WHERE tipo='imposto'
          AND date_trunc('month', data_competencia) = %s
          AND descricao ILIKE 'DAS%%'
    """, (competencia,))
    if existe:
        return jsonify({'ok': False, 'error': f'DAS de {mes:02d}/{ano} já foi lançado'}), 409

    cat = q_one("SELECT id FROM categorias WHERE tipo='despesa' AND nome ILIKE 'Impostos' AND ativo LIMIT 1")
    cat_id = cat['id'] if cat else None
    descricao = f"DAS-MEI {mes:02d}/{ano}"

    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO lancamentos (tipo, descricao, categoria_id, valor_total,
            data_competencia, num_parcelas, criado_por)
        VALUES ('imposto', %s, %s, %s, %s, 1, %s) RETURNING id
    """, (descricao, cat_id, valor, competencia, session['user_id']))
    lid = cur.fetchone()[0]
    cur.execute("""
        INSERT INTO parcelas (lancamento_id, numero, valor, data_vencimento, status)
        VALUES (%s, 1, %s, %s, 'previsto')
    """, (lid, valor, vencimento))
    conn.commit()
    cur.close()
    conn.close()
    return jsonify({'ok': True, 'id': lid, 'descricao': descricao})


# ══════════════════════════════════════════════════════════════════════
# Dashboard
# ══════════════════════════════════════════════════════════════════════
def _saldo_atual():
    base = scalar("SELECT COALESCE(SUM(saldo_inicial),0) FROM contas WHERE ativo") or 0
    mov = scalar("""
        SELECT COALESCE(SUM(CASE WHEN l.tipo='receita' THEN p.valor ELSE -p.valor END),0)
        FROM parcelas p JOIN lancamentos l ON l.id = p.lancamento_id
        WHERE p.status='pago'
    """) or 0
    return round(base + mov, 2)


def _previstas(tipos, ini, fim):
    """Soma de parcelas previstas (não pagas/canceladas) por janela de vencimento."""
    placeholders = ','.join(['%s'] * len(tipos))
    return scalar(f"""
        SELECT COALESCE(SUM(p.valor),0)
        FROM parcelas p JOIN lancamentos l ON l.id = p.lancamento_id
        WHERE p.status='previsto' AND l.tipo IN ({placeholders})
          AND p.data_vencimento >= %s AND p.data_vencimento <= %s
    """, (*tipos, ini, fim)) or 0


@app.route('/api/dashboard/resumo')
@login_required
def dashboard_resumo():
    hoje = date.today()
    saldo = _saldo_atual()

    def receber(ini, fim): return round(_previstas(['receita'], ini, fim), 2)
    def pagar(ini, fim):   return round(_previstas(['despesa', 'imposto'], ini, fim), 2)

    longe = date(2100, 1, 1)
    inicio_mes = hoje.replace(day=1)
    fim_mes = date(hoje.year, hoje.month, calendar.monthrange(hoje.year, hoje.month)[1])

    receita_mes = scalar("""
        SELECT COALESCE(SUM(valor_total),0) FROM lancamentos
        WHERE tipo='receita' AND data_competencia >= %s AND data_competencia <= %s
    """, (inicio_mes, fim_mes)) or 0
    despesa_mes = scalar("""
        SELECT COALESCE(SUM(valor_total),0) FROM lancamentos
        WHERE tipo IN ('despesa','imposto') AND data_competencia >= %s AND data_competencia <= %s
    """, (inicio_mes, fim_mes)) or 0

    return jsonify({'ok': True, 'resumo': {
        'saldo_atual': saldo,
        'a_receber_30': receber(hoje, add_months(hoje, 1)),
        'a_receber_60': receber(hoje, add_months(hoje, 2)),
        'a_receber_90': receber(hoje, add_months(hoje, 3)),
        'a_pagar_30': pagar(hoje, add_months(hoje, 1)),
        'a_pagar_60': pagar(hoje, add_months(hoje, 2)),
        'a_pagar_90': pagar(hoje, add_months(hoje, 3)),
        'vencidos_receber': round(_previstas(['receita'], date(2000, 1, 1), hoje), 2),
        'vencidos_pagar': round(_previstas(['despesa', 'imposto'], date(2000, 1, 1), hoje), 2),
        'receita_mes': round(receita_mes, 2),
        'despesa_mes': round(despesa_mes, 2),
        'resultado_mes': round(receita_mes - despesa_mes, 2),
    }})


@app.route('/api/dashboard/fluxo')
@login_required
def dashboard_fluxo():
    try:
        meses = max(3, min(int(request.args.get('meses', 12)), 24))
    except (TypeError, ValueError):
        meses = 12
    saldo = _saldo_atual()
    hoje = date.today()
    inicio = hoje.replace(day=1)
    rows = []
    acumulado = saldo
    for i in range(meses):
        mref = add_months(inicio, i)
        ult = date(mref.year, mref.month, calendar.monthrange(mref.year, mref.month)[1])
        entradas = round(_previstas(['receita'], mref, ult), 2)
        saidas = round(_previstas(['despesa', 'imposto'], mref, ult), 2)
        liquido = round(entradas - saidas, 2)
        acumulado = round(acumulado + liquido, 2)
        rows.append({
            'mes': f"{mref.year}-{mref.month:02d}",
            'entradas': entradas, 'saidas': saidas,
            'liquido': liquido, 'acumulado': acumulado,
        })
    return jsonify({'ok': True, 'saldo_inicial': saldo, 'rows': rows})


# ══════════════════════════════════════════════════════════════════════
# Export CSV
# ══════════════════════════════════════════════════════════════════════
@app.route('/api/export/lancamentos.csv')
@login_required
def export_csv():
    rows = q_all("""
        SELECT l.data_competencia, l.tipo, l.descricao, cat.nome AS categoria,
               ct.nome AS contato, l.num_nf, p.numero, l.num_parcelas,
               p.valor, p.data_vencimento, p.data_pagamento, p.status, co.nome AS conta
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
                'Parcela', 'TotalParcelas', 'Valor', 'Vencimento', 'Pagamento', 'Status', 'Conta'])
    for r in rows:
        w.writerow([
            r['data_competencia'], r['tipo'], r['descricao'], r['categoria'] or '',
            r['contato'] or '', r['num_nf'] or '', r['numero'], r['num_parcelas'],
            f"{r['valor']:.2f}".replace('.', ','), r['data_vencimento'],
            r['data_pagamento'] or '', r['status'], r['conta'] or '',
        ])
    return Response(
        buf.getvalue(), mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=lancamentos_gestao_joga.csv'}
    )


if __name__ == '__main__':
    porta = int(os.getenv('PORT', '5000'))
    if os.getenv('FLASK_ENV') == 'development' or os.getenv('DEV'):
        app.run(host='0.0.0.0', port=porta, debug=True)
    else:
        from waitress import serve
        print(f"[Gestão JOGA] Servindo em http://0.0.0.0:{porta}")
        serve(app, host='0.0.0.0', port=porta, threads=8)
