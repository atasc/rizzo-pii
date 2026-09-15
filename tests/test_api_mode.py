# -*- coding: utf-8 -*-
"""Modalita' API server (src/app/api_mode.py): auth, CORS, blocchi, errori JSON.

Si prova `api_mode.install()` su una Flask app finta con le stesse rotte di app.py:
niente modello, niente torch. Serve solo flask; se manca (la CI non lo installa)
il test si salta invece di fallire.
"""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "app"))

try:
    from flask import Flask, jsonify, request
    import api_mode
except ImportError:                                    # pragma: no cover
    Flask = None

KEY = "k" * 32


def make_app(env):
    cfg = api_mode.load(env)
    app = Flask(__name__)

    @app.route("/")
    def index():
        return "<html>ui</html>"

    @app.route("/assets/<path:fn>")
    def assets(fn):
        return "asset"

    @app.route("/health")
    @app.route("/healthz")
    def health():
        return jsonify({"status": "ok"})

    @app.route("/analyze", methods=["POST"])
    def analyze():
        if (request.get_json(silent=True) or {}).get("boom"):
            raise RuntimeError("Mario Rossi")        # il dettaglio non deve uscire in API
        return jsonify({"anonymized_text": "[FULLNAME_1]"})

    @app.route("/settings", methods=["GET", "POST"])
    @app.route("/tags", methods=["GET", "POST"])
    def settings():
        return jsonify({"ok": True})

    @app.route("/config", methods=["GET", "POST"])
    def config():
        return jsonify({"host": "127.0.0.1"})

    @app.route("/port-check")
    def port_check():
        return jsonify({"available": True})

    api_mode.install(app, cfg, ui_page=lambda: "<html>ui</html>")
    return app.test_client()


@unittest.skipIf(Flask is None, "flask non installato")
class LoadTest(unittest.TestCase):
    def test_default_disattivo(self):
        cfg = api_mode.load({})
        self.assertFalse(cfg.enabled)
        self.assertFalse(cfg.auth_required)
        self.assertEqual(cfg.max_upload_mb, 50)

    def test_api_senza_chiave_non_parte(self):
        with self.assertRaises(api_mode.ConfigError):
            api_mode.load({"PII_API_MODE": "1"})

    def test_insecure_esplicito(self):
        cfg = api_mode.load({"PII_API_MODE": "1", "PII_API_INSECURE": "1"})
        self.assertTrue(cfg.enabled)
        self.assertFalse(cfg.auth_required)

    def test_chiave_corta_rifiutata(self):
        with self.assertRaises(api_mode.ConfigError):
            api_mode.load({"PII_API_MODE": "1", "PII_API_KEY": "corta"})

    def test_chiavi_da_env_e_file(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write("# commento\n" + "b" * 20 + "\n\n")
        cfg = api_mode.load({"PII_API_KEY": KEY + "," + "a" * 20, "PII_API_KEY_FILE": f.name})
        self.assertEqual(cfg.keys, [KEY, "a" * 20, "b" * 20])
        Path(f.name).unlink()

    def test_file_mancante(self):
        with self.assertRaises(api_mode.ConfigError):
            api_mode.load({"PII_API_KEY_FILE": "/non/esiste"})

    def test_max_upload_non_valido(self):
        with self.assertRaises(api_mode.ConfigError):
            api_mode.load({"PII_MAX_UPLOAD_MB": "tanti"})


@unittest.skipIf(Flask is None, "flask non installato")
class ApiModeTest(unittest.TestCase):
    def setUp(self):
        self.c = make_app({"PII_API_MODE": "1", "PII_API_KEY": KEY})
        self.auth = {"Authorization": f"Bearer {KEY}"}

    def test_health_senza_chiave(self):
        self.assertEqual(self.c.get("/health").status_code, 200)
        self.assertEqual(self.c.get("/healthz").status_code, 200)

    def test_analyze_richiede_chiave(self):
        r = self.c.post("/analyze", json={"text": "x"})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.get_json()["status"], 401)
        self.assertIn("Bearer", r.headers["WWW-Authenticate"])
        r = self.c.post("/analyze", json={"text": "x"}, headers={"Authorization": "Bearer sbagliata"})
        self.assertEqual(r.status_code, 401)

    def test_analyze_con_bearer_e_x_api_key(self):
        self.assertEqual(self.c.post("/analyze", json={}, headers=self.auth).status_code, 200)
        self.assertEqual(self.c.post("/analyze", json={}, headers={"X-API-Key": KEY}).status_code, 200)

    def test_ui_e_desktop_spenti(self):
        for path in ("/", "/assets/logo.png", "/config", "/port-check"):
            r = self.c.get(path, headers=self.auth)
            self.assertEqual(r.status_code, 404, path)
            self.assertTrue(r.is_json, path)

    def test_settings_sola_lettura(self):
        self.assertEqual(self.c.get("/settings", headers=self.auth).status_code, 200)
        for path in ("/settings", "/tags"):
            r = self.c.post(path, json={"mapping_enabled": False}, headers=self.auth)
            self.assertEqual(r.status_code, 403, path)

    def test_errori_json(self):
        r = self.c.get("/non-esiste", headers=self.auth)
        self.assertEqual((r.status_code, r.is_json), (404, True))
        r = self.c.get("/analyze", headers=self.auth)
        self.assertEqual((r.status_code, r.is_json), (405, True))
        self.assertIn("POST", r.headers["Allow"])

    def test_500_non_rivela_il_documento(self):
        r = self.c.post("/analyze", json={"boom": True}, headers=self.auth)
        self.assertEqual(r.status_code, 500)
        self.assertNotIn("Mario", r.get_data(as_text=True))

    def test_413_json(self):
        c = make_app({"PII_API_MODE": "1", "PII_API_KEY": KEY, "PII_MAX_UPLOAD_MB": "1"})
        r = c.post("/analyze", data=b"x" * (2 * 1024 * 1024),
                   headers={**self.auth, "Content-Type": "application/octet-stream"})
        self.assertEqual((r.status_code, r.is_json), (413, True))

    def test_nessun_cors_di_default(self):
        r = self.c.post("/analyze", json={}, headers={**self.auth, "Origin": "https://x.it"})
        self.assertNotIn("Access-Control-Allow-Origin", r.headers)


