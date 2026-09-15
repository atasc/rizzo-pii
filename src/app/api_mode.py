# -*- coding: utf-8 -*-
"""
Modalita' API SERVER: il backend come servizio HTTP per app esterne (tipicamente
in un container), invece che come UI locale di una sola persona.

Tutto da variabili d'ambiente, lette UNA volta all'import di app.py (gunicorn
importa `app:app`, quindi niente CLI):

  PII_API_MODE=1          accende la modalita'. Cambia tre cose:
                          - niente UI: `/`, `/assets/*`, `/favicon.ico` -> 404;
                          - niente endpoint "da desktop": `/config` e `/port-check`
                            -> 404, `POST /settings` (e `/tags`) -> 403. Le
                            preferenze globali non si cambiano via rete: un client
                            non deve poter spegnere il dizionario o escludere tag
                            per tutti gli altri. Si passano per richiesta
                            (`exclude_tags`, `include_mapping`), o si fissano
                            all'avvio con PII_EXCLUDE_TAGS / PII_MAPPING;
                          - ogni errore e' JSON con lo status giusto (un path
                            sbagliato e' 404, non la pagina dell'UI con 200).
  PII_API_KEY=k1,k2       chiavi accettate (piu' d'una = rotazione senza downtime).
  PII_API_KEY_FILE=/path  idem, una chiave per riga (docker/k8s secrets).
                          Con almeno una chiave, TUTTI gli endpoint tranne
                          `/health` e `/healthz` vogliono `Authorization: Bearer <k>`
                          oppure `X-API-Key: <k>`. Vale anche fuori dalla modalita'
                          API (ma l'UI del browser non manda la chiave).
  PII_API_INSECURE=1      in modalita' API senza chiavi il server NON parte (fail-closed:
                          e' un servizio che riceve dati personali). Questa variabile
                          lo permette, per quando l'autenticazione la fa un reverse
                          proxy davanti.
  PII_CORS_ORIGINS=...    origini ammesse per le chiamate da browser, separate da
                          virgola, oppure `*`. Vuoto = nessun header CORS (le chiamate
                          server-to-server funzionano comunque).
  PII_MAX_UPLOAD_MB=50    dimensione massima della richiesta (413 oltre).

Il modulo non dipende dal modello: `install()` si prova su una Flask app qualsiasi.
"""

import os
import secrets
from dataclasses import dataclass, field

from flask import jsonify, request
from werkzeug.exceptions import HTTPException

HEALTH_PATHS = ("/health", "/healthz")
UI_PATHS = ("/", "/favicon.ico")
UI_PREFIXES = ("/assets/",)
DESKTOP_PATHS = ("/config", "/port-check")      # host/porta: roba dello splash di Tauri
SETTINGS_PATHS = ("/settings", "/tags")

# header che un client da browser deve poter leggere dalle risposte di /pdf
EXPOSED_HEADERS = ("Content-Disposition, X-PII-Redactions, X-PII-Residual, "
                   "X-PII-Skipped, X-PII-Notfound")


class ConfigError(Exception):
    """Configurazione che non deve far partire il server."""


def _truthy(value):
    return str(value or "").strip().lower() in ("1", "true", "yes", "y", "on", "si", "sì")


def _split(value):
    return [v.strip() for v in (value or "").replace(";", ",").split(",") if v.strip()]


@dataclass
class ApiConfig:
    enabled: bool = False
    keys: list = field(default_factory=list)
    insecure: bool = False
    cors_origins: list = field(default_factory=list)
    max_upload_mb: int = 50

    @property
    def auth_required(self):
        return bool(self.keys)


