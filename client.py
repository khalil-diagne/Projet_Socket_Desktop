import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import ssl, socket, threading, os, datetime

# ============================================================
# CONFIGURATION
# ============================================================
HOST = '127.0.0.1'
PORT = 5000
CERT = 'cert.pem'

conn      = None
connected = False

# ============================================================
# COULEURS & THÈME (identique au serveur)
# ============================================================
BG        = "#0d1117"
BG2       = "#161b22"
BG3       = "#21262d"
ACCENT    = "#00ff88"
ACCENT2   = "#0ea5e9"
RED       = "#ff4444"
ORANGE    = "#f59e0b"
FG        = "#e6edf3"
FG2       = "#8b949e"
BORDER    = "#30363d"

FONT_MONO  = ("Courier New", 10)
FONT_TITLE = ("Courier New", 13, "bold")
FONT_SMALL = ("Courier New", 9)
FONT_BTN   = ("Courier New", 10, "bold")

# ============================================================
# HISTORIQUE DES COMMANDES (navigation ↑ ↓)
# ============================================================
cmd_history   = []
history_index = -1

# ============================================================
# CONNEXION AU SERVEUR
# La connexion tourne dans un thread pour ne pas bloquer l'UI
# ============================================================
def connect():
    # Lire les widgets ICI (thread principal) avant de lancer le thread
    username = input_username.get()
    password = input_password.get()
    btn_connect.config(state="disabled", text="Connexion...")
    threading.Thread(target=_connect_thread, args=(username, password), daemon=True).start()

def _connect_thread(username, password):
    """Tout le réseau ici — on ne touche JAMAIS aux widgets Tkinter directement"""
    global conn, connected

    def reset_btn():
        btn_connect.config(state="normal", text="▶  CONNEXION")

    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode    = ssl.CERT_NONE

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    conn = context.wrap_socket(sock, server_hostname=HOST)

    try:
        conn.connect((HOST, PORT))
    except Exception as e:
        root.after(0, log, f"❌ Impossible de se connecter : {e}", "error")
        root.after(0, reset_btn)
        return

    first = conn.recv(1024).decode().strip()
    if first == "SERVER_BUSY":
        root.after(0, log, "⛔ Serveur occupé", "error")
        conn.close()
        root.after(0, reset_btn)
        return

    if first != "LOGIN":
        root.after(0, log, f"❌ Protocole inattendu : {first}", "error")
        root.after(0, reset_btn)
        return

    conn.send(username.encode())

    if conn.recv(1024).decode().strip() == "PASSWORD":
        conn.send(password.encode())
        status = conn.recv(1024).decode().strip()
    else:
        status = "PROTOCOL_ERROR"

    if status == "AUTH_SUCCESS":
        connected = True
        ts = _ts()
        root.after(0, _on_auth_success, username, ts)
    elif status == "AUTH_FAILED":
        root.after(0, log, "❌ Identifiants incorrects", "error")
        root.after(0, reset_btn)
    else:
        root.after(0, log, f"❌ Réponse inattendue : {status}", "error")
        root.after(0, reset_btn)

def _on_auth_success(username, ts):
    """Modifications UI après auth réussie — exécuté dans le thread principal"""
    log(f"[{ts}] ✅ Connecté en tant que '{username}'", "success")
    lbl_status.config(text=f"● CONNECTÉ  ({HOST}:{PORT})", fg=ACCENT)
    hide_login_ui()
    show_command_ui()
    threading.Thread(target=listen_server, daemon=True).start()


# ============================================================
# ENVOYER UNE COMMANDE TEXTE
# ============================================================
def send_command(event=None):
    if not connected:
        log("⚠️ Non connecté", "warn")
        return

    cmd = entry_command.get().strip()
    if not cmd:
        return

    cmd_history.append(cmd)
    global history_index
    history_index = -1

    # Si c'est une commande GET, on gère le download
    if cmd.startswith("GET:") or cmd.lower().startswith("get "):
        # Normaliser : "get photo.png" → "GET:photo.png"
        if cmd.lower().startswith("get "):
            filename = cmd[4:].strip()
        else:
            filename = cmd[4:].strip()
        entry_command.delete(0, tk.END)
        threading.Thread(target=download_file, args=(filename,), daemon=True).start()
        return

    log(f"$ {cmd}", "cmd")
    conn.send(cmd.encode())
    entry_command.delete(0, tk.END)