@unittest.skipIf(Flask is None, "flask non installato")
class CorsTest(unittest.TestCase):
    def setUp(self):
        self.c = make_app({"PII_API_MODE": "1", "PII_API_KEY": KEY,
                           "PII_CORS_ORIGINS": "https://app.studio.it"})

    def test_preflight_senza_chiave(self):
        r = self.c.options("/analyze", headers={"Origin": "https://app.studio.it",
                                                "Access-Control-Request-Method": "POST"})
        self.assertLess(r.status_code, 300)
        self.assertEqual(r.headers["Access-Control-Allow-Origin"], "https://app.studio.it")
        self.assertIn("Authorization", r.headers["Access-Control-Allow-Headers"])

    def test_origine_ammessa(self):
        r = self.c.post("/analyze", json={}, headers={"Origin": "https://app.studio.it",
                                                      "X-API-Key": KEY})
        self.assertEqual(r.headers["Access-Control-Allow-Origin"], "https://app.studio.it")
        self.assertIn("X-PII-Residual", r.headers["Access-Control-Expose-Headers"])

    def test_origine_non_ammessa(self):
        r = self.c.post("/analyze", json={}, headers={"Origin": "https://evil.example",
                                                      "X-API-Key": KEY})
        self.assertNotIn("Access-Control-Allow-Origin", r.headers)

    def test_anche_il_401_ha_cors(self):
        # senza header CORS il browser nasconde lo status e il client vede solo "network error"
        r = self.c.post("/analyze", json={}, headers={"Origin": "https://app.studio.it"})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.headers["Access-Control-Allow-Origin"], "https://app.studio.it")


@unittest.skipIf(Flask is None, "flask non installato")
class DesktopModeTest(unittest.TestCase):
    """Senza variabili il comportamento dell'app desktop non cambia."""

    def setUp(self):
        self.c = make_app({})

    def test_tutto_aperto(self):
        self.assertEqual(self.c.get("/").status_code, 200)
        self.assertEqual(self.c.get("/config").status_code, 200)
        self.assertEqual(self.c.post("/settings", json={}).status_code, 200)
        self.assertEqual(self.c.post("/analyze", json={}).status_code, 200)

    def test_404_get_mostra_ui(self):
        r = self.c.get("/qualsiasi")
        self.assertIn("ui", r.get_data(as_text=True))

    def test_chiave_vale_anche_fuori_da_api(self):
        c = make_app({"PII_API_KEY": KEY})
        self.assertEqual(c.post("/analyze", json={}).status_code, 401)
        self.assertEqual(c.post("/analyze", json={}, headers={"X-API-Key": KEY}).status_code, 200)


if __name__ == "__main__":
    unittest.main()
