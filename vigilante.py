"""
Vigilante de resultados - Fase 1 (avisos)

Que hace:
  1. Lee la lista de empresas de empresas.txt
  2. Pregunta a la SEC (web oficial de EE. UU.) si alguna ha presentado
     un formulario 8-K con el apartado 2.02 (= resultados trimestrales)
  3. Si hay uno nuevo, te avisa por Telegram con el enlace
  4. Guarda en estado.json lo que ya te ha avisado, para no repetir

Necesita 3 "secrets" en GitHub:
  TELEGRAM_TOKEN    -> el token de tu bot
  TELEGRAM_CHAT_ID  -> tu numero de chat
  SEC_USER_AGENT    -> un nombre y tu email, ej: "Vigilante tuemail@gmail.com"
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

ARCHIVO_EMPRESAS = "empresas.txt"
ARCHIVO_ESTADO = "estado.json"

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
SEC_USER_AGENT = os.environ.get("SEC_USER_AGENT", "").strip()


# ---------- Utilidades ----------

def descargar_json(url):
    """Descarga una direccion de la SEC respetando sus normas (identificarse y ir despacio)."""
    peticion = urllib.request.Request(url, headers={
        "User-Agent": SEC_USER_AGENT,
        "Accept-Encoding": "identity",
    })
    with urllib.request.urlopen(peticion, timeout=30) as respuesta:
        datos = json.loads(respuesta.read().decode("utf-8"))
    time.sleep(0.3)  # maximo ~3 peticiones por segundo (la SEC permite 10)
    return datos


def enviar_telegram(texto):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    datos = urllib.parse.urlencode({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": texto,
        "disable_web_page_preview": "true",
    }).encode("utf-8")
    with urllib.request.urlopen(url, data=datos, timeout=30) as respuesta:
        resultado = json.loads(respuesta.read().decode("utf-8"))
    if not resultado.get("ok"):
        raise RuntimeError(f"Telegram ha dado un error: {resultado}")


def leer_empresas():
    tickers = []
    with open(ARCHIVO_EMPRESAS, encoding="utf-8") as f:
        for linea in f:
            linea = linea.split("#")[0].strip().upper()
            if linea:
                tickers.append(linea)
    return tickers


def leer_estado():
    if not os.path.exists(ARCHIVO_ESTADO):
        return None  # primera vez
    with open(ARCHIVO_ESTADO, encoding="utf-8") as f:
        return json.load(f)


def guardar_estado(estado):
    with open(ARCHIVO_ESTADO, "w", encoding="utf-8") as f:
        json.dump(estado, f, indent=2, ensure_ascii=False, sort_keys=True)
        f.write("\n")


# ---------- Logica principal ----------

def buscar_resultados(cik):
    """Devuelve los 8-K con apartado 2.02 (resultados) de una empresa."""
    datos = descargar_json(f"https://data.sec.gov/submissions/CIK{cik:010d}.json")
    recientes = datos["filings"]["recent"]
    encontrados = []
    for i, formulario in enumerate(recientes["form"]):
        apartados = recientes["items"][i] or ""
        if formulario in ("8-K", "8-K/A") and "2.02" in apartados:
            numero = recientes["accessionNumber"][i]
            carpeta = numero.replace("-", "")
            documento = recientes["primaryDocument"][i]
            encontrados.append({
                "numero": numero,
                "fecha": recientes["acceptanceDateTime"][i],
                "enlace": f"https://www.sec.gov/Archives/edgar/data/{cik}/{carpeta}/{documento}",
            })
    return encontrados


def main():
    faltan = [n for n, v in [("TELEGRAM_TOKEN", TELEGRAM_TOKEN),
                             ("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID),
                             ("SEC_USER_AGENT", SEC_USER_AGENT)] if not v]
    if faltan:
        sys.exit(f"Faltan estos secrets en GitHub: {', '.join(faltan)}")
    if "@" not in SEC_USER_AGENT:
        sys.exit("SEC_USER_AGENT debe incluir un nombre y tu email, ej: 'Vigilante tuemail@gmail.com'")

    tickers = leer_empresas()
    estado = leer_estado()
    primera_vez = estado is None
    if primera_vez:
        estado = {"avisados": [], "ultima_revision": ""}
    ya_avisados = set(estado["avisados"])

    # Traducir tickers (AMZN) al numero que usa la SEC (CIK)
    tabla = descargar_json("https://www.sec.gov/files/company_tickers.json")
    ticker_a_empresa = {e["ticker"].upper(): e for e in tabla.values()}

    nuevos = 0
    no_encontrados = []
    for ticker in tickers:
        empresa = ticker_a_empresa.get(ticker)
        if not empresa:
            no_encontrados.append(ticker)
            continue
        nombre = empresa["title"]
        for r in buscar_resultados(int(empresa["cik_str"])):
            if r["numero"] in ya_avisados:
                continue
            ya_avisados.add(r["numero"])
            if primera_vez:
                continue  # la primera vez solo apunta lo antiguo, no avisa
            texto = (
                f"📢 {nombre} ({ticker}) ha publicado resultados\n\n"
                f"Hora SEC: {r['fecha']}\n"
                f"Documento: {r['enlace']}"
            )
            enviar_telegram(texto)
            nuevos += 1

    if primera_vez:
        mensaje = ("✅ Vigilante activado. Vigilo: " + ", ".join(tickers) +
                   ". Te avisaré cuando alguna publique resultados.")
        if no_encontrados:
            mensaje += "\n⚠️ No encuentro en la SEC: " + ", ".join(no_encontrados)
        enviar_telegram(mensaje)

    estado["avisados"] = sorted(ya_avisados)
    # Solo la fecha (no la hora): asi se guarda un cambio al dia como maximo
    estado["ultima_revision"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    guardar_estado(estado)
    print(f"Revisado. Avisos nuevos: {nuevos}. No encontrados: {no_encontrados or 'ninguno'}")


if __name__ == "__main__":
    main()