# ============================================================
# ENVOYER UN FICHIER (upload)
# ============================================================
def send_file():
    if not connected:
        log("⚠️ Non connecté", "warn")
        return

    path = filedialog.askopenfilename(title="Choisir un fichier à envoyer")
    if not path:
        return

    filename  = os.path.basename(path)
    file_size = os.path.getsize(path)

    try:
        conn.send(f"FILE:{filename}".encode())

        ack = conn.recv(16).decode().strip()
        if ack != "READY":
            log("❌ Serveur pas prêt pour le fichier", "error")
            return

        conn.send(str(file_size).ljust(16).encode())

        sent = 0
        with open(path, "rb") as f:
            while True:
                chunk = f.read(4096)
                if not chunk:
                    break
                conn.send(chunk)
                sent += len(chunk)
                progress = int(sent / file_size * 100)
                root.after(0, lbl_file_progress.config, {"text": f"📤 {progress}%"})

        root.after(0, lbl_file_progress.config, {"text": ""})
        root.after(0, log, f"✅ Fichier envoyé : {filename} ({file_size} octets)", "success")

    except Exception as e:
        root.after(0, log, f"❌ Erreur envoi fichier : {e}", "error")


# ============================================================
# TÉLÉCHARGER UN FICHIER (download)
# Protocole :
#   Client → "GET:photo.png"
#   Serveur → "FILE_SIZE:1234" ou "ERROR:..."
#   Client → "READY"
#   Serveur → données...
#   Serveur → "FILE_SENT:photo.png"
# ============================================================
def download_file(filename):
    """Tourne dans un thread — ne touche pas aux widgets directement"""
    if not connected:
        root.after(0, log, "⚠️ Non connecté", "warn")
        return

    # Choisir où sauvegarder AVANT de lancer le réseau (doit être dans le thread principal)
    # On passe par root.after pour demander le dossier
    save_event = threading.Event()
    save_path  = [None]  # liste pour pouvoir la modifier depuis le callback

    def ask_save():
        path = filedialog.asksaveasfilename(
            title="Enregistrer le fichier sous...",
            initialfile=filename
        )
        save_path[0] = path
        save_event.set()  # signal : l'utilisateur a répondu

    root.after(0, ask_save)
    save_event.wait()  # on attend la réponse de la boîte de dialogue

    if not save_path[0]:
        root.after(0, log, "⚠️ Téléchargement annulé", "warn")
        return

    try:
        root.after(0, log, f"📥 Demande de téléchargement : {filename}", "info")

        # Étape 1 : demander le fichier
        conn.send(f"GET:{filename}".encode())

        # Étape 2 : recevoir la taille ou une erreur
        response = conn.recv(1024).decode().strip()

        if response.startswith("ERROR:"):
            root.after(0, log, f"❌ {response[6:]}", "error")
            return

        if not response.startswith("FILE_SIZE:"):
            root.after(0, log, f"❌ Réponse inattendue : {response}", "error")
            return

        file_size = int(response[10:])
        root.after(0, log, f"   Taille : {file_size} octets", "info")

        # Étape 3 : signaler qu'on est prêt
        conn.send(b"READY")

        # Étape 4 : recevoir les données par morceaux
        received = 0
        with open(save_path[0], "wb") as f:
            while received < file_size:
                chunk = conn.recv(min(4096, file_size - received))
                if not chunk:
                    break
                f.write(chunk)
                received += len(chunk)
                progress = int(received / file_size * 100)
                root.after(0, lbl_file_progress.config, {"text": f"📥 {progress}%"})

        # Étape 5 : confirmation finale du serveur
        confirmation = conn.recv(1024).decode().strip()
        root.after(0, lbl_file_progress.config, {"text": ""})

        if confirmation.startswith("FILE_SENT:"):
            root.after(0, log, f"✅ Fichier reçu : {save_path[0]}", "success")
        else:
            root.after(0, log, f"⚠️ Fin inattendue : {confirmation}", "warn")

    except Exception as e:
        root.after(0, log, f"❌ Erreur download : {e}", "error")
        root.after(0, lbl_file_progress.config, {"text": ""})


