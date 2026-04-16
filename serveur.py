import tkinter as tk
from tkinter import ttk, messagebox, simpledialog
import socket, ssl, os, subprocess, json, threading, queue, datetime

# ============================================================
# CONFIGURATION
# ============================================================
HOST       = '127.0.0.1'
PORT       = 5000
CERT       = 'cert.pem'
KEY        = 'key.pem'
USERS_FILE = 'users.json'

# ============================================================
# COULEURS & THÈME (style terminal sombre industriel)
# ============================================================
BG         = "#0d1117"   # Fond principal (noir bleuté)
BG2        = "#161b22"   # Fond des panneaux
BG3        = "#21262d"   # Fond des champs / zones
ACCENT     = "#00ff88"   # Vert néon (accents)
ACCENT2    = "#0ea5e9"   # Bleu clair (info)
RED        = "#ff4444"   # Rouge (erreur / stop)
ORANGE     = "#f59e0b"   # Orange (avertissement)
FG         = "#e6edf3"   # Texte principal
FG2        = "#8b949e"   # Texte secondaire
BORDER     = "#30363d"   # Bordures

FONT_MONO  = ("Courier New", 10)
FONT_TITLE = ("Courier New", 13, "bold")
FONT_SMALL = ("Courier New", 9)
FONT_BTN   = ("Courier New", 10, "bold")

# ============================================================
# ÉTAT GLOBAL DU SERVEUR
# ============================================================
server_running    = False
client_connected  = False
server_thread     = None
ssock             = None        # Socket SSL principale
log_queue         = queue.Queue()
cmd_history       = []          # Historique des commandes
connected_clients = []          # Liste des clients connectés

# ============================================================
# CERTIFICAT SSL AUTO-GÉNÉRÉ
# ============================================================
def ensure_certificates():
    if not (os.path.exists(CERT) and os.path.exists(KEY)):
        subprocess.run([
            "openssl", "req", "-x509", "-newkey", "rsa:2048",
            "-keyout", KEY, "-out", CERT, "-days", "365", "-nodes",
            "-subj", "/C=FR/ST=Paris/L=Paris/O=MonServeur/CN=localhost"
        ], capture_output=True)

# ============================================================
# GESTION DES UTILISATEURS
# ============================================================
def load_users():
    if not os.path.exists(USERS_FILE):
        with open(USERS_FILE, 'w') as f:
            json.dump({"admin": "admin"}, f)
    with open(USERS_FILE, 'r') as f:
        return json.load(f)

def save_users(users):
    with open(USERS_FILE, 'w') as f:
        json.dump(users, f, indent=2)

# ============================================================
# EXÉCUTION DE COMMANDES SHELL
# ============================================================
def execute_command(cmd, current_dir=None):
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True,
            text=True, cwd=current_dir, timeout=10
        )
        return result.stdout + result.stderr
    except subprocess.TimeoutExpired:
        return "❌ Timeout : commande trop longue\n"
    except Exception as e:
        return f"❌ Erreur : {e}\n"

