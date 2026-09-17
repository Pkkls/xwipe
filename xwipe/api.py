"""Client X (twitter.com) pour le nettoyage de compte.

Rejoue les requetes du client web officiel avec les cookies de session de
l'utilisateur. Aucune cle d'API, aucun serveur tiers : la machine parle
directement a x.com.

Trois choses rendent ce client different d'un wrapper naif, et chacune vient
d'un bug paye en production :

1. Les `queryId` GraphQL rotent. Ils sont extraits du bundle main.js a chaque
   session plutot que codes en dur.
2. Un retweet ne se defait PAS par DeleteTweet sur son id. Il faut DeleteRetweet
   sur l'id du tweet SOURCE. Ignorer ca laisse les retweets en place en
   repondant 200.
3. Un HTTP 200 ne prouve pas la suppression : X est eventuellement coherent et
   les timelines servent un index retarde. La seule preuve est de redemander le
   tweet par son id.

Stdlib uniquement.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

# Cle publique du client web X. Identique pour tout le monde, ce n'est pas un
# secret : elle identifie l'application web, pas l'utilisateur.
PUBLIC_BEARER = (
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs="
    "1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
BUNDLE_URL = "https://abs.twimg.com/responsive-web/client-web/main.%s.js"
# Dernier hash connu. Sert d'amorce : s'il est perime on retombe sur la homepage.
FALLBACK_BUNDLE_HASH = "ae82e9d02d3328bba"
GQL = "https://x.com/i/api/graphql/%s/%s"

TIMELINES = [
    ("UserTweets", "tweets"),
    ("UserRepliesTimeline", "replies"),
    ("UserRepostsTimeline", "reposts"),
    ("UserOriginalsTimeline", "originals"),
]


class XError(RuntimeError):
    """Erreur metier remontee a l'interface, message deja lisible."""


class AuthError(XError):
    """Session invalide ou expiree. L'utilisateur doit recoller ses cookies."""


