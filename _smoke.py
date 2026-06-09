"""Smoke test end-to-end da API. Não deixa resíduo: limpa dados de teste e
restaura o estado de primeiro acesso (must_change_password=true) no fim."""
import os
import json
import urllib.request
import urllib.error
import http.cookiejar
import psycopg2
from dotenv import load_dotenv

load_dotenv()
BASE = 'http://localhost:5000'
cj = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))


def call(method, path, body=None, expect_ok=True):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header('Content-Type', 'application/json')
    try:
        r = opener.open(req, timeout=10)
        raw = r.read().decode()
        status = r.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        status = e.code
    try:
        j = json.loads(raw)
    except json.JSONDecodeError:
        j = {'raw': raw[:80]}
    flag = 'OK ' if (j.get('ok') is True or status == 200) else 'ERR'
    if expect_ok and j.get('ok') is False:
        flag = 'ERR'
    print(f"  [{flag}] {method:4} {path:42} -> {status} {('' if j.get('ok') else json.dumps(j)[:90])}")
    return j, status


def db():
    return psycopg2.connect(
        host=os.getenv('DB_HOST'), port=os.getenv('DB_PORT'),
        dbname=os.getenv('DB_NAME'), user=os.getenv('DB_USER'),
        password=os.getenv('DB_PASSWORD'))


print("== Preparando: liberar must_change_password do Gabriel para o teste ==")
c = db(); cur = c.cursor()
cur.execute("UPDATE usuarios SET must_change_password=false WHERE email=%s", ('ggabriel.milho@gmail.com',))
c.commit(); cur.close(); c.close()

print("== Auth ==")
call('POST', '/api/login', {'email': 'ggabriel.milho@gmail.com', 'senha': 'joga123'})
call('GET', '/api/me')

print("== Cadastros ==")
cat, _ = call('GET', '/api/categorias?tipo=receita')
cat_id = cat['rows'][0]['id'] if cat.get('rows') else None
ct, _ = call('POST', '/api/contatos', {'nome': '[SMOKE] Cliente Teste', 'tipo': 'cliente'})
contato_id = ct.get('row', {}).get('id')
contas, _ = call('GET', '/api/contas')
conta_id = contas['rows'][0]['id'] if contas.get('rows') else None

print("== Lançamento: receita 12.000 em 3x ==")
lanc, _ = call('POST', '/api/lancamentos', {
    'tipo': 'receita', 'descricao': '[SMOKE] Projeto automação 3x',
    'valor_total': '12000.00', 'data_competencia': '2026-06-08',
    'categoria_id': cat_id, 'contato_id': contato_id,
    'num_parcelas': 3, 'primeiro_vencimento': '2026-06-10', 'conta_id': conta_id})
det, _ = call('GET', f"/api/lancamentos/{lanc.get('id')}")
print(f"     -> parcelas geradas: {len(det.get('parcelas', []))} (esperado 3); "
      f"valores: {[p['valor'] for p in det.get('parcelas', [])]}")

print("== Despesa à vista 350 + pagar ==")
desp, _ = call('POST', '/api/lancamentos', {
    'tipo': 'despesa', 'descricao': '[SMOKE] Cloud mensal', 'valor_total': '350.00',
    'data_competencia': '2026-06-08', 'num_parcelas': 1, 'primeiro_vencimento': '2026-06-08'})
parc, _ = call('GET', '/api/parcelas?direcao=pagar&status=previsto')
alvo = next((p for p in parc['rows'] if p['descricao'].startswith('[SMOKE] Cloud')), None)
if alvo:
    call('POST', f"/api/parcelas/{alvo['id']}/pagar", {'data_pagamento': '2026-06-08', 'conta_id': conta_id})

print("== Receber 1ª parcela da receita ==")
prec, _ = call('GET', '/api/parcelas?direcao=receber')
p1 = next((p for p in prec['rows'] if p['descricao'].startswith('[SMOKE] Projeto')), None)
if p1:
    call('POST', f"/api/parcelas/{p1['id']}/pagar", {'data_pagamento': '2026-06-10', 'conta_id': conta_id})

print("== Impostos / DAS ==")
call('PUT', '/api/parametros', {'das_mei_valor': '76.90', 'das_vencimento_dia': 20})
call('POST', '/api/impostos/das', {'competencia': '2026-06'})
call('POST', '/api/impostos/das', {'competencia': '2026-06'}, expect_ok=False)  # deve dar 409

print("== Dashboard ==")
res, _ = call('GET', '/api/dashboard/resumo')
print(f"     -> resumo: {json.dumps(res.get('resumo'), ensure_ascii=False)}")
flx, _ = call('GET', '/api/dashboard/fluxo?meses=12')
print(f"     -> fluxo: {len(flx.get('rows', []))} meses; saldo_inicial={flx.get('saldo_inicial')}")

print("== Export CSV ==")
req = urllib.request.Request(BASE + '/api/export/lancamentos.csv')
r = opener.open(req, timeout=10)
csv_txt = r.read().decode('utf-8-sig')
print(f"     [OK ] CSV: {len(csv_txt.splitlines())} linhas, header: {csv_txt.splitlines()[0][:60]}")

print("== Limpeza: remover dados de teste e restaurar 1º acesso ==")
c = db(); cur = c.cursor()
cur.execute("DELETE FROM lancamentos WHERE descricao LIKE '[SMOKE]%%' OR descricao LIKE 'DAS-MEI%%'")
cur.execute("DELETE FROM contatos WHERE nome LIKE '[SMOKE]%%'")
cur.execute("UPDATE parametros SET das_mei_valor=0, aliquota_simples=NULL WHERE id=1")
cur.execute("UPDATE usuarios SET must_change_password=true")
c.commit(); cur.close(); c.close()
print("     [OK ] Estado limpo. Gabriel/João voltam a 'joga123' + troca no 1º login.")
print("\n== SMOKE TEST CONCLUÍDO ==")
