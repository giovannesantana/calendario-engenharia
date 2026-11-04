"""avaliar.py - compara a extração da IA com o seu gabarito e calcula métricas.

Uso:
    python src/avaliar.py
    python src/avaliar.py --rotulo flash-v2
    python src/avaliar.py data/raw/entregas_raw.json data/raw/entregas_extraidas.json
    python src/avaliar.py --disciplina sustentavel     # só uma matéria

Como compara: NÃO depende de o texto da descrição ser idêntico ("AV1" x "1ª avaliação (AV1)").
Primeiro casa as entregas pela DATA (e confere o tipo); depois tenta casar pelas
descrições parecidas para achar datas erradas; por fim trata as entregas sem data.

Só são avaliadas as disciplinas presentes na extração, então dá para testar uma matéria por vez.
"""
import argparse
import json
import re
import sys
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

from validar import carregar, converter_data

RAIZ = Path(__file__).resolve().parent.parent
GABARITO_PADRAO = RAIZ / "data" / "raw" / "entregas_raw.json"
EXTRAIDO_PADRAO = RAIZ / "data" / "raw" / "entregas_extraidas.json"
PASTA_SAIDA = RAIZ / "data" / "processed"

LIMIAR_DISCIPLINA = 0.75
LIMIAR_DESCRICAO = 0.60


