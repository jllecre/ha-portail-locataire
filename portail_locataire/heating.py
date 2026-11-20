"""Authenticated, seasonal target-temperature controls; no HVAC mode changes."""
import asyncio
import math
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from aiohttp import ClientError, ClientTimeout, web

MADRID = ZoneInfo("Europe/Madrid")
TEXT = {
    "fr": ["Chauffage au sol", "Temp�rature souhait�e (�C)", "Appliquer", "Demande transmise. V�rification en cours.", "R�glage indisponible", "Disponible du 1er octobre au 30 avril", "R�glage r�serv� � un s�jour en mode Arriv�e", "Prochainement", "V�rifiez la temp�rature et r�essayez", "Consigne confirm�e", "Envoi en cours."],
    "en": ["Underfloor heating", "Desired temperature (�C)", "Apply", "Request sent. Checking.", "Control unavailable", "Available from 1 October to 30 April", "Control available when the stay is in Arrival mode", "Coming soon", "Check the temperature and try again", "Target confirmed", "Sending."],
    "nl": ["Vloerverwarming", "Gewenste temperatuur (�C)", "Toepassen", "Verzoek verstuurd. Controleren.", "Bediening niet beschikbaar", "Beschikbaar van 1 oktober tot 30 april", "Bediening beschikbaar bij verblijfsmodus Aankomst", "Binnenkort", "Controleer de temperatuur en probeer opnieuw", "Doeltemperatuur bevestigd", "Versturen."],
    "es": ["Suelo radiante", "Temperatura deseada (�C)", "Aplicar", "Solicitud enviada. Comprobando.", "Control no disponible", "Disponible del 1 de octubre al 30 de abril", "Control disponible con la estancia en modo Llegada", "Pr�ximamente", "Compruebe la temperatura e int�ntelo de nuevo", "Temperatura objetivo confirmada", "Enviando."],
}


def winter(now=None):
    now = now or datetime.now(MADRID)
    return now.astimezone(MADRID).month in (10, 11, 12, 1, 2, 3, 4)


def number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (ValueError, TypeError, OverflowError):
        return None


def thermostat(state, unit):
    attrs = state.get("attributes", {})
    if not isinstance(attrs, dict):
        return None
    target, low, high, step = [number(attrs.get(key)) for key in
                               ("temperature", "min_temp", "max_temp", "target_temp_step")]
    features = attrs.get("supported_features", 0)
    if (state.get("state") in (None, "unavailable", "off", "cool", "heat_cool")
            or unit != "�C" or None in (target, low, high)
            or not isinstance(features, int) or not features & 1
            or not low <= target <= high):
        return None
    # Home Assistant's single-target climate control defaults to 0.5 �C.
    step = step if step is not None else 0.5
    if not 0 < step <= high - low:
        return None
    return {"target": target, "min": low, "max": high, "step": step}


def valid_target(value, metadata):
    value = number(value)
    if value is None or not metadata["min"] <= value <= metadata["max"]:
        return False
    count = (value - metadata["min"]) / metadata["step"]
    return math.isclose(count, round(count), abs_tol=1e-6)


def register(app, get_session, valid_csrf, language):
    locks = [asyncio.Lock() for _ in range(5)]
    next_command = [0.0] * 5
    settings = app["settings"]
    entities = settings["zone_entities"] + [settings["zone5_entity"]]

    async def read(request, path):
        token = os.environ.get("SUPERVISOR_TOKEN", "")
        if not token:
            return {}
        try:
            async with request.app["ha_client"].get(
                "http://supervisor/core/api/" + path,
                headers={"Authorization": "Bearer " + token},
                allow_redirects=False, timeout=ClientTimeout(total=5),
            ) as response:
                if response.status != 200:
                    return {}
                result = await response.json()
                return result if isinstance(result, dict) else {}
        except (ClientError, asyncio.TimeoutError, ValueError):
            return {}

    def authorized(request, item):
        return get_session(request) is item and item is not None and winter()

    async def snapshot(request):
        active = winter()
        if not active or not settings["floor_heating_enabled"]:
            return {"reason": 5 if not active else 4, "zones": [None] * 5}
        config, rental, *states = await asyncio.gather(
            read(request, "config"),
            read(request, "states/" + settings["stay_sources"]["rental_status_entity"]),
            *(read(request, "states/" + entity) for entity in entities if entity),
        )
        units = config.get("unit_system", {})
        unit = units.get("temperature") if isinstance(units, dict) else None
        iterator = iter(states)
        zones = [thermostat(next(iterator), unit) if entity else None for entity in entities]
        return {"reason": None if rental.get("state") == "Arriv�e" else 6,
                "zones": zones}

    async def status(request):
        item = get_session(request)
        if item is None:
            return web.json_response({"error": "authentication_required"}, status=401)
        data = await snapshot(request)
        if get_session(request) is not item:
            return web.json_response({"error": "authentication_required"}, status=401)
        data["text"] = TEXT[language(request)]
        return web.json_response(data)

    async def command(request):
        item = get_session(request)
        if item is None:
            return web.json_response({"error": "authentication_required"}, status=401)
        form = await request.post()
        if not valid_csrf(form, item) or not authorized(request, item):
            return web.json_response({"error": "forbidden"}, status=403)
        zone = form.get("zone", "")
        if zone not in ("1", "2", "3", "4", "5"):
            return web.json_response({"error": "invalid_zone"}, status=400)
        index = int(zone) - 1
        if not entities[index] or not settings["floor_heating_enabled"]:
            return web.json_response({"error": "unavailable"}, status=403)
        if locks[index].locked():
            return web.json_response({"error": "busy"}, status=429)
        async with locks[index]:
            if time.monotonic() < next_command[index]:
                return web.json_response({"error": "busy"}, status=429)
            next_command[index] = time.monotonic() + 3
            data = await snapshot(request)
            meta = data["zones"][index]
            if data["reason"] is not None or meta is None:
                return web.json_response({"error": "unavailable"}, status=409)
            if not valid_target(form.get("temperature"), meta):
                return web.json_response({"error": "invalid_temperature"}, status=400)
            if not authorized(request, item):
                return web.json_response({"error": "forbidden"}, status=403)
            value = float(form["temperature"])
            try:
                async with request.app["ha_client"].post(
                    "http://supervisor/core/api/services/climate/set_temperature",
                    headers={"Authorization": "Bearer " + os.environ["SUPERVISOR_TOKEN"]},
                    json={"entity_id": entities[index], "temperature": value},
                    allow_redirects=False, timeout=ClientTimeout(total=8),
                ) as response:
                    if response.status != 200:
                        return web.json_response({"error": "home_assistant"}, status=502)
                    await response.read()
            except (ClientError, asyncio.TimeoutError, KeyError):
                return web.json_response({"error": "home_assistant"}, status=502)
            # A successful service call means accepted, not physical confirmation.
            request.app["zones_cache"]["refresh_at"] = 0
            return web.json_response({"accepted": True, "requested": value})

    async def script(request):
        return web.Response(text=SCRIPT, content_type="application/javascript")

    app.router.add_get("/api/heating", status)
    app.router.add_post("/api/heating", command)
    app.router.add_get("/heating.js", script)


