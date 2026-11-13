import asyncio
import hashlib
import hmac
import html
import json
import math
import os
import secrets
import ssl
import time
import re

from collections import deque
from datetime import datetime
from urllib.parse import urlsplit

from aiohttp import ClientError, ClientSession, ClientTimeout, web


INGRESS_IP = "172.30.32.2"

CERT_FILE = "/ssl/test_hinojo_fullchain.pem"
KEY_FILE = "/ssl/test_hinojo_privkey.pem"



COOKIE_NAME = "__Host-portail-session"

SESSION_SECONDS = 8 * 60 * 60
FORM_SECONDS = 10 * 60

MAX_SESSIONS = 64
MAX_PENDING = 128

LOGIN_WINDOW = 10 * 60
LOGIN_LIMIT = 20

# Mémoire uniquement : un redémarrage invalide tout.
sessions = {}
pending = {}
attempts = deque()


# ---------- Configuration ----------

def normalize_origin(value):
    if not isinstance(value, str):
        raise ValueError("Adresse HTTPS invalide.")

    parsed = urlsplit(value)

    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Adresse HTTPS sans chemin requise.")

    port = parsed.port
    if port is None:
        port = 443

    if not 1 <= port <= 65535:
        raise ValueError("Port HTTPS invalide.")

    return ("https", parsed.hostname.lower(), port)


def load_settings():
    with open("/data/options.json", encoding="utf-8") as file:
        options = json.load(file)

    address = options.get("portal_url", "")
    code = options.get("guest_code", "")
    expiry = options.get("expires_at", "")

    if not isinstance(address, str) or not isinstance(expiry, str):
        raise ValueError("portal_url et expires_at doivent être du texte.")

    address = address.strip().rstrip("/")
    origin = normalize_origin(address)

    if not isinstance(code, str) or not 16 <= len(code) <= 128:
        raise ValueError(
            "guest_code doit contenir entre 16 et 128 caractères."
        )

    end = datetime.fromisoformat(
        expiry.strip().replace("Z", "+00:00")
    )

    if end.tzinfo is None or end.utcoffset() is None:
        raise ValueError(
            "expires_at doit inclure un fuseau, par exemple +02:00."
        )
    entity = options.get(
        "consumption_entity",
        "sensor.conso_hinojo45_consototale",
    )
    energy_attribute = options.get(
        "energy_attribute",
        "total_consumption",
    )

    if (
        not isinstance(entity, str)
        or not re.fullmatch(r"sensor\.[a-z0-9_]+", entity)
    ):
        raise ValueError(
            "consumption_entity doit être un identifiant sensor valide."
        )

    if (
        not isinstance(energy_attribute, str)
        or not re.fullmatch(r"[a-z][a-z0-9_]*", energy_attribute)
    ):
        raise ValueError("energy_attribute est invalide.")

    temperature_entity = options.get("temperature_entity", "sensor.termostatoz1_local_temperature")
    if not isinstance(temperature_entity, str) or not re.fullmatch(r"sensor\.[a-z0-9_]+", temperature_entity):
        raise ValueError("temperature_entity doit être un identifiant sensor valide.")
    guest_name = options.get("guest_name", "")
    if (
        not isinstance(guest_name, str)
        or len(guest_name) > 80
        or any(ord(character) < 32 or ord(character) == 127 for character in guest_name)
    ):
        raise ValueError("guest_name doit contenir au maximum 80 caractères sans caractère de contrôle.")
    guest_name = guest_name.strip()

    outdoor_entity = options.get(
        "outdoor_temperature_entity",
        "sensor.hinojo_45_temperature_exterieure",
    )
    if not isinstance(outdoor_entity, str) or not re.fullmatch(r"sensor\.[a-z0-9_]+", outdoor_entity):
        raise ValueError("outdoor_temperature_entity doit être un identifiant sensor valide.")

    phone = options.get("contact_phone", "").strip()
    if phone and not re.fullmatch(r"\+[1-9][0-9]{6,14}", phone):
        raise ValueError("contact_phone doit être au format international.")

    return {
        "outdoor_temperature_url": "http://supervisor/core/api/states/" + outdoor_entity,
        "temperature_url": "http://supervisor/core/api/states/" + temperature_entity,
        "door_code": options.get("door_code", ""),
        "contact_phone": phone,
        "guest_name": guest_name,
        "address": address,
        "origin": origin,
        "code_hash": hashlib.sha256(code.encode("utf-8")).digest(),
        "expires": end.timestamp(),
        "consumption_url": (
            "http://supervisor/core/api/states/" + entity
        ),
        "energy_attribute": energy_attribute,
    }


