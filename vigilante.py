"""
Vigilante de resultados - Fase 2 (avisos + resumen con IA)

Que hace:
  1. Lee la lista de empresas de empresas.txt
  2. Pregunta a la SEC (web oficial de EE. UU.) si alguna ha presentado
     un formulario 8-K con el apartado 2.02 (= resultados trimestrales)
  3. Si hay uno nuevo, descarga la nota de prensa de resultados
  4. Pide a Gemini (IA de Google) un resumen sencillo
  5. Te lo manda por Telegram (si la IA falla, te manda al menos el enlace)
  6. Guarda en estado.json lo que ya te ha avisado, para no repetir

Secrets necesarios en GitHub:
  TELEGRAM_TOKEN    -> el token de tu bot
  TELEGRAM_CHAT_ID  -> tu numero de chat
  SEC_USER_AGENT    -> un nombre y tu email, ej: "Vigilante tuemail@gmail.com"
  GEMINI_API_KEY    -> tu clave de Google AI Studio (opcional: sin ella solo manda el enlace)

Modo prueba (PRUEBA=true): resume los ultimos resultados de la primera empresa
de la lista y te los manda, sin tocar la memoria. Sirve para ver que todo funciona.
"""

import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser

ARCHIVO_EMPRESAS = "empresas.txt"
ARCHIVO_ESTADO = "estado.json"

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
SEC_USER_AGENT = os.environ.get("SEC_USER_AGENT", "").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "").strip()  # opcional: forzar un modelo
PRUEBA = os.environ.get("PRUEBA", "").strip().lower() == "true"

MAX_TEXTO_IA = 60000      # caracteres del documento que se mandan a la IA
MAX_TELEGRAM = 4000       # Telegram admite hasta 4096 por mensaje

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"


# ---------- Descargas ----------

def descargar(url, como_json=True):
    """Descarga de la SEC respetando sus normas (identificarse y ir despacio)."""
    peticion = urllib.request.Request(url, headers={
        "User-Agent": SEC_USER_AGENT,
        "Accept-Encoding": "identity",
    })
    with urllib.request.urlopen(peticion, timeout=30) as respuesta:
        contenido = respuesta.read().decode("utf-8", errors="replace")
    time.sleep(0.3)  # maximo ~3 peticiones por segundo (la SEC permite 10)
    return json.loads(contenido) if como_json else contenido


def descargar_json(url):
    return descargar(url, como_json=True)


class _ExtractorTexto(HTMLParser):
    """Convierte una pagina HTML en texto plano legible."""
    BLOQUES = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "table"}

    def __init__(self):
        super().__init__()
        self.partes = []
        self._saltar = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._saltar += 1
        elif tag in self.BLOQUES:
            self.partes.append("\n")
        elif tag in ("td", "th"):
            self.partes.append(" | ")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._saltar:
            self._saltar -= 1

    def handle_data(self, data):
        if not self._saltar:
            self.partes.append(data)


def html_a_texto(contenido):
    extractor = _ExtractorTexto()
    extractor.feed(contenido)
    texto = html.unescape("".join(extractor.partes))
    texto = re.sub(r"[ \t\xa0]+", " ", texto)
    texto = re.sub(r"\n\s*\n+", "\n", texto)
    return texto.strip()


def texto_nota_de_prensa(cik, carpeta, documento_principal):
    """Busca la nota de prensa (anexo 99.1) dentro del 8-K. Si no la encuentra, usa el 8-K."""
    base = f"https://www.sec.gov/Archives/edgar/data/{cik}/{carpeta}"
    elegido = documento_principal
    try:
        indice = descargar_json(f"{base}/index.json")
        nombres = [f["name"] for f in indice.get("directory", {}).get("item", [])]
        anexos = [n for n in nombres
                  if re.search(r"ex[-_]?99", n, re.I) and n.lower().endswith((".htm", ".html", ".txt"))]
        if anexos:
            elegido = sorted(anexos)[0]  # normalmente ex99-1 va primero
    except Exception as e:
        print(f"Aviso: no pude leer el indice del 8-K ({e}); uso el documento principal")
    contenido = descargar(f"{base}/{elegido}", como_json=False)
    return html_a_texto(contenido), f"{base}/{elegido}"


