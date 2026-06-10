"""Verificação da v3: motor determinístico (unit) + endpoint /api/comercial/chat em modo mock."""
import os, json, urllib.request, urllib.error, http.cookiejar
import psycopg2
from werkzeug.security import generate_password_hash
from dotenv import load_dotenv
import precificador_core as core

load_dotenv()
BASE = 'http://localhost:5000'
TEST_EMAIL = 'smoke@local.test'

print("== UNIT: precificador_core ==")

# a) Multpel-like → Grande, setup alto, recorrência com hosting (infra joga)
mult = core.calcular('bi',
    {'escopo': 5, 'integracoes': 4, 'dados': 5, 'usuarios_rbac': 4,
     'criticidade': 5, 'incerteza': 3, 'infra_deploy': 4},
    porte_cliente='grande', roi='alto', urgencia='normal', infra='joga', economia_mensal=15000)
assert mult['complexidade']['tier'] == 'grande', mult['complexidade']
assert mult['setup_min'] >= 20000, mult
assert mult['recorrencia_breakdown']['hosting'] == core.HOSTING_JOGA
assert mult['roi']['payback_meses'] > 0
print(f"  [OK ] Multpel-like: tier={mult['complexidade']['tier']} "
      f"setup={mult['setup_min']:.0f}-{mult['setup_max']:.0f} sugerido={mult['valor_sugerido']:.0f} "
      f"rec={mult['recorrencia_mensal']:.0f} payback={mult['roi']['payback_meses']}m conf={mult['confianca']}")

# b) Automação simples → Pequeno, sem hosting (infra cliente), confiança e risco refletidos
auto = core.calcular('automacao',
    {'escopo': 2, 'integracoes': 1, 'dados': 2, 'usuarios_rbac': 1,
     'criticidade': 2, 'incerteza': 2, 'infra_deploy': 1},
    porte_cliente='pequeno', roi='medio', urgencia='normal',
    infra='cliente', ambiente_cliente='bom')
assert auto['complexidade']['tier'] == 'pequeno', auto['complexidade']
assert auto['recorrencia_breakdown']['hosting'] == 0.0  # infra do cliente
assert auto['ambiente_setup'] > 0  # levantamento de ambiente
print(f"  [OK ] Automação: tier={auto['complexidade']['tier']} "
      f"setup={auto['setup_min']:.0f}-{auto['setup_max']:.0f} sugerido={auto['valor_sugerido']:.0f} "
      f"amb_setup={auto['ambiente_setup']:.0f} rec={auto['recorrencia_mensal']:.0f}")

# c) WhatsApp bot → extra de recorrência aplicado
bot = core.calcular('whatsapp_bot',
    {'escopo': 3, 'integracoes': 3, 'dados': 2, 'usuarios_rbac': 1,
     'criticidade': 3, 'incerteza': 3, 'infra_deploy': 2},
    porte_cliente='medio', roi='alto', urgencia='alta', infra='joga')
assert bot['recorrencia_breakdown']['extra_tipo'] == core.EXTRA_RECORRENCIA_TIPO['whatsapp_bot']
print(f"  [OK ] WhatsApp bot: tier={bot['complexidade']['tier']} sugerido={bot['valor_sugerido']:.0f} "
      f"rec={bot['recorrencia_mensal']:.0f} (extra={bot['recorrencia_breakdown']['extra_tipo']:.0f})")

# d) Valor empurra o teto: cliente grande > micro pro mesmo escopo
micro = core.calcular('sistema_web', {d: 4 for d in core.DIMENSOES}, porte_cliente='micro', roi='baixo', urgencia='normal', infra='joga')
grande = core.calcular('sistema_web', {d: 4 for d in core.DIMENSOES}, porte_cliente='grande', roi='alto', urgencia='alta', infra='joga')
assert grande['valor_sugerido'] > micro['valor_sugerido'], (micro['valor_sugerido'], grande['valor_sugerido'])
print(f"  [OK ] Valor-based: micro sugerido={micro['valor_sugerido']:.0f} < grande sugerido={grande['valor_sugerido']:.0f}")

print("\n== ENDPOINT: /api/comercial/chat (modo mock, sem OPENAI_API_KEY) ==")
if os.getenv('OPENAI_API_KEY', '').strip():
    print("  [SKIP] OPENAI_API_KEY configurada — endpoint usará a OpenAI de verdade (não testado aqui).")
else:
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

    def call(method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(BASE + path, data=data, method=method)
        req.add_header('Content-Type', 'application/json')
        try:
            r = opener.open(req, timeout=15); return json.loads(r.read().decode()), r.status
        except urllib.error.HTTPError as e:
            return json.loads(e.read().decode()), e.code

    def db():
        return psycopg2.connect(host=os.getenv('DB_HOST'), port=os.getenv('DB_PORT'),
            dbname=os.getenv('DB_NAME'), user=os.getenv('DB_USER'), password=os.getenv('DB_PASSWORD'))

    c = db(); cur = c.cursor()
    cur.execute("DELETE FROM usuarios WHERE email=%s", (TEST_EMAIL,))
    cur.execute("""INSERT INTO usuarios (nome, email, password_hash, role, must_change_password)
                   VALUES ('Smoke Test', %s, %s, 'admin', false)""", (TEST_EMAIL, generate_password_hash('joga123')))
    c.commit(); cur.close(); c.close()

    j, st = call('POST', '/api/login', {'email': TEST_EMAIL, 'senha': 'joga123'})
    print(f"  [{'OK ' if j.get('ok') else 'ERR'}] login -> {st}")
    j, st = call('POST', '/api/comercial/chat', {'messages': [{'role': 'user', 'content': 'Quero orçar um sistema web.'}]})
    assert j.get('ok') and j.get('mock') is True and j.get('estimativa'), j
    e = j['estimativa']
    print(f"  [OK ] chat mock -> estimativa: tier={e['complexidade']['tier']} sugerido={e['valor_sugerido']:.0f} rec={e['recorrencia_mensal']:.0f}")
    j, st = call('GET', '/api/comercial/orcamentos')
    print(f"  [OK ] histórico de orçamentos: {len(j.get('rows', []))} registro(s) salvos")

    c = db(); cur = c.cursor()
    cur.execute("DELETE FROM orcamentos WHERE criado_por=(SELECT id FROM usuarios WHERE email=%s)", (TEST_EMAIL,))
    cur.execute("DELETE FROM usuarios WHERE email=%s", (TEST_EMAIL,))
    c.commit(); cur.close(); c.close()
    print("  [OK ] limpeza (usuário + orçamentos de teste removidos)")

print("\n== SMOKE V3 CONCLUÍDO ==")
