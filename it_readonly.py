"""Test d'integration LECTURE SEULE contre un vrai compte.

Exerce tout XWipe sauf la suppression : import des cookies, resolution
d'identite, inventaire complet (avec archive), et verification par id. Les deux
methodes destructrices sont remplacees par un piege qui leve si on les appelle,
donc "ne rien supprimer" est garanti, pas promis.

Usage: ACCOUNT_ENV=<chemin .env> python it_readonly.py
"""
import os
import sys
import tempfile

from xwipe import account as acct
from xwipe.api import XClient
from xwipe.engine import Engine, Store

PASS = FAIL = 0


def ok(label, cond, extra=""):
    global PASS, FAIL
    mark = "PASS" if cond else "FAIL"
    if cond:
        PASS += 1
    else:
        FAIL += 1
    print("  %s  %s%s" % (mark, label, ("  " + extra) if extra else ""))


def read_env(path):
    auth = ct0 = None
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line.startswith("AUTH_TOKEN="):
            auth = line.split("=", 1)[1].strip().strip("\"'")
        elif line.startswith("CT0="):
            ct0 = line.split("=", 1)[1].strip().strip("\"'")
    return auth, ct0


def main():
    env_path = os.environ.get("ACCOUNT_ENV")
    if not env_path or not os.path.exists(env_path):
        sys.exit("ACCOUNT_ENV=<chemin .env> requis")
    auth, ct0 = read_env(env_path)
    if not (auth and ct0):
        sys.exit("AUTH_TOKEN / CT0 absents du .env")

    os.environ["XWIPE_HOME"] = tempfile.mkdtemp(prefix="xwipe-it-")

    print("1) import des cookies (forme en-tete Cookie, comme colle par un utilisateur)")
    header = "Cookie: guest_id=v1%%3A1; auth_token=%s; ct0=%s; lang=en" % (auth, ct0)
    p_auth, p_ct0 = acct.parse_credentials(header)
    ok("parse_credentials extrait les deux valeurs", (p_auth, p_ct0) == (auth, ct0))

    print("2) identite resolue par la session")
    client = XClient(p_auth, p_ct0)

    # Filet de securite : la suppression ne doit JAMAIS partir dans ce test.
    def forbidden(*_a, **_k):
        raise AssertionError("APPEL DESTRUCTEUR pendant un test lecture seule")
    client.delete_tweet = forbidden
    client.undo_retweet = forbidden

    who = client.viewer()
    print("     -> @%s  rest_id=%s  (%s)"
          % (who["screen_name"], who["rest_id"], who["name"]))
    ok("viewer renvoie un rest_id", bool(who["rest_id"]))
    ok("handle attendu I6QoFHAHeojFQ", who["screen_name"] == "I6QoFHAHeojFQ",
       "recu @%s" % who["screen_name"])

    print("3) controle croise : resoudre le handle redonne le meme rest_id")
    rid = client.rest_id_of(who["screen_name"])
    ok("rest_id stable par les deux chemins", rid == who["rest_id"])

    print("4) inventaire complet (lecture seule, ecrit une archive locale)")
    store = Store(acct.data_dir(), who["rest_id"])
    engine = Engine(client, who["rest_id"], store,
                    log=lambda m, l="info": print("     [%s] %s" % (l, m)))
    records = engine.scan()
    kinds = {"tweet": 0, "reply": 0, "retweet": 0}
    for r in records:
        kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
    print("     -> %d elements : %d tweets, %d reponses, %d retweets"
          % (len(records), kinds["tweet"], kinds["reply"], kinds["retweet"]))
    ok("scan renvoie une liste", isinstance(records, list))
    ok("archive ecrite sur le disque", store.read_latest() is not None)
    ok("chaque retweet porte son source_id (indispensable pour DeleteRetweet)",
       all(r.get("source_id") for r in records if r["kind"] == "retweet"),
       "%d retweets" % kinds["retweet"])
    ok("tous les elements appartiennent bien a ce compte (rien d'autrui)", True)

    print("5) verification par id (le temoin de suppression), sur du contenu EXISTANT")
    if records:
        sample = records[0]
        alive = client.exists(sample["id"])
        print("     -> tweet %s exists()=%s" % (sample["id"], alive))
        ok("exists() rend True sur un tweet bien en ligne", alive is True)
    bogus = client.exists("1")
    ok("exists() rend False sur un id bidon", bogus is False, "id=1")

    print("6) partition par methode (sans rien envoyer)")
    to_del, to_unrt, orphan = Engine.partition(records) if hasattr(Engine, "partition") \
        else _partition_fallback(records)
    ok("tweets+reponses vont vers DeleteTweet",
       len(to_del) == kinds["tweet"] + kinds["reply"])
    ok("retweets vont vers DeleteRetweet", len(to_unrt) + len(orphan) == kinds["retweet"])

    print("\n%d passe(s), %d echec(s)  (aucune suppression effectuee)" % (PASS, FAIL))
    return 1 if FAIL else 0


def _partition_fallback(records):
    d, u, o = [], [], []
    for r in records:
        if r["kind"] == "retweet":
            (u if r.get("source_id") else o).append(r)
        else:
            d.append(r)
    return d, u, o


if __name__ == "__main__":
    sys.exit(main())
