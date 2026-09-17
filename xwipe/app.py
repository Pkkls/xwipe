"""Interface XWipe (tkinter, zero dependance).

Le reseau tourne sur un thread de fond ; l'interface ne parle qu'a une file de
messages relue par `after`, donc elle ne gele jamais et rien ne touche a un
widget depuis un autre thread.

Flux : importer un compte (coller les cookies) -> lire le compte -> cocher ce
qu'on veut retirer -> confirmer -> suppression puis verification.

Design : palette verifiee AA (contrast.py du kit UX), un seul accent, un seul
danger, une echelle d'espacement. Surfaces par profondeur (page < table/journal),
separations a la hairline plutot qu'au trait colore.
"""
from __future__ import annotations

import queue
import threading
import time

import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext

from . import account as acct
from .api import AuthError, XClient, XError
from .engine import CATEGORIES, Engine, Store

APP_TITLE = "XWipe"
CHECK_ON = "☑"   # case cochee (symbole geometrique, pas un emoji)
CHECK_OFF = "☐"  # case vide

# --- systeme de design -------------------------------------------------------
# Contrastes verifies avec le contrast.py du kit UX : texte 16/9.3/5.2:1,
# blanc sur accent 5.85:1, blanc sur danger 4.61:1. Un accent, un danger.
C = {
    "page":    "#0e1116",  # fond, off-black legerement froid
    "raised":  "#161b22",  # entetes de zone
    "sunken":  "#0a0d11",  # table, champs, journal
    "line":    "#232a33",  # hairline
    "line2":   "#2c3742",  # hairline plus marquee (bordures de champ)
    "text":    "#e6edf3",  # 16:1
    "text2":   "#adb7c2",  # 9.3:1
    "muted":   "#7d8792",  # 5.2:1 (AA)
    "accent":  "#1a5fd0",  # bouton primaire (blanc dessus = 5.85:1)
    "accentH": "#2f6fe0",  # accent survol
    "accentT": "#4c9aff",  # accent en texte / selection (6.6:1)
    "danger":  "#da3633",  # blanc dessus = 4.61:1
    "dangerH": "#e5484d",
    "ok":      "#3fb950",
    "warn":    "#d29922",
    "on":      "#ffffff",
    "sel":     "#132133",  # fond d'une ligne cochee (teinte accent, discrete)
    "cursor":  "#1b2b40",  # fond de la ligne au clavier (curseur browse)
    "ghost":   "#1c232c",  # bouton neutre
    "ghostH":  "#252e39",
    "off":     "#2a323c",  # etat desactive
    "offtext": "#5b636d",
}
SP = {"xs": 4, "sm": 8, "md": 12, "lg": 16, "xl": 22, "xxl": 30}
FF = "Segoe UI"
FF_SEMI = "Segoe UI Semibold"
FF_MONO = "Consolas"


class Worker(threading.Thread):
    """Execute un travail reseau hors du thread interface, avec annulation."""

    def __init__(self, fn, q: queue.Queue):
        super().__init__(daemon=True)
        self._fn = fn
        self._q = q
        self._cancel = threading.Event()

    def cancel(self):
        self._cancel.set()

    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def run(self):
        try:
            self._fn(self)
        except AuthError as ex:
            self._q.put(("auth", str(ex)))
        except XError as ex:
            self._q.put(("error", str(ex)))
        except Exception as ex:  # dernier filet, l'interface ne doit jamais mourir
            self._q.put(("error", "erreur inattendue : %s" % ex))
        finally:
            self._q.put(("done", None))