# ---------- Réponses et cookies ----------

LABELS = {
    "fr": {
        "welcome": "Bienvenue",
        "code": "Code du séjour",
        "login": "Se connecter",
        "show": "Afficher le code",
        "hide": "Masquer le code",
        "door": "Porte du deuxième étage",
        "door_help": "Saisissez le code, puis appuyez sur le bouton Yale.",
        "reveal": "Afficher le code de la porte",
        "temp": "Température intérieure",
        "unavailable": "Indisponible",
        "contact": "Contacter Jean Luc",
        "logout": "Se déconnecter"
    },
    "en": {
        "welcome": "Welcome",
        "code": "Stay code",
        "login": "Sign in",
        "show": "Show code",
        "hide": "Hide code",
        "door": "Second-floor door",
        "door_help": "Enter the code, then press the Yale button.",
        "reveal": "Show door code",
        "temp": "Indoor temperature",
        "unavailable": "Unavailable",
        "contact": "Contact Jean Luc",
        "logout": "Sign out"
    },
    "nl": {
        "welcome": "Welkom",
        "code": "Verblijfscode",
        "login": "Inloggen",
        "show": "Code tonen",
        "hide": "Code verbergen",
        "door": "Deur op de tweede verdieping",
        "door_help": "Voer de code in en druk daarna op de Yale-knop.",
        "reveal": "Deurcode tonen",
        "temp": "Binnentemperatuur",
        "unavailable": "Niet beschikbaar",
        "contact": "Contact met Jean Luc",
        "logout": "Uitloggen"
    },
    "es": {
        "welcome": "Bienvenido",
        "code": "Código de estancia",
        "login": "Entrar",
        "show": "Mostrar código",
        "hide": "Ocultar código",
        "door": "Puerta de la segunda planta",
        "door_help": "Introduce el código y pulsa el botón Yale.",
        "reveal": "Mostrar código de la puerta",
        "temp": "Temperatura interior",
        "unavailable": "No disponible",
        "contact": "Contactar con Jean Luc",
        "logout": "Cerrar sesión"
    }
}
ACCESS_ERRORS = {
    "fr": {
        "wrong": "Code du séjour incorrect.",
        "expired": "Votre accès à ce séjour a expiré.",
        "form": "Le formulaire a expiré. Saisissez à nouveau votre code.",
        "rate": "Trop de tentatives. Réessayez dans {wait} s.",
        "busy": "Le portail est temporairement indisponible. Réessayez dans quelques instants."
    },
    "en": {
        "wrong": "Incorrect stay code.",
        "expired": "Your access for this stay has expired.",
        "form": "The form expired. Please enter your code again.",
        "rate": "Too many attempts. Try again in {wait} s.",
        "busy": "The portal is temporarily unavailable. Please try again shortly."
    },
    "nl": {
        "wrong": "Onjuiste verblijfscode.",
        "expired": "Je toegang voor dit verblijf is verlopen.",
        "form": "Het formulier is verlopen. Voer je code opnieuw in.",
        "rate": "Te veel pogingen. Probeer het over {wait} s opnieuw.",
        "busy": "Het portaal is tijdelijk niet beschikbaar. Probeer het zo opnieuw."
    },
    "es": {
        "wrong": "El código de estancia no es correcto.",
        "expired": "Tu acceso para esta estancia ha caducado.",
        "form": "El formulario ha caducado. Vuelve a introducir tu código.",
        "rate": "Demasiados intentos. Vuelve a intentarlo en {wait} s.",
        "busy": "El portal no está disponible temporalmente. Vuelve a intentarlo en unos instantes."
    }
}

