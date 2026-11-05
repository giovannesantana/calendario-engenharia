"""ferramentas.py - as funções que o agente (agente.py) pode chamar.

Cada ferramenta faz UMA coisa e devolve um dicionário simples. O modelo decide QUAL chamar e
em que ordem; mas as ações que importam são protegidas pelo CÓDIGO, não só pelo prompt:

  - registrar_data só aceita uma data que o usuário realmente escreveu no terminal
    (o modelo não consegue "inventar" e gravar uma data);
  - criar_eventos_no_calendario sempre pede confirmação no terminal antes de enviar.

Tudo que o agente faz fica registrado em data/processed/agente_log.jsonl (auditoria).
"""
import functools
import json
import re
from datetime import date, datetime
from pathlib import Path

from extrair import (ARQ_PROMPT, CACHE, INBOX, MIME, ErroCota, ErroFatal,
                     caminho_cache, extrair_arquivo, normalizar)
from validar import validar as validar_itens

RAIZ = Path(__file__).resolve().parent.parent
PASTA = RAIZ / "data" / "processed"
ESTADO_ARQ = PASTA / "agente_estado.json"
LOG_ARQ = PASTA / "agente_log.jsonl"

ANO = str(date.today().year)  # o agente.py pode sobrescrever com --ano

# Memória da sessão: lista de trabalho + tudo o que o usuário escreveu.
ESTADO = {"entregas": [], "mensagens_usuario": []}

MESES = {"jan": 1, "fev": 2, "mar": 3, "abr": 4, "mai": 5, "jun": 6,
         "jul": 7, "ago": 8, "set": 9, "out": 10, "nov": 11, "dez": 12}


# --------------------------------------------------------------------------
# Infraestrutura: registro (log), estado e decorador
# --------------------------------------------------------------------------
def _salvar_estado():
    PASTA.mkdir(parents=True, exist_ok=True)
    ESTADO_ARQ.write_text(json.dumps(ESTADO, indent=2, ensure_ascii=False), encoding="utf-8")


def _log(nome: str, args, resultado):
    PASTA.mkdir(parents=True, exist_ok=True)
    linha = {"quando": datetime.now().isoformat(timespec="seconds"), "ferramenta": nome,
             "args": args, "resultado": str(resultado)[:600]}
    with LOG_ARQ.open("a", encoding="utf-8") as f:
        f.write(json.dumps(linha, ensure_ascii=False) + "\n")


def ferramenta(fn):
    """Registra cada chamada e transforma exceções em respostas de erro que o modelo consegue ler."""
    @functools.wraps(fn)
    def interna(*args, **kwargs):
        try:
            resultado = fn(*args, **kwargs)
        except (Exception, SystemExit) as erro:  # uma ferramenta com problema não derruba o agente
            resultado = {"erro": f"{type(erro).__name__}: {erro}"}
        _log(fn.__name__, kwargs or list(args), resultado)
        return resultado
    return interna


def carregar_estado() -> int:
    """Retoma a lista e as respostas de uma sessão anterior (agente.py --continuar)."""
    if not ESTADO_ARQ.exists():
        return 0
    dados = json.loads(ESTADO_ARQ.read_text(encoding="utf-8"))
    ESTADO["entregas"] = dados.get("entregas", [])
    ESTADO["mensagens_usuario"] = dados.get("mensagens_usuario", [])
    return len(ESTADO["entregas"])


def registrar_mensagem_do_usuario(texto: str):
    """Chamado pelo agente.py: tudo que o usuário digita vale como confirmação de dados."""
    ESTADO["mensagens_usuario"].append(texto)
    _salvar_estado()


def _resumo(item: dict) -> dict:
    return {"id": item["id"], "disciplina": item["disciplina"], "descricao": item["descricao"],
            "tipo": item["metodo"], "data": item["data"], "confianca": item["confianca"],
            "observacao": item.get("observacao"), "trecho_origem": item.get("trecho_origem")}


def _ativas() -> list:
    return [i for i in ESTADO["entregas"] if not i.get("ignorada")]


