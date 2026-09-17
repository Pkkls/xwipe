"""Interface XWipe (tkinter, zero dependance).

Le reseau tourne sur un thread de fond ; l'interface ne parle qu'a une file de
messages relue par `after`, donc elle ne gele jamais et rien ne touche a un
widget depuis un autre thread.

Flux : importer un compte (coller les cookies) -> lire le compte -> cocher ce
qu'on veut retirer -> confirmer -> suppression puis verification.
"""
from __future__ import annotations

import queue
import threading

import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext

from . import account as acct
from .api import AuthError, XClient, XError
from .engine import CATEGORIES, Engine, Store

APP_TITLE = "XWipe"
CHECK_ON = "☑"   # boite cochee
CHECK_OFF = "☐"  # boite vide


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


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1024x680")
        self.minsize(860, 560)

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
        self.configure(bg="#0f1115")
        s = ttk.Style(self)
        try:
            s.theme_use("clam")
        except tk.TclError:
            pass
        bg, fg, acc = "#0f1115", "#e6e8eb", "#1d9bf0"
        s.configure(".", background=bg, foreground=fg, fieldbackground="#1a1d24")
        s.configure("TFrame", background=bg)
        s.configure("TLabel", background=bg, foreground=fg)
        s.configure("Muted.TLabel", foreground="#8b929c")
        s.configure("H1.TLabel", font=("Segoe UI Semibold", 15))
        s.configure("TButton", padding=6)
        s.configure("Accent.TButton", foreground="#ffffff")
        s.map("Accent.TButton",
              background=[("!disabled", acc), ("disabled", "#2a2d34")])
        s.configure("Danger.TButton", foreground="#ffffff")
        s.map("Danger.TButton",
              background=[("!disabled", "#c8102e"), ("disabled", "#2a2d34")])
        s.configure("Treeview", background="#12151b", fieldbackground="#12151b",
                    foreground=fg, rowheight=24, borderwidth=0)
        s.configure("Treeview.Heading", background="#1a1d24", foreground="#c7ccd3",
                    relief="flat")
        s.map("Treeview", background=[("selected", "#1d2733")])

    # ----------------------------------------------------------------- layout

    def _build_layout(self):
        top = ttk.Frame(self, padding=(14, 12))
        top.pack(fill="x")
        ttk.Label(top, text="XWipe", style="H1.TLabel").pack(side="left")
        ttk.Label(top, text="  supprime tes tweets, reponses et retweets",
                  style="Muted.TLabel").pack(side="left")

        bar = ttk.Frame(self, padding=(14, 0))
        bar.pack(fill="x")
        ttk.Label(bar, text="Compte :").pack(side="left")
        self.account_var = tk.StringVar()
        self.account_menu = ttk.Combobox(bar, textvariable=self.account_var,
                                         state="readonly", width=34)
        self.account_menu.pack(side="left", padx=6)
        self.account_menu.bind("<<ComboboxSelected>>", lambda _e: self._on_pick_account())
        ttk.Button(bar, text="Importer un compte",
                   command=self._open_import).pack(side="left", padx=4)
        self.btn_remove = ttk.Button(bar, text="Retirer", command=self._remove_account)
        self.btn_remove.pack(side="left")
        self.btn_purge = ttk.Button(bar, text="Effacer sauvegardes locales",
                                    command=self._purge_local)
        self.btn_purge.pack(side="left", padx=4)
        self.btn_scan = ttk.Button(bar, text="Lire le compte", style="Accent.TButton",
                                   command=self._scan)
        self.btn_scan.pack(side="right")

        filt = ttk.Frame(self, padding=(14, 8))
        filt.pack(fill="x")
        self.cat_vars: dict[str, tk.BooleanVar] = {}
        ttk.Label(filt, text="Afficher :").pack(side="left")
        for kind, label in CATEGORIES:
            v = tk.BooleanVar(value=True)
            self.cat_vars[kind] = v
            ttk.Checkbutton(filt, text=label, variable=v,
                            command=self._repopulate).pack(side="left", padx=4)
        ttk.Button(filt, text="Tout cocher (vue)",
                   command=self._check_all).pack(side="left", padx=(16, 2))
        ttk.Button(filt, text="Tout decocher",
                   command=self._check_none).pack(side="left")
        self.count_lbl = ttk.Label(filt, text="", style="Muted.TLabel")
        self.count_lbl.pack(side="right")

        mid = ttk.Frame(self, padding=(14, 0))
        mid.pack(fill="both", expand=True)
        cols = ("check", "kind", "date", "text", "likes")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", selectmode="none")
        for c, txt, w, anchor in (
                ("check", "", 34, "center"), ("kind", "Type", 90, "w"),
                ("date", "Date", 150, "w"), ("text", "Contenu", 560, "w"),
                ("likes", "Likes", 60, "e")):
            self.tree.heading(c, text=txt)
            self.tree.column(c, width=w, anchor=anchor,
                             stretch=(c == "text"))
        vs = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vs.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        self.tree.bind("<Button-1>", self._on_tree_click)

        bottom = ttk.Frame(self, padding=(14, 10))
        bottom.pack(fill="x")
        self.progress = ttk.Progressbar(bottom, mode="determinate")
        self.progress.pack(fill="x", side="top")
        row = ttk.Frame(bottom)
        row.pack(fill="x", pady=(8, 0))
        self.status = ttk.Label(row, text="Importe un compte pour commencer.",
                                style="Muted.TLabel")
        self.status.pack(side="left")
        self.btn_cancel = ttk.Button(row, text="Arreter", command=self._cancel,
                                     state="disabled")
        self.btn_cancel.pack(side="right")
        self.btn_delete = ttk.Button(row, text="Supprimer la selection",
                                     style="Danger.TButton", command=self._delete,
                                     state="disabled")
        self.btn_delete.pack(side="right", padx=6)

        self.logbox = scrolledtext.ScrolledText(self, height=7, bg="#0b0d11",
                                                fg="#c7ccd3", insertbackground="#c7ccd3",
                                                relief="flat", font=("Consolas", 9))
        self.logbox.pack(fill="x", padx=14, pady=(0, 12))
        self.logbox.configure(state="disabled")
        for tag, col in (("ok", "#3fb950"), ("warn", "#d29922"),
                         ("error", "#f85149"), ("info", "#8b929c")):
            self.logbox.tag_config(tag, foreground=col)

    # -------------------------------------------------------------- log / busy

    def log(self, msg: str, level: str = "info"):
        self.logbox.configure(state="normal")
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

    def _repopulate(self):
        self.tree.delete(*self.tree.get_children())
        self.row_by_iid.clear()
        shown = 0
        for rec in self.records:
            if not self.cat_vars.get(rec["kind"], tk.BooleanVar(value=True)).get():
                continue
            mark = CHECK_ON if rec["id"] in self.checked else CHECK_OFF
            kind = {"tweet": "Tweet", "reply": "Reponse",
                    "retweet": "Retweet"}.get(rec["kind"], rec["kind"])
            date = (rec.get("created_at") or "")[:16]
            text = (rec.get("text") or "").replace("\n", " ")
            if len(text) > 120:
                text = text[:117] + "..."
            iid = self.tree.insert("", "end",
                                   values=(mark, kind, date, text, rec.get("likes") or 0))
            self.row_by_iid[iid] = rec
            shown += 1
        self._update_counts(shown)

    def _update_counts(self, shown: int):
        self.count_lbl.configure(
            text="%d affiche(s) sur %d  |  %d selectionne(s)"
            % (shown, len(self.records), len(self.checked)))
        self._sync_delete_button()

    def _sync_delete_button(self):
        can = bool(self.checked) and self.worker is None
        self.btn_delete.configure(state="normal" if can else "disabled")

    def _on_tree_click(self, event):
        if self.tree.identify_region(event.x, event.y) != "cell":
            return
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        col = self.tree.identify_column(event.x)
        rec = self.row_by_iid.get(iid)
        if not rec:
            return
        # Clic sur la case OU sur la ligne : les deux basculent, plus tolerant.
        if rec["id"] in self.checked:
            self.checked.discard(rec["id"])
            self.tree.set(iid, "check", CHECK_OFF)
        else:
            self.checked.add(rec["id"])
            self.tree.set(iid, "check", CHECK_ON)
        self._update_counts(len(self.tree.get_children()))

    def _check_all(self):
        for iid in self.tree.get_children():
            rec = self.row_by_iid.get(iid)
            if rec:
                self.checked.add(rec["id"])
                self.tree.set(iid, "check", CHECK_ON)
        self._update_counts(len(self.tree.get_children()))

    def _check_none(self):
        for iid in self.tree.get_children():
            self.tree.set(iid, "check", CHECK_OFF)
        for rec in list(self.row_by_iid.values()):
            self.checked.discard(rec["id"])
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
        self.configure(bg="#0f1115")
        self.geometry("560x520")
        self.transient(master)
        self.grab_set()
        self.q: queue.Queue = queue.Queue()
        self.worker: Worker | None = None
        self._build()
        self.after(80, self._pump)

    def _build(self):
        pad = {"padx": 16}
        ttk.Label(self, text="Importer un compte", style="H1.TLabel").pack(
            anchor="w", pady=(14, 2), **pad)
        ttk.Label(self, style="Muted.TLabel", justify="left", wraplength=520, text=(
            "Le plus simple, avec l'extension EditThisCookie :\n"
            "  1. va sur x.com en etant connecte a ton compte,\n"
            "  2. clique sur l'icone EditThisCookie, puis sur Exporter,\n"
            "  3. colle tout ici en brut. XWipe trouve auth_token et ct0 tout seul.\n\n"
            "Marchent aussi : l'en-tete Cookie copie depuis les outils F12, ou juste "
            "les deux valeurs auth_token et ct0 l'une sous l'autre.\n\n"
            "Rien n'est envoye ailleurs : XWipe parle directement a x.com et garde "
            "les cookies chiffres sur cette machine.")).pack(anchor="w", **pad)

        self.text = scrolledtext.ScrolledText(self, height=10, bg="#12151b",
                                              fg="#e6e8eb", insertbackground="#e6e8eb",
                                              relief="flat", font=("Consolas", 9))
        self.text.pack(fill="both", expand=True, pady=8, **pad)

        self.msg = ttk.Label(self, text="", style="Muted.TLabel", wraplength=520,
                             justify="left")
        self.msg.pack(anchor="w", **pad)

        row = ttk.Frame(self)
        row.pack(fill="x", pady=12, **pad)
        self.btn_ok = ttk.Button(row, text="Verifier et importer",
                                 style="Accent.TButton", command=self._import)
        self.btn_ok.pack(side="right")
        ttk.Button(row, text="Annuler", command=self.destroy).pack(side="right", padx=6)

    def _import(self):
        raw = self.text.get("1.0", "end")
        try:
            auth, ct0 = acct.parse_credentials(raw)
        except acct.ImportError_ as ex:
            self.msg.configure(text=str(ex), foreground="#f85149")
            return
        self.msg.configure(text="Verification de la session cote X...",
                           foreground="#8b929c")
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
                    self.msg.configure(text=payload, foreground="#f85149")
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