LANGUAGE_NAMES = {"fr": "Français", "en": "English", "nl": "Nederlands", "es": "Español"}


def language(request):
    for code in (request.query.get("lang"), request.cookies.get("portal_lang")):
        if code in LABELS:
            return code
    for entry in request.headers.get("Accept-Language", "").split(","):
        tag, _, weight = entry.partition(";")
        if weight.strip() in ("q=0", "q=0.0", "q=0.00", "q=0.000"):
            continue
        code = tag.strip().lower().split("-")[0]
        if code in LABELS:
            return code
    return "fr"


def page(title, content, status=200, lang="fr"):
    document = f"""<!doctype html>
<html lang="{lang}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>The Panoramic Penthouse</title>
  <style>
    body {{
      margin: 0;
      padding: 28px 18px;
      background: #10252b;
      color: white;
      font-family: system-ui, sans-serif;
    }}
    main {{
      max-width: 460px;
      margin: auto;
      padding: 24px;
      background: #1b3942;
      border-radius: 18px;
    }}
    input, button {{
      box-sizing: border-box;
      width: 100%;
      padding: 14px;
      margin-top: 12px;
      font-size: 17px;
    }}
    a {{ color: #e4f3dc; }}
    nav {{ display: flex; flex-wrap: wrap; gap: 8px; }}
    nav a, summary {{ padding: 12px; }}
    nav a {{
      display: inline-flex;
      min-width: 44px;
      min-height: 44px;
      align-items: center;
      justify-content: center;
      box-sizing: border-box;
      border: 1px solid #8aa6ac;
      border-radius: 8px;
      text-decoration: none;
      font-weight: bold;
    }}
    nav a[aria-current="true"] {{
      background: #e4f3dc;
      color: #10252b;
    }}
    a:focus-visible, summary:focus-visible, button:focus-visible {{
      outline: 3px solid #f3cc7a;
      outline-offset: 3px;
    }}
    section {{ border-top: 1px solid #8aa6ac; margin-top: 20px; }}
    summary {{ cursor: pointer; }}
    .temperature {{ font-size: 2rem; font-weight: bold; }}
    p {{ line-height: 1.5; }}
  </style>
</head>
<body>
  <main>
    <h1>{html.escape(title)}</h1>
    {content}
  </main>
</body>
</html>
"""
    return web.Response(
        text=document,
        content_type="text/html",
        status=status,
    )


def redirect_home():
    return web.Response(status=303, headers={"Location": "/"})


def set_cookie(response, token, seconds):
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=max(1, int(seconds)),
        path="/",
        secure=True,
        httponly=True,
        samesite="Strict",
    )


def clear_cookie(response):
    response.del_cookie(
        COOKIE_NAME,
        path="/",
        secure=True,
        httponly=True,
        samesite="Strict",
    )


async def toggle_script(request):
    script = """
const button = document.querySelector("#toggle-code");
const input = document.querySelector("#code");
button.hidden = false;
button.addEventListener("click", () => {
    const visible = input.type === "password";
    input.type = visible ? "text" : "password";
    button.textContent = visible ? button.dataset.hide : button.dataset.show;
});
"""
    return web.Response(text=script, content_type="text/javascript")


@web.middleware
async def remember_language(request, handler):
    response = await handler(request)
    selected = request.query.get("lang")
    if selected in LABELS:
        response.set_cookie(
            "portal_lang", selected, secure=True, httponly=True,
            samesite="Strict", max_age=31536000, path="/",
        )
    return response