# ============================================================
# ÉCOUTE DU SERVEUR (thread)
# ============================================================
def listen_server():
    global connected
    while connected:
        try:
            data = conn.recv(4096).decode()
            if data:
                ts = _ts()
                root.after(0, log, f"[{ts}]\n{data.strip()}", "output")
        except:
            if connected:
                root.after(0, log, "🔌 Connexion perdue", "error")
                root.after(0, _do_disconnect)
            break


# ============================================================
# DÉCONNEXION
# ============================================================
def disconnect():
    if messagebox.askyesno("Déconnexion", "Se déconnecter du serveur ?"):
        _do_disconnect()

def _do_disconnect():
    global conn, connected
    connected = False
    try:
        if conn:
            conn.close()
    except:
        pass

    ts = _ts()
    log(f"[{ts}] 🛑 Déconnecté", "warn")
    lbl_status.config(text="● DÉCONNECTÉ", fg=RED)

    show_login_ui()
    hide_command_ui()


# ============================================================
# NAVIGATION HISTORIQUE (touches ↑ ↓)
# ============================================================
def history_up(event):
    global history_index
    if not cmd_history:
        return
    if history_index < len(cmd_history) - 1:
        history_index += 1
    entry_command.delete(0, tk.END)
    entry_command.insert(0, cmd_history[-(history_index + 1)])

def history_down(event):
    global history_index
    if history_index > 0:
        history_index -= 1
        entry_command.delete(0, tk.END)
        entry_command.insert(0, cmd_history[-(history_index + 1)])
    elif history_index == 0:
        history_index = -1
        entry_command.delete(0, tk.END)


# ============================================================
# VIDER LES LOGS
# ============================================================
def clear_logs():
    text_log.config(state="normal")
    text_log.delete("1.0", tk.END)
    text_log.config(state="disabled")


# ============================================================
# HELPERS
# ============================================================
def _ts():
    return datetime.datetime.now().strftime("%H:%M:%S")

def log(msg, tag="info"):
    text_log.config(state="normal")
    text_log.insert(tk.END, msg + "\n", tag)
    text_log.see(tk.END)
    text_log.config(state="disabled")

def hide_login_ui():
    frame_login.pack_forget()

def show_login_ui():
    frame_login.pack(fill="x")

def show_command_ui():
    frame_commands.pack(fill="x")
    root.update_idletasks()          # forcer le rendu avant d'activer
    entry_command.config(state="normal")
    btn_send.config(state="normal")
    btn_disconnect.pack(side="right")
    entry_command.focus_set()

def hide_command_ui():
    btn_disconnect.pack_forget()
    frame_commands.pack_forget()
    entry_command.config(state="disabled")
    btn_send.config(state="disabled")


# ============================================================
# INTERFACE GRAPHIQUE
# ============================================================
root = tk.Tk()
root.title("⚡  Remote Client Console")
root.configure(bg=BG, cursor="arrow")  # fix curseur macOS
root.geometry("820x600")
root.minsize(700, 500)

# Fix curseur macOS : forcer "arrow" sur toute la fenêtre
def fix_cursor(widget):
    try:
        widget.configure(cursor="arrow")
    except:
        pass
    for child in widget.winfo_children():
        fix_cursor(child)

# On applique après que tous les widgets soient créés
root.after(100, lambda: fix_cursor(root))

# On utilise grid sur root pour contrôler précisément la hauteur de chaque zone
root.rowconfigure(0, weight=0)   # barre du haut  → taille fixe
root.rowconfigure(1, weight=0)   # séparateur     → taille fixe
root.rowconfigure(2, weight=1)   # logs           → s'étire
root.rowconfigure(3, weight=0)   # bouton vider   → taille fixe
root.rowconfigure(4, weight=0)   # login / commandes → taille fixe
root.columnconfigure(0, weight=1)

