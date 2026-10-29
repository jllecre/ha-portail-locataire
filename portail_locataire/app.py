import asyncio
import hashlib
import hmac
import html
import json
import os
import secrets
import ssl
import time

from collections import deque
from datetime import datetime
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web


ENTITY = "sensor.hinojo_45_temperature_exterieure"
API_URL = f"http://supervisor/core/api/states/{ENTITY}"

INGRESS_IP = "172.30.32.2"
CERT_FILE = "/ssl/portail_fullchain.pem"
KEY_FILE = "/ssl/portail_privkey.pem"

COOKIE_NAME = "__Host-portail-session"
SESSION_SECONDS = 8 * 60 * 60

# Limitation globale : 20 essais de connexion par période de 10 minutes.
LOGIN_WINDOW = 600
LOGIN_LIMIT = 20

sessions = {}
attempts = deque()


def load_settings():
    with open("/data/options.json", encoding="utf-8") as file:
        options = json.load(file)

    address = options.get("portal_url", "").strip().rstrip("/")
    code = options.get("guest_code", "")
    expiry = options.get("expires_at", "").strip()

    parsed = urlsplit(address)

    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "portal_url doit être une adresse HTTPS sans chemin."
        )

    # Vérifie également que le port est valide.
    parsed.port

    if not isinstance(code, str) or not 16 <= len(code) <= 128:
        raise ValueError(
            "guest_code doit contenir entre 16 et 128 caractères."
        )

    end = datetime.fromisoformat(expiry.replace("Z", "+00:00"))

    if end.tzinfo is None or end.utcoffset() is None:
        raise ValueError(
            "expires_at doit contenir un fuseau, par exemple +02:00."
        )

    return {
        "origin": address,
        "code_hash": hashlib.sha256(code.encode()).digest(),
        "expires": end.timestamp(),
    }


def page(title, content, status=200):
    document = """<!doctype html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Mon séjour</title>
  <style>
    body {
      margin: 0;
      padding: 28px 18px;
      background: #10252b;
      color: white;
      font-family: system-ui, sans-serif;
    }
    main {
      max-width: 460px;
      margin: auto;
    }
    .card {
      padding: 24px;
      background: #1b3942;
      border-radius: 18px;
      margin-bottom: 20px;
    }
    h1, h2, .temperature {
      text-align: center;
    }
    p {
      line-height: 1.5;
    }
    input, button {
      box-sizing: border-box;
      width: 100%;
      padding: 14px;
      margin-top: 12px;
      border-radius: 10px;
      font-size: 17px;
    }
    input {
      border: 1px solid #8aa8b0;
    }
    button {
      border: 0;
      cursor: pointer;
      background: #bdd9c1;
      color: #10252b;
    }
    .temperature {
      font-size: 40px;
      font-weight: bold;
    }
    .note {
      color: #d0e0e5;
    }
  </style>
</head>
<body>
  <main>
    <h1>Mon séjour</h1>
    <section class="card">
      <h2>__TITLE__</h2>
      __CONTENT__
    </section>
  </main>
</body>
</html>
"""
    document = document.replace("__TITLE__", html.escape(title))
    document = document.replace("__CONTENT__", content)

    return web.Response(
        text=document,
        content_type="text/html",
        status=status,
    )


def redirect_home():
    return web.Response(status=303, headers={"Location": "/"})


def clear_cookie(response):
    response.del_cookie(
        COOKIE_NAME,
        path="/",
        secure=True,
        httponly=True,
        samesite="Strict",
    )


def clean_sessions():
    now = time.time()
    expired = [
        token
        for token, session in sessions.items()
        if session["expires"] <= now
    ]

    for token in expired:
        sessions.pop(token, None)


def get_session(request):
    clean_sessions()

    if time.time() >= request.app["settings"]["expires"]:
        return None

    token = request.cookies.get(COOKIE_NAME, "")
    return sessions.get(token)


def origin_allowed(request):
    return (
        request.headers.get("Origin", "")
        == request.app["settings"]["origin"]
    )


async def security_headers(request, response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"

    if request.app["public"]:
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; "
            "style-src 'unsafe-inline'; "
            "form-action 'self'; "
            "frame-ancestors 'none'; "
            "base-uri 'none'"
        )


async def read_temperature(client):
    token = os.environ.get("SUPERVISOR_TOKEN")

    if not token:
        return "Connexion HA non configurée", ""

    try:
        async with client.get(
            API_URL,
            headers={"Authorization": f"Bearer {token}"},
        ) as response:
            if response.status != 200:
                return "Lecture indisponible", ""

            data = await response.json()

        state = data.get("state", "unknown")
        unit = data.get("attributes", {}).get(
            "unit_of_measurement", ""
        )

        if state in ("unknown", "unavailable"):
            return "Capteur indisponible", ""

        return str(state), str(unit)

    except (
        aiohttp.ClientError,
        asyncio.TimeoutError,
        ValueError,
        AttributeError,
    ):
        return "Connexion indisponible", ""


