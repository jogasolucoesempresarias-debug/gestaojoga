"""Smoke test da v2 (recorrência/MRR). Limpa o resíduo e restaura o 1º acesso."""
import os, json, urllib.request, urllib.error, http.cookiejar
from datetime import date
import psycopg2
from werkzeug.security import generate_password_hash
from dotenv import load_dotenv

TEST_EMAIL = 'smoke@local.test'

load_dotenv()
BASE = 'http://localhost:5000'
cj = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header('Content-Type', 'application/json')
    try:
        r = opener.open(req, timeout=10); raw = r.read().decode(); status = r.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode(); status = e.code
    j = json.loads(raw)
    flag = 'OK ' if j.get('ok') else 'ERR'
    print(f"  [{flag}] {method:4} {path:38} -> {status}")
    return j


def db():
    return psycopg2.connect(host=os.getenv('DB_HOST'), port=os.getenv('DB_PORT'),
        dbname=os.getenv('DB_NAME'), user=os.getenv('DB_USER'), password=os.getenv('DB_PASSWORD'))


# Usuário de teste dedicado (não toca em Gabriel/João)
c = db(); cur = c.cursor()
cur.execute("DELETE FROM usuarios WHERE email=%s", (TEST_EMAIL,))
cur.execute("""INSERT INTO usuarios (nome, email, password_hash, role, must_change_password)
               VALUES ('Smoke Test', %s, %s, 'admin', false)""",
            (TEST_EMAIL, generate_password_hash('joga123')))
c.commit(); cur.close(); c.close()

hoje = date.today()
comp = f"{hoje.year}-{hoje.month:02d}"

print("== Auth ==")
call('POST', '/api/login', {'email': TEST_EMAIL, 'senha': 'joga123'})

print("== Criar contrato receita 2.500/mes + despesa 300/mes ==")
call('POST', '/api/contratos', {'tipo': 'receita', 'descricao': '[SMOKE2] Sustentacao Cliente X',
    'valor_mensal': '2500.00', 'dia_vencimento': 10, 'data_inicio': hoje.isoformat()})
call('POST', '/api/contratos', {'tipo': 'despesa', 'descricao': '[SMOKE2] Cloud fixo',
    'valor_mensal': '300.00', 'dia_vencimento': 5, 'data_inicio': hoje.isoformat()})

print("== MRR no resumo ==")
res = call('GET', '/api/dashboard/resumo')['resumo']
print(f"     -> mrr_receita={res['mrr_receita']} mrr_despesa={res['mrr_despesa']} mrr_liquido={res['mrr_liquido']} (esperado 2500/300/2200)")

print("== Fluxo: projecao Opcao 2 (mes seguinte deve ter recorrencia, sem gerar) ==")
flx = call('GET', '/api/dashboard/fluxo?meses=3')['rows']
m2 = flx[1]  # proximo mes
print(f"     -> mes {m2['mes']}: entradas={m2['entradas']} saidas={m2['saidas']} (esperado 2500 / 300, projetado)")

print("== Gerar recorrencias do mes atual ==")
g1 = call('POST', '/api/contratos/gerar', {'competencia': comp})
print(f"     -> gerados={g1['gerados']} (esperado 2)")
g2 = call('POST', '/api/contratos/gerar', {'competencia': comp})
print(f"     -> regerar={g2['gerados']} (esperado 0, idempotente)")

print("== Fluxo de novo: mes atual agora vem da parcela, sem double-count ==")
flx2 = call('GET', '/api/dashboard/fluxo?meses=3')['rows']
m0 = flx2[0]
print(f"     -> mes {m0['mes']}: entradas={m0['entradas']} saidas={m0['saidas']} (esperado 2500 / 300, nao 5000/600)")

print("== Parcela gerada aparece em 'a receber' ==")
prec = call('GET', '/api/parcelas?direcao=receber')
achou = any(p['descricao'].startswith('[SMOKE2] Sustentacao') for p in prec['rows'])
print(f"     -> parcela do contrato em a receber: {achou}")

print("== Contrato lista com flag gerado_mes_atual ==")
cts = call('GET', '/api/contratos')
print(f"     -> {len(cts['rows'])} contratos; gerado_mes_atual: {[c['gerado_mes_atual'] for c in cts['rows']]}")

print("== Limpeza ==")
c = db(); cur = c.cursor()
cur.execute("DELETE FROM lancamentos WHERE descricao LIKE '[SMOKE2]%%'")
cur.execute("DELETE FROM contratos WHERE descricao LIKE '[SMOKE2]%%'")
cur.execute("DELETE FROM usuarios WHERE email=%s", (TEST_EMAIL,))
c.commit(); cur.close(); c.close()
print("     [OK ] resíduo + usuário de teste removidos (Gabriel/João intactos).")
print("\n== SMOKE V2 CONCLUÍDO ==")
