import html
import json
import os
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ENTITY = "sensor.hinojo_45_temperature_exterieure"
API_URL = f"http://supervisor/core/api/states/{ENTITY}"

# Adresse du proxy Ingress de Home Assistant Supervisor.
INGRESS_IP = "172.30.32.2"


def read_temperature():
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return "Configuration incomplète", ""

    request = Request(
        API_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
    )

    try:
        with urlopen(request, timeout=10) as response:
            data = json.load(response)

        state = data.get("state", "unknown")
        unit = data.get("attributes", {}).get("unit_of_measurement", "")

        if state in ("unknown", "unavailable"):
            return "Capteur indisponible", ""

        return str(state), str(unit)

    except HTTPError as error:
        print(f"Lecture HA : erreur HTTP {error.code}", flush=True)
        return "Lecture impossible", ""

    except (URLError, TimeoutError, ValueError, OSError):
        print("Lecture HA : connexion ou réponse invalide", flush=True)
        return "Connexion indisponible", ""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        # Cette première version accepte seulement le proxy Ingress.
        # Ne pas supprimer cette vérification pour ouvrir l'accès locataire.
        if self.client_address[0] != INGRESS_IP:
            self.send_error(403, "Acces reserve a Ingress")
            return

        value, unit = read_temperature()

        page = """<!doctype html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Portail locataire — Test</title>
  <style>
    body {
      margin: 0;
      padding: 28px 18px;
      background: #10252b;
      color: #ffffff;
      font-family: system-ui, sans-serif;
      text-align: center;
    }
    main {
      max-width: 480px;
      margin: auto;
    }
    .card {
      padding: 28px;
      margin: 24px 0;
      background: #1b3942;
      border-radius: 20px;
    }
    .temperature {
      font-size: 42px;
      font-weight: bold;
    }
    button {
      padding: 14px 22px;
      border: 0;
      border-radius: 12px;
      font-size: 17px;
      cursor: pointer;
    }
    .note {
      color: #c4d9df;
      line-height: 1.5;
    }
  </style>
</head>
<body>
  <main>
    <h1>Mon séjour</h1>
    <p>Prototype administrateur</p>

    <section class="card">
      <h2>Température extérieure</h2>
      <p class="temperature">__VALUE__ __UNIT__</p>
    </section>

    <button onclick="window.location.reload()">Actualiser</button>

    <p class="note">
      Lecture seule : aucune commande sur les équipements.
      Cette version n'est pas encore accessible aux locataires.
    </p>
  </main>
</body>
</html>
"""
        page = page.replace("__VALUE__", html.escape(value))
        page = page.replace("__UNIT__", html.escape(unit))
        body = page.encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        # Évite d'enregistrer les chemins d'accès Ingress.
        pass


class LocalPreparationHandler(BaseHTTPRequestHandler):
    """Entrée locale : aucune donnée HA, aucune commande."""

    def do_GET(self):
        page = """<!doctype html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Mon séjour</title>
  <style>
    body {
      margin: 0;
      padding: 40px 20px;
      background: #10252b;
      color: white;
      font-family: system-ui, sans-serif;
      text-align: center;
    }
    main {
      max-width: 480px;
      margin: auto;
      padding: 24px;
      background: #1b3942;
      border-radius: 20px;
    }
    p {
      line-height: 1.6;
    }
  </style>
</head>
<body>
  <main>
    <h1>Mon séjour</h1>
    <h2>Portail en préparation</h2>
    <p>La connexion HTTPS est opérationnelle.</p>
    <p>Aucune information du logement ni commande
       n'est accessible dans cette version.</p>
  </main>
</body>
</html>
"""
        body = page.encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
        )
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


CERT_FILE = "/ssl/portail_fullchain.pem"
KEY_FILE = "/ssl/portail_privkey.pem"


def watch_certificate(context):
    """Recharge le certificat après son renouvellement."""
    last_loaded = None

    while True:
        try:
            current = (
                os.stat(CERT_FILE).st_mtime_ns,
                os.stat(KEY_FILE).st_mtime_ns,
            )

            if current != last_loaded:
                context.load_cert_chain(
                    certfile=CERT_FILE,
                    keyfile=KEY_FILE,
                )
                last_loaded = current
                print("Certificat HTTPS chargé ou actualisé", flush=True)

        except (OSError, ssl.SSLError):
            print(
                "Impossible d'actualiser le certificat HTTPS ; "
                "vérifier les fichiers et les journaux Duck DNS.",
                flush=True,
            )

        time.sleep(60)


if __name__ == "__main__":
    # L'interface administrateur existante reste accessible via Ingress.
    ingress_server = ThreadingHTTPServer(("0.0.0.0", 8099), Handler)

    threading.Thread(
        target=ingress_server.serve_forever,
        daemon=True,
    ).start()

    print("Interface administrateur Ingress démarrée", flush=True)

    try:
        tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls_context.minimum_version = ssl.TLSVersion.TLSv1_2
        tls_context.load_cert_chain(
            certfile=CERT_FILE,
            keyfile=KEY_FILE,
        )

        local_server = ThreadingHTTPServer(
            ("0.0.0.0", 8443),
            LocalPreparationHandler,
        )

        local_server.socket = tls_context.wrap_socket(
            local_server.socket,
            server_side=True,
        )

    except (OSError, ssl.SSLError) as error:
        print(
            f"Entrée HTTPS non démarrée : {error}",
            flush=True,
        )
        print(
            "Vérifier les noms des certificats et l'accès au dossier /ssl. "
            "L'interface Ingress reste disponible.",
            flush=True,
        )

        # Maintient uniquement l'interface administrateur.
        threading.Event().wait()

    else:
        threading.Thread(
            target=watch_certificate,
            args=(tls_context,),
            daemon=True,
        ).start()

        print(
            "HTTPS démarré sur le port 8443 — "
            "page de préparation uniquement",
            flush=True,
        )

        local_server.serve_forever()
