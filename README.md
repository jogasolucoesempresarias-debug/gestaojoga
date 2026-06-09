# Gestão JOGA — Sistema Financeiro

Controle financeiro da **JOGA Soluções Empresariais**: Receitas, Despesas, Impostos e Caixa,
com parcelamento (contas a pagar/receber), registro de NF e fluxo de caixa projetado.

Mesma stack e padrão de deploy do app Multpel (Flask + Docker Swarm + Traefik + GHCR).

---

## 🛠 Stack

| Camada | Tecnologia |
|---|---|
| Backend | Flask 3 + Waitress (WSGI prod) |
| Frontend | HTML + CSS + JS puro (sem build step) + Chart.js (CDN) |
| Banco | PostgreSQL |
| Auth | Sessão Flask + hash Werkzeug (troca de senha no 1º acesso) |
| Deploy | Docker Swarm + Traefik (TLS Let's Encrypt) + Portainer |
| Registry | GitHub Container Registry (GHCR) |
| CI/CD | GitHub Actions (build + push em push pra `main`) |

- **URL produção**: https://gestao.jogasolucoes.com.br
- **Imagem**: `ghcr.io/jogasolucoesempresarias-debug/gestaojoga:latest`

---

## 📁 Estrutura

```
GestaoJoga/
├── server.py                 # Backend Flask (auth + módulo financeiro)
├── init_db.py                # Schema idempotente + seed usuários/categorias
├── _create_db.py             # Cria o database local (quando psql não está no PATH)
├── _smoke.py                 # Smoke test end-to-end da API (não deixa resíduo)
├── requirements.txt
├── Dockerfile
├── docker-compose.prod.yml   # Stack Swarm + Traefik labels
├── docker-compose.dev.yml    # Postgres local em container (opcional)
├── .env.example
├── .github/workflows/deploy.yml
├── static/  (app.css, app.js)
├── login.html / trocar-senha.html
├── index.html (dashboard) / lancamentos.html / contas.html
├── impostos.html / cadastros.html
```

---

## 🧠 Modelo de dados

Separa **competência** (quando a receita/despesa nasce) de **caixa/vencimento**
(quando o dinheiro entra/sai). Um lançamento parcelado vira **1 header + N parcelas**.

- `usuarios` — login (Gabriel + João)
- `categorias` — classificação/centro de custo (receita | despesa)
- `contatos` — clientes e fornecedores
- `contas` — caixa e bancos (saldo inicial + movimentos)
- `lancamentos` — competência (tipo, valor, NF, categoria, contato)
- `parcelas` — caixa (vencimento, status previsto/pago/cancelado; `atrasado` é derivado)
- `parametros` — DAS-MEI fixo + dia de vencimento + alíquota Simples (preparada p/ virada ME)

**Cálculos** (derivados em query, sem cron):
- Saldo atual = Σ saldo inicial das contas ± parcelas pagas
- A receber/pagar (30/60/90d) = parcelas previstas por janela de vencimento
- Fluxo projetado = entradas − saídas previstas por mês, acumulado a partir do saldo

---

## 🔧 Setup local (dev)

```powershell
# 1. venv + deps
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 2. .env (copie de .env.example e ajuste DB_PASSWORD)
#    DB_HOST=localhost  DB_NAME=gestao_joga

# 3. criar database + tabelas + seed
.\.venv\Scripts\python.exe -X utf8 _create_db.py   # cria o database (dispensa psql)
.\.venv\Scripts\python.exe -X utf8 init_db.py      # cria tabelas + usuários + categorias

# 4. subir servidor (DEV=1 usa o reloader do Flask; sem isso usa Waitress)
$env:DEV="1"; .\.venv\Scripts\python.exe -X utf8 server.py
```

Acesse http://localhost:5000 → login `ggabriel.milho@gmail.com` / `joga123`
(troca de senha obrigatória no 1º acesso).

Smoke test da API (sobe nada no banco — limpa os dados de teste ao final):
```powershell
.\.venv\Scripts\python.exe -X utf8 _smoke.py
```

---

## 🚀 Deploy produção

Fluxo: `git push main` → GitHub Actions builda e publica a imagem no GHCR →
no servidor, Portainer/Swarm faz `pull` + `service update`.

### Primeira vez
1. **PostgreSQL**: criar database `gestao_joga` no Postgres compartilhado do Swarm
2. **DNS Cloudflare**: registro `gestao.jogasolucoes.com.br` → IP do servidor
3. **Portainer**: criar stack com `docker-compose.prod.yml` e preencher as env vars
   (`SECRET_KEY`, `DB_USER`, `DB_PASSWORD` — idealmente como secrets)
4. **Inicializar tabelas** dentro do container:
   ```bash
   docker exec $(docker ps -q -f name=gestaojoga) python -X utf8 init_db.py
   ```

### Updates futuros
```bash
docker pull ghcr.io/jogasolucoesempresarias-debug/gestaojoga:latest
docker service update --image ghcr.io/jogasolucoesempresarias-debug/gestaojoga:latest --force gestaojoga_gestaojoga-app
```

---

## 🔐 Ambiente (`.env`)

Mesmos **nomes** em todo lugar; o que muda são os **valores** por ambiente. O código só lê
`os.getenv(...)` — nunca sabe se é local ou prod.

| Variável | Local | Produção (Portainer) |
|---|---|---|
| `DB_HOST` | `localhost` | `postgres_postgres` |
| `DB_PASSWORD` | senha local | **secret** |
| `SECRET_KEY` | qualquer | **secret** (`secrets.token_hex(32)`) |

O `.env` **nunca** é commitado (só o `.env.example`).
