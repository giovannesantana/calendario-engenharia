"""calendario.py - envia as entregas validadas (data/processed/entregas.json) ao Google Agenda.

Uso:
    python src/calendario.py

Na primeira vez abre o navegador para você autorizar o acesso (gera o token.json).
Rodar de novo é seguro: cada evento tem um ID fixo, então ele ATUALIZA em vez de duplicar.
"""
import hashlib
import json
import sys
from datetime import date, timedelta
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

RAIZ = Path(__file__).resolve().parent.parent
ENTREGAS = RAIZ / "data" / "processed" / "entregas.json"
CREDENCIAIS = RAIZ / "credentials.json"
TOKEN = RAIZ / "token.json"

SCOPES = ["https://www.googleapis.com/auth/calendar"]
NOME_AGENDA = "Faculdade"
DIAS_AVISO = [7, 2, 1]  # lembretes: 7, 2 e 1 dia antes
HORA_AVISO = 9          # o aviso toca às 9h


def autenticar():
    creds = None
    if TOKEN.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)

    if creds and not creds.valid and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError as erro:
            # token expirado (modo de teste do Google vence em ~7 dias) ou gerado com outro segredo
            print(f"Autorização anterior inválida ou expirada ({erro}).")
            print("Abrindo o navegador para autorizar de novo...")
            creds = None

    if not creds or not creds.valid:
        if not CREDENCIAIS.exists():
            sys.exit(f"Arquivo não encontrado: {CREDENCIAIS}\n"
                     "Baixe o credentials.json no Google Cloud e coloque na raiz do projeto.")
        flow = InstalledAppFlow.from_client_secrets_file(str(CREDENCIAIS), SCOPES)
        creds = flow.run_local_server(port=0)

    TOKEN.write_text(creds.to_json())
    return build("calendar", "v3", credentials=creds)


def obter_agenda(service) -> str:
    """Devolve o ID da agenda 'Faculdade', criando-a se não existir."""
    pagina = None
    while True:
        resp = service.calendarList().list(pageToken=pagina).execute()
        for cal in resp["items"]:
            if cal["summary"] == NOME_AGENDA:
                return cal["id"]
        pagina = resp.get("nextPageToken")
        if not pagina:
            break
    nova = service.calendars().insert(
        body={"summary": NOME_AGENDA, "timeZone": "America/Sao_Paulo"}
    ).execute()
    return nova["id"]


def id_evento(item: dict) -> str:
    # ID fixo por disciplina + descrição: rodar de novo atualiza, não duplica.
    # Hexadecimal (0-9, a-f) é aceito pela API como ID de evento.
    chave = f"{item['disciplina']}|{item['descricao']}"
    return hashlib.sha1(chave.encode("utf-8")).hexdigest()


def montar_evento(item: dict) -> dict:
    dia = date.fromisoformat(item["data"])
    # Em evento de dia inteiro, "minutos antes" conta a partir de 00:00 do dia.
    # Esta conta faz o aviso tocar às HORA_AVISO horas, N dias antes.
    lembretes = [
        {"method": "popup", "minutes": n * 1440 - HORA_AVISO * 60}
        for n in DIAS_AVISO
    ]
    return {
        "id": id_evento(item),
        "summary": f"[{item['metodo'].upper()}] {item['disciplina']} - {item['descricao']}",
        "start": {"date": dia.isoformat()},
        "end": {"date": (dia + timedelta(days=1)).isoformat()},
        "reminders": {"useDefault": False, "overrides": lembretes},
        "status": "confirmed",
    }


def enviar(itens: list) -> dict:
    """Cria ou atualiza no Google Agenda a lista de entregas já validadas (com data ISO)."""
    service = autenticar()
    agenda = obter_agenda(service)
    criados = atualizados = 0

    for item in itens:
        evento = montar_evento(item)
        try:
            service.events().insert(calendarId=agenda, body=evento).execute()
            criados += 1
        except HttpError as erro:
            if erro.resp.status == 409:  # já existe: atualiza
                service.events().update(
                    calendarId=agenda, eventId=evento["id"], body=evento
                ).execute()
                atualizados += 1
            else:
                raise
    return {"criados": criados, "atualizados": atualizados}


def sincronizar():
    if not ENTREGAS.exists():
        sys.exit(f"Não encontrei {ENTREGAS}. Rode antes: python src/validar.py")

    dados = json.loads(ENTREGAS.read_text(encoding="utf-8"))
    futuras = [e for e in dados["entregas"] if not e["passou"]]

    resultado = enviar(futuras)
    print(f"{resultado['criados']} criados, {resultado['atualizados']} atualizados")
    print(f"{len(dados['pendentes'])} pendentes ficaram de fora (sem data)")


if __name__ == "__main__":
    sincronizar()