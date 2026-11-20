import asyncio
import copy
import json
import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch, AsyncMock, mock_open

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import heating
import app as portal

STATE = {"state": "heat", "attributes": {"temperature": 21, "min_temp": 5,
         "max_temp": 35, "target_temp_step": 0.5, "supported_features": 1}}


class Reply:
    def __init__(self, data, status=200):
        self.data, self.status = data, status
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        pass
    async def json(self):
        return self.data
    async def read(self):
        return b"[]"


class HA:
    def __init__(self):
        self.calls = []
        self.rental = "Arriv�e"
        self.state = copy.deepcopy(STATE)
        self.status = 200
        self.after_read = None
    def get(self, url, **kwargs):
        if url.endswith("/config"):
            data = {"unit_system": {"temperature": "�C"}}
        elif "input_select" in url:
            data = {"state": self.rental}
        else:
            data = self.state
        if self.after_read:
            self.after_read()
        return Reply(data)
    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return Reply([], self.status)


class HeatingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = patch.dict(os.environ, {"SUPERVISOR_TOKEN": "test-only"})
        self.env.start()
        self.season = patch.object(heating, "winter", return_value=True)
        self.winter_mock = self.season.start()
        self.session = {"csrf": "secret"}
        self.ha = HA()
        self.app = web.Application()
        self.app["settings"] = {"zone_entities": [f"climate.termostatoz{i}" for i in range(1, 5)],
            "zone5_entity": "", "floor_heating_enabled": True,
            "stay_sources": {"rental_status_entity": "input_select.statut_location"}}
        self.app["zones_cache"] = {"refresh_at": 999}
        self.app["ha_client"] = self.ha
        heating.register(self.app, lambda r: self.session if r.headers.get("X-Test-Session") == "yes" else None,
            lambda form, item: form.get("csrf") == item["csrf"], lambda r: r.query.get("lang", "fr"))
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()
    async def asyncTearDown(self):
        await self.client.close()
        self.season.stop()
        self.env.stop()
    async def post(self, **values):
        data = {"csrf": "secret", "zone": "3", "temperature": "21.5"}
        data.update(values)
        return await self.client.post('/api/heating', data=data, headers={"X-Test-Session": "yes"})
    async def test_exact_scoped_command(self):
        response = await self.post()
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["accepted"], True)
        self.assertEqual(self.ha.calls[0][1]["json"], {"entity_id": "climate.termostatoz3", "temperature": 21.5})
        self.assertTrue(self.ha.calls[0][0].endswith('/services/climate/set_temperature'))
        self.assertEqual(self.app["zones_cache"]["refresh_at"], 0)
    async def test_anonymous_cannot_read_or_write(self):
        for method in (self.client.get, self.client.post):
            response = await method('/api/heating')
            self.assertEqual(response.status, 401)
        self.assertEqual(self.ha.calls, [])
    async def test_csrf(self):
        self.assertEqual((await self.post(csrf="wrong")).status, 403)
        self.assertEqual(self.ha.calls, [])
    async def test_summer(self):
        self.winter_mock.return_value = False
        self.assertEqual((await self.post()).status, 403)
        self.assertEqual(self.ha.calls, [])
    async def test_departure_and_maintenance(self):
        for mode in ("D�part", "Entretien"):
            self.ha.rental = mode
            self.assertEqual((await self.post(zone="1" if mode == "D�part" else "2")).status, 409)
        self.assertEqual(self.ha.calls, [])
    async def test_invalid_zone_and_uninstalled(self):
        self.assertEqual((await self.post(zone="climate.other")).status, 400)
        self.assertEqual((await self.post(zone="5")).status, 403)
        self.assertEqual(self.ha.calls, [])
    async def test_invalid_values(self):
        for zone, value in zip(("1", "2", "3", "4"), ("nan", "99", "21.2", "-1")):
            self.assertEqual((await self.post(zone=zone, temperature=value)).status, 400)
        self.assertEqual(self.ha.calls, [])
    async def test_session_revoked_during_read(self):
        self.ha.after_read = lambda: setattr(self, 'session', None)
        self.assertEqual((await self.post()).status, 403)
        self.assertEqual(self.ha.calls, [])
    async def test_upstream_failure(self):
        self.ha.status = 500
        self.assertEqual((await self.post()).status, 502)
    async def test_repeat_limited(self):
        self.assertEqual((await self.post()).status, 200)
        self.assertEqual((await self.post()).status, 429)
        self.assertEqual(len(self.ha.calls), 1)
    async def test_unknown_mode_with_valid_attributes(self):
        self.ha.state['state'] = 'unknown'
        self.assertEqual((await self.post()).status, 200)
    async def test_unavailable(self):
        self.ha.state['state'] = 'unavailable'
        self.assertEqual((await self.post()).status, 409)
        self.assertEqual(self.ha.calls, [])
    async def test_disabled(self):
        self.app['settings']['floor_heating_enabled'] = False
        self.assertEqual((await self.post()).status, 403)
    async def test_four_languages(self):
        for lang in ('fr', 'en', 'nl', 'es'):
            r = await self.client.get('/api/heating?lang=' + lang, headers={'X-Test-Session': 'yes'})
            data = await r.json()
            self.assertEqual(data['text'], heating.TEXT[lang])
            self.assertIsNone(data['zones'][4])


