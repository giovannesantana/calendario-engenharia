"""validar.py - confere as entregas, converte as datas para AAAA-MM-DD e separa os pendentes.

Uso:
    python src/validar.py                                  # valida data/raw/entregas_raw.json
    python src/validar.py data/raw/entregas_extraidas.json # valida a saída do extrair.py

Entrada : lista de entregas em JSON (datas em AAAA-MM-DD, dd/mm/aa ou dd/mm/aaaa).
Saída   : data/processed/entregas.json, com {"entregas": [...], "pendentes": [...]}.
"""
import json
import sys
from datetime import date, datetime
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
ENTRADA_PADRAO = RAIZ / "data" / "raw" / "entregas_raw.json"
SAIDA = RAIZ / "data" / "processed" / "entregas.json"

FORMATOS_DATA = ("%Y-%m-%d", "%d/%m/%y", "%d/%m/%Y")


def carregar(caminho: Path) -> list:
    texto = caminho.read_text(encoding="utf-8")
    texto = texto.replace("\u201c", '"').replace("\u201d", '"')  # aspas curvas
    dados = json.loads(texto)
    if isinstance(dados, dict) and isinstance(dados.get("entregas"), list):
        dados = dados["entregas"]
    if not isinstance(dados, list):
        raise ValueError("o arquivo deve conter uma lista de entregas")
    return dados


def converter_data(valor):
    """Aceita AAAA-MM-DD, dd/mm/aa e dd/mm/aaaa. Devolve date ou None se for inválida."""
    texto = str(valor).strip()
    for formato in FORMATOS_DATA:
        try:
            return datetime.strptime(texto, formato).date()
        except ValueError:
            continue
    return None


def validar(itens: list, hoje: date = None):
    hoje = hoje or date.today()
    ok, pendentes = [], []

    for item in itens:
        if not isinstance(item, dict):
            pendentes.append({"item": item, "motivo": "item não é um objeto"})
            continue

        if not item.get("disciplina") or not item.get("descricao"):
            pendentes.append({**item, "motivo": "sem disciplina ou descrição"})
            continue

        item = {**item, "metodo": item.get("metodo") or "outro"}

        if item.get("data") in (None, "", "null", "NULL"):
            pendentes.append({**item, "motivo": "sem data"})
            continue

        dia = converter_data(item["data"])
        if dia is None:
            pendentes.append({**item, "motivo": "data inválida"})
            continue

        ok.append({**item, "data": dia.isoformat(), "passou": dia < hoje})

    return sorted(ok, key=lambda x: x["data"]), pendentes


def main():
    entrada = Path(sys.argv[1]) if len(sys.argv) > 1 else ENTRADA_PADRAO
    if not entrada.exists():
        sys.exit(f"Arquivo não encontrado: {entrada}")

    itens = carregar(entrada)
    ok, pendentes = validar(itens)

    SAIDA.parent.mkdir(parents=True, exist_ok=True)
    SAIDA.write_text(
        json.dumps({"entregas": ok, "pendentes": pendentes}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    futuras = [i for i in ok if not i["passou"]]
    print(f"{len(ok)} com data ({len(futuras)} futuras), {len(pendentes)} pendentes")
    for p in pendentes:
        print(f"  pendente: {p.get('disciplina', '?')} - {p.get('descricao', '?')} ({p['motivo']})")
    print(f"Salvo em {SAIDA}")


if __name__ == "__main__":
    main()