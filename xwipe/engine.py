"""Moteur de suppression selective.

Le tampon entre l'interface et le client X. Il ne decide jamais quoi supprimer :
l'utilisateur choisit dans la liste, l'interface passe cette selection ici, et le
moteur ne touche a rien d'autre. Il n'existe volontairement aucun chemin
"tout effacer sans demander" : la seule entree de `delete_selection` est une liste
d'elements explicitement retenus.

Trois garanties, chacune payee par un bug reel sur du vrai compte :

- On archive AVANT de supprimer. Cote X la suppression est definitive ; le
  fichier local est la seule copie de secours.
- Un retweet se defait par `undo_retweet` sur le tweet source, un tweet ou une
  reponse par `delete_tweet` sur son propre id. Se tromper de methode repond 200
  sans rien defaire.
- Un 200 ne prouve pas la suppression. Chaque element traite est re-interroge par
  son id ; ce qui a survecu est retente une fois avant d'etre declare en echec.
"""
from __future__ import annotations

import json
import os
import time

from .api import AuthError, XClient, XError


class Store:
    """Dossier d'un compte : archive horodatee et etat de reprise."""

    def __init__(self, root: str, rest_id: str):
        self.dir = os.path.join(root, "accounts", rest_id)
        os.makedirs(self.dir, exist_ok=True)

    def _p(self, name: str) -> str:
        return os.path.join(self.dir, name)

    def write_archive(self, records: list[dict]) -> str:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        payload = {"saved_at": int(time.time()), "count": len(records),
                   "records": records}
        path = self._p("archive-%s.json" % stamp)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        with open(self._p("latest.json"), "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        return path

    def read_latest(self) -> dict | None:
        p = self._p("latest.json")
        if not os.path.exists(p):
            return None
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except (ValueError, OSError):
            return None

    def load_state(self) -> dict:
        p = self._p("state.json")
        if not os.path.exists(p):
            return {}
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except (ValueError, OSError):
            return {}

    def save_state(self, state: dict) -> None:
        p = self._p("state.json")
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=1)
        os.replace(tmp, p)

    def shred_archives(self) -> int:
        """Ecrase (3 passes) puis supprime les archives locales : "supprime"
        doit valoir aussi en local, l'archive contient le texte de ce qui a ete lu.

        Sur SSD (wear leveling, TRIM) l'ecrasement en place ne garantit pas
        l'inaccessibilite des blocs physiques : on retire les donnees du systeme
        de fichiers, ce n'est pas un effacement securise materiel.
        """
        n = 0
        for fn in os.listdir(self.dir):
            if not (fn.startswith("archive-") or fn == "latest.json"):
                continue
            p = self._p(fn)
            try:
                size = os.path.getsize(p)
                with open(p, "r+b") as f:
                    for _ in range(3):
                        f.seek(0)
                        f.write(os.urandom(size))
                        f.flush()
                        os.fsync(f.fileno())
                os.remove(p)
                n += 1
            except OSError:
                pass
        return n


class Result:
    def __init__(self):
        self.deleted = 0
        self.failed = 0
        self.skipped = 0
        self.unverified = 0
        self.errors: list[str] = []

    def summary(self) -> str:
        bits = ["%d supprime(s)" % self.deleted]
        if self.skipped:
            bits.append("%d deja fait(s)" % self.skipped)
        if self.failed:
            bits.append("%d en echec" % self.failed)
        if self.unverified:
            bits.append("%d non verifiable(s)" % self.unverified)
        return ", ".join(bits)


# Categories proposees a l'utilisateur dans l'interface. Il coche celles qu'il
# veut, et peut encore deselectionner element par element.
CATEGORIES = [
    ("tweet", "Tweets"),
    ("reply", "Reponses"),
    ("retweet", "Retweets"),
    ("like", "Likes"),
]