class RulesTests(unittest.TestCase):
    def test_season_madrid_boundaries(self):
        for date, expected in [('2026-09-30T21:59:59+00:00', False),
            ('2026-09-30T22:00:00+00:00', True), ('2027-04-30T21:59:59+00:00', True),
            ('2027-04-30T22:00:00+00:00', False), ('2027-01-01T00:00:00+00:00', True)]:
            self.assertEqual(heating.winter(datetime.fromisoformat(date)), expected)
    def test_metadata_and_values(self):
        meta = heating.thermostat(STATE, '�C')
        self.assertTrue(heating.valid_target('21.5', meta))
        for v in (None, True, 'nan', 'inf', '21.2', 36):
            self.assertFalse(heating.valid_target(v, meta))
        self.assertIsNone(heating.thermostat(STATE, '�F'))
        self.assertIsNone(heating.thermostat({'attributes': None}, '�C'))


class PortalTests(unittest.IsolatedAsyncioTestCase):
    async def test_home_and_configuration_compatibility(self):
        options = {'portal_url': 'https://example.test', 'guest_code': 'a' * 20,
                   'expires_at': '2099-12-31T12:00:00+01:00'}
        with patch('builtins.open', mock_open(read_data=json.dumps(options))):
            settings = portal.load_settings()
        self.assertFalse(settings['floor_heating_enabled'])
        self.assertEqual(settings['zone5_entity'], '')
        self.assertEqual(len(settings['zone_entities']), 4)
        request = type('Request', (), {'app': {'settings': settings}})()
        item = {'csrf': 'test-csrf'}
        energy = portal.stay_energy_data({}, {}, {}, {})
        for lang in ('fr', 'en', 'nl', 'es'):
            with patch.object(portal, 'language', return_value=lang), \
                 patch.object(portal, 'get_session', return_value=item), \
                 patch.object(portal, 'stay_expired', return_value=False), \
                 patch.object(portal, 'zone_temperatures', new=AsyncMock(return_value=[21, 22, 23, 24, None])), \
                 patch.object(portal, 'cached_stay_energy', new=AsyncMock(return_value=energy)):
                response = await portal.home(request)
            self.assertEqual(response.status, 200)
            zones_html = response.text.split('<section id="zones"', 1)[1].split('</section>', 1)[0]
            self.assertEqual(zones_html.count('class="zone-card"'), 5)
            self.assertIn('/heating.js', response.text)
            self.assertIn('test-csrf', response.text)
            self.assertIn(portal.LABELS[lang]['soon'], response.text)

    async def test_uninstalled_zone_is_not_queried(self):
        ha = HA()
        calls = []
        original = ha.get
        def get(url, **kwargs):
            calls.append(url)
            return original(url, **kwargs)
        ha.get = get
        request = type('Request', (), {'app': {'settings': {
            'zone_entities': [f'climate.termostatoz{i}' for i in range(1, 5)],
            'zone5_entity': ''}, 'ha_client': ha}})()
        with patch.dict(os.environ, {'SUPERVISOR_TOKEN': 'test'}):
            values = await portal.read_zone_temperatures(request)
        self.assertEqual(len(values), 5)
        self.assertIsNone(values[4])
        self.assertEqual(len(calls), 5)  # config plus four installed thermostats


if __name__ == '__main__':
    unittest.main()
