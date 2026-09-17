"""Import et stockage des comptes.

Import : on accepte ce que les gens ont reellement sous la main plutot que
d'imposer un format. Export de cookies d'une extension, en-tete `Cookie` copie
depuis les outils de developpement, ou les deux valeurs collees a la main.

Stockage : sous Windows les cookies sont chiffres par DPAPI, lie au compte
Windows courant. Un fichier vole sur une autre machine ou sous un autre
utilisateur est inexploitable. Ailleurs on ecrit en clair avec permissions
restreintes et on le DIT, plutot que de faire semblant avec un encodage.
"""
from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes as wintypes
import json
import os
import re
import sys
import time

APP_NAME = "XWipe"
AUTH_RE = re.compile(r"^[0-9a-f]{32,60}$", re.I)
CT0_RE = re.compile(r"^[0-9a-f]{100,200}$", re.I)


class ImportError_(ValueError):
    """Entree illisible, message destine a l'utilisateur."""


# --------------------------------------------------------------------- DPAPI

class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> _Blob:
    buf = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def _blob_bytes(b: _Blob) -> bytes:
    return ctypes.string_at(b.pbData, b.cbData)


def dpapi_available() -> bool:
    return sys.platform == "win32"


def _dpapi(func_name: str, data: bytes) -> bytes:
    crypt32 = ctypes.WinDLL("crypt32.dll")
    kernel32 = ctypes.WinDLL("kernel32.dll")
    fn = getattr(crypt32, func_name)
    src, out = _blob(data), _Blob()
    desc = ctypes.c_wchar_p()
    if func_name == "CryptProtectData":
        ok = fn(ctypes.byref(src), APP_NAME, None, None, None, 0, ctypes.byref(out))
    else:
        ok = fn(ctypes.byref(src), ctypes.byref(desc), None, None, None, 0,
                ctypes.byref(out))
    if not ok:
        raise OSError("DPAPI %s a echoue (code %d)" % (func_name, ctypes.get_last_error()))
    try:
        return _blob_bytes(out)
    finally:
        kernel32.LocalFree(out.pbData)


def seal(text: str) -> dict:
    """Chiffre un secret pour l'utilisateur Windows courant."""
    raw = text.encode("utf-8")
    if dpapi_available():
        try:
            return {"enc": "dpapi",
                    "data": base64.b64encode(_dpapi("CryptProtectData", raw)).decode()}
        except OSError:
            pass  # on retombe en clair plutot que de perdre le compte
    return {"enc": "plain", "data": base64.b64encode(raw).decode()}


def unseal(box: dict) -> str:
    data = base64.b64decode(box["data"])
    if box.get("enc") == "dpapi":
        return _dpapi("CryptUnprotectData", data).decode("utf-8")
    return data.decode("utf-8")


# -------------------------------------------------------------------- import

def _from_cookie_export(text: str) -> tuple[str, str] | None:
    """Export JSON d'une extension de cookies : liste d'objets name/value."""
    try:
        blob = json.loads(text)
    except ValueError:
        return None
    items = None
    if isinstance(blob, list):
        items = blob
    elif isinstance(blob, dict):
        for key in ("cookies", "Cookies"):
            if isinstance(blob.get(key), list):
                items = blob[key]
                break
        if items is None:
            # dict simple {"auth_token": "...", "ct0": "..."}
            a = blob.get("auth_token") or blob.get("AUTH_TOKEN")
            c = blob.get("ct0") or blob.get("CT0")
            if a and c:
                return str(a).strip(), str(c).strip()
            return None
    if items is None:
        return None
    found: dict[str, str] = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or it.get("Name") or "").strip()
        value = it.get("value", it.get("Value"))
        if name in ("auth_token", "ct0") and isinstance(value, str) and value.strip():
            domain = str(it.get("domain") or "")
            # Si plusieurs domaines exportes, preferer x.com / twitter.com.
            if name not in found or ("x.com" in domain or "twitter.com" in domain):
                found[name] = value.strip()
    if "auth_token" in found and "ct0" in found:
        return found["auth_token"], found["ct0"]
    return None