async def security_headers(request, response):
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["Pragma"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"

    if request.app["public"]:
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; "
            "style-src 'unsafe-inline'; script-src 'self'; connect-src 'self'; "
            "form-action 'self'; "
            "frame-ancestors 'none'; "
            "base-uri 'none'"
        )


# ---------- Sessions et expiration ----------

def stay_expired(request):
    return time.time() >= request.app["settings"]["expires"]


def clean_sessions():
    now = time.monotonic()

    for store in (sessions, pending):
        expired = [
            token
            for token, item in store.items()
            if item["deadline"] <= now
        ]
        for token in expired:
            store.pop(token, None)


def invalidate_browser(request):
    token = request.cookies.get(COOKIE_NAME, "")
    sessions.pop(token, None)
    pending.pop(token, None)


def get_session(request):
    clean_sessions()

    if stay_expired(request):
        return None

    return sessions.get(request.cookies.get(COOKIE_NAME, ""))


def expired_response(request):
    sessions.clear()
    pending.clear()

    if request.path.startswith("/api/"):
        response = web.json_response(
            {"error": "stay_expired"},
            status=403,
        )
    else:
        response = access_page(request, "expired", status=403)

    clear_cookie(response)
    return response


# ---------- Adresse publique et protection CSRF ----------

def origin_header_allowed(request):
    value = request.headers.get("Origin")

    # L'absence d'Origin n'est pas une autorisation :
    # les POST doivent aussi présenter un jeton CSRF valide.
    if value is None:
        return True

    try:
        return (
            normalize_origin(value)
            == request.app["settings"]["origin"]
        )
    except ValueError:
        return False


def valid_csrf(form, item):
    supplied = form.get("csrf", "")

    return (
        item is not None
        and isinstance(supplied, str)
        and hmac.compare_digest(
            supplied.encode("utf-8"),
            item["csrf"].encode("utf-8"),
        )
    )


def invalid_form_response(request):
    return access_page(request, "form", status=403)


@web.middleware
async def public_security(request, handler):
    # Ce serveur utilise directement TLS.
    # Les en-têtes X-Forwarded-* ne sont pas utilisés.
    if not request.secure:
        return page(
            "HTTPS requis",
            "<p>Utilisez l’adresse HTTPS du portail.</p>",
            status=400,
        )

    settings = request.app["settings"]

    try:
        actual_origin = normalize_origin(
            "https://" + request.host
        )
    except ValueError:
        actual_origin = None

    if actual_origin != settings["origin"]:
        address = html.escape(settings["address"])
        return page(
            "Adresse du portail incorrecte",
            f"<p>Adresse attendue : <code>{address}</code></p>"
            "<p>Vérifiez le nom de domaine et le port.</p>",
            status=400,
        )

    if stay_expired(request):
        return expired_response(request)

    if (
        request.method not in ("GET", "HEAD", "OPTIONS")
        and not origin_header_allowed(request)
    ):
        return page(
            "Requête refusée",
            "<p>L’origine de cette requête ne correspond pas "
            "à l’adresse configurée du portail.</p>",
            status=403,
        )

    response = await handler(request)

    # L'expiration peut survenir pendant la lecture d'un formulaire.
    if stay_expired(request):
        return expired_response(request)

    return response

# ---------- Home Assistant : lecture seule ----------

async def ha_client_context(app):
    timeout = ClientTimeout(total=8)

    async with ClientSession(timeout=timeout) as client:
        app["ha_client"] = client
        yield


def finite_number(value):
    if isinstance(value, bool):
        return None

    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None

    if not math.isfinite(number):
        return None

    return number


def extract_power(data, attributes):
    # Ne jamais présenter une valeur avec une unité supposée.
    if attributes.get("unit_of_measurement") != "W":
        return {"available": False}

    value = finite_number(data.get("state"))

    if value is None:
        return {"available": False}

    return {
        "available": True,
        "value": value,
        "unit": "W",
    }


