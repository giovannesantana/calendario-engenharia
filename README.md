# Calendário de Entregas com IA

Agente que lê os planos de ensino de um semestre (PDF ou imagem), extrai provas, trabalhos e entregas com um modelo de linguagem, **pergunta ao usuário o que ficou ambíguo** e cria os eventos com lembretes no Google Agenda.

## O problema

No começo do semestre, as datas chegam espalhadas em vários planos de ensino, cada um com um formato. Reunir tudo num calendário é manual, repetitivo e sujeito a erro: datas escritas sem ano ("21/set"), entregas sem data definida ("a combinar"), datas com duas opções ("23 ou 30/nov") e várias entregas no mesmo dia.

## Como funciona

```
planos de ensino (PDF/PNG)
        │
        ▼
  extrair.py        LLM lê o documento e devolve as entregas em JSON
        │           (limpeza, normalização, cache, novas tentativas)
        ▼
  agente.py         o modelo decide a ordem das ações chamando ferramentas
        │             ├─ pendentes → pergunta ao usuário → registra a data
        │             └─ resumo do calendário (avisa sobre conflitos de data)
        ▼
  confirmação       o usuário aprova no terminal
        │
        ▼
  calendario.py     cria/atualiza os eventos no Google Agenda (sem duplicar)
```

O mesmo fluxo existe em modo manual, passo a passo: `extrair.py` → `validar.py` → `calendario.py`.

## O agente e suas travas

O agente (`src/agente.py`) usa *function calling* do Gemini com 8 ferramentas (`src/ferramentas.py`): listar arquivos, extrair entregas, listar pendentes, perguntar ao usuário, registrar data, ignorar entrega, resumir o calendário e criar eventos. O modelo escolhe a ordem; as proteções ficam **no código**, não só no prompt:

- **`registrar_data` só aceita uma data que o usuário escreveu.** Se o modelo tentar gravar uma data que o usuário não informou, a ferramenta recusa e devolve um erro.
- **`criar_eventos_no_calendario` sempre pede confirmação** (`[s/N]`) antes de enviar qualquer coisa ao Google Agenda.
- **Textos dos documentos são tratados como dados**, não como instruções (mitigação de *prompt injection* a partir dos PDFs).
- **Auditoria:** cada chamada de ferramenta é registrada em `data/processed/agente_log.jsonl`.

## Decisões de projeto

- **Nunca inventar data.** Datas relativas, "a combinar" ou com duas opções viram `null` com confiança baixa e vão para a lista de pendentes, em vez de serem chutadas.
- **Gabarito separado da extração.** `data/raw/entregas_raw.json` é a base de comparação; a saída da IA vai para outro arquivo, e o `avaliar.py` mede a diferença.
- **Provedor de IA isolado em uma função** (`chamar_llm`). Trocar de modelo ou de provedor significa reescrever só essa função.
- **Validação independente do modelo.** O prompt reduz erros; a validação em código protege o calendário do pior caso.
- **Idempotência.** Cada evento tem um ID calculado a partir de disciplina e descrição, então rodar de novo atualiza em vez de duplicar.
- **Resiliência à cota e à rede.** Cache das respostas (não gasta cota ao repetir), novas tentativas com espera crescente em erros 503, parada imediata em erros que não se resolvem esperando (modelo inexistente, chave inválida) e aviso claro quando a cota diária acaba.

## Avaliação

Modelo: `gemini-3.6-flash`. Métricas do `src/avaliar.py`, que compara a extração com um gabarito.

| Conjunto | Prompt v2 | Prompt v3 |
|---|---|---|
| Plano real: Desenvolvimento Sustentável (6 datas) | 6 de 6 | 6 de 6 |
| Plano real: PDS (2 datas + 3 pendentes) | **0 de 2** datas; pendentes 3 de 3 | **2 de 2** datas; pendentes 3 de 3 |
| Teste sintético (3 datas, 4 pendentes, 3 datas que não são entregas) | 3 de 3; pendentes 4 de 4; 0 extras | 3 de 3; pendentes 4 de 4; 0 extras |