def _from_cookie_header(text: str) -> tuple[str, str] | None:
    """En-tete `Cookie:` copie depuis les outils de developpement."""
    pairs = dict(re.findall(r"(auth_token|ct0)\s*=\s*([^;\s\"',]+)", text))
    if "auth_token" in pairs and "ct0" in pairs:
        return pairs["auth_token"], pairs["ct0"]
    return None


def _from_loose_tokens(text: str) -> tuple[str, str] | None:
    """Deux valeurs collees sans etiquette : on les distingue par leur forme.

    auth_token fait ~40 caracteres hexa, ct0 ~160. Sans cette heuristique on ne
    peut pas savoir laquelle est laquelle, et les inverser donne un 403 opaque.
    """
    tokens = re.findall(r"\b[0-9a-f]{32,200}\b", text, re.I)
    auth = [t for t in tokens if AUTH_RE.match(t)]
    ct0 = [t for t in tokens if CT0_RE.match(t)]
    if auth and ct0:
        return auth[0], ct0[0]
    return None


def parse_credentials(text: str) -> tuple[str, str]:
    """Extrait (auth_token, ct0) de n'importe laquelle des formes acceptees."""
    text = (text or "").strip()
    if not text:
        raise ImportError_("Colle d'abord tes cookies.")
    for parser in (_from_cookie_export, _from_cookie_header, _from_loose_tokens):
        got = parser(text)
        if got:
            auth, ct0 = got
            if not AUTH_RE.match(auth):
                raise ImportError_(
                    "auth_token invalide : attendu ~40 caracteres hexadecimaux, "
                    "recu %d caracteres." % len(auth))
            if len(ct0) < 100:
                raise ImportError_(
                    "ct0 invalide : attendu ~160 caracteres hexadecimaux, "
                    "recu %d caracteres." % len(ct0))
            return auth, ct0
    raise ImportError_(
        "auth_token et ct0 introuvables.\n\n"
        "Accepte : l'export JSON d'une extension de cookies, un en-tete "
        "Cookie copie depuis les outils de developpement, ou les deux valeurs "
        "collees l'une sous l'autre."
    )


# ------------------------------------------------------------------- stockage

def data_dir() -> str:
    """Dossier de donnees, a cote de l'executable s'il est portable."""
    env = os.environ.get("XWIPE_HOME")
    if env:
        base = env
    elif sys.platform == "win32":
        base = os.path.join(os.environ.get("APPDATA") or
                            os.path.expanduser("~"), APP_NAME)
    else:
        base = os.path.join(os.path.expanduser("~"), "." + APP_NAME.lower())
    os.makedirs(base, exist_ok=True)
    return base


def _store_path() -> str:
    return os.path.join(data_dir(), "accounts.json")


def _restrict(path: str) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def load_accounts() -> list[dict]:
    p = _store_path()
    if not os.path.exists(p):
        return []
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
    except (ValueError, OSError):
        return []
    return data.get("accounts", []) if isinstance(data, dict) else []


def save_accounts(accounts: list[dict]) -> None:
    p = _store_path()
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "accounts": accounts}, f, ensure_ascii=False, indent=2)
    _restrict(tmp)
    os.replace(tmp, p)
    _restrict(p)


def upsert_account(rest_id: str, screen_name: str, name: str,
                   auth_token: str, ct0: str) -> dict:
    """Ajoute ou met a jour un compte, indexe par rest_id.

    L'index est le rest_id et non le pseudo : un compte renomme reste le meme
    compte, et deux pseudos differents peuvent avoir designe la meme personne.
    """
    accounts = load_accounts()
    entry = {
        "rest_id": rest_id,
        "screen_name": screen_name,
        "name": name,
        "auth": seal(auth_token),
        "ct0": seal(ct0),
        "updated_at": int(time.time()),
    }
    for i, a in enumerate(accounts):
        if a.get("rest_id") == rest_id:
            accounts[i] = entry
            break
    else:
        accounts.append(entry)
    save_accounts(accounts)
    return entry


def remove_account(rest_id: str) -> None:
    save_accounts([a for a in load_accounts() if a.get("rest_id") != rest_id])


def credentials_of(account: dict) -> tuple[str, str]:
    return unseal(account["auth"]), unseal(account["ct0"])
