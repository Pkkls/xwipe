"""Tests hors-ligne de XWipe. Aucun appel reseau.

Chaque test porte un temoin : il verifie qu'un cas correct passe ET qu'un cas
casse est bien rejete, sinon un test peut etre vert sans rien prouver.

    python tests.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from xwipe import account as acct  # noqa: E402
from xwipe.engine import Engine, Result  # noqa: E402

PASS, FAIL = 0, 0


def check(label, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS  " + label)
    else:
        FAIL += 1
        print("  FAIL  " + label)


AUTH = "a1b2c3d4a1b2c3d4a1b2c3d4a1b2c3d4a1b2c3d4"           # bidon, juste la forme (40 hex)
CT0 = "8" * 160                                              # bidon (160 hex)


def test_import_formats():
    print("import des cookies")
    a, c = acct.parse_credentials('[{"name":"auth_token","value":"%s","domain":".x.com"},'
                                  '{"name":"ct0","value":"%s","domain":".x.com"}]' % (AUTH, CT0))
    check("export JSON d'extension", (a, c) == (AUTH, CT0))

    a, c = acct.parse_credentials("Cookie: guest_id=v1; auth_token=%s; ct0=%s; lang=fr"
                                  % (AUTH, CT0))
    check("en-tete Cookie brut", (a, c) == (AUTH, CT0))

    a, c = acct.parse_credentials("%s\n%s\n" % (AUTH, CT0))
    check("deux valeurs nues, ordre normal", (a, c) == (AUTH, CT0))

    a, c = acct.parse_credentials("%s\n%s" % (CT0, AUTH))
    check("deux valeurs nues, ordre inverse (desambigue par la forme)",
          (a, c) == (AUTH, CT0))

    a, c = acct.parse_credentials('{"auth_token":"%s","ct0":"%s"}' % (AUTH, CT0))
    check("dict simple", (a, c) == (AUTH, CT0))

    # Export brut EditThisCookie : tableau complet, champs supplementaires,
    # cookies parasites. C'est le "clique Exporter, colle tout" du produit.
    etc = json.dumps([
        {"domain": ".x.com", "name": "guest_id", "value": "v1%3A123",
         "path": "/", "secure": True, "httpOnly": False, "id": 1},
        {"domain": ".x.com", "name": "__cf_bm", "value": "abcDEF_-123",
         "path": "/", "secure": True, "httpOnly": True, "id": 2},
        {"domain": ".x.com", "name": "auth_token", "value": AUTH,
         "path": "/", "secure": True, "httpOnly": True, "expirationDate": 1.0, "id": 3},
        {"domain": ".x.com", "name": "ct0", "value": CT0,
         "path": "/", "secure": True, "httpOnly": False, "id": 4},
        {"domain": "x.com", "name": "lang", "value": "fr", "id": 5},
    ])
    a, c = acct.parse_credentials(etc)
    check("export brut EditThisCookie (tableau complet + parasites)",
          (a, c) == (AUTH, CT0))

    # temoins : ces entrees DOIVENT echouer
    for bad, why in [("", "vide"),
                     ("juste du texte sans cookie", "aucun token"),
                     (AUTH, "ct0 manquant"),
                     ("auth_token=xyz; ct0=abc", "formes invalides")]:
        try:
            acct.parse_credentials(bad)
            check("temoin rejet (%s)" % why, False)
        except acct.ImportError_:
            check("temoin rejet (%s)" % why, True)


def test_seal():
    print("chiffrement au repos")
    box = acct.seal("secret-valeur")
    check("unseal(seal(x)) == x", acct.unseal(box) == "secret-valeur")
    check("le clair n'apparait pas dans le blob", "secret-valeur" not in str(box))
    if acct.dpapi_available():
        check("DPAPI utilise sous Windows", box.get("enc") == "dpapi")
    else:
        check("repli en clair annonce hors Windows", box.get("enc") == "plain")


def test_store_roundtrip():
    print("stockage des comptes")
    with tempfile.TemporaryDirectory() as d:
        os.environ["XWIPE_HOME"] = d
        acct.upsert_account("111", "alice", "Alice", AUTH, CT0)
        acct.upsert_account("222", "bob", "Bob", AUTH, CT0)
        accts = acct.load_accounts()
        check("deux comptes stockes", len(accts) == 2)
        # meme rest_id => remplacement, pas doublon
        acct.upsert_account("111", "alice2", "Alice 2", AUTH, CT0)
        accts = acct.load_accounts()
        check("re-import du meme rest_id remplace, ne duplique pas", len(accts) == 2)
        got = [a for a in accts if a["rest_id"] == "111"][0]
        check("pseudo mis a jour", got["screen_name"] == "alice2")
        a, c = acct.credentials_of(got)
        check("cookies relus intacts", (a, c) == (AUTH, CT0))
        acct.remove_account("111")
        check("suppression d'un compte", len(acct.load_accounts()) == 1)
        del os.environ["XWIPE_HOME"]


def test_partition_method():
    print("choix de la methode de suppression")
    recs = [
        {"id": "1", "kind": "tweet", "source_id": None},
        {"id": "2", "kind": "reply", "source_id": None},
        {"id": "3", "kind": "retweet", "source_id": "900"},
        {"id": "4", "kind": "retweet", "source_id": None},  # orphelin
    ]

    class FakeClient:
        def __init__(self):
            self.deleted, self.unrt = [], []
            self.alive = set()

        def set_stop_check(self, fn):
            pass

        def delete_tweet(self, tid):
            self.deleted.append(tid)
            return True

        def undo_retweet(self, src):
            self.unrt.append(src)
            return True

        def exists(self, tid):
            return tid in self.alive

    fc = FakeClient()

    class FakeStore:
        def read_latest(self):
            return {"count": 4}

        def load_state(self):
            return {}

        def save_state(self, s):
            pass

    eng = Engine(fc, "42", FakeStore())
    res = eng.delete_selection(recs, delay=0, verify=True)
    check("tweet et reponse -> delete_tweet sur leur id",
          set(fc.deleted) == {"1", "2"})
    check("retweet -> undo_retweet sur le SOURCE, jamais delete_tweet",
          fc.unrt == ["900"] and "3" not in fc.deleted)
    check("retweet orphelin (sans source) compte en echec, pas en succes",
          res.failed == 1 and res.deleted == 3)
    # temoin : un tweet encore vivant apres coup est retente puis compte en echec
    fc2 = FakeClient()
    fc2.alive = {"1"}  # X pretend l'avoir supprime mais il reste
    eng2 = Engine(fc2, "42", FakeStore())
    res2 = eng2.delete_selection(
        [{"id": "1", "kind": "tweet", "source_id": None}], delay=0, verify=True)
    check("temoin: 200 menteur -> element retente puis marque en echec",
          res2.failed == 1 and res2.deleted == 0)


def test_shred_archives():
    print("purge locale des archives")
    import tempfile
    from xwipe.engine import Store
    with tempfile.TemporaryDirectory() as d:
        st = Store(d, "42")
        st.write_archive([{"id": "1", "text": "texte sensible"}])
        st.save_state({"done": {"1": "ok"}})           # temoin : ne doit PAS partir
        check("archive presente avant purge", st.read_latest() is not None)
        n = st.shred_archives()
        check("shred efface l'archive", n >= 1 and st.read_latest() is None)
        check("temoin: state.json (pas une archive) survit", st.load_state() != {})


def test_result_summary():
    print("resume lisible")
    r = Result()
    r.deleted, r.failed = 5, 1
    check("resume mentionne succes et echecs",
          "5 supprime(s)" in r.summary() and "1 en echec" in r.summary())


def main():
    for fn in (test_import_formats, test_seal, test_store_roundtrip,
               test_partition_method, test_shred_archives, test_result_summary):
        fn()
    print("\n%d passe(s), %d echec(s)" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