Em todos os conjuntos: **0 datas erradas, 0 datas inventadas e 0 erros com confiança alta.**

**Um erro encontrado e corrigido.** A v2 deixou sem data a 1ª avaliação e a recuperação do PDS, escritas no slide como "21/set" e "14/dez" (sem ano, mês abreviado). Todas as omissões vieram com confiança baixa, ou seja, o erro era visível. A v3 passou a informar o ano letivo ao modelo e a tratar essas datas como completas; o teste de regressão nos outros dois conjuntos não mostrou piora.

## Limitações

- **Amostra pequena:** 2 planos reais e 1 documento sintético.
- **A v3 foi ajustada depois de ver o erro, no mesmo documento em que foi medida.** Falta medir em planos que ela nunca viu.
- **O gabarito foi gerado por um modelo no chat e conferido manualmente**, não é uma fonte independente.
- **O agente foi testado com o modelo real em poucas sessões**, sem uma avaliação sistemática da qualidade das perguntas e da ordem das ações.
- **Plano gratuito da API:** limites diários baixos e sobrecargas (503) frequentes; em geral o conteúdo enviado pode ser usado pelo provedor, então não envie documentos pessoais.
- Se a descrição de uma entrega mudar, o ID do evento muda e o antigo precisa ser apagado à mão.

## Estrutura

```
src/
  extrair.py       extração com LLM (cache, novas tentativas, normalização)
  validar.py       validação de datas e separação de pendentes
  calendario.py    integração com o Google Calendar
  ferramentas.py   ferramentas do agente, com as travas de segurança
  agente.py        agente com function calling
  avaliar.py       métricas contra o gabarito
prompts/           prompts versionados (v2, v3)
data/
  inbox/           planos de ensino (não versionados)
  raw/             gabarito e saídas da extração
  processed/       resultados validados e relatórios
  teste/           documento sintético e gabarito para testes
```

## Instalação

Requisitos: Python 3.10+.

```powershell
python -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
```

### Configuração

1. **Chave do modelo.** Copie `.env.example` para `.env` e preencha `GEMINI_API_KEY` (chave gratuita do Google AI Studio). Ajuste `MODELO_LLM` para um modelo Flash disponível; `MODELO_AGENTE` é opcional e usa outro modelo (e outra cota) para o agente.
2. **Google Calendar.** No Google Cloud, ative a Google Calendar API, crie um ID de cliente OAuth do tipo *App para computador*, baixe o JSON como `credentials.json` na raiz e adicione seu e-mail como usuário de teste. Na primeira execução, o navegador pede a autorização e gera o `token.json`.

> `.env`, `credentials.json` e `token.json` estão no `.gitignore` e nunca devem ser versionados.

## Uso

**Com o agente:**

```powershell
# coloque os planos (PDF/PNG) em data/inbox/
python src/agente.py              # fluxo completo, com perguntas e confirmação
python src/agente.py --continuar  # retoma a sessão anterior
```

**Passo a passo:**

```powershell
python src/extrair.py                                  # gera data/raw/entregas_extraidas.json
python src/validar.py data/raw/entregas_extraidas.json # gera data/processed/entregas.json
python src/calendario.py                               # envia ao Google Agenda
```

**Avaliar a extração:**

```powershell
python src/avaliar.py --disciplina "nome da disciplina" --rotulo prompt-v3
```

## Status

- [x] Extração com LLM, cache e tratamento de erros de cota e de rede
- [x] Validação de datas e separação de pendentes
- [x] Integração com Google Calendar sem duplicar eventos
- [x] Avaliação contra gabarito e comparação entre versões do prompt
- [x] Agente com function calling, perguntas ao usuário e confirmação antes de enviar
- [ ] Testes automatizados (`validar.py`, `ferramentas.py`)
- [ ] Avaliar o prompt em planos novos (generalização)
- [ ] Avaliação sistemática do comportamento do agente