"""Inicializa o banco gestao_joga. Rode 1x: python -X utf8 init_db.py
Idempotente: pode rodar quantas vezes quiser sem efeito colateral."""
import os
import psycopg2
from werkzeug.security import generate_password_hash
from dotenv import load_dotenv

load_dotenv()

SENHA_INICIAL = os.getenv('SEED_SENHA_INICIAL', 'joga123')

conn = psycopg2.connect(
    host=os.getenv('DB_HOST', 'localhost'),
    port=os.getenv('DB_PORT', '5432'),
    dbname=os.getenv('DB_NAME', 'gestao_joga'),
    user=os.getenv('DB_USER', 'postgres'),
    password=os.getenv('DB_PASSWORD', ''),
)
cur = conn.cursor()

# ── Usuários ──────────────────────────────────────────────────────────
cur.execute("""
    CREATE TABLE IF NOT EXISTS usuarios (
        id                   SERIAL PRIMARY KEY,
        nome                 VARCHAR(255) NOT NULL,
        email                VARCHAR(255) UNIQUE NOT NULL,
        password_hash        VARCHAR(255) NOT NULL,
        role                 VARCHAR(20) DEFAULT 'admin',
        ativo                BOOLEAN DEFAULT true,
        must_change_password BOOLEAN DEFAULT true,
        criado_em            TIMESTAMP DEFAULT NOW()
    );
""")
cur.execute("CREATE INDEX IF NOT EXISTS ix_usuarios_email ON usuarios(email);")

# ── Categorias (centro de custo / classificação) ──────────────────────
cur.execute("""
    CREATE TABLE IF NOT EXISTS categorias (
        id    SERIAL PRIMARY KEY,
        nome  VARCHAR(120) NOT NULL,
        tipo  VARCHAR(10) NOT NULL CHECK (tipo IN ('receita', 'despesa')),
        cor   VARCHAR(9) DEFAULT '#38bdf8',
        ativo BOOLEAN DEFAULT true
    );
""")

# ── Contatos (clientes e fornecedores leves) ──────────────────────────
cur.execute("""
    CREATE TABLE IF NOT EXISTS contatos (
        id        SERIAL PRIMARY KEY,
        nome      VARCHAR(255) NOT NULL,
        tipo      VARCHAR(12) NOT NULL DEFAULT 'cliente'
                  CHECK (tipo IN ('cliente', 'fornecedor', 'ambos')),
        documento VARCHAR(20),
        email     VARCHAR(255),
        telefone  VARCHAR(30),
        ativo     BOOLEAN DEFAULT true
    );
""")

# ── Contas / Caixa (bancos e caixa) ───────────────────────────────────
cur.execute("""
    CREATE TABLE IF NOT EXISTS contas (
        id            SERIAL PRIMARY KEY,
        nome          VARCHAR(120) NOT NULL,
        saldo_inicial NUMERIC(14,2) DEFAULT 0,
        ativo         BOOLEAN DEFAULT true
    );
""")

# ── Lançamentos (competência: quando a receita/despesa nasce) ─────────
cur.execute("""
    CREATE TABLE IF NOT EXISTS lancamentos (
        id               SERIAL PRIMARY KEY,
        tipo             VARCHAR(10) NOT NULL
                         CHECK (tipo IN ('receita', 'despesa', 'imposto')),
        descricao        VARCHAR(255) NOT NULL,
        contato_id       INTEGER REFERENCES contatos(id) ON DELETE SET NULL,
        categoria_id     INTEGER REFERENCES categorias(id) ON DELETE SET NULL,
        valor_total      NUMERIC(14,2) NOT NULL,
        data_competencia DATE NOT NULL,
        num_nf           VARCHAR(60),
        nf_obs           TEXT,
        num_parcelas     INTEGER DEFAULT 1,
        criado_por       INTEGER REFERENCES usuarios(id) ON DELETE SET NULL,
        criado_em        TIMESTAMP DEFAULT NOW()
    );
""")
cur.execute("CREATE INDEX IF NOT EXISTS ix_lanc_tipo ON lancamentos(tipo);")
cur.execute("CREATE INDEX IF NOT EXISTS ix_lanc_comp ON lancamentos(data_competencia);")

# ── Parcelas (caixa: quando o dinheiro entra/sai) ─────────────────────
cur.execute("""
    CREATE TABLE IF NOT EXISTS parcelas (
        id               SERIAL PRIMARY KEY,
        lancamento_id    INTEGER NOT NULL REFERENCES lancamentos(id) ON DELETE CASCADE,
        numero           INTEGER NOT NULL DEFAULT 1,
        valor            NUMERIC(14,2) NOT NULL,
        data_vencimento  DATE NOT NULL,
        data_pagamento   DATE,
        conta_id         INTEGER REFERENCES contas(id) ON DELETE SET NULL,
        forma_pagamento  VARCHAR(40),
        status           VARCHAR(12) NOT NULL DEFAULT 'previsto'
                         CHECK (status IN ('previsto', 'pago', 'cancelado')),
        observacao       TEXT
    );
""")
cur.execute("CREATE INDEX IF NOT EXISTS ix_parc_lanc   ON parcelas(lancamento_id);")
cur.execute("CREATE INDEX IF NOT EXISTS ix_parc_venc   ON parcelas(data_vencimento);")
cur.execute("CREATE INDEX IF NOT EXISTS ix_parc_status ON parcelas(status);")

