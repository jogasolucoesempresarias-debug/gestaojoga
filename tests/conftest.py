"""Fixtures: banco de TESTE próprio (gestao_joga_test), recriado a cada rodada.

Roda com o mesmo Postgres do .env local (ou o service do CI). Nunca toca no banco de dev/prod:
o nome do banco é forçado aqui antes de importar o servidor.
"""
import os
import sys

import psycopg2
import pytest
from dotenv import load_dotenv

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)
load_dotenv(os.path.join(RAIZ, '.env'))

BANCO_TESTE = 'gestao_joga_test'
os.environ['DB_NAME'] = BANCO_TESTE
os.environ['COOKIE_SECURE'] = '0'
os.environ['SECRET_KEY'] = 'teste'
os.environ.pop('OPENAI_API_KEY', None)  # agente em modo mock, sem custo

EMAIL = 'teste@local.test'
SENHA = 'senha-de-teste-123'


def _admin_conn():
    conn = psycopg2.connect(
        host=os.getenv('DB_HOST', 'localhost'), port=os.getenv('DB_PORT', '5432'),
        dbname='postgres', user=os.getenv('DB_USER', 'postgres'), password=os.getenv('DB_PASSWORD', ''))
    conn.autocommit = True
    return conn


@pytest.fixture(scope='session')
def server():
    conn = _admin_conn()
    cur = conn.cursor()
    cur.execute(f"DROP DATABASE IF EXISTS {BANCO_TESTE} WITH (FORCE)")
    cur.execute(f"CREATE DATABASE {BANCO_TESTE}")
    conn.close()

    import server as srv
    srv.iniciar()
    from werkzeug.security import generate_password_hash
    srv.execute("INSERT INTO usuarios (nome, email, password_hash, must_change_password) VALUES (%s,%s,%s,false)",
                ('Teste', EMAIL, generate_password_hash(SENHA)))
    yield srv
    if srv._pool:
        srv._pool.closeall()


@pytest.fixture(autouse=True)
def limpar(server):
    server.execute("""
        TRUNCATE lancamentos, parcelas, contratos, contrato_pulos, transferencias,
                 historico, orcamentos, contatos, contas RESTART IDENTITY CASCADE
    """)
    server.execute("INSERT INTO contas (nome, saldo_inicial) VALUES ('Caixa', 0)")
    server.execute("""
        UPDATE parametros SET regime='MEI', das_mei_valor=0, das_vencimento_dia=20, aliquota_simples=NULL,
               simples_base='competencia', recorrencia_modo='manual', recorrencia_auto_desde=NULL WHERE id=1
    """)
    server._falhas_login.clear()
    server._auto_dia['ultimo'] = None
    yield


@pytest.fixture
def client(server):
    c = server.app.test_client()
    r = c.post('/api/login', json={'email': EMAIL, 'senha': SENHA})
    assert r.status_code == 200, r.get_json()
    return c