class Engine:
    """Un compte : inventaire, suppression d'une selection, preuve."""

    def __init__(self, client: XClient, rest_id: str, store: Store, *,
                 log=None, progress=None, should_stop=None):
        self.client = client
        self.rest_id = rest_id
        self.store = store
        self.log = log or (lambda *_a, **_k: None)
        self.progress = progress or (lambda *_a, **_k: None)
        self.should_stop = should_stop or (lambda: False)
        client.set_stop_check(self.should_stop)

    def scan(self) -> list[dict]:
        """Inventaire complet, archive automatique. Ne supprime rien."""
        self.log("Lecture du compte...", "info")

        def on_page(label, page, added, total):
            self.progress("scan", total, 0, "%s (%d)" % (label, total))

        records = self.client.scan(self.rest_id, progress=on_page)
        path = self.store.write_archive(records)
        counts = {"tweet": 0, "reply": 0, "retweet": 0, "like": 0}
        for r in records:
            counts[r["kind"]] = counts.get(r["kind"], 0) + 1
        self.log("Trouve : %d tweets, %d reponses, %d retweets, %d likes"
                 % (counts["tweet"], counts["reply"], counts["retweet"],
                    counts["like"]), "ok")
        self.log("Sauvegarde locale : %s" % os.path.basename(path), "info")
        return records

    def _remove(self, rec: dict) -> None:
        """Applique la bonne operation selon le type. Un retweet se defait sur le
        tweet source, un like par UnfavoriteTweet, le reste par DeleteTweet."""
        kind = rec.get("kind")
        if kind == "retweet":
            src = rec.get("source_id")
            if not src:
                raise XError("retweet sans tweet source, impossible a defaire")
            self.client.undo_retweet(src)
        elif kind == "like":
            self.client.undo_like(rec["id"])
        else:
            self.client.delete_tweet(rec["id"])

    def _still_there(self, rec: dict):
        """True si l'element n'a pas ete retire, False sinon, None indetermine.
        Un like n'est pas 'supprime' (le tweet reste), on regarde `favorited`."""
        if rec.get("kind") == "like":
            return self.client.still_liked(rec["id"])
        return self.client.exists(rec["id"])

    def delete_selection(self, selection: list[dict], *, delay: float = 1.2,
                         verify: bool = True) -> Result:
        """Supprime exactement les elements retenus par l'utilisateur.

        `selection` est la liste que l'interface a construite a partir des cases
        cochees. Le moteur n'ajoute ni n'enleve rien : ce qui n'est pas dans la
        liste n'est pas touche.
        """
        res = Result()
        if not selection:
            return res
        if not self.store.read_latest():
            raise XError("Fais d'abord une lecture du compte (l'archive sert de secours).")

        state = self.store.load_state()
        done = state.setdefault("done", {})
        total = len(selection)
        self.log("Suppression de %d element(s) choisi(s)" % total, "info")
        touched: list[dict] = []

        for i, rec in enumerate(selection, 1):
            if self.should_stop():
                self.log("Arrete a ta demande", "warn")
                break
            key = rec["id"]
            if done.get(key) == "ok":
                res.skipped += 1
                self.progress("delete", i, total, "deja fait")
                continue
            try:
                self._remove(rec)
                done[key] = "ok"
                touched.append(rec)
                res.deleted += 1
            except AuthError:
                raise
            except XError as ex:
                done[key] = "err"
                res.failed += 1
                res.errors.append("%s : %s" % (key, ex))
            self.store.save_state(state)
            self.progress("delete", i, total, rec.get("kind", ""))
            time.sleep(delay)

        self.store.save_state(state)
        if verify and touched and not self.should_stop():
            self._verify(touched, res, done, state, delay=max(0.35, delay / 3))
        return res

    def _verify(self, touched: list[dict], res: Result, done: dict,
                state: dict, *, delay: float) -> None:
        """Re-interroge chaque element par son id ; retente ce qui a survecu.

        X est eventuellement coherent : sans cette passe on annoncerait "fini"
        sur du contenu encore en ligne.
        """
        self.log("Verification de %d element(s)..." % len(touched), "info")
        survivors: list[dict] = []
        for i, rec in enumerate(touched, 1):
            if self.should_stop():
                break
            alive = self._still_there(rec)
            if alive is None:
                res.unverified += 1
            elif alive:
                survivors.append(rec)
            self.progress("verify", i, len(touched), "")
            time.sleep(delay)

        if not survivors:
            self.log("Verifie : tout est bien supprime", "ok")
            return

        self.log("%d element(s) encore en ligne apres un 200, nouvelle tentative"
                 % len(survivors), "warn")
        still: list[dict] = []
        for rec in survivors:
            if self.should_stop():
                break
            try:
                self._remove(rec)
                time.sleep(delay)
                if self._still_there(rec):
                    still.append(rec)
            except XError:
                still.append(rec)
        for rec in still:
            done[rec["id"]] = "err"
            res.deleted = max(0, res.deleted - 1)
            res.failed += 1
            res.errors.append("%s : toujours en ligne apres deux tentatives" % rec["id"])
        self.store.save_state(state)
        if still:
            self.log("%d element(s) resistent : reessaie plus tard, X peut limiter"
                     % len(still), "warn")
        else:
            self.log("Verifie : tout est bien supprime apres reprise", "ok")
