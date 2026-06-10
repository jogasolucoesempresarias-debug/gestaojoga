"""
Motor determinístico de precificação da JOGA.

A IA (Agente Comercial) NÃO calcula preço — ela entrevista o sócio, pontua a rubrica e chama
`calcular()`. Toda a matemática vive aqui: reprodutível, auditável e testável sem API.

As constantes abaixo são a "fonte de verdade" e espelham docs/PRECIFICACAO.md. Calibre aqui
conforme o histórico real de vendas.
"""
from __future__ import annotations

# ── Constantes calibráveis ────────────────────────────────────────────
FAIXA_TIER = {
    'pequeno': (2000.0, 6000.0),
    'medio':   (6000.0, 20000.0),
    'grande':  (20000.0, 50000.0),   # escada contínua, calibrada p/ operação enxuta premium
}

FATOR_TIPO = {
    'automacao':    0.7,
    'agente_ia':    1.0,
    'whatsapp_bot': 0.9,
    'bi':           0.9,
    'sistema_web':  1.1,
    'dados':        1.0,
    'integracao':   0.9,
}

EXTRA_RECORRENCIA_TIPO = {  # custo recorrente adicional por natureza do serviço
    'whatsapp_bot': 150.0,
    'agente_ia':    100.0,
}

# Multiplicadores de valor — calibrados pra não inflar o topo (operação enxuta premium).
MULT_PORTE     = {'micro': 1.0, 'pequeno': 1.3, 'medio': 1.6, 'grande': 2.0}
MULT_ROI       = {'baixo': 1.0, 'medio': 1.2, 'alto': 1.35}
MULT_URGENCIA  = {'normal': 1.0, 'alta': 1.15, 'urgente': 1.25}
MULT_RISCO_AMBIENTE = {'bom': 1.1, 'medio': 1.2, 'ruim': 1.3, 'desconhecido': 1.3}

# Quanto o valor percebido "estica" o teto da faixa (e o cap desse esticão).
UPLIFT_COEF    = 0.30   # antes 0.50 — amansa o topo
UPLIFT_CAP     = 1.50   # teto nunca infla mais que +50% sobre a faixa-base

HOSTING_JOGA   = 80.0       # rateio do VPS (≤R$100) + margem
SUPORTE_MIN    = 300.0      # piso da recorrência de suporte/evolução
SUPORTE_PCT    = 0.02       # 2% do valor sugerido / mês
VALOR_HORA     = 180.0      # banco de horas (evolução além do escopo)

DIMENSOES = ['escopo', 'integracoes', 'dados', 'usuarios_rbac',
             'criticidade', 'incerteza', 'infra_deploy']


# ── Helpers ────────────────────────────────────────────────────────────
def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _round50(v):
    """Arredonda pra múltiplo de R$ 50 (preço comercial limpo)."""
    return round(v / 50.0) * 50.0


def _tier(soma):
    if soma <= 14:
        return 'pequeno'
    if soma <= 24:
        return 'medio'
    return 'grande'


