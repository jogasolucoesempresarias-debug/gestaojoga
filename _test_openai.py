"""Teste REAL do agente comercial com a OpenAI (uma chamada curta)."""
import os, json, urllib.request, urllib.error, http.cookiejar
import psycopg2
from werkzeug.security import generate_password_hash
from dotenv import load_dotenv

load_dotenv()
BASE = 'http://localhost:5000'
TEST_EMAIL = 'smoke@local.test'
cj = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header('Content-Type', 'application/json')
    try:
        r = opener.open(req, timeout=90); return json.loads(r.read().decode()), r.status
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

call('POST', '/api/login', {'email': TEST_EMAIL, 'senha': 'joga123'})

briefing = ("Base rápida: sistema web sob medida pra uma clínica de porte médio — agenda, prontuário "
            "e financeiro, com integração ao WhatsApp, vários usuários com permissões diferentes. "
            "É crítico pro negócio e o escopo está claro. Vai rodar na nossa infra (JOGA). "
            "A clínica economiza cerca de R$ 5.000/mês com isso. Me dá uma base de preço já calculada.")
print("Enviando briefing real pro gpt-4.1-mini...\n")
j, st = call('POST', '/api/comercial/chat', {'messages': [{'role': 'user', 'content': briefing}]})

print(f"status={st}  ok={j.get('ok')}  mock={j.get('mock')}")
print("\n--- RESPOSTA DO AGENTE ---\n" + (j.get('reply') or '(vazio)')[:1500])
if j.get('estimativa'):
    e = j['estimativa']
    print("\n--- ESTIMATIVA (do motor determinístico) ---")
    print(f"  tipo={e['tipo_servico']} tier={e['complexidade']['tier']} soma={e['complexidade']['soma']}")
    print(f"  setup={e['setup_min']:.0f}-{e['setup_max']:.0f} sugerido={e['valor_sugerido']:.0f} "
          f"rec={e['recorrencia_mensal']:.0f} conf={e['confianca']}")
    if e.get('roi'):
        print(f"  ROI: payback={e['roi']['payback_meses']}m ganho_12m={e['roi']['ganho_12m']:.0f}")
else:
    print("\n(sem estimativa — o agente provavelmente fez pergunta de follow-up)")
if not j.get('ok'):
    print("\nERRO:", j.get('error'))

c = db(); cur = c.cursor()
cur.execute("DELETE FROM orcamentos WHERE criado_por=(SELECT id FROM usuarios WHERE email=%s)", (TEST_EMAIL,))
cur.execute("DELETE FROM usuarios WHERE email=%s", (TEST_EMAIL,))
c.commit(); cur.close(); c.close()
print("\n[OK] limpeza feita.")
