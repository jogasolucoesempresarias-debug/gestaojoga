"""Schema do banco gestao_joga — idempotente.

Roda no boot do servidor (uma vez por processo) e pelo `init_db.py`. Pode rodar quantas vezes
quiser: cria o que falta e não mexe no que já existe.

Cuidado com DDL em produção: `ALTER TABLE` pega lock exclusivo mesmo com IF NOT EXISTS, então as
colunas novas só são adicionadas depois de conferir no catálogo que estão faltando, e tudo roda
com `lock_timeout` curto para nunca ficar preso esperando uma tabela em uso.
"""
import os

from werkzeug.security import generate_password_hash

LOCK_MIGRACAO = 7_310_001  # chave do pg_advisory_xact_lock (evita 2 processos migrando juntos)

TABELAS = [
    # ── Usuários ──────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS usuarios (
        id                   SERIAL PRIMARY KEY,
        nome                 VARCHAR(255) NOT NULL,
        email                VARCHAR(255) UNIQUE NOT NULL,
        password_hash        VARCHAR(255) NOT NULL,
        role                 VARCHAR(20) DEFAULT 'admin',
        ativo                BOOLEAN DEFAULT true,
        must_change_password BOOLEAN DEFAULT true,
        criado_em            TIMESTAMP DEFAULT NOW()
    )""",
    "CREATE INDEX IF NOT EXISTS ix_usuarios_email ON usuarios(email)",

    # ── Categorias (centro de custo / classificação) ──────────────────
    """
    CREATE TABLE IF NOT EXISTS categorias (
        id    SERIAL PRIMARY KEY,
        nome  VARCHAR(120) NOT NULL,
        tipo  VARCHAR(10) NOT NULL CHECK (tipo IN ('receita', 'despesa')),
        cor   VARCHAR(9) DEFAULT '#38bdf8',
        ativo BOOLEAN DEFAULT true
    )""",

    # ── Contatos (clientes e fornecedores leves) ──────────────────────
    """
    CREATE TABLE IF NOT EXISTS contatos (
        id        SERIAL PRIMARY KEY,
        nome      VARCHAR(255) NOT NULL,
        tipo      VARCHAR(12) NOT NULL DEFAULT 'cliente'
                  CHECK (tipo IN ('cliente', 'fornecedor', 'ambos')),
        documento VARCHAR(20),
        email     VARCHAR(255),
        telefone  VARCHAR(30),
        ativo     BOOLEAN DEFAULT true
    )""",

    # ── Contas / Caixa (bancos e caixa) ───────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS contas (
        id            SERIAL PRIMARY KEY,
        nome          VARCHAR(120) NOT NULL,
        saldo_inicial NUMERIC(14,2) DEFAULT 0,
        ativo         BOOLEAN DEFAULT true
    )""",

    # ── Lançamentos (competência: quando a receita/despesa nasce) ─────
    """
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
    )""",
    "CREATE INDEX IF NOT EXISTS ix_lanc_tipo ON lancamentos(tipo)",
    "CREATE INDEX IF NOT EXISTS ix_lanc_comp ON lancamentos(data_competencia)",

    # ── Parcelas (caixa: quando o dinheiro entra/sai) ─────────────────
    """
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
    )""",
    "CREATE INDEX IF NOT EXISTS ix_parc_lanc   ON parcelas(lancamento_id)",
    "CREATE INDEX IF NOT EXISTS ix_parc_venc   ON parcelas(data_vencimento)",
    "CREATE INDEX IF NOT EXISTS ix_parc_status ON parcelas(status)",

    # ── Parâmetros (impostos + modo da recorrência) — linha única ─────
    """
    CREATE TABLE IF NOT EXISTS parametros (
        id                 INTEGER PRIMARY KEY DEFAULT 1,
        regime             VARCHAR(20) DEFAULT 'MEI',
        das_mei_valor      NUMERIC(14,2) DEFAULT 0,
        das_vencimento_dia INTEGER DEFAULT 20,
        aliquota_simples   NUMERIC(6,3),
        CONSTRAINT parametros_single_row CHECK (id = 1)
    )""",

    # ── Contratos / Recorrências (receita ou despesa fixa mensal) ─────
    """
    CREATE TABLE IF NOT EXISTS contratos (
        id              SERIAL PRIMARY KEY,
        contato_id      INTEGER REFERENCES contatos(id) ON DELETE SET NULL,
        descricao       VARCHAR(255) NOT NULL,
        tipo            VARCHAR(10) NOT NULL CHECK (tipo IN ('receita', 'despesa')),
        categoria_id    INTEGER REFERENCES categorias(id) ON DELETE SET NULL,
        valor_mensal    NUMERIC(14,2) NOT NULL,
        dia_vencimento  INTEGER DEFAULT 10,
        data_inicio     DATE NOT NULL,
        data_fim        DATE,
        status          VARCHAR(12) DEFAULT 'ativo'
                        CHECK (status IN ('ativo', 'pausado', 'cancelado')),
        criado_por      INTEGER REFERENCES usuarios(id) ON DELETE SET NULL,
        criado_em       TIMESTAMP DEFAULT NOW()
    )""",
    "CREATE INDEX IF NOT EXISTS ix_contratos_status ON contratos(status)",

    # Meses em que o contrato NÃO deve gerar cobrança (o lançamento gerado foi excluído de propósito)
    """
    CREATE TABLE IF NOT EXISTS contrato_pulos (
        contrato_id INTEGER NOT NULL REFERENCES contratos(id) ON DELETE CASCADE,
        mes         DATE NOT NULL,
        criado_em   TIMESTAMP DEFAULT NOW(),
        PRIMARY KEY (contrato_id, mes)
    )""",

    # ── Transferências entre contas (não é receita nem despesa) ───────
    """
    CREATE TABLE IF NOT EXISTS transferencias (
        id            SERIAL PRIMARY KEY,
        conta_origem  INTEGER NOT NULL REFERENCES contas(id),
        conta_destino INTEGER NOT NULL REFERENCES contas(id),
        valor         NUMERIC(14,2) NOT NULL CHECK (valor > 0),
        data          DATE NOT NULL,
        descricao     VARCHAR(255),
        criado_por    INTEGER REFERENCES usuarios(id) ON DELETE SET NULL,
        criado_em     TIMESTAMP DEFAULT NOW(),
        CHECK (conta_origem <> conta_destino)
    )""",

    # ── Orçamentos gerados pelo Agente Comercial (histórico) ──────────
    """
    CREATE TABLE IF NOT EXISTS orcamentos (
        id           SERIAL PRIMARY KEY,
        criado_por   INTEGER REFERENCES usuarios(id) ON DELETE SET NULL,
        tipo_servico VARCHAR(30),
        payload      JSONB,
        criado_em    TIMESTAMP DEFAULT NOW()
    )""",

    # ── Histórico de alterações (quem excluiu/estornou/editou o quê) ──
    """
    CREATE TABLE IF NOT EXISTS historico (
        id          SERIAL PRIMARY KEY,
        usuario_id  INTEGER REFERENCES usuarios(id) ON DELETE SET NULL,
        acao        VARCHAR(40) NOT NULL,
        entidade    VARCHAR(40) NOT NULL,
        entidade_id INTEGER,
        detalhe     JSONB,
        criado_em   TIMESTAMP DEFAULT NOW()
    )""",
]