def _datas_citadas(texto: str) -> set:
    """Pares (dia, mês) que aparecem em um texto: aceita 30/11, 30-11, 2026-11-30 e '30 nov'."""
    pares = set()
    for a, m, d in re.findall(r"(\d{4})-(\d{1,2})-(\d{1,2})", texto):
        pares.add((int(d), int(m)))
    texto = re.sub(r"\d{4}-\d{1,2}-\d{1,2}", " ", texto)
    for d, m in re.findall(r"(\d{1,2})\s*[/.\-]\s*(\d{1,2})", texto):
        pares.add((int(d), int(m)))
    for d, mes in re.findall(r"(\d{1,2})\s*(?:de\s+|/)?\s*(jan|fev|mar|abr|mai|jun|jul|ago|set|out|nov|dez)",
                             texto.lower()):
        pares.add((int(d), MESES[mes]))
    return pares


# --------------------------------------------------------------------------
# Ferramentas
# --------------------------------------------------------------------------
@ferramenta
def listar_arquivos() -> dict:
    """Lista os planos de ensino (PDF, PNG ou JPG) disponíveis na pasta data/inbox."""
    INBOX.mkdir(parents=True, exist_ok=True)
    arquivos = sorted(p.name for p in INBOX.iterdir() if p.suffix.lower() in MIME)
    return {"pasta": "data/inbox", "arquivos": arquivos}


@ferramenta
def extrair_entregas(arquivo: str) -> dict:
    """Extrai com IA as entregas e avaliações de UM plano de ensino da pasta inbox e as adiciona
    à lista de trabalho. Chame uma vez por arquivo, usando o nome exato devolvido por listar_arquivos."""
    caminho = INBOX / Path(arquivo).name  # só aceita arquivos da inbox
    if not caminho.is_file() or caminho.suffix.lower() not in MIME:
        return {"erro": f"arquivo '{arquivo}' não encontrado na inbox"}

    prompt = ARQ_PROMPT.read_text(encoding="utf-8").replace("{{ANO}}", ANO)
    cache = caminho_cache(caminho, prompt)
    if cache.exists():
        brutos, origem = json.loads(cache.read_text(encoding="utf-8")), "cache"
    else:
        try:
            brutos = extrair_arquivo(caminho, prompt)
        except ErroCota:
            return {"erro": "cota diária da API esgotada; avise o usuário e pare"}
        except (ErroFatal, RuntimeError) as erro:
            return {"erro": str(erro)[:300]}
        CACHE.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(brutos, indent=2, ensure_ascii=False), encoding="utf-8")
        origem = "api"

    existentes = {(i["disciplina"].lower(), i["descricao"].lower()) for i in ESTADO["entregas"]}
    novas = sem_data = 0
    for bruto in brutos:
        item, _ = normalizar(bruto, caminho.name)
        if item is None or (item["disciplina"].lower(), item["descricao"].lower()) in existentes:
            continue
        item["id"] = max((i["id"] for i in ESTADO["entregas"]), default=0) + 1
        ESTADO["entregas"].append(item)
        existentes.add((item["disciplina"].lower(), item["descricao"].lower()))
        novas += 1
        sem_data += item["data"] is None
    _salvar_estado()
    return {"arquivo": caminho.name, "origem": origem, "entregas_adicionadas": novas,
            "sem_data": sem_data, "total_na_lista": len(_ativas())}


@ferramenta
def listar_pendentes() -> dict:
    """Lista as entregas que precisam de decisão do usuário: sem data ou com confiança baixa."""
    pendentes = [_resumo(i) for i in _ativas() if i["data"] is None or i["confianca"] == "baixa"]
    return {"quantidade": len(pendentes), "pendentes": pendentes}


@ferramenta
def perguntar_ao_usuario(pergunta: str) -> dict:
    """Faz UMA pergunta objetiva ao usuário no terminal e devolve a resposta. Use para resolver
    pendentes e peça sempre a data no formato dd/mm."""
    print(f"\n[Agente] {pergunta}")
    resposta = input("Você: ").strip()
    ESTADO["mensagens_usuario"].append(resposta)
    _salvar_estado()
    return {"resposta": resposta}