# ---------- Telegram ----------

def enviar_telegram(texto):
    if len(texto) > MAX_TELEGRAM:
        texto = texto[:MAX_TELEGRAM - 20] + "\n…(recortado)"
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


# ---------- Gemini ----------

def _gemini_peticion(url, cuerpo=None):
    datos = json.dumps(cuerpo).encode("utf-8") if cuerpo is not None else None
    peticion = urllib.request.Request(url, data=datos, headers={
        "x-goog-api-key": GEMINI_API_KEY,
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(peticion, timeout=120) as respuesta:
        return json.loads(respuesta.read().decode("utf-8"))


def modelos_candidatos():
    """Elige modelos 'flash' disponibles con tu clave (los nombres cambian con el tiempo)."""
    if GEMINI_MODEL:
        return [GEMINI_MODEL]
    nombres = []
    try:
        url = f"{GEMINI_BASE}/models?pageSize=200"
        datos = _gemini_peticion(url)
        for m in datos.get("models", []):
            nombre = m.get("name", "").replace("models/", "")
            metodos = m.get("supportedGenerationMethods", [])
            if "generateContent" not in metodos or "flash" not in nombre:
                continue
            if re.search(r"image|tts|audio|live|embed|vision", nombre):
                continue
            nombres.append(nombre)
    except Exception as e:
        print(f"Aviso: no pude listar modelos de Gemini ({e})")

    def prioridad(n):
        # 1) alias 'latest'  2) estables  3) lite  4) preview/experimental; dentro, version mas alta primero
        version = re.findall(r"(\d+(?:\.\d+)?)", n)
        v = float(version[0]) if version else 0.0
        return (
            0 if "latest" in n else 1,
            1 if ("preview" in n or "exp" in n) else 0,
            1 if "lite" in n else 0,
            -v,
        )

    nombres = sorted(set(nombres), key=prioridad)
    # Respaldo por si el listado falla
    for respaldo in ("gemini-flash-latest", "gemini-2.5-flash", "gemini-2.5-flash-lite"):
        if respaldo not in nombres:
            nombres.append(respaldo)
    return nombres[:5]


INSTRUCCIONES = """Eres un analista que explica resultados empresariales a un inversor principiante que habla español.
Lee la nota de prensa de resultados de {empresa} ({ticker}) y escribe un resumen en ESPAÑOL, en texto plano
(sin asteriscos, sin almohadillas, sin formato markdown), con este formato exacto:

📊 Lo más importante: (1-2 frases sencillas)

💰 Cifras clave:
- Ingresos: (cifra y cambio frente al mismo trimestre del año anterior)
- Beneficio neto y beneficio por acción (BPA): (cifras y cambio)
- Otra cifra relevante para este negocio: (por ejemplo márgenes, flujo de caja, suscriptores, un segmento)

🔮 Previsiones de la empresa (guidance): (qué espera para el próximo trimestre o año; si no lo dice, escribe "No aparecen en la nota")

✅ Puntos positivos: (1-2 frases)
⚠️ Puntos negativos o riesgos: (1-2 frases)

🧭 Para tener en cuenta: (1 frase; explica cualquier palabra técnica que hayas usado)

Reglas obligatorias:
- Usa SOLO cifras que aparezcan en el documento. Si un dato no aparece, escribe "no aparece en la nota". Nunca inventes.
- La nota no incluye lo que esperaban los analistas: no digas si "superó" o "decepcionó" a las expectativas del mercado.
- No recomiendes comprar, vender ni mantener. No predigas el precio de la acción.
- Máximo 1.500 caracteres en total.

DOCUMENTO:
{texto}"""


def resumir_con_ia(empresa, ticker, texto):
    """Devuelve (resumen, modelo_usado). Lanza error si ningun modelo funciona."""
    prompt = INSTRUCCIONES.format(empresa=empresa, ticker=ticker, texto=texto[:MAX_TEXTO_IA])
    cuerpo = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 4096},
    }
    errores = []
    for modelo in modelos_candidatos():
        try:
            datos = _gemini_peticion(f"{GEMINI_BASE}/models/{modelo}:generateContent", cuerpo)
            partes = datos.get("candidates", [{}])[0].get("content", {}).get("parts", [])
            resumen = "".join(p.get("text", "") for p in partes if not p.get("thought")).strip()
            if resumen:
                resumen = re.sub(r"\*\*|__|^#+\s*", "", resumen, flags=re.M)  # quitar markdown
                return resumen, modelo
            errores.append(f"{modelo}: respuesta vacia")
        except urllib.error.HTTPError as e:
            detalle = e.read().decode("utf-8", errors="replace")[:200]
            errores.append(f"{modelo}: HTTP {e.code} {detalle}")
        except Exception as e:
            errores.append(f"{modelo}: {e}")
    raise RuntimeError(" / ".join(errores))