# ── Parâmetros (config de impostos: DAS-MEI fixo + % futura ME) ───────
cur.execute("""
    CREATE TABLE IF NOT EXISTS parametros (
        id                 INTEGER PRIMARY KEY DEFAULT 1,
        regime             VARCHAR(20) DEFAULT 'MEI',
        das_mei_valor      NUMERIC(14,2) DEFAULT 0,
        das_vencimento_dia INTEGER DEFAULT 20,
        aliquota_simples   NUMERIC(6,3),
        CONSTRAINT parametros_single_row CHECK (id = 1)
    );
""")

# ── Contratos / Recorrências (receita ou despesa fixa mensal) ─────────
cur.execute("""
    CREATE TABLE IF NOT EXISTS contratos (
        id              SERIAL PRIMARY KEY,
        contato_id      INTEGER REFERENCES contatos(id) ON DELETE SET NULL,
        descricao       VARCHAR(255) NOT NULL,
        tipo            VARCHAR(10) NOT NULL CHECK (tipo IN ('receita', 'despesa')),
        categoria_id    INTEGER REFERENCES categorias(id) ON DELETE SET NULL,
        valor_mensal    NUMERIC(14,2) NOT NULL,
        dia_vencimento  INTEGER DEFAULT 10,
        data_inicio     DATE NOT NULL,
        data_fim        DATE,                          -- NULL = indefinido
        status          VARCHAR(12) DEFAULT 'ativo'
                        CHECK (status IN ('ativo', 'pausado', 'cancelado')),
        criado_por      INTEGER REFERENCES usuarios(id) ON DELETE SET NULL,
        criado_em       TIMESTAMP DEFAULT NOW()
    );
""")
cur.execute("CREATE INDEX IF NOT EXISTS ix_contratos_status ON contratos(status);")

# Liga o lançamento gerado ao contrato de origem (rastreio, dedup e MRR)
cur.execute("ALTER TABLE lancamentos ADD COLUMN IF NOT EXISTS contrato_id INTEGER REFERENCES contratos(id) ON DELETE SET NULL;")
cur.execute("CREATE INDEX IF NOT EXISTS ix_lanc_contrato ON lancamentos(contrato_id);")

# ── Seeds ─────────────────────────────────────────────────────────────

# Linha única de parâmetros
cur.execute("INSERT INTO parametros (id) VALUES (1) ON CONFLICT (id) DO NOTHING;")

# Usuários (Gabriel + João) — admin, troca de senha no 1º acesso
USUARIOS_SEED = [
    ('Gabriel', 'ggabriel.milho@gmail.com'),
    ('João',    'jjvictorep@gmail.com'),
]
for nome, email in USUARIOS_SEED:
    cur.execute("SELECT id FROM usuarios WHERE email = %s", (email,))
    if not cur.fetchone():
        cur.execute(
            """INSERT INTO usuarios (nome, email, password_hash, role, must_change_password)
               VALUES (%s, %s, %s, 'admin', true)""",
            (nome, email, generate_password_hash(SENHA_INICIAL))
        )
        print(f"[OK] Usuario criado: {email} / {SENHA_INICIAL} -- TROCAR no primeiro login!")
    else:
        print(f"[OK] Usuario {email} ja existe.")

# Categorias default (só se a tabela estiver vazia)
cur.execute("SELECT COUNT(*) FROM categorias;")
if cur.fetchone()[0] == 0:
    CATEGORIAS_SEED = [
        ('Serviços / Projetos', 'receita', '#34d399'),
        ('Automação / IA',      'receita', '#38bdf8'),
        ('Consultoria',         'receita', '#818cf8'),
        ('Outras receitas',     'receita', '#a3e635'),
        ('Infra / Cloud',       'despesa', '#f87171'),
        ('Salários / Pró-labore', 'despesa', '#fb923c'),
        ('Ferramentas / SaaS',  'despesa', '#fbbf24'),
        ('Marketing',           'despesa', '#f472b6'),
        ('Impostos',            'despesa', '#94a3b8'),
        ('Outras despesas',     'despesa', '#64748b'),
    ]
    cur.executemany(
        "INSERT INTO categorias (nome, tipo, cor) VALUES (%s, %s, %s)",
        CATEGORIAS_SEED
    )
    print(f"[OK] {len(CATEGORIAS_SEED)} categorias default criadas.")

# Conta/caixa default
cur.execute("SELECT COUNT(*) FROM contas;")
if cur.fetchone()[0] == 0:
    cur.execute("INSERT INTO contas (nome, saldo_inicial) VALUES (%s, %s)", ('Caixa', 0))
    print("[OK] Conta 'Caixa' default criada.")

conn.commit()
cur.close()
conn.close()
print("[OK] Banco pronto. Rode: python -X utf8 server.py")