# ============================================================
# GESTION D'UN CLIENT
# ============================================================
def handle_client(conn, addr):
    global client_connected
    USERS = load_users()
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    log_queue.put(("info", f"[{ts}] 🔌 Connexion entrante : {addr[0]}:{addr[1]}"))

    try:
        # ---------- Authentification ----------
        conn.send(b"LOGIN\n")
        username = conn.recv(1024).decode().strip()
        conn.send(b"PASSWORD\n")
        password = conn.recv(1024).decode().strip()

        if USERS.get(username) != password:
            conn.send(b"AUTH_FAILED\n")
            conn.close()
            ts = datetime.datetime.now().strftime("%H:%M:%S")
            log_queue.put(("error", f"[{ts}] ❌ Auth échouée pour '{username}' ({addr[0]})"))
            client_connected = False
            log_queue.put(("client_update", None))
            return

        conn.send(b"AUTH_SUCCESS\n")
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        log_queue.put(("success", f"[{ts}] ✅ '{username}' authentifié ({addr[0]}:{addr[1]})"))

        # Ajout à la liste des clients
        client_info = {"user": username, "ip": addr[0], "port": addr[1], "since": ts}
        connected_clients.append(client_info)
        log_queue.put(("client_update", None))

        # ---------- Boucle de commandes ----------
        current_dir = os.getcwd()
        while True:
            data = conn.recv(4096).decode().strip()
            if not data:
                break

            ts = datetime.datetime.now().strftime("%H:%M:%S")

            # Changement de répertoire
            if data.startswith("cd "):
                path = data[3:].strip()
                try:
                    os.chdir(path)
                    current_dir = os.getcwd()
                    conn.send(f"✅ Répertoire : {current_dir}\n".encode())
                    log_queue.put(("cmd", f"[{ts}] [{username}] $ {data}"))
                    cmd_history.append({"time": ts, "user": username, "cmd": data, "status": "ok"})
                except Exception as e:
                    conn.send(f"❌ Erreur cd : {e}\n".encode())
                    log_queue.put(("error", f"[{ts}] [{username}] ❌ cd échoué : {e}"))
                    cmd_history.append({"time": ts, "user": username, "cmd": data, "status": "err"})
                log_queue.put(("history_update", None))
                continue

            # Réception de fichier
            if data.startswith("FILE:"):
                filename = data[5:]
                conn.send(b"READY\n")
                file_size = int(conn.recv(16).decode())
                with open("server_" + filename, "wb") as f:
                    remaining = file_size
                    while remaining > 0:
                        chunk = conn.recv(min(4096, remaining))
                        if not chunk:
                            break
                        f.write(chunk)
                        remaining -= len(chunk)
                conn.send(f"FILE_RECEIVED:{filename}".encode())
                log_queue.put(("success", f"[{ts}] [{username}] 📁 Fichier reçu : {filename}"))
                cmd_history.append({"time": ts, "user": username, "cmd": f"FILE: {filename}", "status": "ok"})
                log_queue.put(("history_update", None))
                continue

            # Envoi de fichier vers le client (download)
            if data.startswith("GET:"):
                filename = data[4:].strip()
                filepath = os.path.join(current_dir, filename)

                # Étape 1 : le fichier existe ?
                if not os.path.exists(filepath):
                    conn.send(f"ERROR:Fichier introuvable : {filename}\n".encode())
                    log_queue.put(("error", f"[{ts}] [{username}] ❌ GET échoué : {filename} introuvable"))
                    cmd_history.append({"time": ts, "user": username, "cmd": f"GET: {filename}", "status": "err"})
                    log_queue.put(("history_update", None))
                    continue

                file_size = os.path.getsize(filepath)

                # Étape 2 : on envoie la taille au client
                conn.send(f"FILE_SIZE:{file_size}\n".encode())

                # Étape 3 : on attend que le client soit prêt
                ack = conn.recv(16).decode().strip()
                if ack != "READY":
                    log_queue.put(("error", f"[{ts}] [{username}] ❌ Client pas prêt pour GET"))
                    continue

                # Étape 4 : on envoie le fichier par morceaux
                sent = 0
                with open(filepath, "rb") as f:
                    while True:
                        chunk = f.read(4096)
                        if not chunk:
                            break
                        conn.send(chunk)
                        sent += len(chunk)

                conn.send(f"FILE_SENT:{filename}".encode())
                log_queue.put(("success", f"[{ts}] [{username}] 📤 Fichier envoyé : {filename} ({file_size} octets)"))
                cmd_history.append({"time": ts, "user": username, "cmd": f"GET: {filename}", "status": "ok"})
                log_queue.put(("history_update", None))
                continue

            # Commande shell
            result = execute_command(data, current_dir=current_dir)
            conn.send(result.encode())
            log_queue.put(("cmd", f"[{ts}] [{username}] $ {data}"))
            cmd_history.append({"time": ts, "user": username, "cmd": data, "status": "ok"})
            log_queue.put(("history_update", None))

    except Exception as e:
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        log_queue.put(("error", f"[{ts}] ⚠️ Erreur client {addr[0]} : {e}"))
    finally:
        conn.close()
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        log_queue.put(("warn", f"[{ts}] 🔌 Client déconnecté : {addr[0]}:{addr[1]}"))
        # Retirer de la liste
        connected_clients[:] = [c for c in connected_clients if not (c["ip"] == addr[0] and c["port"] == addr[1])]
        client_connected = False
        log_queue.put(("client_update", None))