def _hr(parent, color=C["line"]):
    """Separateur hairline horizontal, plus discret qu'un trait colore."""
    f = tk.Frame(parent, height=1, bg=color)
    f.pack(fill="x")
    return f


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1080x720")
        self.minsize(920, 600)

        self.q: queue.Queue = queue.Queue()
        self.worker: Worker | None = None
        self.records: list[dict] = []
        self.checked: set[str] = set()
        self.row_by_iid: dict[str, dict] = {}
        self.current: dict | None = None

        self._build_style()
        self._build_layout()
        self._refresh_accounts()
        self.after(80, self._pump)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ style

    def _build_style(self):
        self.configure(bg=C["page"])
        s = ttk.Style(self)
        try:
            s.theme_use("clam")
        except tk.TclError:
            pass

        s.configure(".", background=C["page"], foreground=C["text"],
                    fieldbackground=C["sunken"], bordercolor=C["line"],
                    focuscolor=C["accentT"], font=(FF, 10))
        s.configure("TFrame", background=C["page"])
        s.configure("TLabel", background=C["page"], foreground=C["text"])
        s.configure("Sub.TLabel", foreground=C["text2"])
        s.configure("Muted.TLabel", foreground=C["muted"])
        s.configure("Title.TLabel", foreground=C["text"], font=(FF_SEMI, 19))
        s.configure("Eyebrow.TLabel", foreground=C["muted"], font=(FF, 9))
        s.configure("Count.TLabel", foreground=C["text2"], font=(FF_MONO, 9))
        s.configure("H1.TLabel", foreground=C["text"], font=(FF_SEMI, 15))

        # Boutons : trois roles seulement.
        for name, bg, bgh, fg in (
                ("Accent", C["accent"], C["accentH"], C["on"]),
                ("Danger", C["danger"], C["dangerH"], C["on"]),
                ("Ghost",  C["ghost"],  C["ghostH"],  C["text2"])):
            s.configure("%s.TButton" % name, background=bg, foreground=fg,
                        borderwidth=0, relief="flat", padding=(14, 8),
                        font=(FF, 10))
            s.map("%s.TButton" % name,
                  background=[("disabled", C["off"]), ("pressed", bg),
                              ("active", bgh)],
                  foreground=[("disabled", C["offtext"])])
        s.configure("Ghost.TButton", bordercolor=C["line2"], borderwidth=1)
        # Bouton primaire un cran plus grand pour asseoir la hierarchie.
        s.configure("Accent.TButton", padding=(18, 9), font=(FF_SEMI, 10))
        s.configure("Danger.TButton", padding=(16, 9), font=(FF_SEMI, 10))

        s.configure("TCombobox", fieldbackground=C["sunken"], background=C["ghost"],
                    foreground=C["text"], arrowcolor=C["text2"],
                    bordercolor=C["line2"], borderwidth=1, padding=(10, 7))
        s.map("TCombobox", fieldbackground=[("readonly", C["sunken"])],
              foreground=[("disabled", C["offtext"])],
              arrowcolor=[("disabled", C["off"])])

        s.configure("TCheckbutton", background=C["page"], foreground=C["text2"],
                    focuscolor=C["accentT"])
        s.map("TCheckbutton", foreground=[("active", C["text"])],
              indicatorcolor=[("selected", C["accentT"]), ("!selected", C["sunken"])])

        s.configure("Treeview", background=C["sunken"], fieldbackground=C["sunken"],
                    foreground=C["text2"], rowheight=30, borderwidth=0,
                    font=(FF, 10))
        s.configure("Treeview.Heading", background=C["raised"], foreground=C["muted"],
                    relief="flat", font=(FF, 9), padding=(8, 6))
        s.map("Treeview.Heading", background=[("active", C["raised"])])
        s.map("Treeview", background=[("selected", C["cursor"])],
              foreground=[("selected", C["text"])])

        s.configure("TProgressbar", troughcolor=C["sunken"], background=C["accentT"],
                    borderwidth=0, thickness=4)
        s.configure("Vertical.TScrollbar", background=C["ghost"],
                    troughcolor=C["page"], bordercolor=C["page"],
                    arrowcolor=C["muted"])

    # ----------------------------------------------------------------- layout

    def _build_layout(self):
        # -- Entete : identite du produit, une phrase concrete ----------------
        head = ttk.Frame(self, padding=(SP["xl"], SP["lg"], SP["xl"], SP["md"]))
        head.pack(fill="x")
        ttk.Label(head, text="XWipe", style="Title.TLabel").pack(side="left")
        ttk.Label(head, text="  supprime tes tweets, reponses et retweets sur X",
                  style="Muted.TLabel").pack(side="left", pady=(6, 0))

        # -- Barre compte : la 1re etape, action primaire a droite ------------
        acct_bar = ttk.Frame(self, padding=(SP["xl"], 0, SP["xl"], SP["md"]))
        acct_bar.pack(fill="x")
        ttk.Label(acct_bar, text="COMPTE", style="Eyebrow.TLabel").pack(
            side="left", padx=(0, SP["sm"]), pady=(4, 0))
        self.account_var = tk.StringVar()
        self.account_menu = ttk.Combobox(acct_bar, textvariable=self.account_var,
                                         state="readonly", width=30)
        self.account_menu.pack(side="left")
        self.account_menu.bind("<<ComboboxSelected>>", lambda _e: self._on_pick_account())
        ttk.Button(acct_bar, text="+  Importer", style="Ghost.TButton",
                   command=self._open_import).pack(side="left", padx=(SP["sm"], SP["xs"]))
        self.btn_remove = ttk.Button(acct_bar, text="Retirer", style="Ghost.TButton",
                                     command=self._remove_account)
        self.btn_remove.pack(side="left", padx=SP["xs"])
        self.btn_purge = ttk.Button(acct_bar, text="Effacer sauvegardes",
                                    style="Ghost.TButton", command=self._purge_local)
        self.btn_purge.pack(side="left", padx=SP["xs"])
        self.btn_scan = ttk.Button(acct_bar, text="⟳  Lire le compte",
                                   style="Accent.TButton", command=self._scan)
        self.btn_scan.pack(side="right")

        _hr(self)

        # -- Barre de tri/selection : filtres + tout cocher + compteur --------
        filt = ttk.Frame(self, padding=(SP["xl"], SP["md"], SP["xl"], SP["md"]))
        filt.pack(fill="x")
        ttk.Label(filt, text="AFFICHER", style="Eyebrow.TLabel").pack(
            side="left", padx=(0, SP["sm"]))
        self.cat_vars: dict[str, tk.BooleanVar] = {}
        for kind, label in CATEGORIES:
            v = tk.BooleanVar(value=True)
            self.cat_vars[kind] = v
            ttk.Checkbutton(filt, text=label, variable=v,
                            command=self._repopulate).pack(side="left", padx=(0, SP["md"]))
        self.count_lbl = ttk.Label(filt, text="", style="Count.TLabel")
        self.count_lbl.pack(side="right")
        ttk.Button(filt, text="Tout decocher", style="Ghost.TButton",
                   command=self._check_none).pack(side="right", padx=(SP["sm"], SP["lg"]))
        ttk.Button(filt, text="Tout cocher (vue)", style="Ghost.TButton",
                   command=self._check_all).pack(side="right", padx=SP["xs"])

        # -- Table : surface enfoncee, bordure hairline, etat vide ------------
        mid = ttk.Frame(self, padding=(SP["xl"], 0, SP["xl"], 0))
        mid.pack(fill="both", expand=True)
        border = tk.Frame(mid, bg=C["line"])            # fausse bordure 1px
        border.pack(fill="both", expand=True)
        holder = tk.Frame(border, bg=C["sunken"])
        holder.pack(fill="both", expand=True, padx=1, pady=1)

        cols = ("check", "kind", "date", "text", "likes")
        self.tree = ttk.Treeview(holder, columns=cols, show="headings",
                                 selectmode="browse")
        for c, txt, w, anchor, stretch in (
                ("check", "", 40, "center", False), ("kind", "TYPE", 96, "w", False),
                ("date", "DATE", 150, "w", False), ("text", "CONTENU", 560, "w", True),
                ("likes", "LIKES", 70, "e", False)):
            self.tree.heading(c, text=txt)
            self.tree.column(c, width=w, anchor=anchor, stretch=stretch)
        self.tree.tag_configure("on", background=C["sel"], foreground=C["text"])
        self.tree.tag_configure("off", background=C["sunken"])
        vs = ttk.Scrollbar(holder, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vs.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        self.tree.bind("<Button-1>", self._on_tree_click)
        self.tree.bind("<space>", self._toggle_focus_row)
        self.tree.bind("<Return>", self._toggle_focus_row)

        # Etat vide, pose par-dessus la table quand il n'y a rien a montrer.
        self.empty = tk.Frame(holder, bg=C["sunken"])
        self.empty_glyph = tk.Label(self.empty, text=CHECK_OFF, bg=C["sunken"],
                                    fg=C["line2"], font=(FF, 40))
        self.empty_glyph.pack()
        self.empty_title = tk.Label(self.empty, bg=C["sunken"], fg=C["text2"],
                                    font=(FF_SEMI, 13))
        self.empty_title.pack(pady=(SP["md"], SP["xs"]))
        self.empty_sub = tk.Label(self.empty, bg=C["sunken"], fg=C["muted"],
                                  font=(FF, 10), justify="center")
        self.empty_sub.pack()

        # -- Pied : progression + statut a gauche, actions a droite -----------
        _hr(self)
        foot = ttk.Frame(self, padding=(SP["xl"], SP["md"], SP["xl"], SP["md"]))
        foot.pack(fill="x")
        self.progress = ttk.Progressbar(foot, mode="determinate")
        self.progress.pack(fill="x", side="top", pady=(0, SP["md"]))
        row = ttk.Frame(foot)
        row.pack(fill="x")
        self.status = ttk.Label(row, text="Importe un compte pour commencer.",
                                style="Sub.TLabel")
        self.status.pack(side="left")
        self.btn_delete = ttk.Button(row, text="Supprimer la selection",
                                     style="Danger.TButton", command=self._delete,
                                     state="disabled")
        self.btn_delete.pack(side="right")
        self.btn_cancel = ttk.Button(row, text="Arreter", style="Ghost.TButton",
                                     command=self._cancel, state="disabled")
        self.btn_cancel.pack(side="right", padx=(0, SP["sm"]))

        # -- Journal d'activite : panneau discret, pas un terminal brut -------
        logwrap = ttk.Frame(self, padding=(SP["xl"], 0, SP["xl"], SP["lg"]))
        logwrap.pack(fill="x")
        ttk.Label(logwrap, text="ACTIVITE", style="Eyebrow.TLabel").pack(
            anchor="w", pady=(0, SP["xs"]))
        self.logbox = scrolledtext.ScrolledText(
            logwrap, height=6, bg=C["sunken"], fg=C["text2"],
            insertbackground=C["text2"], relief="flat", highlightthickness=1,
            highlightbackground=C["line"], highlightcolor=C["line"],
            font=(FF_MONO, 9), padx=SP["md"], pady=SP["sm"], wrap="word")
        self.logbox.pack(fill="x")
        self.logbox.configure(state="disabled")
        self.logbox.tag_config("ts", foreground=C["muted"])
        for tag, col in (("ok", C["ok"]), ("warn", C["warn"]),
                         ("error", C["danger"]), ("info", C["text2"])):
            self.logbox.tag_config(tag, foreground=col)

        self._show_empty(True)

    # -------------------------------------------------------------- log / busy

    def log(self, msg: str, level: str = "info"):
        self.logbox.configure(state="normal")
        self.logbox.insert("end", time.strftime("%H:%M:%S "), "ts")
        self.logbox.insert("end", msg + "\n", level)
        self.logbox.see("end")
        self.logbox.configure(state="disabled")

    def _busy(self, on: bool):
        state = "disabled" if on else "normal"
        for b in (self.btn_scan, self.btn_delete, self.btn_remove, self.btn_purge):
            b.configure(state=state)
        self.account_menu.configure(state="disabled" if on else "readonly")
        self.btn_cancel.configure(state="normal" if on else "disabled")
        if not on:
            self._sync_delete_button()

    # ------------------------------------------------------------ comptes

    def _refresh_accounts(self):
        self.accounts = acct.load_accounts()
        labels = ["@%s  (%s)" % (a.get("screen_name") or "?", a["rest_id"])
                  for a in self.accounts]
        self.account_menu.configure(values=labels)
        if self.accounts and not self.current:
            self.account_menu.current(0)
            self._on_pick_account()
        elif not self.accounts:
            self.account_var.set("")
            self.current = None
            self._repopulate()

    def _on_pick_account(self):
        i = self.account_menu.current()
        if i < 0 or i >= len(self.accounts):
            return
        self.current = self.accounts[i]
        self.records = []
        self.checked.clear()
        self._repopulate()
        enc = self.current["auth"].get("enc")
        note = "chiffres (DPAPI)" if enc == "dpapi" else "stockage local"
        self.status.configure(text="Compte @%s pret. Cookies %s. Clique Lire le compte."
                               % (self.current.get("screen_name") or "?", note))

    def _remove_account(self):
        if not self.current:
            return
        if messagebox.askyesno(
                "Retirer le compte",
                "Retirer @%s de XWipe ?\n\nCela efface ses cookies stockes sur cette "
                "machine. Ton compte X n'est pas touche."
                % (self.current.get("screen_name") or "?")):
            acct.remove_account(self.current["rest_id"])
            self.current = None
            self._refresh_accounts()
            self.status.configure(text="Compte retire.")

    def _purge_local(self):
        """Efface du disque les sauvegardes locales : l'archive garde le texte
        de ce qui a ete lu, "supprime" doit valoir aussi en local."""
        if not self.current or self.worker:
            return
        if not messagebox.askyesno(
                "Effacer les sauvegardes locales",
                "Effacer les sauvegardes locales de @%s ?\n\n"
                "XWipe ecrase puis supprime les archives qu'il a ecrites sur cette "
                "machine (elles contiennent le texte de ce qui a ete lu). Ton compte "
                "X n'est pas touche.\n\nNote : sur SSD, l'effacement n'est pas garanti "
                "au niveau materiel." % (self.current.get("screen_name") or "?"),
                icon="warning", default="no"):
            return
        rest_id = self.current["rest_id"]
        self._busy(True)
        self.status.configure(text="Effacement des sauvegardes locales...")

        def job(_w):
            n = Store(acct.data_dir(), rest_id).shred_archives()
            self.q.put(("log", ("%d sauvegarde(s) locale(s) effacee(s)" % n,
                                "ok" if n else "info")))
            self.q.put(("status", "Sauvegardes locales effacees (%d)." % n))

        self.worker = Worker(job, self.q)
        self.worker.start()

    # ------------------------------------------------------------ import

    def _open_import(self):
        ImportDialog(self, on_done=self._after_import)

    def _after_import(self, entry: dict):
        self._refresh_accounts()
        for i, a in enumerate(self.accounts):
            if a["rest_id"] == entry["rest_id"]:
                self.account_menu.current(i)
                self._on_pick_account()
                break
        self.log("Compte @%s importe." % (entry.get("screen_name") or "?"), "ok")

    # ------------------------------------------------------------ tableau

    def _show_empty(self, show: bool, filtered: bool = False):
        if show:
            if filtered:
                self.empty_glyph.configure(text="☷")
                self.empty_title.configure(text="Rien pour ce filtre")
                self.empty_sub.configure(
                    text="Coche un type dans AFFICHER pour voir plus d'elements.")
            elif self.current:
                self.empty_glyph.configure(text="⟳")
                self.empty_title.configure(text="Compte pret a lire")
                self.empty_sub.configure(
                    text="Clique Lire le compte pour charger tweets, reponses et retweets.")
            else:
                self.empty_glyph.configure(text=CHECK_OFF)
                self.empty_title.configure(text="Aucun compte")
                self.empty_sub.configure(
                    text="Clique Importer et colle tes cookies pour commencer.")
            self.empty.place(relx=0.5, rely=0.5, anchor="center")
        else:
            self.empty.place_forget()

    def _repopulate(self):
        self.tree.delete(*self.tree.get_children())
        self.row_by_iid.clear()
        shown = 0
        for rec in self.records:
            if not self.cat_vars.get(rec["kind"], tk.BooleanVar(value=True)).get():
                continue
            on = rec["id"] in self.checked
            kind = {"tweet": "Tweet", "reply": "Reponse",
                    "retweet": "Retweet"}.get(rec["kind"], rec["kind"])
            date = (rec.get("created_at") or "")[:16]
            text = (rec.get("text") or "").replace("\n", " ")
            if len(text) > 120:
                text = text[:117] + "..."
            iid = self.tree.insert(
                "", "end", tags=("on" if on else "off",),
                values=(CHECK_ON if on else CHECK_OFF, kind, date, text,
                        rec.get("likes") or 0))
            self.row_by_iid[iid] = rec
            shown += 1
        self._update_counts(shown)

    def _update_counts(self, shown: int):
        total = len(self.records)
        self.count_lbl.configure(
            text="%d affiches / %d  |  %d selectionnes" % (shown, total, len(self.checked)))
        self._show_empty(shown == 0, filtered=(total > 0 and shown == 0))
        self._sync_delete_button()

    def _sync_delete_button(self):
        n = len(self.checked)
        self.btn_delete.configure(
            text=("Supprimer la selection (%d)" % n) if n else "Supprimer la selection",
            state="normal" if (n and self.worker is None) else "disabled")

    def _set_row(self, iid: str, rec: dict, on: bool):
        if on:
            self.checked.add(rec["id"])
        else:
            self.checked.discard(rec["id"])
        self.tree.set(iid, "check", CHECK_ON if on else CHECK_OFF)
        self.tree.item(iid, tags=("on" if on else "off",))

    def _on_tree_click(self, event):
        if self.tree.identify_region(event.x, event.y) != "cell":
            return
        iid = self.tree.identify_row(event.y)
        rec = self.row_by_iid.get(iid)
        if not rec:
            return
        self._set_row(iid, rec, rec["id"] not in self.checked)
        self._update_counts(len(self.tree.get_children()))

    def _toggle_focus_row(self, _event=None):
        iid = self.tree.focus()
        rec = self.row_by_iid.get(iid)
        if not rec:
            return "break"
        self._set_row(iid, rec, rec["id"] not in self.checked)
        self._update_counts(len(self.tree.get_children()))
        return "break"

    def _check_all(self):
        for iid in self.tree.get_children():
            rec = self.row_by_iid.get(iid)
            if rec:
                self._set_row(iid, rec, True)
        self._update_counts(len(self.tree.get_children()))

    def _check_none(self):
        for iid in self.tree.get_children():
            rec = self.row_by_iid.get(iid)
            if rec:
                self._set_row(iid, rec, False)
        self._update_counts(len(self.tree.get_children()))

    # ------------------------------------------------------------ actions

    def _make_engine(self) -> tuple[Engine, XClient]:
        auth, ct0 = acct.credentials_of(self.current)
        client = XClient(auth, ct0, log=lambda m, l="info": self.q.put(("log", (m, l))))
        store = Store(acct.data_dir(), self.current["rest_id"])
        engine = Engine(
            client, self.current["rest_id"], store,
            log=lambda m, l="info": self.q.put(("log", (m, l))),
            progress=lambda phase, cur, tot, extra="":
                self.q.put(("progress", (phase, cur, tot, extra))),
            should_stop=lambda: self.worker is not None and self.worker.cancelled())
        return engine, client

    def _scan(self):
        if not self.current or self.worker:
            return
        self._busy(True)
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)
        self.status.configure(text="Lecture du compte...")

        def job(_w):
            engine, _ = self._make_engine()
            records = engine.scan()
            self.q.put(("records", records))

        self.worker = Worker(job, self.q)
        self.worker.start()

    def _delete(self):
        if not self.current or self.worker or not self.checked:
            return
        sel = [r for r in self.records if r["id"] in self.checked]
        by = {"tweet": 0, "reply": 0, "retweet": 0}
        for r in sel:
            by[r["kind"]] = by.get(r["kind"], 0) + 1
        if not messagebox.askyesno(
                "Confirmer la suppression",
                "Supprimer definitivement %d element(s) sur @%s ?\n\n"
                "  %d tweet(s), %d reponse(s), %d retweet(s)\n\n"
                "X ne permet aucun retour en arriere. Une sauvegarde locale a ete "
                "ecrite lors de la lecture." % (
                    len(sel), self.current.get("screen_name") or "?",
                    by["tweet"], by["reply"], by["retweet"]),
                icon="warning", default="no"):
            return
        self._busy(True)
        self.progress.configure(mode="determinate", value=0)
        self.status.configure(text="Suppression...")

        def job(_w):
            engine, _ = self._make_engine()
            res = engine.delete_selection(sel)
            self.q.put(("result", (res, {r["id"] for r in sel})))

        self.worker = Worker(job, self.q)
        self.worker.start()

    def _cancel(self):
        if self.worker:
            self.worker.cancel()
            self.status.configure(text="Arret demande...")

    # ------------------------------------------------------------ pump

    def _pump(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self.log(*payload)
                elif kind == "status":
                    self.status.configure(text=payload)
                elif kind == "progress":
                    self._on_progress(*payload)
                elif kind == "records":
                    self.records = payload
                    self.checked.clear()
                    self._repopulate()
                elif kind == "result":
                    self._on_result(*payload)
                elif kind == "auth":
                    self.log(payload, "error")
                    messagebox.showerror(
                        "Session expiree",
                        payload + "\n\nRe-importe le compte avec des cookies frais.")
                elif kind == "error":
                    self.log(payload, "error")
                    self.status.configure(text=payload)
                elif kind == "done":
                    self._on_worker_done()
        except queue.Empty:
            pass
        self.after(80, self._pump)

    def _on_progress(self, phase, cur, tot, extra):
        if phase == "scan":
            self.status.configure(text="Lecture... %d elements  %s" % (cur, extra))
        elif phase == "delete":
            if tot:
                self.progress.configure(maximum=tot, value=cur)
            self.status.configure(text="Suppression %d / %d  %s" % (cur, tot, extra))
        elif phase == "verify":
            if tot:
                self.progress.configure(maximum=tot, value=cur)
            self.status.configure(text="Verification %d / %d" % (cur, tot))

    def _on_result(self, res, deleted_ids):
        failed = {e.split(" :")[0] for e in res.errors}
        gone = {i for i in deleted_ids if i not in failed}
        self.records = [r for r in self.records if r["id"] not in gone]
        self.checked.clear()
        self._repopulate()
        msg = res.summary()
        self.log("Termine : " + msg, "ok" if not res.failed else "warn")
        self.status.configure(text="Termine : " + msg)
        if res.errors:
            messagebox.showwarning(
                "Termine avec des restes",
                "%s\n\nPremiers soucis :\n%s" % (msg, "\n".join(res.errors[:6])))

    def _on_worker_done(self):
        self.worker = None
        self.progress.stop()
        self.progress.configure(mode="determinate", value=0)
        self._busy(False)

    def _on_close(self):
        if self.worker:
            self.worker.cancel()
        self.destroy()


class ImportDialog(tk.Toplevel):
    """Fenetre d'import : coller des cookies, l'identite est verifiee cote X."""

    def __init__(self, master: App, on_done):
        super().__init__(master)
        self.master_app = master
        self.on_done = on_done
        self.title("Importer un compte")
        self.configure(bg=C["page"])
        self.geometry("580x560")
        self.minsize(520, 500)
        self.transient(master)
        self.grab_set()
        self.q: queue.Queue = queue.Queue()
        self.worker: Worker | None = None
        self._build()
        self.after(80, self._pump)

    def _build(self):
        pad = {"padx": SP["xl"]}
        ttk.Label(self, text="Importer un compte", style="Title.TLabel").pack(
            anchor="w", pady=(SP["xl"], SP["xs"]), **pad)
        ttk.Label(self, text="Le plus simple, avec l'extension EditThisCookie",
                  style="Sub.TLabel").pack(anchor="w", **pad)
        steps = tk.Frame(self, bg=C["page"])
        steps.pack(anchor="w", fill="x", pady=(SP["sm"], SP["md"]), **pad)
        for n, t in enumerate((
                "va sur x.com en etant connecte a ton compte,",
                "clique l'icone EditThisCookie, puis Exporter,",
                "colle tout ici en brut. XWipe trouve auth_token et ct0."), 1):
            line = tk.Frame(steps, bg=C["page"])
            line.pack(anchor="w", fill="x", pady=1)
            tk.Label(line, text=str(n), bg=C["accent"], fg=C["on"], width=2,
                     font=(FF_SEMI, 9)).pack(side="left", padx=(0, SP["sm"]))
            tk.Label(line, text=t, bg=C["page"], fg=C["text2"],
                     font=(FF, 10), anchor="w").pack(side="left")
        ttk.Label(self, style="Muted.TLabel", justify="left", wraplength=520, text=(
            "Marchent aussi : l'en-tete Cookie copie depuis les outils F12, ou les "
            "deux valeurs auth_token et ct0 l'une sous l'autre. Rien n'est envoye "
            "ailleurs : XWipe parle a x.com directement et garde les cookies "
            "chiffres sur cette machine.")).pack(anchor="w", **pad)

        wrap = tk.Frame(self, bg=C["line"])
        wrap.pack(fill="both", expand=True, pady=SP["md"], **pad)
        self.text = tk.Text(wrap, height=8, bg=C["sunken"], fg=C["text"],
                            insertbackground=C["text"], relief="flat",
                            font=(FF_MONO, 9), padx=SP["md"], pady=SP["sm"], wrap="none")
        self.text.pack(fill="both", expand=True, padx=1, pady=1)

        self.msg = ttk.Label(self, text="", style="Muted.TLabel", wraplength=520,
                             justify="left")
        self.msg.pack(anchor="w", **pad)

        row = ttk.Frame(self)
        row.pack(fill="x", pady=SP["lg"], **pad)
        self.btn_ok = ttk.Button(row, text="Verifier et importer",
                                 style="Accent.TButton", command=self._import)
        self.btn_ok.pack(side="right")
        ttk.Button(row, text="Annuler", style="Ghost.TButton",
                   command=self.destroy).pack(side="right", padx=(0, SP["sm"]))

    def _import(self):
        raw = self.text.get("1.0", "end")
        try:
            auth, ct0 = acct.parse_credentials(raw)
        except acct.ImportError_ as ex:
            self.msg.configure(text=str(ex), foreground=C["danger"])
            return
        self.msg.configure(text="Verification de la session cote X...",
                           foreground=C["muted"])
        self.btn_ok.configure(state="disabled")

        def job(_w):
            client = XClient(auth, ct0)
            who = client.viewer()
            entry = acct.upsert_account(who["rest_id"], who["screen_name"],
                                        who["name"], auth, ct0)
            self.q.put(("ok", (entry, who)))

        self.worker = Worker(job, self.q)
        self.worker.start()

    def _pump(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "ok":
                    entry, who = payload
                    self.on_done(entry)
                    self.destroy()
                    return
                elif kind in ("auth", "error"):
                    self.msg.configure(text=payload, foreground=C["danger"])
                    self.btn_ok.configure(state="normal")
                elif kind == "done":
                    self.worker = None
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(80, self._pump)


def main():
    App().mainloop()


if __name__ == "__main__":
    main()