def extract_energy(attributes, attribute_name):
    raw = attributes.get(attribute_name)

    # Format attendu : "8470.25 kWh".
    # Une valeur numérique sans unité n'est pas interprétée au hasard.
    if not isinstance(raw, str):
        return {"available": False}

    parts = raw.split()

    if len(parts) != 2 or parts[1] != "kWh":
        return {"available": False}

    value = finite_number(parts[0])

    if value is None:
        return {"available": False}

    return {
        "available": True,
        "value": value,
        "unit": "kWh",
    }


async def read_consumption(request):
    token = os.environ.get("SUPERVISOR_TOKEN")

    if not token:
        return {
            "available": False,
            "error": "ha_access_not_configured",
        }, 503

    settings = request.app["settings"]

    try:
        async with request.app["ha_client"].get(
            settings["consumption_url"],
            headers={"Authorization": f"Bearer {token}"},
            allow_redirects=False,
        ) as response:
            if response.status != 200:
                print(
                    "Lecture du capteur HA indisponible : "
                    f"HTTP {response.status}",
                    flush=True,
                )

                return {
                    "available": False,
                    "error": "consumption_unavailable",
                }, 503

            data = await response.json()

        if not isinstance(data, dict):
            raise ValueError("Réponse HA invalide")

        # Si l'entité est signalée indisponible, ne pas afficher
        # un ancien attribut qui aurait été conservé.
        if data.get("state") in (None, "unknown", "unavailable"):
            return {
                "available": False,
                "error": "consumption_unavailable",
            }, 503

        attributes = data.get("attributes", {})

        if not isinstance(attributes, dict):
            raise ValueError("Attributs HA invalides")

        power = extract_power(data, attributes)
        energy = extract_energy(
            attributes,
            settings["energy_attribute"],
        )

        available = power["available"] or energy["available"]

        result = {
            "available": available,
            "power": power,
            "energy": energy,
        }

        if not available:
            result["error"] = "consumption_unavailable"

        return result, 200 if available else 503

    except (
        ClientError,
        asyncio.TimeoutError,
        ValueError,
        TypeError,
        OverflowError,
    ):
        return {
            "available": False,
            "error": "consumption_unavailable",
        }, 503


async def consumption_api(request):
    item = get_session(request)

    if item is None:
        return web.json_response(
            {"error": "authentication_required"},
            status=401,
        )

    data, status = await read_consumption(request)

    # Recontrôle après l'attente de Home Assistant.
    if stay_expired(request):
        return expired_response(request)

    if get_session(request) is not item:
        return web.json_response(
            {"error": "authentication_required"},
            status=401,
        )

    return web.json_response(data, status=status)


def measurement_text(measurement, decimals):
    if not measurement.get("available"):
        return "Indisponible"

    value = f"{measurement['value']:,.{decimals}f}"
    value = value.replace(",", " ").replace(".", ",")

    return html.escape(f"{value} {measurement['unit']}")


# ---------- Pages publiques ----------

async def read_temperature(request, url_key="temperature_url"):
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return None
    try:
        async with request.app["ha_client"].get(
            request.app["settings"][url_key],
            headers={"Authorization": f"Bearer {token}"},
            allow_redirects=False,
        ) as response:
            if response.status != 200:
                return None
            data = await response.json()
        if data["attributes"].get("unit_of_measurement") != "°C":
            return None
        return finite_number(data["state"])
    except (ClientError, asyncio.TimeoutError, ValueError, KeyError, TypeError, AttributeError):
        return None


async def welcome_script(request):
    return web.Response(text=WELCOME_SCRIPT, content_type="text/javascript")


async def welcome_api(request):
    # Seule la température extérieure configurée est publique.
    async with request.app["outdoor_lock"]:
        cache = request.app["outdoor_cache"]
        if time.monotonic() >= cache["refresh_at"]:
            cache["value"] = await read_temperature(request, "outdoor_temperature_url")
            cache["refresh_at"] = time.monotonic() + 60
        value = cache["value"]
    return web.json_response({"temperature_c": value})


