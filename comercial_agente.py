"""
Agente de IA Comercial JOGA — orquestra a conversa com o sócio comercial.

O modelo (OpenAI gpt-4.1-mini) entrevista, pontua a rubrica e CHAMA a função `calcular_orcamento`
(tool) — o preço sai do `precificador_core` (determinístico), nunca da imaginação do modelo.

Sem OPENAI_API_KEY → MODO MOCK: exercita o pipeline sem custo (pra dev/teste).
"""
import os
import json
import requests

import precificador_core as core

OPENAI_URL = 'https://api.openai.com/v1/chat/completions'

SYSTEM_PROMPT = """Você é o **Agente de IA Comercial da JOGA Soluções Empresariais**.
A JOGA faz desenvolvimento sob medida: automação (Python/RPA), agentes de IA, bots de WhatsApp,
BI/Power BI, sistemas web, banco de dados/engenharia de dados e integrações.

SEU PAPEL: ajudar o sócio comercial a precificar projetos com confiança e excelência. Você:
1. ENTREVISTA: faz as perguntas certas pra entender o escopo, o cliente e a dor dele.
2. QUANTIFICA: quando tiver dados suficientes, CHAMA a função `calcular_orcamento` (nunca invente
   preço — o valor sai sempre dela).
3. JUSTIFICA com ROI: ancora o preço no ganho do cliente.
4. TREINA: dá dicas de abordagem, script e tratamento de objeção quando pedido.

MODOS (detecte pelo que o sócio escreve):
- "Orçar projeto": conduza a entrevista, uma pergunta por vez, objetivo. Cubra: tipo de serviço;
  complexidade (escopo, integrações, dados, usuários/permissões, criticidade, incerteza, infra);
  porte do cliente; urgência; ROI/dor; e ONDE roda (infra da JOGA ou do cliente — se for do cliente,
  pergunte sobre o ambiente: servidor/cloud, Docker, acessos, quem mantém).
- "Base rápida": o sócio cola um briefing curto → faça suposições razoáveis, chame a função e
  deixe explícito o que precisa confirmar.
- "Pedir dica": responda como consultor comercial (ancoragem, 3 opções, anual, nunca liderar com
  preço, setup como compromisso).

PRINCÍPIOS (Manual de Precificação JOGA):
- Preço é sobre VALOR, não sobre tempo. "Velocidade não é desconto" — nunca diga que foi rápido.
- Corredor de preço: respeite o piso, mire o teto do cliente (porte + ROI).
- Recorrência é inegociável no custom (hosting + suporte + evolução).
- Sempre apresente: faixa (mín–máx), VALOR SUGERIDO, recorrência, ROI (se houver), nível de
  confiança, premissas e o que ainda falta confirmar.
- Ao final de um orçamento, ofereça um TEXTO DE PROPOSTA pronto pro sócio copiar (WhatsApp/e-mail).

Tom: português do Brasil, objetivo, consultivo, confiante. Faça UMA pergunta por vez na entrevista.
Quando faltar um dado pra chamar a função, escolha um default razoável e sinalize a suposição."""


def _sanitize(messages):
    out = []
    for m in messages or []:
        role = m.get('role')
        content = m.get('content')
        if role in ('user', 'assistant') and isinstance(content, str) and content.strip():
            out.append({'role': role, 'content': content})
    return out[-20:]  # limita histórico (custo)


def _openai(convo, model, api_key):
    resp = requests.post(
        OPENAI_URL,
        headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
        json={'model': model, 'messages': convo, 'tools': [core.TOOL_SCHEMA],
              'tool_choice': 'auto', 'temperature': 0.4, 'max_tokens': 900},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()


def _run_tool(args):
    """Mapeia os args da tool pro core e calcula."""
    return core.calcular(
        tipo_servico=args.get('tipo_servico', 'sistema_web'),
        scores=args.get('scores', {}),
        porte_cliente=args.get('porte_cliente', 'micro'),
        roi=args.get('roi', 'baixo'),
        urgencia=args.get('urgencia', 'normal'),
        infra=args.get('infra', 'joga'),
        ambiente_cliente=args.get('ambiente_cliente', 'bom'),
        economia_mensal=args.get('economia_mensal'),
    )


def chat(messages):
    """Retorna {'reply': str, 'estimativa': dict|None, 'mock': bool}."""
    api_key = os.getenv('OPENAI_API_KEY', '').strip()
    model = os.getenv('OPENAI_MODEL', 'gpt-4.1-mini')

    if not api_key:
        return _mock(messages)

    convo = [{'role': 'system', 'content': SYSTEM_PROMPT}] + _sanitize(messages)
    estimativa = None
    for _ in range(4):  # permite algumas rodadas de tool-call
        data = _openai(convo, model, api_key)
        msg = data['choices'][0]['message']
        convo.append(msg)
        tool_calls = msg.get('tool_calls')
        if not tool_calls:
            return {'reply': msg.get('content') or '', 'estimativa': estimativa, 'mock': False}
        for tc in tool_calls:
            try:
                args = json.loads(tc['function'].get('arguments') or '{}')
                result = _run_tool(args)
                estimativa = result
            except Exception as e:
                result = {'erro': str(e)}
            convo.append({'role': 'tool', 'tool_call_id': tc['id'],
                          'content': json.dumps(result, ensure_ascii=False)})
    return {'reply': 'Não consegui finalizar o cálculo — tente reformular o escopo.',
            'estimativa': estimativa, 'mock': False}


def _mock(messages):
    """Sem chave: roda o core com um cenário-exemplo e devolve uma resposta de demonstração."""
    estimativa = core.calcular(
        tipo_servico='sistema_web',
        scores={'escopo': 4, 'integracoes': 3, 'dados': 4, 'usuarios_rbac': 3,
                'criticidade': 4, 'incerteza': 3, 'infra_deploy': 3},
        porte_cliente='medio', roi='alto', urgencia='normal',
        infra='joga', economia_mensal=4000,
    )
    reply = (
        "🔧 **MODO MOCK** (sem OPENAI_API_KEY configurada).\n\n"
        "O fluxo está funcionando: o motor determinístico calculou um orçamento de exemplo "
        "(sistema web, cliente médio, ROI alto, hospedado na JOGA). Configure a chave da OpenAI "
        "no `.env`/Portainer pra ativar a entrevista de verdade.\n\n"
        "Veja o card de estimativa ao lado — é exatamente o que o agente vai gerar após a conversa."
    )
    return {'reply': reply, 'estimativa': estimativa, 'mock': True}
