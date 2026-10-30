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

from collections import deque
from datetime import datetime
from urllib.parse import urlsplit

from aiohttp import web


INGRESS_IP = "172.30.32.2"

CERT_FILE = "/ssl/portail_fullchain.pem"
KEY_FILE = "/ssl/portail_privkey.pem"

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

    return {
        "address": address,
        "origin": origin,
        "code_hash": hashlib.sha256(code.encode("utf-8")).digest(),
        "expires": end.timestamp(),
    }


# ---------- Réponses et cookies ----------

def page(title, content, status=200):
    document = f"""<!doctype html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Portail du séjour</title>
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
    a {{ color: #bdd9c1; }}
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


async def security_headers(request, response):
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["Pragma"] = "no-cache"
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
        response = page(
            "Accès terminé",
            "<p>L’accès à ce séjour a expiré.</p>",
            status=403,
        )

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


def invalid_form_response():
    return page(
        "Formulaire périmé",
        '<p>Rechargez la page puis réessayez.</p>'
        '<p><a href="/">Retour au portail</a></p>',
        status=403,
    )


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


# ---------- Pages publiques ----------

async def home(request):
    item = get_session(request)

    if item is not None:
        csrf = html.escape(item["csrf"], quote=True)

        return page(
            "Connexion autorisée",
            f"""
            <p>Votre session est active.</p>
            <p>Aucun thermostat ni capteur n’est connecté
            dans cette version de test.</p>
            <form method="post" action="/logout">
              <input type="hidden" name="csrf" value="{csrf}">
              <button type="submit">Se déconnecter</button>
            </form>
            """,
        )

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
        "Bienvenue",
        f"""
        <p>Saisissez le code fourni pour votre séjour.</p>
        <form method="post" action="/login">
          <input type="hidden" name="csrf" value="{csrf}">
          <label for="code">Code du séjour</label>
          <input
            id="code"
            name="code"
            type="password"
            autocomplete="current-password"
            maxlength="128"
            required
          >
          <button type="submit">Se connecter</button>
        </form>
        """,
    )

    set_cookie(
        response,
        token,
        item["deadline"] - time.monotonic(),
    )
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
        response = page(
            "Veuillez patienter",
            f"<p>Trop de tentatives. Réessayez dans "
            f"{wait} secondes.</p>",
            status=429,
        )
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
        return invalid_form_response()

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
        return page(
            "Connexion refusée",
            '<p>Code du séjour incorrect.</p>'
            '<p><a href="/">Réessayer</a></p>',
            status=401,
        )

    if len(sessions) >= MAX_SESSIONS:
        return page(
            "Portail temporairement indisponible",
            "<p>Le nombre maximal de sessions est atteint.</p>",
            status=503,
        )

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
        return invalid_form_response()

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
        "<p>Le portail 0.3.0 est démarré.</p>"
        "<p>Cette page ne connecte pas le locataire.</p>"
        "<p>Pour les tests, ouvrez directement l’adresse HTTPS "
        "configurée dans portal_url.</p>"
        "<p>Aucun accès aux équipements Home Assistant "
        "dans cette version.</p>",
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
        middlewares=[public_security],
        client_max_size=4096,
    )
    public["public"] = True
    public["settings"] = settings
    public.on_response_prepare.append(security_headers)

    public.router.add_get("/", home)
    public.router.add_post("/login", login)
    public.router.add_post("/logout", logout)
    public.router.add_get("/api/session", session_status)

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
            "Portail 0.3.0 démarré : "
            "Ingress 8099, HTTPS 8443, sécurité uniquement.",
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