# (tabela, coluna, definição) — só roda o ALTER se a coluna não existir
COLUNAS = [
    # Liga o lançamento gerado ao contrato de origem (rastreio, dedup e MRR)
    ('lancamentos', 'contrato_id', 'INTEGER REFERENCES contratos(id) ON DELETE SET NULL'),
    # Baixa com valor diferente do previsto (juros/desconto). NULL = pagou exatamente o valor.
    ('parcelas', 'valor_pago', 'NUMERIC(14,2)'),
    # Recorrência: 'manual' (botão) ou 'automatico' (gera sozinho todo mês)
    ('parametros', 'recorrencia_modo', "VARCHAR(12) DEFAULT 'manual'"),
    ('parametros', 'recorrencia_auto_desde', 'DATE'),
    # Simples Nacional: base do DAS por competência (padrão) ou por caixa
    ('parametros', 'simples_base', "VARCHAR(12) DEFAULT 'competencia'"),
]

INDICES_POS = [
    "CREATE INDEX IF NOT EXISTS ix_lanc_contrato ON lancamentos(contrato_id)",
    "CREATE INDEX IF NOT EXISTS ix_parc_conta ON parcelas(conta_id)",
]

USUARIOS_SEED = [
    ('Gabriel', 'ggabriel.milho@gmail.com'),
    ('João',    'jjvictorep@gmail.com'),
]

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


