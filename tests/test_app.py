"""Testes do servidor: segurança, edição/exclusão, recorrência, baixa, saldo, dashboard e DAS."""
from datetime import date, timedelta

from conftest import EMAIL


def ok(r, status=200):
    j = r.get_json()
    assert r.status_code == status, (r.status_code, j)
    return j


def hoje():
    return date.today()


def mes(d):
    return d.strftime('%Y-%m')


def add_meses(d, n):
    m = d.month - 1 + n
    return date(d.year + m // 12, m % 12 + 1, 1)


def criar_lanc(c, **kw):
    corpo = {'tipo': 'receita', 'descricao': 'Projeto X', 'valor_total': 1000,
             'data_competencia': hoje().isoformat(), 'num_parcelas': 1,
             'primeiro_vencimento': hoje().isoformat()}
    corpo.update(kw)
    return ok(c.post('/api/lancamentos', json=corpo))['id']


def parcelas_de(c, lid):
    return ok(c.get(f'/api/lancamentos/{lid}'))['parcelas']


def saldo_contas(c):
    return {r['nome']: r['saldo_atual'] for r in ok(c.get('/api/contas'))['rows']}


def criar_contrato(c, **kw):
    corpo = {'tipo': 'receita', 'descricao': 'Manutenção', 'valor_mensal': 150,
             'dia_vencimento': 5, 'data_inicio': hoje().replace(day=1).isoformat()}
    corpo.update(kw)
    return ok(c.post('/api/contratos', json=corpo))['row']['id']


# ── Segurança ─────────────────────────────────────────────────────────
def test_sem_cors_para_outro_site(client):
    r = client.get('/api/me', headers={'Origin': 'https://site-malicioso.com'})
    assert 'Access-Control-Allow-Origin' not in r.headers


def test_bloqueia_alteracao_vinda_de_outro_site(client):
    r = client.post('/api/categorias', json={'nome': 'X', 'tipo': 'receita'},
                    headers={'Origin': 'https://site-malicioso.com'})
    assert r.status_code == 403
    r = client.post('/api/categorias', json={'nome': 'X', 'tipo': 'receita'},
                    headers={'Origin': 'http://localhost'})
    assert r.status_code == 200


def test_headers_de_seguranca(client):
    r = client.get('/health')
    assert r.headers['X-Frame-Options'] == 'DENY'
    assert r.headers['X-Content-Type-Options'] == 'nosniff'


def test_login_trava_depois_de_5_erros(server):
    c = server.app.test_client()
    for _ in range(5):
        assert c.post('/api/login', json={'email': EMAIL, 'senha': 'errada'}).status_code == 401
    r = c.post('/api/login', json={'email': EMAIL, 'senha': 'senha-de-teste-123'})
    assert r.status_code == 429


def test_senha_minima_8(client):
    r = client.post('/api/trocar-senha', json={'nova': '1234567', 'confirma': '1234567'})
    assert r.status_code == 400


def test_erro_interno_nao_vaza_detalhe(client):
    # categoria inexistente estoura FK no banco → mensagem genérica, sem SQL
    r = client.post('/api/lancamentos', json={'tipo': 'receita', 'descricao': 'x', 'valor_total': 10,
                                              'categoria_id': 999999})
    assert r.status_code == 500
    assert 'violates' not in r.get_json()['error']


# ── Contratos: pausar não apaga nada; edição parcial ─────────────────
def test_pausar_contrato_preserva_cliente_categoria_e_fim(client, server):
    ct = ok(client.post('/api/contatos', json={'nome': 'Barbearia'}))['row']['id']
    cat = server.q_one("SELECT id FROM categorias WHERE tipo='receita' LIMIT 1")['id']
    fim = (hoje() + timedelta(days=200)).isoformat()
    cid = criar_contrato(client, contato_id=ct, categoria_id=cat, data_fim=fim)

    ok(client.put(f'/api/contratos/{cid}', json={'status': 'pausado'}))
    row = server.q_one("SELECT * FROM contratos WHERE id=%s", (cid,))
    assert row['status'] == 'pausado'
    assert row['contato_id'] == ct and row['categoria_id'] == cat and row['data_fim'] == fim


def test_editar_contrato_e_aplicar_no_mes_atual(client, server):
    cid = criar_contrato(client)
    ok(client.post('/api/contratos/gerar', json={}))
    ok(client.put(f'/api/contratos/{cid}', json={'valor_mensal': 200, 'aplicar_mes_atual': True}))
    lanc = server.q_one("SELECT * FROM lancamentos WHERE contrato_id=%s", (cid,))
    assert lanc['valor_total'] == 200
    assert server.scalar("SELECT valor FROM parcelas WHERE lancamento_id=%s", (lanc['id'],)) == 200


def test_excluir_contrato_definitivo_so_sem_lancamentos(client):
    cid = criar_contrato(client)
    ok(client.post('/api/contratos/gerar', json={}))
    assert client.delete(f'/api/contratos/{cid}?definitivo=1').status_code == 409
    cid2 = criar_contrato(client, data_inicio=add_meses(hoje(), 2).isoformat())
    ok(client.delete(f'/api/contratos/{cid2}?definitivo=1'))


# ── Recorrência: manual × automática, pulo de mês ────────────────────
def test_modo_manual_nao_gera_sozinho(client, server):
    criar_contrato(client)
    server._auto_dia['ultimo'] = None
    client.get('/api/me')
    assert server.scalar("SELECT COUNT(*) FROM lancamentos") == 0
    j = ok(client.post('/api/contratos/gerar', json={}))
    assert j['gerados'] == 1
    assert ok(client.post('/api/contratos/gerar', json={}))['gerados'] == 0  # idempotente


def test_modo_automatico_gera_ao_ligar_e_ao_criar(client, server):
    criar_contrato(client)
    j = ok(client.put('/api/parametros', json={'recorrencia_modo': 'automatico'}))
    assert j['gerados'] == 1
    j = ok(client.post('/api/contratos', json={'tipo': 'despesa', 'descricao': 'Vivo', 'valor_mensal': 50,
                                                'dia_vencimento': 6, 'data_inicio': hoje().isoformat()}))
    assert j['gerados'] == 1
    assert server.scalar("SELECT COUNT(*) FROM lancamentos") == 2


def test_excluir_lancamento_gerado_pula_o_mes(client, server):
    cid = criar_contrato(client)
    ok(client.put('/api/parametros', json={'recorrencia_modo': 'automatico'}))
    lid = server.scalar("SELECT id FROM lancamentos WHERE contrato_id=%s", (cid,))
    ok(client.delete(f'/api/lancamentos/{lid}'))
    assert server.rodar_automaticos() == 0  # não volta sozinho
    contrato = [r for r in ok(client.get('/api/contratos'))['rows'] if r['id'] == cid][0]
    assert contrato['pulado_mes_atual'] and not contrato['gerado_mes_atual']
    # botão "gerar" do contrato desfaz o pulo
    assert ok(client.post(f'/api/contratos/{cid}/gerar', json={}))['gerados'] == 1


# ── Lançamentos: edição e exclusão ───────────────────────────────────
def test_editar_metadados_nao_mexe_nas_parcelas(client, server):
    lid = criar_lanc(client, num_parcelas=3, valor_total=900)
    antes = [p['id'] for p in parcelas_de(client, lid)]
    ok(client.put(f'/api/lancamentos/{lid}', json={'descricao': 'Novo nome', 'num_nf': '123',
                                                    'valor_total': 900, 'num_parcelas': 3}))
    assert [p['id'] for p in parcelas_de(client, lid)] == antes
    assert server.q_one("SELECT descricao FROM lancamentos WHERE id=%s", (lid,))['descricao'] == 'Novo nome'


def test_editar_valor_refaz_parcelas_se_nada_pago(client):
    lid = criar_lanc(client, num_parcelas=2, valor_total=1000)
    ok(client.put(f'/api/lancamentos/{lid}', json={'valor_total': 1500, 'num_parcelas': 3}))
    ps = parcelas_de(client, lid)
    assert len(ps) == 3 and round(sum(p['valor'] for p in ps), 2) == 1500


def test_editar_valor_bloqueado_com_parcela_paga(client):
    lid = criar_lanc(client, num_parcelas=2, valor_total=1000)
    ok(client.post(f"/api/parcelas/{parcelas_de(client, lid)[0]['id']}/pagar", json={}))
    r = client.put(f'/api/lancamentos/{lid}', json={'valor_total': 2000})
    assert r.status_code == 409
    ok(client.put(f'/api/lancamentos/{lid}', json={'descricao': 'só o nome pode'}))


def test_excluir_lancamento_pago_pede_confirmacao(client):
    lid = criar_lanc(client)
    ok(client.post(f"/api/parcelas/{parcelas_de(client, lid)[0]['id']}/pagar", json={}))
    r = client.delete(f'/api/lancamentos/{lid}')
    assert r.status_code == 409 and r.get_json()['precisa_confirmar']
    ok(client.delete(f'/api/lancamentos/{lid}?forcar=1'))
    assert saldo_contas(client)['Caixa'] == 0


def test_exclusao_fica_no_historico(client, server):
    lid = criar_lanc(client)
    ok(client.delete(f'/api/lancamentos/{lid}'))
    h = server.q_one("SELECT * FROM historico WHERE entidade='lancamento' AND acao='excluir'")
    assert h['entidade_id'] == lid and h['detalhe']['lancamento']['descricao'] == 'Projeto X'


# ── Parcelas: baixa, parcial, juros, edição, cancelar ────────────────
def test_baixa_sem_conta_vai_para_conta_padrao(client):
    lid = criar_lanc(client, valor_total=300)
    ok(client.post(f"/api/parcelas/{parcelas_de(client, lid)[0]['id']}/pagar", json={'conta_id': None}))
    assert saldo_contas(client)['Caixa'] == 300


def test_baixa_com_juros_entra_pelo_valor_pago(client):
    lid = criar_lanc(client, valor_total=300)
    ok(client.post(f"/api/parcelas/{parcelas_de(client, lid)[0]['id']}/pagar", json={'valor_pago': 315}))
    assert saldo_contas(client)['Caixa'] == 315
    assert ok(client.get('/api/dashboard/resumo'))['resumo']['saldo_atual'] == 315


def test_baixa_parcial_deixa_restante_em_aberto(client):
    lid = criar_lanc(client, valor_total=1000)
    pid = parcelas_de(client, lid)[0]['id']
    ok(client.post(f'/api/parcelas/{pid}/pagar', json={'valor_pago': 400, 'restante_em_aberto': True}))
    ps = parcelas_de(client, lid)
    assert [(p['valor'], p['status']) for p in ps] == [(400, 'pago'), (600, 'previsto')]
    assert saldo_contas(client)['Caixa'] == 400


def test_nao_paga_duas_vezes(client):
    lid = criar_lanc(client)
    pid = parcelas_de(client, lid)[0]['id']
    ok(client.post(f'/api/parcelas/{pid}/pagar', json={}))
    assert client.post(f'/api/parcelas/{pid}/pagar', json={}).status_code == 409


def test_estornar_limpa_valor_pago(client, server):
    lid = criar_lanc(client, valor_total=300)
    pid = parcelas_de(client, lid)[0]['id']
    ok(client.post(f'/api/parcelas/{pid}/pagar', json={'valor_pago': 290}))
    ok(client.post(f'/api/parcelas/{pid}/estornar'))
    p = server.q_one("SELECT * FROM parcelas WHERE id=%s", (pid,))
    assert p['status'] == 'previsto' and p['valor_pago'] is None and p['data_pagamento'] is None


def test_editar_parcela_recalcula_total(client, server):
    lid = criar_lanc(client, num_parcelas=2, valor_total=1000)
    pid = parcelas_de(client, lid)[1]['id']
    nova_data = (hoje() + timedelta(days=45)).isoformat()
    ok(client.put(f'/api/parcelas/{pid}', json={'valor': 700, 'data_vencimento': nova_data}))
    assert server.scalar("SELECT valor_total FROM lancamentos WHERE id=%s", (lid,)) == 1200
    ok(client.post(f'/api/parcelas/{pid}/cancelar'))
    assert server.scalar("SELECT valor_total FROM lancamentos WHERE id=%s", (lid,)) == 500
    ok(client.post(f'/api/parcelas/{pid}/reativar'))
    assert server.scalar("SELECT valor_total FROM lancamentos WHERE id=%s", (lid,)) == 1200


def test_parcela_paga_nao_edita(client):
    lid = criar_lanc(client)
    pid = parcelas_de(client, lid)[0]['id']
    ok(client.post(f'/api/parcelas/{pid}/pagar', json={}))
    assert client.put(f'/api/parcelas/{pid}', json={'valor': 1}).status_code == 409


# ── Contas: transferência, saldo, exclusão ───────────────────────────
def test_transferencia_move_saldo_sem_mudar_total(client):
    banco = ok(client.post('/api/contas', json={'nome': 'Banco', 'saldo_inicial': 1000}))['row']['id']
    caixa = [r for r in ok(client.get('/api/contas'))['rows'] if r['nome'] == 'Caixa'][0]['id']
    ok(client.post('/api/transferencias', json={'conta_origem': banco, 'conta_destino': caixa, 'valor': 250}))
    assert saldo_contas(client) == {'Caixa': 250, 'Banco': 750}
    assert ok(client.get('/api/dashboard/resumo'))['resumo']['saldo_atual'] == 1000
    tid = ok(client.get('/api/transferencias'))['rows'][0]['id']
    ok(client.delete(f'/api/transferencias/{tid}'))
    assert saldo_contas(client) == {'Caixa': 0, 'Banco': 1000}


def test_nao_exclui_conta_com_saldo(client):
    banco = ok(client.post('/api/contas', json={'nome': 'Banco', 'saldo_inicial': 10}))['row']['id']
    assert client.delete(f'/api/contas/{banco}').status_code == 409
    ok(client.put(f'/api/contas/{banco}', json={'saldo_inicial': 0}))
    ok(client.delete(f'/api/contas/{banco}'))


def test_editar_contato_parcial(client, server):
    cid = ok(client.post('/api/contatos', json={'nome': 'Cliente', 'email': 'a@b.com', 'telefone': '27 9999'}))['row']['id']
    ok(client.put(f'/api/contatos/{cid}', json={'nome': 'Cliente Novo'}))
    row = server.q_one("SELECT * FROM contatos WHERE id=%s", (cid,))
    assert row['nome'] == 'Cliente Novo' and row['email'] == 'a@b.com' and row['telefone'] == '27 9999'


# ── Dashboard ────────────────────────────────────────────────────────
def test_a_receber_inclui_contrato_nao_gerado(client):
    venc = hoje() + timedelta(days=5)
    criar_contrato(client, valor_mensal=1200, dia_vencimento=min(venc.day, 28),
                   data_inicio=venc.replace(day=1).isoformat())
    r = ok(client.get('/api/dashboard/resumo'))['resumo']
    if venc.day <= 28:
        assert r['a_receber_30'] >= 1200


def test_vencido_nao_inclui_hoje(client):
    criar_lanc(client, primeiro_vencimento=hoje().isoformat())
    assert ok(client.get('/api/dashboard/resumo'))['resumo']['vencidos_receber'] == 0
    criar_lanc(client, primeiro_vencimento=(hoje() - timedelta(days=1)).isoformat(), valor_total=50)
    assert ok(client.get('/api/dashboard/resumo'))['resumo']['vencidos_receber'] == 50


def test_fluxo_nao_conta_em_dobro(client):
    criar_contrato(client, valor_mensal=100)
    antes = ok(client.get('/api/dashboard/fluxo?meses=3'))['rows'][0]['entradas']
    ok(client.post('/api/contratos/gerar', json={}))
    depois = ok(client.get('/api/dashboard/fluxo?meses=3'))['rows'][0]['entradas']
    assert antes == depois == 100


# ── Impostos: DAS do Simples ─────────────────────────────────────────
def test_das_simples_sobre_faturamento_do_mes_anterior(client):
    anterior = add_meses(hoje(), -1)
    criar_lanc(client, valor_total=10000, data_competencia=anterior.isoformat(),
               primeiro_vencimento=anterior.isoformat())
    ok(client.put('/api/parametros', json={'regime': 'ME-Simples', 'aliquota_simples': 6}))
    prev = ok(client.get('/api/impostos/das/previa'))
    assert prev['competencia_padrao'] == mes(anterior)
    assert prev['previa']['valor'] == 600 and prev['previa']['base'] == 10000
    assert prev['previa']['vencimento'][:7] == mes(hoje())  # vence no mês seguinte
    j = ok(client.post('/api/impostos/das', json={}))
    assert j['valor'] == 600 and j['descricao'].startswith('DAS-Simples')
    assert client.post('/api/impostos/das', json={}).status_code == 409


def test_das_simples_sem_aliquota_recusa(client):
    ok(client.put('/api/parametros', json={'regime': 'ME-Simples'}))
    r = client.post('/api/impostos/das', json={})
    assert r.status_code == 400 and 'alíquota' in r.get_json()['error']


def test_das_mei_continua_igual(client):
    ok(client.put('/api/parametros', json={'das_mei_valor': 81.05}))
    j = ok(client.post('/api/impostos/das', json={'competencia': mes(hoje())}))
    assert j['valor'] == 81.05 and j['descricao'] == f"DAS-MEI {hoje():%m/%Y}"


def test_primeira_cobranca_nao_vence_antes_do_inicio(client, server):
    inicio = hoje().replace(day=15) if hoje().day <= 28 else hoje()
    cid = criar_contrato(client, dia_vencimento=1, data_inicio=inicio.isoformat())
    if inicio.month == hoje().month:
        ok(client.post('/api/contratos/gerar', json={'competencia': mes(inicio)}))
        venc = server.scalar("""SELECT p.data_vencimento FROM parcelas p JOIN lancamentos l ON l.id = p.lancamento_id
                                WHERE l.contrato_id=%s""", (cid,))
        assert venc == inicio
    # mês seguinte volta ao dia normal
    prox = add_meses(inicio, 1)
    ok(client.post('/api/contratos/gerar', json={'competencia': mes(prox)}))
    vencs = [r['data_vencimento'] for r in server.q_all(
        "SELECT p.data_vencimento FROM parcelas p JOIN lancamentos l ON l.id = p.lancamento_id WHERE l.contrato_id=%s", (cid,))]
    assert prox.isoformat() in vencs