# ============================================================
# BOUCLE PRINCIPALE DU SERVEUR (dans un thread)
# ============================================================
def server_loop():
    global server_running, client_connected, ssock

    ensure_certificates()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=CERT, keyfile=KEY)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(1.0)  # timeout pour vérifier server_running
        sock.bind((HOST, PORT))
        sock.listen(1)

        ts = datetime.datetime.now().strftime("%H:%M:%S")
        log_queue.put(("success", f"[{ts}] 🚀 Serveur démarré sur {HOST}:{PORT}"))

        with context.wrap_socket(sock, server_side=True) as ssock_local:
            ssock = ssock_local
            while server_running:
                try:
                    client_conn, client_addr = ssock_local.accept()
                except ssl.SSLError:
                    continue
                except socket.timeout:
                    continue
                except OSError:
                    break

                if client_connected:
                    client_conn.send(b"SERVER_BUSY\n")
                    client_conn.close()
                    ts = datetime.datetime.now().strftime("%H:%M:%S")
                    log_queue.put(("warn", f"[{ts}] ⛔ Connexion refusée : {client_addr[0]} (serveur occupé)"))
                    continue

                client_connected = True
                t = threading.Thread(target=handle_client, args=(client_conn, client_addr), daemon=True)
                t.start()

    ts = datetime.datetime.now().strftime("%H:%M:%S")
    log_queue.put(("warn", f"[{ts}] 🛑 Serveur arrêté"))