# ── Barre du haut ──────────────────────────────────────────
top = tk.Frame(root, bg=BG, pady=8, padx=16)
top.grid(row=0, column=0, sticky="ew")

tk.Label(top, text="⚡  REMOTE CLIENT CONSOLE",
         font=FONT_TITLE, fg=ACCENT, bg=BG).pack(side="left")

lbl_status = tk.Label(top, text="● DÉCONNECTÉ",
                      font=FONT_BTN, fg=RED, bg=BG)
lbl_status.pack(side="left", padx=20)

# ── Séparateur ─────────────────────────────────────────────
tk.Frame(root, bg=BORDER, height=1).grid(row=1, column=0, sticky="ew")

# ── Zone de logs ───────────────────────────────────────────
log_outer = tk.Frame(root, bg=BG, padx=10, pady=8)
log_outer.grid(row=2, column=0, sticky="nsew")
log_outer.rowconfigure(0, weight=1)
log_outer.columnconfigure(0, weight=1)

log_frame = tk.Frame(log_outer, bg=BG3, highlightthickness=1,
                     highlightbackground=BORDER)
log_frame.grid(row=0, column=0, sticky="nsew")

text_log = tk.Text(
    log_frame, bg=BG3, fg=FG, font=FONT_MONO,
    insertbackground=ACCENT, relief="flat",
    selectbackground=BG2, wrap="word", state="disabled"
)
sb = tk.Scrollbar(log_frame, command=text_log.yview, bg=BG2)
text_log.configure(yscrollcommand=sb.set)
sb.pack(side="right", fill="y")
text_log.pack(fill="both", expand=True, padx=6, pady=6)

# Tags couleur
text_log.tag_config("success", foreground=ACCENT)
text_log.tag_config("error",   foreground=RED)
text_log.tag_config("warn",    foreground=ORANGE)
text_log.tag_config("info",    foreground=ACCENT2)
text_log.tag_config("cmd",     foreground=FG2)
text_log.tag_config("output",  foreground=FG)

# ── Bouton vider logs ──────────────────────────────────────
btn_clear_row = tk.Frame(root, bg=BG, padx=10)
btn_clear_row.grid(row=3, column=0, sticky="ew")
tk.Button(btn_clear_row, text="🗑 Vider logs", font=FONT_SMALL,
          fg=FG2, bg=BG2, relief="flat", padx=8,
          command=clear_logs).pack(anchor="e", pady=2)

# ── Zone du bas : login OU commandes (toujours visible) ────
bottom = tk.Frame(root, bg=BG)
bottom.grid(row=4, column=0, sticky="ew")

# ── Formulaire de login ────────────────────────────────────
frame_login = tk.Frame(bottom, bg=BG, pady=10, padx=16)
frame_login.pack(fill="x")  # visible au démarrage

# Ligne séparatrice
tk.Frame(frame_login, bg=BORDER, height=1).grid(
    row=0, column=0, columnspan=3, sticky="ew", pady=(0, 10))

tk.Label(frame_login, text="Utilisateur", font=FONT_SMALL,
         fg=FG2, bg=BG).grid(row=1, column=0, padx=(0, 8), sticky="e")
input_username = tk.Entry(frame_login, font=FONT_MONO,
                          bg=BG3, fg=FG, insertbackground=ACCENT,
                          relief="flat", highlightthickness=1,
                          highlightbackground=BORDER, width=18)
input_username.grid(row=1, column=1, padx=4, pady=4, sticky="w")
input_username.insert(0, "admin")

tk.Label(frame_login, text="Mot de passe", font=FONT_SMALL,
         fg=FG2, bg=BG).grid(row=2, column=0, padx=(0, 8), sticky="e")
input_password = tk.Entry(frame_login, font=FONT_MONO, show="*",
                          bg=BG3, fg=FG, insertbackground=ACCENT,
                          relief="flat", highlightthickness=1,
                          highlightbackground=BORDER, width=18)