def load(env=None):
    """ApiConfig dall'ambiente. Solleva ConfigError sulle configurazioni pericolose
    o non valide: meglio non partire che partire aperti."""
    env = os.environ if env is None else env
    keys = _split(env.get("PII_API_KEY"))
    key_file = env.get("PII_API_KEY_FILE")
    if key_file:
        try:
            with open(key_file, encoding="utf-8") as f:
                keys += [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
        except OSError as e:
            raise ConfigError(f"PII_API_KEY_FILE non leggibile: {e}")
    short = [k for k in keys if len(k) < 16]
    if short:
        raise ConfigError("PII_API_KEY: ogni chiave deve avere almeno 16 caratteri "
                          "(genera con: python -c \"import secrets;print(secrets.token_urlsafe(32))\")")

    try:
        max_mb = int(env.get("PII_MAX_UPLOAD_MB") or 50)
    except ValueError:
        raise ConfigError("PII_MAX_UPLOAD_MB deve essere un intero (MB).")
    if max_mb <= 0:
        raise ConfigError("PII_MAX_UPLOAD_MB deve essere > 0.")

    cfg = ApiConfig(
        enabled=_truthy(env.get("PII_API_MODE")),
        keys=list(dict.fromkeys(keys)),
        insecure=_truthy(env.get("PII_API_INSECURE")),
        cors_origins=_split(env.get("PII_CORS_ORIGINS")),
        max_upload_mb=max_mb,
    )
    if cfg.enabled and not cfg.keys and not cfg.insecure:
        raise ConfigError("PII_API_MODE=1 senza PII_API_KEY / PII_API_KEY_FILE: il server "
                          "non parte senza autenticazione. Imposta una chiave, oppure "
                          "PII_API_INSECURE=1 se l'autenticazione la fa un reverse proxy.")
    return cfg


def _error(msg, status, headers=None):
    resp = jsonify({"error": msg, "status": status})
    resp.status_code = status
    for k, v in (headers or {}).items():
        resp.headers[k] = v
    return resp


def _presented_key():
    auth = request.headers.get("Authorization", "")
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip()
    return request.headers.get("X-API-Key", "").strip()


def _key_ok(cfg, presented):
    if not presented:
        return False
    # confronto a tempo costante su TUTTE le chiavi: il tempo non dice quale ha quasi matchato
    ok = False
    for k in cfg.keys:
        ok |= secrets.compare_digest(presented.encode(), k.encode())
    return ok


def _cors_origin(cfg):
    origin = request.headers.get("Origin")
    if not origin or not cfg.cors_origins:
        return None
    if "*" in cfg.cors_origins:
        return "*"
    return origin if origin in cfg.cors_origins else None


def install(app, cfg, ui_page=None):
    """Aggancia a `app` autenticazione, CORS, blocchi della modalita' API e gestione
    degli errori. `ui_page()` e' la pagina dell'UI: fuori dalla modalita' API un 404
    su una GET dal browser la mostra (comportamento storico)."""
    app.config["MAX_CONTENT_LENGTH"] = cfg.max_upload_mb * 1024 * 1024

    @app.before_request
    def _guard():
        path = request.path

        # preflight CORS: il browser non manda credenziali, va risolto prima dell'auth
        if request.method == "OPTIONS" and request.headers.get("Access-Control-Request-Method"):
            resp = app.make_default_options_response()
            if _cors_origin(cfg):
                resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
                resp.headers["Access-Control-Allow-Headers"] = \
                    "Authorization, X-API-Key, Content-Type"
                resp.headers["Access-Control-Max-Age"] = "600"
            return resp

        if cfg.enabled:
            if path in UI_PATHS or path.startswith(UI_PREFIXES) or path in DESKTOP_PATHS:
                return _error("Not found.", 404)

        if path not in HEALTH_PATHS and cfg.auth_required and not _key_ok(cfg, _presented_key()):
            return _error("API key mancante o non valida.", 401,
                          {"WWW-Authenticate": 'Bearer realm="rizzo-pii"'})

        # Flask applica MAX_CONTENT_LENGTH solo leggendo il body, e get_json(silent=True)
        # inghiotte il 413: un JSON troppo grande diventerebbe "Nessun testo" (400).
        if (request.content_length or 0) > app.config["MAX_CONTENT_LENGTH"]:
            return _error(f"Richiesta oltre il limite di {cfg.max_upload_mb} MB.", 413)

        if cfg.enabled and path in SETTINGS_PATHS and request.method == "POST":
            return _error("In modalita' API le preferenze globali non si cambiano via rete: "
                          "passa exclude_tags / include_mapping nella richiesta, oppure "
                          "PII_EXCLUDE_TAGS / PII_MAPPING all'avvio.", 403)
        return None

    @app.after_request
    def _cors(resp):
        origin = _cors_origin(cfg)
        if origin:
            resp.headers["Access-Control-Allow-Origin"] = origin
            resp.headers["Access-Control-Expose-Headers"] = EXPOSED_HEADERS
            if origin != "*":
                resp.headers.add("Vary", "Origin")
        return resp

    @app.errorhandler(HTTPException)
    def _http_error(e):
        if (e.code == 404 and not cfg.enabled and ui_page is not None
                and request.method == "GET"):
            return ui_page()                    # l'UI resta la "pagina di atterraggio"
        headers = dict(e.get_headers()) if e.code == 405 else {}
        headers.pop("Content-Type", None)
        return _error(e.description if e.code != 404 else "Not found.", e.code, headers)

    @app.errorhandler(Exception)
    def _unhandled(e):
        app.logger.exception("errore non gestito su %s", request.path)
        # in API il dettaglio resta nei log: l'eccezione puo' citare pezzi del documento
        msg = "Errore interno." if cfg.enabled else f"Errore interno: {e}"
        return _error(msg, 500)

    return cfg