WELCOME_SCRIPT = "const clock = document.querySelector(\"#welcome-time\");\nconst temperature = document.querySelector(\"#welcome-temperature\");\nconst formatter = new Intl.DateTimeFormat(\"es-ES\", {\n    timeZone: \"Europe/Madrid\",\n    hour: \"2-digit\",\n    minute: \"2-digit\",\n    hourCycle: \"h23\"\n});\nconst unavailable = \"Temperatura exterior no disponible\";\nlet loading = false;\n\nfunction updateClock() {\n    clock.textContent = \" Son las \" + formatter.format(new Date());\n}\n\nasync function updateTemperature() {\n    if (loading || document.hidden) return;\n    loading = true;\n    try {\n        const response = await fetch(\"/api/welcome\", {\n            cache: \"no-store\",\n            credentials: \"same-origin\"\n        });\n        if (!response.ok) throw new Error(\"Temperature unavailable\");\n        const data = await response.json();\n        const value = data.temperature_c;\n        temperature.textContent = typeof value === \"number\" && Number.isFinite(value)\n            ? new Intl.NumberFormat(\"es-ES\", {\n                minimumFractionDigits: 1,\n                maximumFractionDigits: 1\n              }).format(value) + \" °C\"\n            : unavailable;\n    } catch {\n        temperature.textContent = unavailable;\n    } finally {\n        loading = false;\n    }\n}\n\nupdateClock();\nupdateTemperature();\nsetInterval(updateClock, 15000);\nsetInterval(updateTemperature, 60000);\ndocument.addEventListener(\"visibilitychange\", () => {\n    if (!document.hidden) {\n        updateClock();\n        updateTemperature();\n    }\n});\n"


async def home(request):
    lang = language(request)
    t = LABELS[lang]
    intro = '<p>The Panoramic Penthouse · Las Colinas</p>'
    item = get_session(request)
    if item is not None:
        temperature = await read_temperature(request)
        if stay_expired(request):
            return expired_response(request)
        if get_session(request) is not item:
            return redirect_home()

        settings = request.app["settings"]
        csrf = html.escape(item["csrf"], quote=True)
        temp = t["unavailable"] if temperature is None else f"{temperature:.1f} °C"
        if temperature is not None and lang != "en":
            temp = temp.replace(".", ",")
        door_code = html.escape(settings["door_code"])
        door = (
            f'<details><summary>{t["reveal"]}</summary><p>{door_code}</p><p>{t["door_help"]}</p></details>'
            if door_code else f'<p>{t["unavailable"]}</p>'
        )
        phone = settings["contact_phone"]
        contact = (
            f'<section><h2><a href="https://wa.me/{phone[1:]}">'
            f'{t["contact"]} · WhatsApp</a></h2></section>'
            if phone else ""
        )
        guest_name = settings["guest_name"]
        welcome = t["welcome"]
        if guest_name:
            welcome = f"{welcome} {guest_name}"
        return page(
            welcome,
            f"""
            {intro}
            <section><h2>{t["door"]}</h2>{door}</section>
            <section><h2>{t["temp"]}</h2><p class="temperature">{temp}</p></section>
            {contact}
            <form method="post" action="/logout">
              <input type="hidden" name="csrf" value="{csrf}">
              <button type="submit">{t["logout"]}</button>
            </form>
            """,
            lang=lang,
        )

    return access_page(request)