# ── Núcleo ──────────────────────────────────────────────────────────────
def calcular(
    tipo_servico,
    scores,                      # dict com as 7 dimensões (1–5)
    porte_cliente='micro',
    roi='baixo',
    urgencia='normal',
    infra='joga',                # 'joga' | 'cliente'
    ambiente_cliente='bom',      # só relevante p/ infra='cliente'
    economia_mensal=None,        # R$/mês que o cliente economiza/ganha (opcional → ROI)
):
    """Quantifica um projeto. Retorna dict serializável (sem dependências externas)."""
    tipo = tipo_servico if tipo_servico in FATOR_TIPO else 'sistema_web'
    fator_tipo = FATOR_TIPO[tipo]

    # 1) Complexidade → tier → faixa base
    scs = {d: int(_clamp(int(scores.get(d, 3)), 1, 5)) for d in DIMENSOES}
    soma = sum(scs.values())
    tier = _tier(soma)
    base_min, base_max = FAIXA_TIER[tier]
    base_min *= fator_tipo
    base_max *= fator_tipo

    # 2) Multiplicador de valor (empurra o teto + posição do sugerido)
    valor_mult = (MULT_PORTE.get(porte_cliente, 1.0)
                  * MULT_ROI.get(roi, 1.0)
                  * MULT_URGENCIA.get(urgencia, 1.0))

    # 3) Fator de infra
    risco = 1.0
    ambiente_setup = 0.0
    if infra == 'cliente':
        risco = MULT_RISCO_AMBIENTE.get(ambiente_cliente, 1.3)
        # levantamento de ambiente proporcional à complexidade de deploy (1–5)
        ambiente_setup = 800.0 + (scs['infra_deploy'] - 1) * 300.0  # 800 → 2000

    base_min *= risco
    base_max *= risco

    # teto sobe com o valor (com cap); piso é o chão da faixa
    uplift = min(1.0 + (valor_mult - 1.0) * UPLIFT_COEF, UPLIFT_CAP)
    setup_min = base_min + ambiente_setup
    setup_max = base_max * uplift + ambiente_setup

    # posição do valor sugerido dentro da faixa, puxada pelo valor percebido
    pos = _clamp(0.30 + (valor_mult - 1.0) * 0.20, 0.30, 0.85)
    valor_sugerido = setup_min + (setup_max - setup_min) * pos

    setup_min = _round50(setup_min)
    setup_max = _round50(setup_max)
    valor_sugerido = _round50(valor_sugerido)

    # 4) Recorrência
    hosting = HOSTING_JOGA if infra == 'joga' else 0.0
    suporte = max(SUPORTE_MIN, SUPORTE_PCT * valor_sugerido)
    extra = EXTRA_RECORRENCIA_TIPO.get(tipo, 0.0)
    recorrencia = _round50(hosting + suporte + extra)

    # 5) Banco de horas
    pacote_horas = 10 if tier == 'grande' else 5
    banco_horas = {'valor_hora': VALOR_HORA, 'pacote_horas_mes': pacote_horas,
                   'valor_pacote': _round50(VALOR_HORA * pacote_horas)}

    # 6) ROI (se a dor foi quantificada)
    roi_calc = None
    if economia_mensal and economia_mensal > 0:
        payback = round(valor_sugerido / economia_mensal, 1)
        roi_calc = {
            'economia_mensal': round(economia_mensal, 2),
            'payback_meses': payback,
            'ganho_12m': round(economia_mensal * 12, 2),
            'ganho_liquido_12m': round(economia_mensal * 12 - valor_sugerido, 2),
        }

    # 7) Confiança do orçamento
    if scs['incerteza'] >= 4 or (infra == 'cliente' and ambiente_cliente in ('ruim', 'desconhecido')):
        confianca = 'baixa'
    elif scs['incerteza'] <= 2 and not (infra == 'cliente' and ambiente_cliente == 'desconhecido'):
        confianca = 'alta'
    else:
        confianca = 'media'

    # 8) Premissas e o que ainda falta
    premissas = [
        'Escopo fechado conforme descrito; mudanças entram via banco de horas.',
        f"Infraestrutura: {'hospedagem na JOGA (inclusa na recorrência)' if infra == 'joga' else 'ambiente do cliente'}.",
        'Recorrência cobre suporte, evolução' + (' e hospedagem.' if infra == 'joga' else '.'),
    ]
    perguntas_abertas = []
    if confianca == 'baixa':
        perguntas_abertas.append('Escopo/ambiente com incerteza alta — recomendado diagnóstico pago antes de cravar o valor.')
    if economia_mensal is None:
        perguntas_abertas.append('Quantificar a dor do cliente (horas/custo/receita) para calcular o ROI.')
    if infra == 'cliente' and ambiente_cliente == 'desconhecido':
        perguntas_abertas.append('Levantar a infra do cliente (servidor/cloud, Docker, acessos, quem mantém).')

    return {
        'tipo_servico': tipo,
        'complexidade': {'scores': scs, 'soma': soma, 'tier': tier},
        'valor_mult': round(valor_mult, 2),
        'infra': infra,
        'ambiente_setup': _round50(ambiente_setup) if ambiente_setup else 0.0,
        'setup_min': setup_min,
        'setup_max': setup_max,
        'valor_sugerido': valor_sugerido,
        'recorrencia_mensal': recorrencia,
        'recorrencia_breakdown': {'hosting': hosting, 'suporte_evolucao': _round50(suporte), 'extra_tipo': extra},
        'banco_horas': banco_horas,
        'roi': roi_calc,
        'confianca': confianca,
        'premissas': premissas,
        'perguntas_abertas': perguntas_abertas,
        'moeda': 'BRL',
    }


# Schema da tool exposta ao modelo OpenAI (function calling)
TOOL_SCHEMA = {
    'type': 'function',
    'function': {
        'name': 'calcular_orcamento',
        'description': ('Calcula o orçamento (setup, valor sugerido, recorrência, ROI) de um '
                        'projeto da JOGA a partir da rubrica. Chame quando tiver dados suficientes '
                        'da entrevista. NÃO invente valores: use esta função.'),
        'parameters': {
            'type': 'object',
            'properties': {
                'tipo_servico': {'type': 'string', 'enum': list(FATOR_TIPO.keys())},
                'scores': {
                    'type': 'object',
                    'description': 'Notas 1–5 para cada dimensão de complexidade.',
                    'properties': {d: {'type': 'integer', 'minimum': 1, 'maximum': 5} for d in DIMENSOES},
                    'required': DIMENSOES,
                },
                'porte_cliente': {'type': 'string', 'enum': list(MULT_PORTE.keys())},
                'roi': {'type': 'string', 'enum': list(MULT_ROI.keys())},
                'urgencia': {'type': 'string', 'enum': list(MULT_URGENCIA.keys())},
                'infra': {'type': 'string', 'enum': ['joga', 'cliente']},
                'ambiente_cliente': {'type': 'string', 'enum': ['bom', 'medio', 'ruim', 'desconhecido']},
                'economia_mensal': {'type': 'number', 'description': 'R$/mês que o cliente economiza ou ganha (opcional).'},
            },
            'required': ['tipo_servico', 'scores', 'porte_cliente', 'roi', 'urgencia', 'infra'],
        },
    },
}