def _coluna_existe(cur, tabela, coluna):
    cur.execute("""
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name = %s AND column_name = %s
    """, (tabela, coluna))
    return cur.fetchone() is not None


def migrar(conn, log=print):
    """Cria/atualiza o schema e os seeds. Não faz commit — quem chama decide."""
    cur = conn.cursor()
    cur.execute("SET LOCAL lock_timeout = '5s'")
    cur.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_MIGRACAO,))

    for sql in TABELAS:
        cur.execute(sql)
    for tabela, coluna, definicao in COLUNAS:
        if not _coluna_existe(cur, tabela, coluna):
            cur.execute(f"ALTER TABLE {tabela} ADD COLUMN {coluna} {definicao}")
            log(f"[migração] coluna {tabela}.{coluna} criada")
    for sql in INDICES_POS:
        cur.execute(sql)

    # ── Seeds ─────────────────────────────────────────────────────────
    cur.execute("INSERT INTO parametros (id) VALUES (1) ON CONFLICT (id) DO NOTHING")

    senha_inicial = os.getenv('SEED_SENHA_INICIAL', 'joga123')
    for nome, email in USUARIOS_SEED:
        cur.execute("SELECT id FROM usuarios WHERE email = %s", (email,))
        if not cur.fetchone():
            cur.execute(
                """INSERT INTO usuarios (nome, email, password_hash, role, must_change_password)
                   VALUES (%s, %s, %s, 'admin', true)""",
                (nome, email, generate_password_hash(senha_inicial))
            )
            log(f"[seed] usuário criado: {email} (troca de senha obrigatória no 1º acesso)")

    cur.execute("SELECT COUNT(*) FROM categorias")
    if cur.fetchone()[0] == 0:
        cur.executemany("INSERT INTO categorias (nome, tipo, cor) VALUES (%s, %s, %s)", CATEGORIAS_SEED)
        log(f"[seed] {len(CATEGORIAS_SEED)} categorias default criadas")

    cur.execute("SELECT COUNT(*) FROM contas")
    if cur.fetchone()[0] == 0:
        cur.execute("INSERT INTO contas (nome, saldo_inicial) VALUES ('Caixa', 0)")
        log("[seed] conta 'Caixa' criada")

    # ── Correção de dados: toda baixa precisa de conta ────────────────
    # Antes a baixa aceitava "sem conta" e o valor entrava no saldo do Dashboard mas em nenhuma
    # conta. Essas baixas antigas passam para a primeira conta ativa (normalmente "Caixa").
    cur.execute("""
        UPDATE parcelas SET conta_id = (SELECT id FROM contas WHERE ativo ORDER BY id LIMIT 1)
        WHERE status = 'pago' AND conta_id IS NULL
          AND EXISTS (SELECT 1 FROM contas WHERE ativo)
    """)
    if cur.rowcount:
        log(f"[migração] {cur.rowcount} baixa(s) sem conta atribuída(s) à conta padrão")

    cur.close()