def access_page(request, error=None, status=200, wait=None):
    lang = language(request)
    t = LABELS[lang]
    links = " ".join(
        f'<a href="/?lang={code}" lang="{code}" aria-label="{name}"'
        f' aria-current="{str(code == lang).lower()}">{code.upper()}</a>'
        for code, name in LANGUAGE_NAMES.items()
    )
    intro = (
        '<p>The Panoramic Penthouse · Las Colinas</p>'
        '<p lang="es">¡Hola!<span id="welcome-time"></span>'
        ' · <span aria-hidden="true">🌡️</span> '
        '<span id="welcome-temperature">Temperatura exterior no disponible</span></p>'
        '<script src="/welcome.js" defer></script>'
        f'<nav>{links}</nav>'
    )
    message = ""
    if error:
        message = html.escape(ACCESS_ERRORS[lang][error].format(wait=wait))
        message = f'<p role="alert">{message}</p>'
    if error == "expired":
        return page(t["welcome"], intro + message, status=status, lang=lang)

    clean_sessions()

    old_token = request.cookies.get(COOKIE_NAME, "")
    item = pending.get(old_token)

    if item is None:
        # Les formulaires anonymes ne peuvent pas évincer
        # les sessions déjà authentifiées.
        if len(pending) >= MAX_PENDING:
            pending.pop(next(iter(pending)))

        token = secrets.token_urlsafe(32)
        item = {
            "csrf": secrets.token_urlsafe(32),
            "deadline": time.monotonic() + FORM_SECONDS,
        }
        pending[token] = item
    else:
        token = old_token

    csrf = html.escape(item["csrf"], quote=True)

    response = page(
        t["welcome"],
        f"""
        {intro}
        <form method="post" action="/login">
          <input type="hidden" name="csrf" value="{csrf}">
          <label for="code">{t["code"]}</label>
          <input
            id="code"
            name="code"
            type="password"
            autocomplete="current-password"
            maxlength="128"
            required
          >
          {message}
          <button type="button" id="toggle-code" hidden
                  data-show="{t['show']}" data-hide="{t['hide']}">{t["show"]}</button>
          <button type="submit">{t["login"]}</button>
        </form>
        <script src="/toggle-code.js" defer></script>
        """,
        status=status,
        lang=lang,
    )
    set_cookie(response, token, item["deadline"] - time.monotonic())
    return response


async def login(request):
    # Limite globale : elle évite de dépendre d'en-têtes IP
    # éventuellement falsifiés.
    now = time.monotonic()

    while attempts and attempts[0] <= now - LOGIN_WINDOW:
        attempts.popleft()

    if len(attempts) >= LOGIN_LIMIT:
        wait = max(
            1,
            math.ceil(attempts[0] + LOGIN_WINDOW - now),
        )
        response = access_page(request, "rate", status=429, wait=wait)
        response.headers["Retry-After"] = str(wait)
        return response

    attempts.append(now)

    form = await request.post()

    if stay_expired(request):
        return expired_response(request)

    clean_sessions()

    old_token = request.cookies.get(COOKIE_NAME, "")
    item = pending.get(old_token)

    if not valid_csrf(form, item):
        return invalid_form_response(request)

    supplied = form.get("code", "")

    if not isinstance(supplied, str) or not 16 <= len(supplied) <= 128:
        supplied = ""

    supplied_hash = hashlib.sha256(
        supplied.encode("utf-8")
    ).digest()

    if not hmac.compare_digest(
        supplied_hash,
        request.app["settings"]["code_hash"],
    ):
        return access_page(request, "wrong", status=401)

    if len(sessions) >= MAX_SESSIONS:
        return access_page(request, "busy", status=503)

    # Le formulaire de connexion est consommé.
    # Le cookie anonyme n'est jamais réutilisé comme session autorisée.
    invalidate_browser(request)

    duration = min(
        SESSION_SECONDS,
        request.app["settings"]["expires"] - time.time(),
    )

    if duration <= 0:
        return expired_response(request)

    token = secrets.token_urlsafe(32)
    sessions[token] = {
        "csrf": secrets.token_urlsafe(32),
        "deadline": time.monotonic() + duration,
    }

    response = redirect_home()
    set_cookie(response, token, duration)
    return response