# --------------------------------------------------------------------------
# Utilitários
# --------------------------------------------------------------------------
def norm(texto) -> str:
    texto = unicodedata.normalize("NFKD", str(texto or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", texto.lower()).strip()


def semelhanca(a, b) -> float:
    return SequenceMatcher(None, norm(a), norm(b)).ratio()


def semelhanca_desc(a, b) -> float:
    """Para descrições: aguenta abreviações (a IA escreve 'Entrega 02 - Soluções' e o gabarito
    traz o título inteiro). Usa o maior entre a semelhança de texto e a sobreposição de palavras."""
    ta, tb = set(norm(a).split()), set(norm(b).split())
    sobreposicao = 0.0
    if min(len(ta), len(tb)) >= 2:
        sobreposicao = len(ta & tb) / min(len(ta), len(tb))
    return max(semelhanca(a, b), sobreposicao)


def data_iso(valor):
    if valor in (None, "", "null", "NULL"):
        return None
    d = converter_data(valor)
    return d.isoformat() if d else None


def preparar(itens: list) -> list:
    saida = []
    for it in itens:
        if not isinstance(it, dict):
            continue
        saida.append({
            "disciplina": it.get("disciplina") or "",
            "metodo": norm(it.get("metodo")),
            "descricao": it.get("descricao") or "",
            "data": data_iso(it.get("data")),
            "confianca": norm(it.get("confianca")) or "?",
        })
    return saida


def mapear_disciplinas(nomes_extraidos, nomes_gabarito) -> dict:
    """Liga cada nome da extração ao nome mais parecido do gabarito."""
    mapa = {}
    for nome in nomes_extraidos:
        melhor, nota = None, 0.0
        for alvo in nomes_gabarito:
            s = semelhanca(nome, alvo)
            if s > nota:
                melhor, nota = alvo, s
        mapa[nome] = melhor if nota >= LIMIAR_DISCIPLINA else None
    return mapa


# --------------------------------------------------------------------------
# Casamento entre gabarito (G) e extração (E) dentro de uma disciplina
# --------------------------------------------------------------------------
def casar(G: list, E: list) -> list:
    linhas = []
    g_livres, e_livres = list(G), list(E)

    # 1) mesma data: o mais forte. Prefere mesmo tipo e descrição parecida.
    for g in [x for x in G if x["data"]]:
        candidatos = [e for e in e_livres if e["data"] == g["data"]]
        if not candidatos:
            continue
        e = max(candidatos, key=lambda c: (c["metodo"] == g["metodo"], semelhanca_desc(c["descricao"], g["descricao"])))
        status = "ok" if e["metodo"] == g["metodo"] else "metodo_diferente"
        linhas.append({"status": status, "g": g, "e": e})
        g_livres.remove(g)
        e_livres.remove(e)

    # 2) datas diferentes, mas descrições parecidas: provável data errada
    pares = sorted(
        ((semelhanca_desc(g["descricao"], e["descricao"]), g, e)
         for g in g_livres if g["data"] for e in e_livres if e["data"]),
        key=lambda t: t[0], reverse=True)
    for nota, g, e in pares:
        if nota < LIMIAR_DESCRICAO or g not in g_livres or e not in e_livres:
            continue
        linhas.append({"status": "data_errada", "g": g, "e": e})
        g_livres.remove(g)
        e_livres.remove(e)

    # 2b) o gabarito tem data, mas a IA devolveu o mesmo item SEM data (deixou null)
    pares_nulos = sorted(
        ((semelhanca_desc(g["descricao"], e["descricao"]), g, e)
         for g in g_livres if g["data"] for e in e_livres if not e["data"]),
        key=lambda t: t[0], reverse=True)
    for nota, g, e in pares_nulos:
        if nota < 0.5 or g not in g_livres or e not in e_livres:
            continue
        linhas.append({"status": "data_nao_extraida", "g": g, "e": e})
        g_livres.remove(g)
        e_livres.remove(e)

    # 3) entregas SEM data no gabarito: a IA deveria deixar null
    for g in [x for x in g_livres if not x["data"]]:
        if not e_livres:
            break
        e = max(e_livres, key=lambda c: semelhanca_desc(c["descricao"], g["descricao"]))
        if semelhanca_desc(e["descricao"], g["descricao"]) < 0.5:
            continue
        linhas.append({"status": "pendente_ok" if not e["data"] else "inventada", "g": g, "e": e})
        g_livres.remove(g)
        e_livres.remove(e)

    # 3b) por eliminação: sobrou um pendente de cada lado, ambos sem data
    g_nulos = [x for x in g_livres if not x["data"]]
    e_nulos = [x for x in e_livres if not x["data"]]
    if len(g_nulos) == 1 and len(e_nulos) == 1:
        linhas.append({"status": "pendente_ok", "g": g_nulos[0], "e": e_nulos[0]})
        g_livres.remove(g_nulos[0])
        e_livres.remove(e_nulos[0])

    # 4) sobras
    for g in g_livres:
        linhas.append({"status": "faltou" if g["data"] else "faltou_pendente", "g": g, "e": None})
    for e in e_livres:
        linhas.append({"status": "extra" if e["data"] else "extra_pendente", "g": None, "e": e})
    return linhas


# --------------------------------------------------------------------------
# Métricas
# --------------------------------------------------------------------------
def pct(parte, total):
    return round(100 * parte / total, 1) if total else None


def calcular(linhas: list) -> dict:
    cont = {}
    for l in linhas:
        cont[l["status"]] = cont.get(l["status"], 0) + 1
    c = lambda k: cont.get(k, 0)

    acertos = c("ok") + c("metodo_diferente")
    gab_datadas = acertos + c("data_errada") + c("faltou") + c("data_nao_extraida")
    ext_datadas = acertos + c("data_errada") + c("extra") + c("inventada")
    gab_pendentes = c("pendente_ok") + c("inventada") + c("faltou_pendente")

    erros_de_data = [l for l in linhas if l["status"] in ("data_errada", "extra", "inventada")]
    erros_baixa = sum(1 for l in erros_de_data if l["e"]["confianca"] == "baixa")
    todos_e = [l["e"] for l in linhas if l["e"]]

    return {
        "contagens": cont,
        "gabarito_com_data": gab_datadas,
        "gabarito_sem_data": gab_pendentes,
        "extraidas_com_data": ext_datadas,
        "datas_corretas": acertos,
        "recall_data_pct": pct(acertos, gab_datadas),
        "precisao_data_pct": pct(acertos, ext_datadas),
        "datas_erradas": c("data_errada"),
        "datas_inventadas": c("inventada"),
        "entregas_faltando": c("faltou"),
        "datas_nao_extraidas": c("data_nao_extraida"),
        "nao_extraidas_sinalizadas_baixa": sum(
            1 for l in linhas if l["status"] == "data_nao_extraida" and l["e"]["confianca"] == "baixa"),
        "entregas_extras": c("extra"),
        "tipo_diferente": c("metodo_diferente"),
        "pendentes_tratados_corretamente": c("pendente_ok"),
        "erros_de_data_total": len(erros_de_data),
        "erros_sinalizados_baixa_confianca": erros_baixa,
        "erros_com_confianca_alta_ou_media": len(erros_de_data) - erros_baixa,
        "itens_baixa_confianca": sum(1 for e in todos_e if e["confianca"] == "baixa"),
    }


def descrever(linha) -> str:
    g, e = linha["g"], linha["e"]
    esq = f"{g['data'] or 'sem data'} | {g['descricao']}" if g else "-"
    dir_ = f"{e['data'] or 'sem data'} | {e['descricao']} (confiança {e['confianca']})" if e else "-"
    return f"gabarito: {esq}   ||   IA: {dir_}"


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Avalia a extração da IA contra o gabarito.")
    ap.add_argument("gabarito", nargs="?", default=str(GABARITO_PADRAO))
    ap.add_argument("extraido", nargs="?", default=str(EXTRAIDO_PADRAO))
    ap.add_argument("--disciplina", help="avalia só disciplinas cujo nome contenha este texto")
    ap.add_argument("--rotulo", default="", help="nome desta rodada (ex.: flash-v2), vai no relatório")
    args = ap.parse_args()

    for caminho in (args.gabarito, args.extraido):
        if not Path(caminho).exists():
            sys.exit(f"Arquivo não encontrado: {caminho}")

    G_all = preparar(carregar(Path(args.gabarito)))
    E_all = preparar(carregar(Path(args.extraido)))

    if args.disciplina:
        filtro = norm(args.disciplina)
        G_all = [x for x in G_all if filtro in norm(x["disciplina"])]
        E_all = [x for x in E_all if filtro in norm(x["disciplina"])]
    if not E_all:
        sys.exit("Nenhuma entrega na extração para avaliar.")

    mapa = mapear_disciplinas({x["disciplina"] for x in E_all}, {x["disciplina"] for x in G_all})
    sem_par = [n for n, alvo in mapa.items() if alvo is None]
    for nome in sem_par:
        print(f"Aviso: a disciplina '{nome}' não existe no gabarito; foi ignorada.")

    linhas = []
    for alvo in sorted({a for a in mapa.values() if a}):
        G = [x for x in G_all if x["disciplina"] == alvo]
        E = [x for x in E_all if mapa.get(x["disciplina"]) == alvo]
        print(f"\n=== {alvo} ===")
        parcial = casar(G, E)
        linhas += parcial
        for l in parcial:
            if l["status"] not in ("ok", "pendente_ok"):
                print(f"  [{l['status']}] {descrever(l)}")
        print(f"  {sum(1 for l in parcial if l['status'] in ('ok', 'pendente_ok'))} de {len(parcial)} linhas sem divergência")

    if not linhas:
        sys.exit("Nada para avaliar (nenhuma disciplina em comum).")

    m = calcular(linhas)
    print("\n--- Resumo ---")
    print(f"Datas do gabarito: {m['gabarito_com_data']} | extraídas pela IA: {m['extraidas_com_data']}")
    mostrar = lambda v: f"{v}%" if v is not None else "n/d"
    print(f"Datas corretas: {m['datas_corretas']} (cobertura {mostrar(m['recall_data_pct'])} | precisão {mostrar(m['precisao_data_pct'])})")
    print(f"Datas erradas: {m['datas_erradas']} | inventadas: {m['datas_inventadas']} | "
          f"faltando: {m['entregas_faltando']} | extras: {m['entregas_extras']} | tipo diferente: {m['tipo_diferente']}")
    print(f"Datas não extraídas (IA deixou sem data): {m['datas_nao_extraidas']} "
          f"({m['nao_extraidas_sinalizadas_baixa']} com confiança baixa)")
    print(f"Pendentes (sem data no gabarito) tratados corretamente: "
          f"{m['pendentes_tratados_corretamente']} de {m['gabarito_sem_data']}")
    print(f"Erros de data: {m['erros_de_data_total']} | sinalizados com confiança baixa: "
          f"{m['erros_sinalizados_baixa_confianca']} | passaram com confiança alta/média: "
          f"{m['erros_com_confianca_alta_ou_media']}")

    PASTA_SAIDA.mkdir(parents=True, exist_ok=True)
    nome = f"avaliacao_{norm(args.rotulo).replace(' ', '_')}.json" if args.rotulo else "avaliacao.json"
    destino = PASTA_SAIDA / nome
    relatorio = {"rotulo": args.rotulo, "metricas": m,
                 "divergencias": [{"status": l["status"], "gabarito": l["g"], "ia": l["e"]}
                                  for l in linhas if l["status"] not in ("ok", "pendente_ok")]}
    destino.write_text(json.dumps(relatorio, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Relatório salvo em {destino}")


if __name__ == "__main__":
    main()