SCRIPT = r"""'use strict';
(() => {
 const cards = [...document.querySelectorAll('#zones .zone-card')];
 const csrf = document.querySelector('form[action="/logout"] input[name="csrf"]')?.value;
 if (!csrf || cards.length !== 5) return;
 let busy = false;
 async function refresh() {
  if (busy || document.hidden) return;
  try {
   const r = await fetch('/api/heating', {cache: 'no-store'});
   if (r.status === 401 || r.status === 403) {location.replace('/'); return;}
   if (!r.ok) throw Error();
   const d = await r.json(), t = d.text;
   cards.forEach((card, i) => {
    let host = card.querySelector('[data-heating]');
    if (!host) {host = document.createElement('div'); host.dataset.heating = ''; card.append(host);}
    const z = d.zones[i];
    const activeInput = host.querySelector('input');
    if (d.reason === null && z && activeInput && !activeInput.disabled &&
        (host.dataset.dirty === 'true' || activeInput === document.activeElement)) return;
    delete host.dataset.dirty;
    const previous = host.querySelector('output');
    const requested = host.dataset.requested;
    let message = previous?.textContent || '';
    if (requested && z && Math.abs(Number(requested) - z.target) < 0.001) {
     message = t[9]; delete host.dataset.requested;
    }
    host.replaceChildren();
    if (d.reason !== null || !z) {
     const p = document.createElement('p'); p.textContent = t[d.reason ?? (i === 4 ? 7 : 4)]; host.append(p); return;
    }
    const form = document.createElement('form'), label = document.createElement('label');
    const input = document.createElement('input'), button = document.createElement('button');
    const output = document.createElement('output'); output.setAttribute('aria-live', 'polite');
    output.textContent = message;
    label.textContent = t[0] + ' � ' + t[1]; input.id = 'target-zone-' + (i + 1); label.htmlFor = input.id;
    input.type = 'number'; input.min = z.min; input.max = z.max; input.step = z.step;
    input.value = z.target; input.required = true; button.textContent = t[2]; button.type = 'submit';
    input.addEventListener('input', () => {host.dataset.dirty = 'true';});
    const row = document.createElement('div'); row.style.display = 'flex'; row.style.gap = '6px';
    input.style.minWidth = '0'; input.style.width = '100%';
    for (const [symbol, operation] of [['-', 'stepDown'], ['+', 'stepUp']]) {
     const adjust = document.createElement('button'); adjust.type = 'button'; adjust.textContent = symbol;
     adjust.style.minWidth = '44px'; adjust.style.minHeight = '44px';
     adjust.setAttribute('aria-label', t[1] + ' ' + symbol);
     adjust.addEventListener('click', () => {input[operation](); host.dataset.dirty = 'true';});
     row.append(adjust);
    }
    row.insertBefore(input, row.lastChild);
    form.append(label, row, button, output); host.append(form);
    form.addEventListener('submit', async event => {
     event.preventDefault(); if (busy || !form.reportValidity()) return;
     busy = true; button.disabled = true; output.textContent = t[10];
     try {
      const r = await fetch('/api/heating', {method: 'POST', body: new URLSearchParams({
       csrf, zone: String(i + 1), temperature: input.value
      })});
      if (r.status === 401 || r.status === 403) {location.replace('/'); return;}
      if (!r.ok) throw Error();
      delete host.dataset.dirty; host.dataset.requested = input.value; output.textContent = t[3];
     } catch (_) {output.textContent = t[8];}
     finally {busy = false; button.disabled = false;}
    });
   });
  } catch (_) {
   // Fail closed: old forms must not appear usable after a failed refresh.
   cards.forEach(card => card.querySelectorAll('[data-heating] input, [data-heating] button').forEach(e => {e.disabled = true;}));
  }
 }
 refresh(); setInterval(refresh, 15000);
 document.addEventListener('visibilitychange', refresh);
})();
"""