async def logout(request):
    form = await request.post()

    item = get_session(request)

    if item is not None and not valid_csrf(form, item):
        lang = language(request)
        message = html.escape(ACCESS_ERRORS[lang]["form"])
        return page(LABELS[lang]["welcome"], f'<p role="alert">{message}</p>', status=403, lang=lang)

    # Si la session a déjà disparu, la déconnexion est sans effet
    # sur le serveur, mais nettoie tout de même le cookie.
    invalidate_browser(request)

    response = redirect_home()
    clear_cookie(response)
    return response


async def session_status(request):
    if get_session(request) is None:
        return web.json_response(
            {"error": "authentication_required"},
            status=401,
        )

    return web.json_response({"authenticated": True})


# ---------- Ingress : état uniquement ----------

async def admin_home(request):
    if request.remote != INGRESS_IP:
        return web.Response(
            status=403,
            text="Accès Ingress uniquement",
        )

    return page(
        "Administration",
        "<p>Le portail 0.6.4 est démarré.</p>"
        "<p>Cette page ne connecte pas le locataire.</p>"
        "<p>Ouvrez directement l’adresse HTTPS configurée "
        "pour tester la connexion locataire.</p>"
        "<p>La puissance et l’énergie Home Assistant sont disponibles "
        "après authentification, en lecture seule.</p>",
    )

# ---------- HTTPS et démarrage ----------

async def watch_certificate(context):
    last_loaded = None

    while True:
        try:
            current = (
                os.stat(CERT_FILE).st_mtime_ns,
                os.stat(KEY_FILE).st_mtime_ns,
            )

            if current != last_loaded:
                context.load_cert_chain(CERT_FILE, KEY_FILE)
                last_loaded = current
                print(
                    "Certificat HTTPS chargé ou actualisé",
                    flush=True,
                )

        except (OSError, ssl.SSLError):
            print(
                "Certificat non actualisé : vérifier les fichiers SSL.",
                flush=True,
            )

        await asyncio.sleep(60)


async def main():
    settings = load_settings()

    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.minimum_version = ssl.TLSVersion.TLSv1_2
    tls.load_cert_chain(CERT_FILE, KEY_FILE)

    public = web.Application(
        middlewares=[remember_language, public_security],
        client_max_size=4096,
    )
    public["public"] = True
    public["settings"] = settings
    public["outdoor_lock"] = asyncio.Lock()
    public["outdoor_cache"] = {"value": None, "refresh_at": 0}
    public.cleanup_ctx.append(ha_client_context)
    public.on_response_prepare.append(security_headers)

    public.router.add_get("/", home)
    public.router.add_get("/toggle-code.js", toggle_script)
    public.router.add_get("/welcome.js", welcome_script)
    public.router.add_get("/api/welcome", welcome_api)
    public.router.add_post("/login", login)
    public.router.add_post("/logout", logout)
    public.router.add_get("/api/session", session_status)
    public.router.add_get("/api/consumption", consumption_api)

    admin = web.Application(client_max_size=4096)
    admin["public"] = False
    admin.on_response_prepare.append(security_headers)
    admin.router.add_get("/", admin_home)

    public_runner = web.AppRunner(public, access_log=None)
    admin_runner = web.AppRunner(admin, access_log=None)

    watcher = None

    try:
        await admin_runner.setup()
        await public_runner.setup()

        await web.TCPSite(
            admin_runner,
            "0.0.0.0",
            8099,
        ).start()

        await web.TCPSite(
            public_runner,
            "0.0.0.0",
            8443,
            ssl_context=tls,
        ).start()

        watcher = asyncio.create_task(watch_certificate(tls))

        print(
            "Portail 0.6.4 démarré : "
            "Ingress 8099, HTTPS 8443, guide de séjour.",
            flush=True,
        )

        await asyncio.Event().wait()
    finally:
        if watcher is not None:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)

        await public_runner.cleanup()
        await admin_runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (ValueError, OSError, ssl.SSLError) as error:
        print(f"Démarrage impossible : {error}", flush=True)
        raise SystemExit(1)
