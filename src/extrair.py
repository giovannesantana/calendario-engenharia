"""extrair.py - lê planos de ensino (PDF/PNG/JPG) e gera um JSON de entregas com um LLM.

Uso:
    python src/extrair.py                         # processa tudo em data/inbox/
    python src/extrair.py plano.pdf cronograma.png
    python src/extrair.py data/inbox --saida data/raw/teste.json

A saída NÃO sobrescreve o seu gabarito (entregas_raw.json): por padrão vai para
data/raw/entregas_extraidas.json. Depois rode validar.py e avaliar.py.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

RAIZ = Path(__file__).resolve().parent.parent
load_dotenv(RAIZ / ".env")

ARQ_PROMPT = RAIZ / "prompts" / "extracao_v3.txt"  # padrão; troque com --prompt
INBOX = RAIZ / "data" / "inbox"
SAIDA_PADRAO = RAIZ / "data" / "raw" / "entregas_extraidas.json"
CACHE = RAIZ / "data" / "cache"  # respostas já obtidas: evita gastar a cota da API à toa

# Os nomes de modelo mudam: se der erro 404, confira o Flash atual no Google AI Studio e ajuste no .env
MODELO = os.getenv("MODELO_LLM", "gemini-3.8-flash")
MODELO_RESERVA = os.getenv("MODELO_RESERVA", "").strip()  # opcional: usado se o principal ficar sobrecarregado
TENTATIVAS = 5

MIME = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}
METODOS = {"prova", "teste", "trabalho", "projeto", "sprint", "outro"}
CONFIANCAS = {"alta", "media", "baixa"}


# --------------------------------------------------------------------------
# Camada do provedor: é a ÚNICA função que conhece o Gemini.
# Para trocar de IA (ex.: Claude), reescreva só esta função.
# --------------------------------------------------------------------------
def chamar_llm(caminho: Path, prompt: str, modelo: str = None) -> str:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    arquivo = types.Part.from_bytes(
        data=caminho.read_bytes(), mime_type=MIME[caminho.suffix.lower()]
    )
    resposta = client.models.generate_content(
        model=modelo or MODELO,
        contents=[arquivo, prompt],
        config=types.GenerateContentConfig(
            temperature=0, response_mime_type="application/json"
        ),
    )
    return resposta.text


# --------------------------------------------------------------------------
# Interpretação e limpeza da resposta
# --------------------------------------------------------------------------
def interpretar(bruto: str) -> list:
    """Transforma o texto devolvido pelo modelo em uma lista de dicionários."""
    texto = (bruto or "").strip()
    texto = texto.replace("\u201c", '"').replace("\u201d", '"')  # aspas curvas
    texto = re.sub(r"^```(?:json)?\s*|\s*```$", "", texto)       # cercas de código
    dados = json.loads(texto)

    if isinstance(dados, dict):  # às vezes vem {"entregas": [...]}
        listas = [v for v in dados.values() if isinstance(v, list)]
        dados = listas[0] if listas else [dados]
    if not isinstance(dados, list):
        raise ValueError("a resposta não é uma lista JSON")
    return dados


def normalizar(item: dict, origem: str):
    """Garante campos e valores válidos. Devolve (item_limpo, avisos) ou (None, avisos)."""
    avisos = []
    if not isinstance(item, dict):
        return None, ["item que não é objeto JSON"]

    disciplina = str(item.get("disciplina") or "").strip()
    descricao = str(item.get("descricao") or "").strip()
    if not disciplina or not descricao:
        return None, ["item sem disciplina ou descrição (descartado)"]

    metodo = str(item.get("metodo") or "").strip().lower()
    if metodo not in METODOS:
        avisos.append(f"metodo '{metodo}' fora da lista, trocado por 'outro'")
        metodo = "outro"

    data = item.get("data")
    if isinstance(data, str) and data.strip().lower() in ("", "null", "none"):
        data = None
    if data is not None:
        try:
            date.fromisoformat(str(data).strip())
            data = str(data).strip()
        except ValueError:
            avisos.append(f"data inválida '{data}', trocada por null")
            data = None

    confianca = str(item.get("confianca") or "").strip().lower()
    if confianca not in CONFIANCAS:
        avisos.append("confianca ausente ou inválida")
        confianca = "baixa"
    if data is None or avisos:
        confianca = "baixa"

    observacao = item.get("observacao")
    if avisos:
        extras = "; ".join(avisos)
        observacao = f"{observacao} | {extras}" if observacao else extras

    limpo = {
        "disciplina": disciplina,
        "metodo": metodo,
        "descricao": descricao,
        "data": data,
        "observacao": observacao or None,
        "confianca": confianca,
        "trecho_origem": item.get("trecho_origem") or None,
        "arquivo_origem": origem,
    }
    return limpo, avisos


class ErroFatal(Exception):
    """Erro que não se resolve tentando de novo (modelo inexistente, chave inválida)."""


class ErroCota(Exception):
    """Cota diária do plano gratuito esgotada: esperar não adianta (só amanhã ou outro modelo)."""


def e_cota_diaria(erro) -> bool:
    texto = str(erro)
    return "PerDay" in texto or "per day" in texto.lower()


CODIGOS_FATAIS = (400, 401, 403, 404)
STATUS_FATAIS = ("NOT_FOUND", "PERMISSION_DENIED", "API_KEY_INVALID", "API key not valid")
CODIGOS_TEMPORARIOS = (429, 500, 503, 504)
STATUS_TEMPORARIOS = ("RESOURCE_EXHAUSTED", "UNAVAILABLE", "DEADLINE_EXCEEDED")


def e_fatal(erro) -> bool:
    if getattr(erro, "code", None) in CODIGOS_FATAIS:
        return True
    return any(status in str(erro) for status in STATUS_FATAIS)


def e_temporario(erro) -> bool:
    if getattr(erro, "code", None) in CODIGOS_TEMPORARIOS:
        return True
    return any(status in str(erro) for status in STATUS_TEMPORARIOS)


# --------------------------------------------------------------------------
# Execução com novas tentativas
# --------------------------------------------------------------------------
def tentar_modelo(caminho: Path, prompt: str, modelo: str) -> list:
    ultimo_erro = None
    for tentativa in range(1, TENTATIVAS + 1):
        try:
            return interpretar(chamar_llm(caminho, prompt, modelo))
        except ValueError as erro:  # inclui JSONDecodeError: resposta mal formada
            ultimo_erro, espera = erro, 2
        except Exception as erro:
            if e_cota_diaria(erro):
                raise ErroCota(str(erro)) from erro
            if e_fatal(erro):
                raise ErroFatal(str(erro)) from erro
            ultimo_erro = erro
            # sobrecarga/limite de uso: espera crescente (15s, 30s, 45s, 60s)
            espera = 15 * tentativa if e_temporario(erro) else 5 * tentativa
        if tentativa < TENTATIVAS:
            print(f"   tentativa {tentativa} falhou ({str(ultimo_erro)[:90]}...); nova tentativa em {espera}s")
            time.sleep(espera)
    raise RuntimeError(f"{modelo}: falhou após {TENTATIVAS} tentativas: {ultimo_erro}")


def extrair_arquivo(caminho: Path, prompt: str) -> list:
    modelos = [MODELO] + ([MODELO_RESERVA] if MODELO_RESERVA and MODELO_RESERVA != MODELO else [])
    ultimo_erro = None
    for i, modelo in enumerate(modelos):
        try:
            return tentar_modelo(caminho, prompt, modelo)
        except (RuntimeError, ErroCota) as erro:
            ultimo_erro = erro
            if i < len(modelos) - 1:
                print(f"   {modelo} indisponível; tentando o modelo reserva {modelos[i + 1]}")
    if isinstance(ultimo_erro, ErroCota):
        raise ultimo_erro
    raise RuntimeError(str(ultimo_erro))


def caminho_cache(caminho: Path, prompt: str) -> Path:
    """Muda se o arquivo OU o prompt mudarem, então trocar de prompt refaz a extração."""
    h = hashlib.sha1(caminho.read_bytes() + prompt.encode("utf-8")).hexdigest()[:12]
    return CACHE / f"{caminho.stem}.{h}.json"


def coletar_arquivos(entradas: list) -> list:
    caminhos = []
    for entrada in entradas:
        p = Path(entrada)
        if p.is_dir():
            caminhos += sorted(x for x in p.iterdir() if x.suffix.lower() in MIME)
        elif p.is_file() and p.suffix.lower() in MIME:
            caminhos.append(p)
        else:
            print(f"Ignorado (não encontrado ou formato não suportado): {entrada}")
    return caminhos


def main():
    parser = argparse.ArgumentParser(description="Extrai entregas de planos de ensino com IA.")
    parser.add_argument("entradas", nargs="*", default=[str(INBOX)],
                        help="arquivos ou pastas (padrão: data/inbox)")
    parser.add_argument("--saida", default=str(SAIDA_PADRAO), help="arquivo JSON de saída")
    parser.add_argument("--prompt", default=str(ARQ_PROMPT),
                        help="arquivo de prompt (padrão: prompts/extracao_v3.txt)")
    parser.add_argument("--ano", default=str(date.today().year),
                        help="ano letivo, usado quando o documento traz datas sem ano (ex.: 21/set)")
    parser.add_argument("--refazer", action="store_true",
                        help="ignora o cache e chama a API de novo para todos os arquivos")
    args = parser.parse_args()

    if not os.getenv("GEMINI_API_KEY"):
        sys.exit("GEMINI_API_KEY não encontrada. Crie o arquivo .env (veja .env.example).")
    arq_prompt = Path(args.prompt)
    if not arq_prompt.exists():
        sys.exit(f"Prompt não encontrado: {arq_prompt}")

    arquivos = coletar_arquivos(args.entradas)
    if not arquivos:
        sys.exit("Nenhum PDF/PNG/JPG para processar. Coloque os planos em data/inbox/.")

    prompt = arq_prompt.read_text(encoding="utf-8").replace("{{ANO}}", str(args.ano))
    print(f"Modelo: {MODELO} | prompt: {arq_prompt.name} | ano letivo: {args.ano} | {len(arquivos)} arquivo(s)\n")

    todos, vistos, descartados, falhas = [], set(), 0, []
    cota_esgotada = False
    for arq in arquivos:
        print(f"{arq.name}")
        cache = caminho_cache(arq, prompt)
        if cache.exists() and not args.refazer:
            brutos = json.loads(cache.read_text(encoding="utf-8"))
            print("   (resultado em cache: nenhuma chamada à API)")
        else:
            try:
                brutos = extrair_arquivo(arq, prompt)
                CACHE.mkdir(parents=True, exist_ok=True)
                cache.write_text(json.dumps(brutos, indent=2, ensure_ascii=False), encoding="utf-8")
            except ErroCota:
                print("   Cota diária do plano gratuito esgotada para este modelo.")
                print("   Opções: esperar a renovação da cota, definir MODELO_RESERVA no .env "
                      "com outro modelo, ou gerar o JSON pelo chat.")
                cota_esgotada = True
                break
            except ErroFatal as erro:
                sys.exit(
                    f"\nERRO que não se resolve tentando de novo:\n{erro}\n\n"
                    "Se for 404/NOT_FOUND, o modelo mudou: ajuste MODELO_LLM no .env "
                    "para um modelo Flash listado no Google AI Studio.\n"
                    "Se for 401/403, confira a GEMINI_API_KEY."
                )
            except RuntimeError as erro:
                print(f"   ERRO: {erro}")
                falhas.append(arq.name)
                continue

        novos = 0
        for bruto in brutos:
            item, avisos = normalizar(bruto, arq.name)
            for aviso in avisos:
                print(f"   aviso: {aviso}")
            if item is None:
                descartados += 1
                continue
            chave = (item["disciplina"].lower(), item["descricao"].lower())
            if chave in vistos:
                continue
            vistos.add(chave)
            todos.append(item)
            novos += 1
        print(f"   {novos} entrega(s) extraída(s)")

    if not todos:
        sys.exit("\nNenhuma entrega extraída; o arquivo de saída NÃO foi alterado.")

    saida = Path(args.saida)
    saida.parent.mkdir(parents=True, exist_ok=True)
    saida.write_text(json.dumps(todos, indent=2, ensure_ascii=False), encoding="utf-8")

    sem_data = sum(1 for i in todos if i["data"] is None)
    baixa = sum(1 for i in todos if i["confianca"] == "baixa")
    print("\n--- Resumo ---")
    print(f"Entregas: {len(todos)} | sem data: {sem_data} | confiança baixa: {baixa} | descartadas: {descartados}")
    if falhas:
        print(f"Arquivos que falharam: {', '.join(falhas)}")
    if cota_esgotada:
        print("ATENÇÃO: a extração parou por cota esgotada; os arquivos restantes ficaram de fora.")
        print("Rode de novo depois: os já extraídos vêm do cache e não gastam a cota.")
    print(f"Salvo em {saida}")
    print("Próximo passo: revise os itens de confiança baixa e rode validar.py.")


if __name__ == "__main__":
    main()
