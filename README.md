Calendário de Entregas com IA

Automação que lê os planos de ensino de um semestre (PDF ou imagem), extrai provas, trabalhos e entregas com um modelo de linguagem, valida os dados e cria os eventos com lembretes no Google Agenda.

O problema

No começo de cada semestre, as datas importantes chegam espalhadas em vários planos de ensino, cada um com um formato. Reunir tudo num calendário é um trabalho manual, repetitivo e sujeito a erro (datas ambíguas, entregas sem data definida, duas entregas no mesmo dia).

Como funciona
planos de ensino (PDF/PNG)
        │
        ▼
  extrair.py      LLM lê o documento e devolve entregas em JSON
        │
        ▼
  validar.py      confere datas, separa pendentes, marca o que já passou
        │
        ▼
  calendario.py   cria/atualiza eventos no Google Agenda (com lembretes)
  
Extração (src/extrair.py): envia cada arquivo de data/inbox/ ao modelo, com um prompt versionado (prompts/). A resposta passa por limpeza e normalização: método restrito a uma lista, datas em AAAA-MM-DD, e confiança rebaixada para baixa quando algo é duvidoso. Em caso de resposta mal formada ou limite de uso, tenta de novo.
Validação (src/validar.py): converte as datas, rejeita datas inexistentes (como 31/09), separa as entregas sem data em uma lista de pendentes (com o motivo) e ordena por data.
Calendário (src/calendario.py): cria eventos de dia inteiro na agenda "Faculdade", com avisos 7, 2 e 1 dia antes, às 9h. Cada evento tem um ID calculado a partir de disciplina e descrição, então rodar de novo atualiza em vez de duplicar.
Decisões de projeto
Nunca inventar data. Datas relativas, "a combinar" ou com duas opções (ex.: "23/11 ou 30/11") ficam como null, com confiança baixa e uma observação, e vão para os pendentes em vez de serem chutadas.
Gabarito separado da extração. data/raw/entregas_raw.json é a base conferida à mão; a saída da IA vai para data/raw/entregas_extraidas.json, e os dois podem ser comparados.
Provedor de IA isolado em uma função. Só chamar_llm() em extrair.py conhece o modelo usado; trocar de provedor significa reescrever essa função.
Validação independente do modelo. O prompt reduz erros, mas a validação em código é quem protege o calendário do pior caso.
Estrutura
src/
  extrair.py       extração com LLM
  validar.py       validação e separação de pendentes
  calendario.py    integração com Google Calendar
prompts/
  extracao_v2.txt  prompt de extração (versionado)
data/
  inbox/           planos de ensino (não versionados)
  raw/             gabarito e saída da extração
  processed/       entregas validadas
Instalação

Requisitos: Python 3.10+.

powershell
python -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
Configuração
Chave do modelo. Copie .env.example para .env e preencha GEMINI_API_KEY (chave gratuita do Google AI Studio). Ajuste MODELO_LLM para um modelo Flash disponível.
Google Calendar. No Google Cloud, ative a Google Calendar API, crie um ID de cliente OAuth do tipo App para computador, baixe o JSON como credentials.json na raiz do projeto e adicione seu e-mail como usuário de teste. Na primeira execução, o navegador pede a autorização e gera o token.json.

.env, credentials.json e token.json estão no .gitignore e nunca devem ser versionados.

Uso
powershell
# 1. coloque os planos de ensino (PDF/PNG) em data/inbox/
python src/extrair.py                                   # gera data/raw/entregas_extraidas.json

# 2. valide (use o gabarito ou a extração)
python src/validar.py data/raw/entregas_extraidas.json  # gera data/processed/entregas.json

# 3. envie ao Google Agenda
python src/calendario.py

Revise os itens de confiança baixa e os pendentes antes do passo 3.

Status
 Validação de datas e separação de pendentes
 Integração com Google Calendar (sem duplicar eventos)
 Extração com LLM, saída estruturada e novas tentativas
 Avaliação da extração contra o gabarito (avaliar.py), comparando versões do prompt
 Revisão interativa dos itens duvidosos antes de enviar ao calendário
 Comando único que roda o fluxo inteiro
 Agente com chamada de ferramentas para resolver pendentes e conflitos
 Testes automatizados do validar.py

Avaliação (gemini-3.6-flash, prompt v2), 2 planos reais:
- Desenvolvimento Sustentável: 6 de 6 datas corretas.
- PDS: 3 de 3 entregas sem data tratadas como null; 2 datas não extraídas,
  ambas sinalizadas com confiança baixa.
- Total: cobertura 75%, precisão 100%, 0 datas erradas ou inventadas.
Teste sintético (datas ambíguas): 3 de 3 datas, 4 de 4 pendentes, 0 extras.

Limitações conhecidas
A extração depende da qualidade do documento (PDFs escaneados ruins reduzem a precisão).
O nível gratuito da API tem limites de uso e, em geral, o conteúdo enviado pode ser usado pelo provedor para melhorar seus produtos: não envie documentos pessoais.
Se a descrição de uma entrega mudar, o ID do evento muda e o antigo precisa ser apagado à mão.