input_password.grid(row=2, column=1, padx=4, pady=4, sticky="w")
input_password.insert(0, "admin")

btn_connect = tk.Button(
    frame_login, text="▶  CONNEXION", font=FONT_BTN,
    fg=BG, bg=ACCENT, activebackground=ACCENT,
    relief="flat", padx=14, pady=4,
    command=connect
)
btn_connect.grid(row=1, column=2, rowspan=2, padx=16)

# ── Zone de commandes (cachée au départ) ──────────────────
frame_commands = tk.Frame(bottom, bg=BG, pady=8, padx=10)
# affiché après connexion via show_command_ui()

# Ligne séparatrice
tk.Frame(frame_commands, bg=BORDER, height=1).pack(fill="x", pady=(0, 8))

# Ligne 1 : champ commande + bouton envoyer
cmd_row = tk.Frame(frame_commands, bg=BG)
cmd_row.pack(fill="x")

tk.Label(cmd_row, text="$", font=FONT_TITLE,
         fg=ACCENT, bg=BG).pack(side="left", padx=(0, 6))

entry_command = tk.Entry(
    cmd_row, font=FONT_MONO, bg=BG3, fg=FG,
    insertbackground=ACCENT, relief="flat",
    highlightthickness=1, highlightbackground=BORDER,
    state="disabled"
)
entry_command.pack(side="left", fill="x", expand=True)

entry_command.bind("<Return>", send_command)
entry_command.bind("<Up>",     history_up)
entry_command.bind("<Down>",   history_down)

btn_send = tk.Button(
    cmd_row, text="Envoyer ↵", font=FONT_BTN,
    fg=BG, bg=ACCENT, activebackground=ACCENT,
    relief="flat", padx=10, pady=3,
    command=send_command, state="disabled"
)
btn_send.pack(side="left", padx=(8, 0))

# Ligne 2 : fichier + progression + déconnexion
action_row = tk.Frame(frame_commands, bg=BG, pady=6)
action_row.pack(fill="x")

btn_file = tk.Button(
    action_row, text="📤 Envoyer fichier", font=FONT_SMALL,
    fg=FG, bg=BG2, activebackground=BG3,
    relief="flat", padx=10, pady=3,
    command=send_file
)
btn_file.pack(side="left")

btn_download = tk.Button(
    action_row, text="📥 Télécharger fichier", font=FONT_SMALL,
    fg=FG, bg=BG2, activebackground=BG3,
    relief="flat", padx=10, pady=3,
    command=lambda: _ask_and_download()
)
btn_download.pack(side="left", padx=(6, 0))

lbl_file_progress = tk.Label(action_row, text="", font=FONT_SMALL,
                              fg=ACCENT2, bg=BG)
lbl_file_progress.pack(side="left", padx=10)

btn_disconnect = tk.Button(
    action_row, text="✕ Déconnexion", font=FONT_SMALL,
    fg=FG, bg=RED, activebackground="#cc2222",
    relief="flat", padx=10, pady=3,
    command=disconnect
)
# affiché après connexion via show_command_ui()

def _ask_and_download():
    """Demande le nom du fichier via une boîte de dialogue simple puis lance le download"""
    if not connected:
        log("⚠️ Non connecté", "warn")
        return
    from tkinter import simpledialog
    filename = simpledialog.askstring(
        "Télécharger un fichier",
        "Nom du fichier sur le serveur :",
        parent=root
    )
    if filename and filename.strip():
        threading.Thread(target=download_file, args=(filename.strip(),), daemon=True).start()

# ── Message d'accueil ─────────────────────────────────────
log("⚡ Remote Client Console — prêt", "success")
log(f"   Serveur cible : {HOST}:{PORT}", "info")
log("   Entrez vos identifiants pour vous connecter", "info")
log("   Commandes : tapez directement ou utilisez les boutons", "info")
log("   Download  : get photo.png  ou bouton 📥\n", "info")

root.mainloop()