@ferramenta
def registrar_data(id_entrega: int, data: str) -> dict:
    """Define a data (formato AAAA-MM-DD) de uma entrega DEPOIS que o usuário informou essa data.
    Nunca invente: só funciona com uma data que o próprio usuário escreveu."""
    item = next((i for i in _ativas() if i["id"] == id_entrega), None)
    if item is None:
        return {"erro": f"não existe entrega com id {id_entrega}"}
    try:
        dia = date.fromisoformat(data)
    except ValueError:
        return {"erro": "data inválida; use AAAA-MM-DD"}
    if abs(dia.year - int(ANO)) > 1:
        return {"erro": f"ano {dia.year} fora do esperado ({ANO})"}

    citadas = set()
    for msg in ESTADO["mensagens_usuario"]:
        citadas |= _datas_citadas(msg)
    if (dia.day, dia.month) not in citadas:
        return {"erro": "essa data não foi informada pelo usuário; pergunte a ele (formato dd/mm) "
                        "e só então registre"}

    item["data"] = dia.isoformat()
    item["confianca"] = "alta"
    nota = "data informada pelo usuário"
    item["observacao"] = f"{item['observacao']} | {nota}" if item.get("observacao") else nota
    _salvar_estado()
    return {"ok": True, "entrega": _resumo(item)}


@ferramenta
def ignorar_entrega(id_entrega: int, motivo: str) -> dict:
    """Tira da lista uma entrega que o usuário disse que NÃO deve ir para o calendário
    (por exemplo, uma que não tem data e não precisa de lembrete)."""
    item = next((i for i in _ativas() if i["id"] == id_entrega), None)
    if item is None:
        return {"erro": f"não existe entrega com id {id_entrega}"}
    item["ignorada"] = True
    item["motivo_ignorada"] = motivo
    _salvar_estado()
    return {"ok": True, "ignorada": _resumo(item)}


@ferramenta
def resumo_do_calendario() -> dict:
    """Mostra o que irá para o calendário: entregas futuras com data, pendentes que sobraram e
    dias com mais de uma entrega (conflitos). Use antes de criar os eventos."""
    ok, pendentes = validar_itens([dict(i) for i in _ativas()])
    futuras = [i for i in ok if not i["passou"]]
    por_dia = {}
    for i in futuras:
        por_dia.setdefault(i["data"], []).append(f"{i['disciplina']} - {i['descricao']}")
    return {
        "entregas_futuras_com_data": len(futuras),
        "entregas_passadas_ignoradas": len(ok) - len(futuras),
        "pendentes_sem_data": [f"{p.get('disciplina')} - {p.get('descricao')}" for p in pendentes],
        "dias_com_mais_de_uma_entrega": {d: v for d, v in por_dia.items() if len(v) > 1},
        "proxima_entrega": f"{futuras[0]['data']} - {futuras[0]['disciplina']} - {futuras[0]['descricao']}"
                           if futuras else None,
    }


@ferramenta
def criar_eventos_no_calendario() -> dict:
    """Cria ou atualiza no Google Agenda as entregas futuras que têm data. O usuário confirma
    no terminal antes do envio; se ele recusar, nada é enviado."""
    ok, _ = validar_itens([dict(i) for i in _ativas()])
    futuras = [i for i in ok if not i["passou"]]
    if not futuras:
        return {"enviado": False, "motivo": "não há entregas futuras com data"}

    print(f"\n[Confirmação] {len(futuras)} evento(s) serão criados/atualizados no Google Agenda:")
    for i in futuras:
        print(f"  {i['data']}  {i['disciplina']} - {i['descricao']}")
    if input("Confirmar o envio? [s/N]: ").strip().lower() not in ("s", "sim"):
        return {"enviado": False, "motivo": "o usuário não confirmou"}

    import calendario  # só aqui, para não exigir as bibliotecas do Google em outros usos
    return {"enviado": True, **calendario.enviar(futuras)}


FERRAMENTAS = [listar_arquivos, extrair_entregas, listar_pendentes, perguntar_ao_usuario,
               registrar_data, ignorar_entrega, resumo_do_calendario, criar_eventos_no_calendario]