async def home(request):
    settings = request.app["settings"]

    if time.time() >= settings["expires"]:
        response = page(
            "Accès terminé",
            "<p>L’accès à ce séjour a expiré.</p>",
            status=403,
        )
        clear_cookie(response)
        return response

    session = get_session(request)

    if session is None:
        return page(
            "Bienvenue",
            """
            <p>Saisissez le code du portail fourni pour votre séjour.</p>
            <form method="post" action="/login">
              <label for="code">Code du séjour</label>
              <input
                id="code"
                name="code"
                type="password"
                autocomplete="current-password"
                minlength="16"
                maxlength="128"
                required
              >
              <button type="submit">Se connecter</button>
            </form>
            """,
        )

    value, unit = await read_temperature(request.app["client"])
    csrf = html.escape(session["csrf"], quote=True)

    return page(
        "Température extérieure",
        f"""
        <p class="temperature">
          {html.escape(value)} {html.escape(unit)}
        </p>
        <form method="get" action="/">
          <button type="submit">Actualiser</button>
        </form>
        <form method="post" action="/logout">
          <input type="hidden" name="csrf" value="{csrf}">
          <button type="submit">Se déconnecter</button>
        </form>
        <p class="note">
          Version de test : aucune commande sur les équipements.
        </p>
        """,
    )


async def login(request):
    settings = request.app["settings"]

    if not origin_allowed(request):
        return page(
            "Connexion refusée",
            "<p>Ouvrez le portail avec son adresse HTTPS configurée.</p>",
            status=403,
        )

    if time.time() >= settings["expires"]:
        return page(
            "Accès terminé",
            "<p>L’accès à ce séjour a expiré.</p>",
            status=403,
        )

    now = time.monotonic()

    while attempts and attempts[0] <= now - LOGIN_WINDOW:
        attempts.popleft()

    if len(attempts) >= LOGIN_LIMIT:
        response = page(
            "Veuillez patienter",
            "<p>Trop de tentatives. Réessayez dans dix minutes.</p>",
            status=429,
        )
        response.headers["Retry-After"] = "600"
        return response

    attempts.append(now)

    form = await request.post()
    supplied = form.get("code", "")

    if not isinstance(supplied, str) or len(supplied) > 128:
        supplied = ""

    supplied_hash = hashlib.sha256(supplied.encode()).digest()

    if not hmac.compare_digest(
        supplied_hash,
        settings["code_hash"],
    ):
        return page(
            "Connexion refusée",
            '<p>Code incorrect.</p><p><a href="/">Réessayer</a></p>',
            status=401,
        )

    clean_sessions()

    # Une nouvelle connexion remplace la session de ce navigateur.
    old_token = request.cookies.get(COOKIE_NAME, "")
    sessions.pop(old_token, None)

    # Limite la mémoire utilisée par les sessions.
    if len(sessions) >= 64:
        sessions.pop(next(iter(sessions)))

    token = secrets.token_urlsafe(32)
    expires = min(
        time.time() + SESSION_SECONDS,
        settings["expires"],
    )

    sessions[token] = {
        "expires": expires,
        "csrf": secrets.token_urlsafe(32),
    }

    response = redirect_home()
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=max(1, int(expires - time.time())),
        path="/",
        secure=True,
        httponly=True,
        samesite="Strict",
    )

    return response


async def logout(request):
    if not origin_allowed(request):
        return web.Response(status=403, text="Requête refusée")

    session = get_session(request)

    if session is not None:
        form = await request.post()
        supplied = form.get("csrf", "")

        if (
            not isinstance(supplied, str)
            or not hmac.compare_digest(
                supplied.encode(),
                session["csrf"].encode(),
            )
        ):
            return web.Response(status=403, text="Requête refusée")

    token = request.cookies.get(COOKIE_NAME, "")
    sessions.pop(token, None)

    response = redirect_home()
    clear_cookie(response)
    return response


async def admin_home(request):
    if request.remote != INGRESS_IP:
        return web.Response(status=403, text="Accès Ingress uniquement")

    value, unit = await read_temperature(request.app["client"])

    return page(
        "Administration — lecture seule",
        f"""
        <p>Le portail est démarré.</p>
        <p class="temperature">
          {html.escape(value)} {html.escape(unit)}
        </p>
        <p>La connexion locataire se teste avec l’adresse HTTPS
        du portail, pas avec cette interface Ingress.</p>
        """,
    )


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
                print("Certificat HTTPS chargé ou actualisé", flush=True)

        except (OSError, ssl.SSLError):
            print(
                "Renouvellement HTTPS non chargé : "
                "vérifier les certificats.",
                flush=True,
            )

        await asyncio.sleep(60)


async def main():
    settings = load_settings()

    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.minimum_version = ssl.TLSVersion.TLSv1_2
    tls.load_cert_chain(CERT_FILE, KEY_FILE)

    timeout = aiohttp.ClientTimeout(total=10)

    async with aiohttp.ClientSession(timeout=timeout) as client:
        public = web.Application(client_max_size=4096)
        public["public"] = True
        public["settings"] = settings
        public["client"] = client
        public.on_response_prepare.append(security_headers)

        public.router.add_get("/", home)
        public.router.add_post("/login", login)
        public.router.add_post("/logout", logout)

        admin = web.Application(client_max_size=4096)
        admin["public"] = False
        admin["client"] = client
        admin.on_response_prepare.append(security_headers)
        admin.router.add_get("/", admin_home)

        admin_runner = web.AppRunner(admin, access_log=None)
        public_runner = web.AppRunner(public, access_log=None)

        watcher = None

        try:
            await admin_runner.setup()
            await public_runner.setup()

            await web.TCPSite(
                admin_runner, "0.0.0.0", 8099
            ).start()

            await web.TCPSite(
                public_runner,
                "0.0.0.0",
                8443,
                ssl_context=tls,
            ).start()

            watcher = asyncio.create_task(watch_certificate(tls))

            print(
                "Portail 0.2.0 démarré : "
                "Ingress 8099 et HTTPS 8443 avec connexion locataire.",
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
        print(
            f"Démarrage impossible : {error}",
            flush=True,
        )
        raise SystemExit(1)
