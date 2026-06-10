# Manual de Precificação — JOGA Soluções Empresariais

> Base de conhecimento do **Agente de IA Comercial JOGA**. Também serve de guia pro time
> comercial. Os números aqui são as **constantes calibráveis** do motor `precificador_core.py` —
> mudou aqui, muda lá (mantenha os dois em sincronia).

---

## 1. Filosofia — preço é sobre VALOR, não sobre tempo

Três formas de precificar:

| Lógica | Como pensa | Veredito |
|---|---|---|
| Custo + margem | "gastei X, cobro X+30%" | ❌ Com IA o custo despenca → você se autossabota |
| Mercado | "fulano cobra Y" | ⚠️ Só referência, te ancora na média |
| **Valor** | "isso resolve/gera R$ Z pro cliente" | ✅ A única certa pra software |

**Regras de ouro:**
- O custo marginal de software é ≈ zero. O preço acompanha o **problema resolvido**, não o esforço.
- **"Velocidade não é desconto."** Entregar rápido é vantagem competitiva da JOGA — nunca motivo pra cobrar menos. Não diga "foi rápido de fazer".
- **Nunca lidere com preço.** Primeiro a dor e o valor; o número por último.

### O corredor de preço
```
 PISO ───────────[ zona de negociação ]─────────── TETO
(abaixo            (mercado te posiciona)         (valor percebido
 desvaloriza)                                      pelo cliente: ROI, porte)
```
- **Preço astronômico** = passou do **teto daquele cliente** (não existe "caro" no absoluto).
- **Desvalorizar** = ancorou no piso.
- Método: achar o **teto do cliente** (porte + dor + ROI) e vender perto dele, respeitando o piso.

---

## 2. Mercado BR (referência)

| Segmento | Faixa de referência |
|---|---|
| Dev sob medida (projeto fechado, médio→grande) | R$ 15k–80k+ setup · R$ 1k–5k/mês |
| Automação de processo / RPA | R$ 2k–8k setup · R$ 200–600/mês |
| Agente de IA / bot de WhatsApp | R$ 3k–20k setup · R$ 300–1.5k/mês (msg + manutenção) |
| BI / Power BI | R$ 6k–30k setup · R$ 500–2k/mês |
| Hora de referência (só pra sentir o chão, **não** é como cobramos) | R$ 150–350/h |

Posicionamento JOGA: **boutique técnica premium** — entrega rápida, sob medida, com IA/automação.
Não compete por preço; compete por **velocidade + ajuste fino**.

---

## 3. Tipos de serviço e a economia de cada um

| Tipo | Característica | Fator de setup | Recorrência típica |
|---|---|---|---|
| `automacao` | script/RPA, processo pontual | 0.7 | baixa (suporte) |
| `agente_ia` | agente/copilot, RAG, classificação | 1.0 | média (+API/manutenção) |
| `whatsapp_bot` | bot de atendimento no WhatsApp | 0.9 | **alta** (msg + fluxo 24/7) |
| `bi` | dashboard / Power BI | 0.9 | média (evolução de dados) |
| `sistema_web` | app sob medida (tipo Gestão JOGA) | 1.1 | média/alta (hosting + evolução) |
| `dados` | banco de dados / engenharia de dados | 1.0 | média |
| `integracao` | API / integração entre sistemas | 0.9 | média (quebra quando o outro lado muda) |

O **fator de setup** multiplica a faixa-base do tier (seção 4).

---

## 4. Rubrica de quantificação (o coração)

### 4.1 Complexidade — 7 dimensões, nota 1–5
| Dimensão | 1 (simples) | 5 (complexo) |
|---|---|---|
| `escopo` | 1 função/tela | sistema multi-módulo |
| `integracoes` | nenhuma | várias (ERP, APIs, BI) |
| `dados` | CRUD básico | modelagem pesada, cálculos críticos |
| `usuarios_rbac` | 1 perfil | múltiplos perfis e permissões |
| `criticidade` | "ajudaria" | "o negócio depende disso" |
| `incerteza` | escopo claro | requisito vago, muita descoberta |
| `infra_deploy` | trivial | ambiente complexo/desconhecido |

**Soma (7–35) → tier e faixa-base de setup:**
| Soma | Tier | Faixa-base (R$) |
|---|---|---|
| ≤ 14 | Pequeno | 2.000 – 6.000 |
| 15–24 | Médio | 6.000 – 20.000 |
| ≥ 25 | Grande | 20.000 – 50.000 |

`faixa_base = faixa_do_tier × fator_do_tipo`

### 4.2 Multiplicador de valor
`valor_mult = porte × roi × urgência` (calibrado pra operação enxuta premium — sem inflar o topo)