class XClient:
    """Session X authentifiee par cookies.

    `auth_token` et `ct0` sont les deux cookies du navigateur. `ct0` sert aussi
    d'en-tete anti-CSRF : les deux doivent venir de la MEME session, sinon X
    repond 403.
    """

    def __init__(self, auth_token: str, ct0: str, *, timeout: int = 45, log=None):
        if not auth_token or not ct0:
            raise AuthError("auth_token et ct0 sont requis")
        self.auth_token = auth_token.strip()
        self.ct0 = ct0.strip()
        self.timeout = timeout
        self._log = log or (lambda *_a, **_k: None)
        self._bundle: str | None = None
        self._bundle_hash = FALLBACK_BUNDLE_HASH
        self._ops: dict[str, tuple[str, dict]] = {}
        self._stop = None  # callable -> bool, pour annuler une boucle longue

    # ------------------------------------------------------------------ HTTP

    def set_stop_check(self, fn):
        """Installe un predicat d'annulation consulte dans les boucles longues."""
        self._stop = fn

    def _should_stop(self) -> bool:
        return bool(self._stop and self._stop())

    def _headers(self, *, json_body: bool = False) -> dict:
        h = {
            "authorization": "Bearer " + PUBLIC_BEARER,
            "x-csrf-token": self.ct0,
            "x-twitter-auth-type": "OAuth2Session",
            "x-twitter-active-user": "yes",
            "x-twitter-client-language": "en",
            "cookie": "auth_token=%s; ct0=%s; lang=en" % (self.auth_token, self.ct0),
            "user-agent": USER_AGENT,
            "referer": "https://x.com/",
        }
        if json_body:
            h["content-type"] = "application/json"
            h["origin"] = "https://x.com"
        return h

    def _raw(self, url: str, *, data: bytes | None = None, method: str = "GET",
             headers: dict | None = None) -> tuple[int, dict, str]:
        req = urllib.request.Request(url, data=data,
                                     headers=headers or self._headers(json_body=data is not None),
                                     method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status, dict(r.headers), r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as ex:
            return ex.code, dict(ex.headers), ex.read().decode("utf-8", "replace")
        except urllib.error.URLError as ex:
            raise XError("reseau indisponible: %s" % ex.reason) from ex

    def _sleep_for_rate_limit(self, headers: dict, attempt: int) -> None:
        """Respecte x-rate-limit-reset quand X le donne, sinon backoff borne."""
        wait = 60.0 * attempt
        reset = headers.get("x-rate-limit-reset")
        if reset:
            try:
                delta = float(reset) - time.time()
                if 0 < delta < 900:
                    wait = delta + 2
            except ValueError:
                pass
        wait = max(5.0, min(wait, 300.0))
        self._log("limite de debit atteinte, pause %d s" % int(wait), "warn")
        deadline = time.time() + wait
        while time.time() < deadline:
            if self._should_stop():
                raise XError("annule pendant la pause de limite de debit")
            time.sleep(0.5)

    # ---------------------------------------------------------------- bundle

    def _fetch_bundle(self, h: str) -> str | None:
        st, _, js = self._raw(BUNDLE_URL % h, headers={"user-agent": USER_AGENT})
        return js if st == 200 and "operationName:" in js else None

    def bundle(self) -> str:
        """Le bundle du client web, source des queryId.

        Le CDN abs.twimg.com est fiable ; la homepage x.com flappe en 401
        (challenge anti-bot). On tente donc le hash connu d'abord et on ne
        retombe sur la homepage que s'il a expire.
        """
        if self._bundle:
            return self._bundle
        js = self._fetch_bundle(self._bundle_hash)
        if not js:
            self._log("bundle connu perime, recherche du hash courant", "warn")
            for attempt in range(4):
                st, _, html = self._raw("https://x.com/")
                if st == 200:
                    m = re.search(r"main\.([0-9a-f]+)\.js", html)
                    if m:
                        js = self._fetch_bundle(m.group(1))
                        if js:
                            self._bundle_hash = m.group(1)
                            break
                time.sleep(3 * (attempt + 1))
        if not js:
            raise XError(
                "impossible de charger le bundle X (main.js). "
                "Verifie ta connexion, puis reessaie."
            )
        self._bundle = js
        return js

    def op(self, name: str) -> tuple[str, dict]:
        """(queryId, features) d'une operation GraphQL, lus dans le bundle.

        Les `featureSwitches` listes par le bundle sont envoyes a True : X rejette
        la requete si une feature attendue manque, mais tolere qu'elles soient
        toutes activees.
        """
        if name in self._ops:
            return self._ops[name]
        js = self.bundle()
        m = re.search(r'queryId:"([^"]+)",operationName:"%s"' % re.escape(name), js)
        if not m:
            raise XError("operation %s absente du bundle X (API modifiee ?)" % name)
        block = js[m.start():m.start() + 4000]
        fm = re.search(r"featureSwitches:\[([^\]]*)\]", block)
        feats = {t: True for t in re.findall(r'"([^"]+)"', fm.group(1))} if fm else {}
        self._ops[name] = (m.group(1), feats)
        return self._ops[name]

    # ----------------------------------------------------------------- GraphQL

    def gql_get(self, name: str, variables: dict, *, tries: int = 5) -> dict:
        qid, feats = self.op(name)
        qs = urllib.parse.urlencode({
            "variables": json.dumps(variables, separators=(",", ":")),
            "features": json.dumps(feats, separators=(",", ":")),
        })
        url = "%s?%s" % (GQL % (qid, name), qs)
        for attempt in range(1, tries + 1):
            if self._should_stop():
                raise XError("annule")
            st, hd, txt = self._raw(url)
            if st == 429:
                self._sleep_for_rate_limit(hd, attempt)
                continue
            if st in (401, 403):
                raise AuthError(
                    "session refusee par X (HTTP %d). Les cookies ont expire "
                    "ou proviennent de deux sessions differentes." % st)
            if st == 404:
                raise XError("operation %s introuvable cote X (404)" % name)
            if st != 200:
                raise XError("%s: HTTP %d" % (name, st))
            data = json.loads(txt)
            # X renvoie parfois des erreurs partielles AVEC des donnees valides
            # (un tweet supprime dans une page, par exemple) : on ne jette que
            # si rien n'est exploitable.
            if data.get("errors") and not data.get("data"):
                raise XError("%s: %s" % (name, " | ".join(
                    e.get("message", "?") for e in data["errors"])))
            return data
        raise XError("%s: limite de debit persistante" % name)

    def gql_post(self, name: str, variables: dict, *, tries: int = 4) -> dict:
        qid, _ = self.op(name)
        body = json.dumps({"variables": variables, "queryId": qid}).encode("utf-8")
        url = GQL % (qid, name)
        for attempt in range(1, tries + 1):
            if self._should_stop():
                raise XError("annule")
            st, hd, txt = self._raw(url, data=body, method="POST")
            if st == 429:
                self._sleep_for_rate_limit(hd, attempt)
                continue
            if st in (401, 403):
                raise AuthError(
                    "session refusee par X (HTTP %d) pendant une suppression." % st)
            if st != 200:
                return {"_http": st, "_body": txt[:200]}
            try:
                return json.loads(txt)
            except ValueError:
                return {"_http": st, "_body": txt[:200]}
        raise XError("%s: limite de debit persistante" % name)

    # ---------------------------------------------------------------- identite

    def viewer(self) -> dict:
        """Qui est reellement connecte.

        C'est le `auth_token` qui decide du compte modifie, jamais le pseudo
        affiche. Un compte renomme garde son rest_id : c'est le seul
        identifiant sur lequel on peut asserter quoi que ce soit.
        """
        data = self.gql_get("Viewer", {"withCommunitiesMemberships": True})
        user = (((data.get("data") or {}).get("viewer") or {})
                .get("user_results") or {}).get("result") or {}
        rest_id = user.get("rest_id")
        if not rest_id:
            raise AuthError(
                "impossible d'identifier la session : cookies invalides ou expires")
        core = user.get("core") or {}
        legacy = user.get("legacy") or {}
        return {
            "rest_id": rest_id,
            "screen_name": core.get("screen_name") or legacy.get("screen_name") or "",
            "name": core.get("name") or legacy.get("name") or "",
        }

    def rest_id_of(self, screen_name: str) -> str | None:
        data = self.gql_get("UserByScreenName",
                            {"screen_name": screen_name, "withGrokTranslatedBio": True})
        res = ((data.get("data") or {}).get("user") or {}).get("result") or {}
        return res.get("rest_id")

    # ------------------------------------------------------------- inventaire

    @staticmethod
    def _unwrap(res: dict | None) -> dict | None:
        if not res:
            return None
        if res.get("__typename") == "TweetWithVisibilityResults":
            return res.get("tweet")
        return res

    def _classify(self, res: dict | None, owner_id: str) -> dict | None:
        tw = self._unwrap(res)
        if not tw or tw.get("__typename") == "TweetTombstone":
            return None
        lg = tw.get("legacy") or {}
        author = (((tw.get("core") or {}).get("user_results") or {}).get("result") or {})
        if (author.get("rest_id") or lg.get("user_id_str")) != owner_id:
            return None  # jamais toucher au contenu de quelqu'un d'autre
        rid = tw.get("rest_id") or lg.get("id_str")
        if not rid:
            return None
        text = lg.get("full_text", "") or ""
        is_rt = bool(lg.get("retweeted_status_result")) or text.startswith("RT @")
        is_reply = bool(lg.get("in_reply_to_status_id_str")) or bool(
            lg.get("in_reply_to_screen_name"))
        source_id = None
        if is_rt:
            src = ((lg.get("retweeted_status_result") or {}).get("result")) or {}
            src = self._unwrap(src) or {}
            source_id = src.get("rest_id") or (src.get("legacy") or {}).get("id_str")
        return {
            "id": rid,
            "kind": "retweet" if is_rt else ("reply" if is_reply else "tweet"),
            "source_id": source_id,
            "created_at": lg.get("created_at"),
            "text": text,
            "reply_to": lg.get("in_reply_to_screen_name"),
            "likes": lg.get("favorite_count"),
            "retweets": lg.get("retweet_count"),
        }

    @staticmethod
    def _find_instructions(node, depth: int = 0):
        """Le schema varie (timeline / timeline_v2). On descend jusqu'a la
        premiere cle `instructions` qui est une liste, au lieu de coder un
        chemin en dur qui casse a la prochaine migration."""
        if depth > 7 or not isinstance(node, dict):
            return None
        v = node.get("instructions")
        if isinstance(v, list):
            return v
        for k in ("data", "user", "result", "timeline_v2", "timeline"):
            if k in node:
                found = XClient._find_instructions(node[k], depth + 1)
                if found is not None:
                    return found
        return None

    def _classify_like(self, res: dict | None) -> dict | None:
        """Un like porte sur le tweet de QUELQU'UN D'AUTRE : pas de filtre owner.
        L'id retenu est celui du tweet aime (ce que prend UnfavoriteTweet)."""
        tw = self._unwrap(res)
        if not tw or tw.get("__typename") == "TweetTombstone":
            return None
        lg = tw.get("legacy") or {}
        rid = tw.get("rest_id") or lg.get("id_str")
        if not rid:
            return None
        author = (((tw.get("core") or {}).get("user_results") or {}).get("result") or {})
        handle = ((author.get("core") or {}).get("screen_name")
                  or (author.get("legacy") or {}).get("screen_name"))
        return {
            "id": rid, "kind": "like", "source_id": None,
            "created_at": lg.get("created_at"), "text": lg.get("full_text", "") or "",
            "reply_to": handle,  # auteur du tweet aime, pour le contexte
            "likes": lg.get("favorite_count"), "retweets": lg.get("retweet_count"),
        }

    def _walk(self, instructions, owner_id: str, seen: dict, source: str,
              *, like_mode: bool = False) -> str | None:
        bottom = None
        for ins in instructions or []:
            entries = list(ins.get("entries") or [])
            if ins.get("entry"):
                entries.append(ins["entry"])
            for e in entries:
                c = e.get("content") or {}
                et = c.get("entryType")
                if et == "TimelineTimelineCursor" and c.get("cursorType") == "Bottom":
                    bottom = c.get("value")
                    continue
                results = []
                if et == "TimelineTimelineItem":
                    tr = (c.get("itemContent") or {}).get("tweet_results")
                    if tr:
                        results.append(tr.get("result"))
                elif et == "TimelineTimelineModule":
                    for it in c.get("items") or []:
                        ic = (it.get("item") or {}).get("itemContent") or {}
                        if ic.get("itemType") == "TimelineTweet" and ic.get("tweet_results"):
                            results.append(ic["tweet_results"].get("result"))
                for r in results:
                    rec = self._classify_like(r) if like_mode else self._classify(r, owner_id)
                    if not rec:
                        continue
                    prev = seen.get(rec["id"])
                    if prev:
                        if source not in prev["sources"]:
                            prev["sources"].append(source)
                        continue
                    rec["sources"] = [source]
                    seen[rec["id"]] = rec
        return bottom

    def _paginate(self, op_name: str, base_vars: dict, owner_id: str, seen: dict,
                  label: str, *, like_mode: bool = False, max_pages: int = 400,
                  page_delay: float = 1.2, progress=None) -> None:
        """Pagine une timeline jusqu'au bout (le curseur Bottom ne bouge plus,
        ou plus aucun element neuf). C'est ce qui fait remonter jusqu'a la
        creation du compte, page apres page."""
        try:
            self.op(op_name)
        except XError as ex:
            self._log("%s indisponible: %s" % (op_name, ex), "warn")
            return
        cursor, page = None, 0
        while True:
            if self._should_stop():
                break
            page += 1
            variables = dict(base_vars)
            if cursor:
                variables["cursor"] = cursor
            try:
                data = self.gql_get(op_name, variables)
            except AuthError:
                raise
            except XError as ex:
                self._log("%s page %d: %s" % (op_name, page, ex), "warn")
                break
            before = len(seen)
            bottom = self._walk(self._find_instructions(data), owner_id, seen, label,
                                like_mode=like_mode)
            added = len(seen) - before
            if progress:
                progress(label, page, added, len(seen))
            if added == 0 or not bottom or bottom == cursor:
                break
            cursor = bottom
            if page >= max_pages:
                self._log("garde-fou: %d pages sur %s" % (max_pages, op_name), "warn")
                break
            time.sleep(page_delay)

    def scan(self, owner_id: str, *, page_delay: float = 1.2,
             max_pages: int = 400, progress=None) -> list[dict]:
        """Inventaire complet du compte, jusqu'a sa creation.

        Chaque source est paginee jusqu'au bout puis fusionnee par id : les
        timelines de contenu possede (tweets, reponses, retweets) et la timeline
        des Likes, qui portent sur des tweets d'autrui et se retirent par
        UnfavoriteTweet. X plafonne l'historique profil (~3200 posts) : au-dela
        de ce plafond cote serveur, aucun client ne peut remonter plus loin.
        """
        seen: dict[str, dict] = {}
        owned = {"userId": owner_id, "count": 100, "includePromotedContent": False,
                 "withCommunity": True, "withVoice": True, "withV2Timeline": True}
        for op_name, label in TIMELINES:
            if self._should_stop():
                break
            self._paginate(op_name, owned, owner_id, seen, label,
                           max_pages=max_pages, page_delay=page_delay, progress=progress)
        if not self._should_stop():
            likes = {"userId": owner_id, "count": 100, "includePromotedContent": False,
                     "withClientEventToken": False, "withVoice": True,
                     "withV2Timeline": True}
            self._paginate("Likes", likes, owner_id, seen, "like", like_mode=True,
                           max_pages=max_pages, page_delay=page_delay, progress=progress)
        out = list(seen.values())
        out.sort(key=lambda r: int(r["id"]), reverse=True)
        return out

    # ------------------------------------------------------------- suppression

    def delete_tweet(self, tweet_id: str) -> bool:
        """Supprime un tweet ou une reponse. Le retour 200 ne prouve rien :
        seule une verification par id tranche (cf. `exists`)."""
        res = self.gql_post("DeleteTweet",
                            {"tweet_id": tweet_id, "dark_request": False})
        return "delete_tweet" in json.dumps(res.get("data", {}))

    def undo_retweet(self, source_tweet_id: str) -> bool:
        """Annule un retweet. Prend l'id du tweet SOURCE, pas celui du retweet :
        DeleteTweet sur l'id du retweet repond 200 et ne defait rien."""
        res = self.gql_post("DeleteRetweet",
                            {"source_tweet_id": source_tweet_id, "dark_request": False})
        return "unretweet" in json.dumps(res.get("data", {})).lower() or bool(res.get("data"))

    def undo_like(self, tweet_id: str) -> bool:
        """Retire un like (UnfavoriteTweet sur le tweet aime). Ne supprime pas le
        tweet, qui appartient a autrui : on defait seulement la relation."""
        res = self.gql_post("UnfavoriteTweet", {"tweet_id": tweet_id})
        return "unfavorite" in json.dumps(res.get("data", {})).lower() or bool(res.get("data"))

    def _tweet_result(self, tweet_id: str) -> dict | None:
        try:
            data = self.gql_get("TweetResultByRestId", {
                "tweetId": tweet_id, "withCommunity": False,
                "includePromotedContent": False, "withVoice": False,
            }, tries=3)
        except AuthError:
            raise
        except XError:
            return None
        return ((data.get("data") or {}).get("tweetResult") or {}).get("result") or {}

    def exists(self, tweet_id: str) -> bool | None:
        """True encore en ligne, False disparu, None indetermine.

        C'est le seul temoin fiable d'une suppression : les timelines servent un
        index retarde et continuent d'afficher des tweets deja morts.
        """
        res = self._tweet_result(tweet_id)
        if res is None:
            return None
        tn = res.get("__typename")
        if not tn:
            return False
        if tn in ("TweetTombstone", "TweetUnavailable"):
            return False
        return True

    def still_liked(self, tweet_id: str) -> bool | None:
        """True si le tweet est encore aime, False sinon, None indetermine.

        Pour un like, `exists` ne dit rien : le tweet reste en ligne. C'est la
        relation `favorited` du viewer qui doit passer a faux.
        """
        res = self._tweet_result(tweet_id)
        if res is None:
            return None
        res = self._unwrap(res) or res
        return bool((res.get("legacy") or {}).get("favorited"))