# ============================================================
# INTERFACE GRAPHIQUE
# ============================================================
class ServerGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("⚙  Remote Server Console")
        self.root.configure(bg=BG, cursor="arrow")  # fix curseur macOS
        self.root.geometry("1000x700")
        self.root.minsize(900, 600)

        self._build_ui()
        self._poll_queue()

        # Fix curseur macOS : appliquer après que tous les widgets soient créés
        self.root.after(100, lambda: self._fix_cursor(self.root))

    def _fix_cursor(self, widget):
        try:
            widget.configure(cursor="arrow")
        except:
            pass
        for child in widget.winfo_children():
            self._fix_cursor(child)

    # ----------------------------------------------------------
    # CONSTRUCTION DE L'UI
    # ----------------------------------------------------------
    def _build_ui(self):
        # === BARRE DU HAUT (titre + statut + bouton start/stop) ===
        top = tk.Frame(self.root, bg=BG, pady=8, padx=16)
        top.pack(fill="x")

        tk.Label(top, text="⚙  REMOTE SERVER CONSOLE",
                 font=("Courier New", 14, "bold"), fg=ACCENT, bg=BG).pack(side="left")

        self.lbl_status = tk.Label(top, text="● ARRÊTÉ",
                                   font=FONT_BTN, fg=RED, bg=BG)
        self.lbl_status.pack(side="left", padx=20)

        self.btn_toggle = tk.Button(
            top, text="▶  DÉMARRER", font=FONT_BTN,
            fg=BG, bg=ACCENT, activebackground=ACCENT,
            relief="flat", padx=14, pady=4,
            command=self.toggle_server
        )
        self.btn_toggle.pack(side="right")

        tk.Label(top, text=f"{HOST}:{PORT}", font=FONT_SMALL, fg=FG2, bg=BG).pack(side="right", padx=12)

        # Séparateur
        tk.Frame(self.root, bg=BORDER, height=1).pack(fill="x")

        # === CORPS PRINCIPAL (3 colonnes) ===
        body = tk.Frame(self.root, bg=BG)
        body.pack(fill="both", expand=True, padx=10, pady=8)

        body.columnconfigure(0, weight=3)   # Logs
        body.columnconfigure(1, weight=1)   # Clients + Utilisateurs
        body.rowconfigure(0, weight=1)

        # === COLONNE GAUCHE ===
        left = tk.Frame(body, bg=BG)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        left.rowconfigure(0, weight=3)
        left.rowconfigure(1, weight=1)

        # -- Logs --
        self._section(left, "📡  LOGS EN TEMPS RÉEL", row=0)
        log_frame = tk.Frame(left, bg=BG3, bd=0, highlightthickness=1,
                             highlightbackground=BORDER)
        log_frame.grid(row=1, column=0, sticky="nsew")
        left.rowconfigure(1, weight=3)

        self.text_log = tk.Text(
            log_frame, bg=BG3, fg=FG, font=FONT_MONO,
            insertbackground=ACCENT, relief="flat",
            selectbackground=BG2, wrap="word", state="disabled"
        )
        sb1 = tk.Scrollbar(log_frame, command=self.text_log.yview, bg=BG2)
        self.text_log.configure(yscrollcommand=sb1.set)
        sb1.pack(side="right", fill="y")
        self.text_log.pack(fill="both", expand=True, padx=6, pady=6)

        # Tags de couleur pour les logs
        self.text_log.tag_config("success", foreground=ACCENT)
        self.text_log.tag_config("error",   foreground=RED)
        self.text_log.tag_config("warn",    foreground=ORANGE)
        self.text_log.tag_config("info",    foreground=ACCENT2)
        self.text_log.tag_config("cmd",     foreground=FG2)

        # -- Historique des commandes --
        self._section(left, "🕒  HISTORIQUE DES COMMANDES", row=2)
        hist_frame = tk.Frame(left, bg=BG3, highlightthickness=1,
                              highlightbackground=BORDER)
        hist_frame.grid(row=3, column=0, sticky="nsew")
        left.rowconfigure(3, weight=1)

        cols = ("Heure", "Utilisateur", "Commande", "Statut")
        self.tree_hist = ttk.Treeview(hist_frame, columns=cols, show="headings", height=6)
        for c in cols:
            self.tree_hist.heading(c, text=c)
            self.tree_hist.column(c, width=90 if c != "Commande" else 260, anchor="w")

        sb2 = tk.Scrollbar(hist_frame, command=self.tree_hist.yview)
        self.tree_hist.configure(yscrollcommand=sb2.set)
        sb2.pack(side="right", fill="y")
        self.tree_hist.pack(fill="both", expand=True)

        self._style_treeview()

        # === COLONNE DROITE ===
        right = tk.Frame(body, bg=BG)
        right.grid(row=0, column=1, sticky="nsew")
        right.rowconfigure(1, weight=1)
        right.rowconfigure(3, weight=2)

        # -- Clients connectés --
        self._section(right, "👤  CLIENTS CONNECTÉS", row=0)
        client_frame = tk.Frame(right, bg=BG3, highlightthickness=1,
                                highlightbackground=BORDER)
        client_frame.grid(row=1, column=0, sticky="nsew", pady=(0, 8))

        self.list_clients = tk.Listbox(
            client_frame, bg=BG3, fg=ACCENT, font=FONT_MONO,
            relief="flat", selectbackground=BG2,
            highlightthickness=0, height=5
        )
        self.list_clients.pack(fill="both", expand=True, padx=6, pady=6)

        # -- Gestion des utilisateurs --
        self._section(right, "🔑  UTILISATEURS", row=2)
        user_frame = tk.Frame(right, bg=BG3, highlightthickness=1,
                              highlightbackground=BORDER)
        user_frame.grid(row=3, column=0, sticky="nsew")

        self.list_users = tk.Listbox(
            user_frame, bg=BG3, fg=FG, font=FONT_MONO,
            relief="flat", selectbackground=BG2,
            highlightthickness=0
        )
        self.list_users.pack(fill="both", expand=True, padx=6, pady=6)

        # Boutons utilisateurs
        btn_frame = tk.Frame(right, bg=BG, pady=4)
        btn_frame.grid(row=4, column=0, sticky="ew")

        tk.Button(btn_frame, text="+ Ajouter", font=FONT_SMALL,
                  fg=BG, bg=ACCENT, relief="flat", padx=8,
                  command=self.add_user).pack(side="left", padx=2)
        tk.Button(btn_frame, text="✕ Supprimer", font=FONT_SMALL,
                  fg=FG, bg=RED, relief="flat", padx=8,
                  command=self.delete_user).pack(side="left", padx=2)
        tk.Button(btn_frame, text="↺ MDP", font=FONT_SMALL,
                  fg=BG, bg=ORANGE, relief="flat", padx=8,
                  command=self.change_password).pack(side="left", padx=2)

        # Chargement initial des utilisateurs
        self.refresh_users()

    # ----------------------------------------------------------
    # HELPERS UI
    # ----------------------------------------------------------
    def _section(self, parent, title, row):
        """Titre de section stylisé"""
        tk.Label(parent, text=title, font=FONT_SMALL,
                 fg=FG2, bg=BG, anchor="w"
                 ).grid(row=row, column=0, sticky="ew", pady=(6, 2))

    def _style_treeview(self):
        """Style du tableau Treeview"""
        style = ttk.Style()
        style.theme_use("default")
        style.configure("Treeview",
                        background=BG3, foreground=FG,
                        fieldbackground=BG3, rowheight=22,
                        font=FONT_SMALL, borderwidth=0)
        style.configure("Treeview.Heading",
                        background=BG2, foreground=FG2,
                        font=FONT_SMALL, relief="flat")
        style.map("Treeview", background=[("selected", BG2)])

    # ----------------------------------------------------------
    # DÉMARRAGE / ARRÊT DU SERVEUR
    # ----------------------------------------------------------
    def toggle_server(self):
        global server_running, server_thread
        if not server_running:
            server_running = True
            self.btn_toggle.config(text="■  ARRÊTER", bg=RED)
            self.lbl_status.config(text="● EN LIGNE", fg=ACCENT)
            server_thread = threading.Thread(target=server_loop, daemon=True)
            server_thread.start()
        else:
            server_running = False
            self.btn_toggle.config(text="▶  DÉMARRER", bg=ACCENT)
            self.lbl_status.config(text="● ARRÊTÉ", fg=RED)

    # ----------------------------------------------------------
    # POLLING DE LA QUEUE (met à jour l'UI depuis les threads)
    # ----------------------------------------------------------
    def _poll_queue(self):
        while not log_queue.empty():
            kind, msg = log_queue.get_nowait()
            if kind == "client_update":
                self.refresh_clients()
            elif kind == "history_update":
                self.refresh_history()
            else:
                self._log(msg, kind)
        self.root.after(150, self._poll_queue)

    def _log(self, msg, tag="info"):
        self.text_log.config(state="normal")
        self.text_log.insert(tk.END, msg + "\n", tag)
        self.text_log.see(tk.END)
        self.text_log.config(state="disabled")

    # ----------------------------------------------------------
    # RAFRAÎCHISSEMENT DES LISTES
    # ----------------------------------------------------------
    def refresh_clients(self):
        self.list_clients.delete(0, tk.END)
        if connected_clients:
            for c in connected_clients:
                self.list_clients.insert(tk.END, f"  {c['user']}  {c['ip']}:{c['port']}")
        else:
            self.list_clients.insert(tk.END, "  (aucun client)")

    def refresh_history(self):
        for row in self.tree_hist.get_children():
            self.tree_hist.delete(row)
        for entry in reversed(cmd_history[-50:]):  # 50 dernières commandes
            icon = "✅" if entry["status"] == "ok" else "❌"
            self.tree_hist.insert("", "end", values=(
                entry["time"], entry["user"], entry["cmd"], icon
            ))

    def refresh_users(self):
        self.list_users.delete(0, tk.END)
        users = load_users()
        for username in users:
            self.list_users.insert(tk.END, f"  {username}")

    # ----------------------------------------------------------
    # GESTION DES UTILISATEURS
    # ----------------------------------------------------------
    def add_user(self):
        username = simpledialog.askstring("Ajouter", "Nom d'utilisateur :",
                                          parent=self.root)
        if not username:
            return
        password = simpledialog.askstring("Ajouter", "Mot de passe :",
                                          show="*", parent=self.root)
        if not password:
            return
        users = load_users()
        if username in users:
            messagebox.showerror("Erreur", f"L'utilisateur '{username}' existe déjà.")
            return
        users[username] = password
        save_users(users)
        self.refresh_users()
        self._log(f"[système] 👤 Utilisateur ajouté : {username}", "success")

    def delete_user(self):
        sel = self.list_users.curselection()
        if not sel:
            messagebox.showwarning("Attention", "Sélectionne un utilisateur.")
            return
        username = self.list_users.get(sel[0]).strip()
        if username == "admin":
            messagebox.showerror("Erreur", "Impossible de supprimer 'admin'.")
            return
        if not messagebox.askyesno("Confirmer", f"Supprimer '{username}' ?"):
            return
        users = load_users()
        users.pop(username, None)
        save_users(users)
        self.refresh_users()
        self._log(f"[système] 🗑️ Utilisateur supprimé : {username}", "warn")

    def change_password(self):
        sel = self.list_users.curselection()
        if not sel:
            messagebox.showwarning("Attention", "Sélectionne un utilisateur.")
            return
        username = self.list_users.get(sel[0]).strip()
        new_pass = simpledialog.askstring("Mot de passe",
                                          f"Nouveau mot de passe pour '{username}' :",
                                          show="*", parent=self.root)
        if not new_pass:
            return
        users = load_users()
        users[username] = new_pass
        save_users(users)
        self._log(f"[système] 🔑 Mot de passe changé : {username}", "info")


# ============================================================
# LANCEMENT
# ============================================================
if __name__ == "__main__":
    root = tk.Tk()
    app = ServerGUI(root)
    root.mainloop()