| Porte do cliente | × | | ROI/dor | × | | Urgência | × |
|---|---|---|---|---|---|---|---|
| micro | 1.0 | | baixo | 1.0 | | normal | 1.0 |
| pequeno | 1.3 | | médio | 1.2 | | alta | 1.15 |
| médio | 1.6 | | alto | 1.35 | | urgente | 1.25 |
| grande | 2.0 | | | | | | |

O valor **empurra o teto** e a posição do valor sugerido dentro da faixa — não muda o piso.
O esticão do teto é limitado: `uplift = mín(1 + (valor_mult−1)×0.30, 1.50)` — o teto nunca infla
mais que +50% sobre a faixa-base, pra manter o preço honesto pro porte da JOGA.

### 4.3 Fator de infra (onde roda)
- **`joga`** (nossa estrutura): recorrência **inclui hosting** (R$ 80/mês — rateio do VPS ≤R$100 + margem). Mais controle, menos risco.
- **`cliente`**: **sem** hosting na recorrência, mas:
  - **+ levantamento de ambiente** no setup (R$ 800–2.000 conforme `infra_deploy`).
  - **multiplicador de risco** 1.1–1.3 conforme maturidade do ambiente (bom/médio/ruim-desconhecido).
  - O agente **faz o sócio avaliar a infra do cliente** (tem servidor/cloud? Docker? acesso? quem mantém?).

### 4.4 Recorrência
`recorrência_mês = hosting + suporte_evolução + extra_do_tipo`
- `hosting` = R$ 80 se `joga`, senão 0
- `suporte_evolução` = **máx(R$ 300, 2% do valor sugerido)**
- `extra_do_tipo` = whatsapp_bot +R$ 150 · agente_ia +R$ 100 · senão 0
- **Recorrência é inegociável no custom** (hosting + suporte + evolução). Nunca entregue sistemão sem mensalidade.

### 4.5 Banco de horas
Evolução além do escopo: **R$ 180/h**, pacote sugerido de 5h (Pequeno/Médio) a 10h (Grande)/mês.

---

## 5. ROI e venda de valor

Pergunte a **dor quantificável** do cliente: quantas horas/mês gasta, custo do erro, receita perdida.
- `economia_mensal` estimada → `payback (meses) = valor_sugerido / economia_mensal`
- `ganho_12m = economia_mensal × 12`
- Use na conversa: *"o sistema se paga em X meses e gera R$ Y no primeiro ano"*. Ancorar o preço no ganho do cliente é o que justifica o valor premium.

---

## 6. Alavancas comerciais

- **Ancoragem**: ofereça **3 opções**; o cliente tende ao do meio.
- **Anual com desconto** (paga 10, leva 12): trava o cliente e adianta caixa.
- **Setup fee é compromisso**: cliente que paga setup não some.
- **Trial/POC** no produtizado; **diagnóstico pago** no custom (filtra curioso).
- **WhatsApp/IA** = diferencial → add-on premium, não brinde.

---

## 7. Guarda-corpos (proteger o desenvolvedor)

- **Sem piso fixo**, mas respeite os **mínimos de sanidade** por tipo (faixa Pequeno × fator).
- **Escopo fechado + banco de horas**: mudança = orçamento novo, não favor.
- **Premissas explícitas** em toda proposta (o que está incluído e o que não está).
- **Confiança do orçamento**: se `incerteza` alta ou infra do cliente desconhecida → marcar **baixa** e tratar como **faixa de descoberta** (sugerir diagnóstico pago antes de cravar).

---

## 8. Exemplos resolvidos

**a) Multpel (BI grande)** — escopo 5, integrações 4, dados 5, rbac 4, criticidade 5, incerteza 3, infra 4 = **30 → Grande**. Tipo `bi` (0.9). Cliente médio/grande, ROI alto. Resultado: **setup ~R$ 30–65k (sugerido ~50k) + R$ 1.5–2.5k/mês**. → Os 10k cogitados eram o **piso**, não o preço.

**b) Automação pra contador** — escopo 2, integrações 1, dados 2, rbac 1, criticidade 2, incerteza 2, infra 3 = ~**13 → Médio (borda)**. Tipo `automacao` (0.7). Microempresa, ROI médio, infra do cliente. Resultado: **setup ~R$ 9–25k (sugerido ~14–15k) + R$ 300/mês**.

**c) Bot de WhatsApp com IA** — escopo 3, integrações 3, dados 2, rbac 1, criticidade 3, incerteza 3, infra 2 = **17 → Médio**. Tipo `whatsapp_bot` (0.9). ROI alto (atende 24/7). Resultado: **setup ~R$ 8–18k + R$ 450–700/mês** (inclui extra de mensagens/manutenção).

---

> **Calibração**: estes valores são ponto de partida. Ajuste as constantes em
> `precificador_core.py` conforme o histórico real de vendas da JOGA for mostrando o que fecha.