# ---------- Memoria ----------

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
    """Devuelve los 8-K con apartado 2.02 (resultados) de una empresa, del mas nuevo al mas antiguo."""
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
                "carpeta": carpeta,
                "documento": documento,
                "enlace": f"https://www.sec.gov/Archives/edgar/data/{cik}/{carpeta}/{documento}",
            })
    return encontrados


def preparar_mensaje(nombre, ticker, cik, r):
    """Construye el mensaje de Telegram: resumen de IA si se puede, si no solo el enlace."""
    cabecera = f"📢 {nombre} ({ticker}) ha publicado resultados\nHora SEC: {r['fecha']}\n"
    enlace = r["enlace"]
    if not GEMINI_API_KEY:
        return cabecera + f"\nDocumento: {enlace}"
    try:
        texto, enlace = texto_nota_de_prensa(cik, r["carpeta"], r["documento"])
        resumen, modelo = resumir_con_ia(nombre, ticker, texto)
        return (cabecera + "\n" + resumen +
                f"\n\n📄 Nota original: {enlace}"
                f"\n🤖 Resumen hecho por IA ({modelo}). Puede contener errores: comprueba las cifras."
                f" No es asesoramiento financiero.")
    except Exception as e:
        print(f"Aviso: no se pudo hacer el resumen con IA: {e}")
        return (cabecera + f"\nDocumento: {enlace}"
                f"\n\n(No se pudo generar el resumen con IA esta vez.)")


def main():
    faltan = [n for n, v in [("TELEGRAM_TOKEN", TELEGRAM_TOKEN),
                             ("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID),
                             ("SEC_USER_AGENT", SEC_USER_AGENT)] if not v]
    if faltan:
        sys.exit(f"Faltan estos secrets en GitHub: {', '.join(faltan)}")
    if "@" not in SEC_USER_AGENT:
        sys.exit("SEC_USER_AGENT debe incluir un nombre y tu email, ej: 'Vigilante tuemail@gmail.com'")

    tickers = leer_empresas()

    # Traducir tickers (AMZN) al numero que usa la SEC (CIK)
    tabla = descargar_json("https://www.sec.gov/files/company_tickers.json")
    ticker_a_empresa = {e["ticker"].upper(): e for e in tabla.values()}

    if PRUEBA:
        for ticker in tickers:
            empresa = ticker_a_empresa.get(ticker)
            if not empresa:
                continue
            cik = int(empresa["cik_str"])
            resultados = buscar_resultados(cik)
            if resultados:
                mensaje = preparar_mensaje(empresa["title"], ticker, cik, resultados[0])
                enviar_telegram("🧪 PRUEBA (resultados más recientes)\n\n" + mensaje)
                print("Prueba enviada.")
                return
        sys.exit("Prueba: no encontre resultados para ninguna empresa de la lista.")

    estado = leer_estado()
    primera_vez = estado is None
    if primera_vez:
        estado = {"avisados": [], "ultima_revision": ""}
    ya_avisados = set(estado["avisados"])

    nuevos = 0
    no_encontrados = []
    for ticker in tickers:
        empresa = ticker_a_empresa.get(ticker)
        if not empresa:
            no_encontrados.append(ticker)
            continue
        nombre = empresa["title"]
        cik = int(empresa["cik_str"])
        for r in buscar_resultados(cik):
            if r["numero"] in ya_avisados:
                continue
            if not primera_vez:  # la primera vez solo apunta lo antiguo, no avisa
                enviar_telegram(preparar_mensaje(nombre, ticker, cik, r))
                nuevos += 1
            ya_avisados.add(r["numero"])

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
