"""Autenticacao OAuth do Mercado Livre (Authorization Code + PKCE).
Tokens ficam criptografados em SQLite para TESTES. Render Free nao tem disco duravel.
"""
import base64
import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import closing
from cryptography.fernet import Fernet, InvalidToken

_AUTH = "https://auth.mercadolivre.com.br/authorization"
_TOKEN = "https://api.mercadolibre.com/oauth/token"
_REFRESH_LOCK = threading.RLock()


class OAuthConfigError(Exception):
    pass


class OAuthFlowError(Exception):
    pass


def _config():
    config = {key: os.environ.get(key, "").strip() for key in
              ("ML_CLIENT_ID", "ML_CLIENT_SECRET", "ML_REDIRECT_URI", "OFERTIX_TOKEN_KEY")}
    if not config["ML_CLIENT_ID"].isdigit() or not config["ML_CLIENT_SECRET"]:
        raise OAuthConfigError("Falta ML_CLIENT_ID ou ML_CLIENT_SECRET no Render.")
    uri = urllib.parse.urlparse(config["ML_REDIRECT_URI"])
    if (uri.scheme != "https" or not uri.hostname or uri.path != "/oauth/callback"
            or uri.username or uri.password or uri.query or uri.fragment):
        raise OAuthConfigError("ML_REDIRECT_URI deve ser uma URL HTTPS completa com /oauth/callback.")
    if len(config["OFERTIX_TOKEN_KEY"]) < 32:
        raise OAuthConfigError("Crie OFERTIX_TOKEN_KEY no Render usando Generate (minimo 32 caracteres).")
    return config


def ready():
    try:
        _config()
        return True
    except OAuthConfigError:
        return False


def _cipher():
    secret = _config()["OFERTIX_TOKEN_KEY"].encode("utf-8")
    key = base64.urlsafe_b64encode(hashlib.sha256(secret).digest())
    return Fernet(key)


def _encrypt(obj):
    return _cipher().encrypt(json.dumps(obj).encode("utf-8")).decode("ascii")


def _decrypt(value):
    try:
        return json.loads(_cipher().decrypt(value.encode("ascii")))
    except (InvalidToken, ValueError, TypeError, json.JSONDecodeError):
        raise OAuthFlowError("Dados OAuth invalidos ou chave de criptografia alterada.")


def init(db_path):
    with closing(sqlite3.connect(db_path, timeout=20)) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS oauth_pending (
            state_hash TEXT PRIMARY KEY,
            verifier_encrypted TEXT NOT NULL,
            expires_at INTEGER NOT NULL
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS oauth_tokens (
            singleton INTEGER PRIMARY KEY CHECK (singleton=1),
            token_encrypted TEXT NOT NULL
        )""")
        db.commit()


def start(db_path):
    config = _config()
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    now = int(time.time())
    with closing(sqlite3.connect(db_path, timeout=20)) as db:
        db.execute("DELETE FROM oauth_pending WHERE expires_at < ?", (now,))
        db.execute("INSERT INTO oauth_pending VALUES (?,?,?)",
                   (hashlib.sha256(state.encode("ascii")).hexdigest(),
                    _encrypt({"verifier": verifier}), now + 600))
        db.commit()
    query = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": config["ML_CLIENT_ID"],
        "redirect_uri": config["ML_REDIRECT_URI"],
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    return _AUTH + "?" + query


def _post_token(payload):
    request = urllib.request.Request(
        _TOKEN,
        data=urllib.parse.urlencode(payload).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            reply = json.load(response)
    except urllib.error.HTTPError as exc:
        raise OAuthFlowError("Autorizacao recusada pelo Mercado Livre (HTTP " + str(exc.code) + ").")
    except (urllib.error.URLError, TimeoutError):
        raise OAuthFlowError("Nao foi possivel conectar ao servidor OAuth do Mercado Livre.")
    if not isinstance(reply, dict) or not reply.get("access_token"):
        raise OAuthFlowError("O Mercado Livre nao retornou um access token valido.")
    return reply


def _save_token(db_path, data):
    previous = _load_token(db_path)
    token = {
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token") or (previous or {}).get("refresh_token"),
        "expires_at": int(time.time()) + max(60, int(data.get("expires_in", 21600))),
        "scope": data.get("scope", ""),
    }
    with closing(sqlite3.connect(db_path, timeout=20)) as db:
        db.execute(
            "INSERT OR REPLACE INTO oauth_tokens(singleton,token_encrypted) VALUES (1,?)",
            (_encrypt(token),),
        )
        db.commit()
    return token


def _load_token(db_path):
    with closing(sqlite3.connect(db_path, timeout=20)) as db:
        row = db.execute("SELECT token_encrypted FROM oauth_tokens WHERE singleton=1").fetchone()
    if not row:
        return None
    return _decrypt(row[0])


def connected(db_path):
    try:
        return bool(ready() and _load_token(db_path))
    except (OAuthFlowError, sqlite3.Error):
        return False


def exchange(db_path, state, code):
    config = _config()
    if not state or not code or len(state) > 300 or len(code) > 3000:
        raise OAuthFlowError("Resposta OAuth incompleta.")
    state_hash = hashlib.sha256(state.encode("utf-8")).hexdigest()
    # Um state valido pode ser usado uma unica vez e tem prazo de 10 minutos.
    with closing(sqlite3.connect(db_path, timeout=20)) as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT verifier_encrypted,expires_at FROM oauth_pending WHERE state_hash=?",
            (state_hash,),
        ).fetchone()
        db.execute("DELETE FROM oauth_pending WHERE state_hash=?", (state_hash,))
        db.commit()
    if not row or row[1] <= int(time.time()):
        raise OAuthFlowError("Autorizacao expirada ou ja utilizada. Inicie novamente no painel.")
    verifier = _decrypt(row[0])["verifier"]
    response = _post_token({
        "grant_type": "authorization_code",
        "client_id": config["ML_CLIENT_ID"],
        "client_secret": config["ML_CLIENT_SECRET"],
        "code": code,
        "redirect_uri": config["ML_REDIRECT_URI"],
        "code_verifier": verifier,
    })
    return _save_token(db_path, response)


def access_token(db_path):
    with _REFRESH_LOCK:
        token = _load_token(db_path)
        if not token:
            return None
        if token.get("expires_at", 0) > int(time.time()) + 120:
            return token["access_token"]
        refresh = token.get("refresh_token")
        if not refresh:
            raise OAuthFlowError("Token expirou e nao existe Refresh Token. Reconecte a conta.")
        config = _config()
        response = _post_token({
            "grant_type": "refresh_token",
            "client_id": config["ML_CLIENT_ID"],
            "client_secret": config["ML_CLIENT_SECRET"],
            "refresh_token": refresh,
        })
        if not response.get("refresh_token"):
            raise OAuthFlowError("O Mercado Livre nao devolveu um novo Refresh Token. Reconecte a conta.")
        updated = _save_token(db_path, response)
        return updated["access_token"]
