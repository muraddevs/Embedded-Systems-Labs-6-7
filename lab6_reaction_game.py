"""
Lab 6 — Two-Player Reaction Game (PC-side GUI)

Talks to the Arduino over serial. The Arduino runs the actual game
state machine (random delay, buzzer, button capture, servo + stepper
actuation) and sends one-line status messages back. This script just:

  - collects the two player names,
  - sends START / RESET commands to the Arduino,
  - parses the lines the Arduino sends back,
  - keeps score / history / leaderboard,
  - saves each round to a per-player CSV file,
  - plots reaction times, win rates, and head-to-head matchups.

Wire protocol (Arduino -> PC):
    GO              buzzer just fired, players should react now
    TIMEOUT         nobody pressed within the window, will replay
    P1:<ms>         player 1 won the round, reaction time in ms
    P2:<ms>         player 2 won the round, reaction time in ms
    P1_FALSE        player 1 jumped before GO, point goes to P2
    P2_FALSE        player 2 jumped before GO, point goes to P1

Wire protocol (PC -> Arduino):
    START\n         arm the next round
    RESET\n         reset Arduino-side state for a new match
"""

import tkinter as tk
import tkinter.messagebox as msg
import tkinter.ttk as ttk
import os
import csv
import datetime
import serial
import time
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from collections import defaultdict

# ── Color palette ─────────────────────────────────────────────
# Dark theme. Two accent colours (blue/pink) are used consistently
# for Player 1 and Player 2 across every widget and every chart,
# so the colour itself communicates which player a value belongs to.
BG_DARK      = "#0d0d14"
BG_CARD      = "#16162a"
BG_INPUT     = "#1e1e35"
ACCENT_BLUE  = "#4f8ef7"   # Player 1
ACCENT_PINK  = "#f74f8e"   # Player 2
ACCENT_GREEN = "#4ff7a0"   # success / positive actions
ACCENT_GOLD  = "#f7c94f"   # leaderboard
TEXT_WHITE   = "#e8e8f0"
TEXT_GRAY    = "#7070a0"
BORDER       = "#2a2a45"

# ── Fonts ────────────────────────────────────────────────────
# Helvetica because it ships on every macOS/Windows install — no
# extra setup needed on the lab machine.
FONT_TITLE   = ("Helvetica", 20, "bold")
FONT_HEADING = ("Helvetica", 11, "bold")
FONT_BODY    = ("Helvetica", 10)
FONT_SCORE   = ("Helvetica", 28, "bold")
FONT_STATUS  = ("Helvetica", 11)
FONT_BTN     = ("Helvetica", 10, "bold")
FONT_SMALL   = ("Helvetica", 9)

# ============================================================
#  SERIAL CONNECTION
# ============================================================

# macOS-style USB-modem device name. On Linux this would be /dev/ttyACM0
# and on Windows it would be a COM port. Update SERIAL_PORT for your machine.
SERIAL_PORT = "/dev/cu.usbmodem141301"
BAUD_RATE   = 9600

# We open the port once, at import time. If the Arduino isn't plugged in,
# we still want the GUI to come up (so we can browse old stats) — hence
# the try/except setting a flag instead of crashing.
try:
    ser = serial.Serial(SERIAL_PORT, BAUD_RATE)
    # The Uno auto-resets when the port is opened. Give the bootloader
    # time to hand control to the sketch before sending anything,
    # otherwise the first START gets eaten.
    time.sleep(2)
    serial_connected = True
    print("Arduino connected on", SERIAL_PORT)
except Exception as e:
    print(f"Serial not connected: {e}")
    serial_connected = False

# ============================================================
#  FILE PATHS
# ============================================================

# Two persistence layers:
#   leaderboard.txt   — plain text, one player name per line, top of file = best rank
#   player_data/<name>.csv — per-player round history (timestamps, opponents, reaction times)
# A flat CSV per player keeps the format trivially diffable and lets you
# open it in Excel/Numbers for sanity-checking without writing any code.
LEADERBOARD_FILE = "leaderboard.txt"
DATA_DIR = "player_data"
os.makedirs(DATA_DIR, exist_ok=True)

def player_file(name):
    """Path to a single player's CSV file."""
    return os.path.join(DATA_DIR, f"{name}.csv")

# ============================================================
#  WINDOW SETUP
# ============================================================

root = tk.Tk()
root.title("Reaction Game")
root.geometry("600x800")
root.resizable(False, False)  # fixed size — layout was designed for one aspect ratio
root.configure(bg=BG_DARK)

# ── TTK Notebook style ───────────────────────────────────────
# Tk's default ttk theme has a light grey tab look that clashes with the
# dark background. Override the "TNotebook" and "TNotebook.Tab" styles
# so tabs look native to the rest of the dark UI.
style = ttk.Style()
style.theme_use("default")
style.configure("TNotebook",
    background=BG_DARK, borderwidth=0, tabmargins=[2, 8, 2, 0])
style.configure("TNotebook.Tab",
    background=BG_CARD, foreground=TEXT_GRAY,
    font=FONT_BTN, padding=[24, 10], borderwidth=0)
style.map("TNotebook.Tab",
    background=[("selected", BG_INPUT)],
    foreground=[("selected", TEXT_WHITE)])

# Two tabs: live game on one side, historical stats / charts on the other.
notebook = ttk.Notebook(root)
notebook.pack(fill="both", expand=True, padx=14, pady=14)

game_frame  = tk.Frame(notebook, bg=BG_DARK)
stats_frame = tk.Frame(notebook, bg=BG_DARK)
notebook.add(game_frame,  text="  Game  ")
notebook.add(stats_frame, text="  Stats  ")

# ── Helpers ──────────────────────────────────────────────────
# Small factory functions so every card/button/entry has identical
# colouring and spacing without repeating the kwargs everywhere.
def card(parent, **kw):
    """A 'card' panel: BG_CARD fill with a thin BORDER outline."""
    return tk.Frame(parent, bg=BG_CARD,
                    highlightbackground=BORDER,
                    highlightthickness=1, **kw)

def styled_btn(parent, text, command, color=ACCENT_BLUE, width=15):
    """Flat-style dark button. `color` is the text colour AND the
    active (hover) background, so each action gets its own accent
    while sharing the same neutral resting state."""
    return tk.Button(parent,
        text=text, command=command,
        font=FONT_BTN, bg=BG_INPUT, fg=color,
        activebackground=color, activeforeground=BG_DARK,
        relief="flat", bd=0, width=width,
        cursor="hand2", pady=9,
        highlightbackground=BORDER, highlightthickness=1)

def styled_entry(parent, width=20):
    """Dark-themed Entry. Caret colour matches the player-1 accent."""
    return tk.Entry(parent,
        font=FONT_BODY, bg=BG_INPUT, fg=TEXT_WHITE,
        insertbackground=ACCENT_BLUE,
        relief="flat", bd=0, width=width,
        highlightbackground=BORDER, highlightthickness=1)

# ============================================================
#  GAME TAB
# ============================================================

# ── Title ────────────────────────────────────────────────────
tk.Label(game_frame,
    text="Reaction Game",
    font=FONT_TITLE, bg=BG_DARK, fg=TEXT_WHITE).pack(pady=(14, 2))

tk.Label(game_frame,
    text="First to 3 wins takes the match",
    font=FONT_SMALL, bg=BG_DARK, fg=TEXT_GRAY).pack(pady=(0, 10))

# ── Players card ─────────────────────────────────────────────
# Two name fields. Names are used as filenames (player_data/<name>.csv)
# and as keys in the leaderboard, so they must match exactly across rounds.
pc = card(game_frame)
pc.pack(fill="x", padx=16, pady=4)

tk.Label(pc, text="Players", font=FONT_HEADING,
         bg=BG_CARD, fg=TEXT_GRAY).pack(anchor="w", padx=14, pady=(10,6))

# Player 1 row — label coloured blue so it ties to the score/charts later.
p1_row = tk.Frame(pc, bg=BG_CARD)
p1_row.pack(fill="x", padx=14, pady=3)
tk.Label(p1_row, text="Player 1", font=FONT_BODY,
         bg=BG_CARD, fg=ACCENT_BLUE, width=9, anchor="w").pack(side="left")
p1_entry = styled_entry(p1_row, width=24)
p1_entry.pack(side="left", padx=(6,0))

# Player 2 row — same structure, pink accent.
p2_row = tk.Frame(pc, bg=BG_CARD)
p2_row.pack(fill="x", padx=14, pady=3)
tk.Label(p2_row, text="Player 2", font=FONT_BODY,
         bg=BG_CARD, fg=ACCENT_PINK, width=9, anchor="w").pack(side="left")
p2_entry = styled_entry(p2_row, width=24)
p2_entry.pack(side="left", padx=(6,0))

# Thin separator at the bottom of the card.
tk.Frame(pc, bg=BORDER, height=1).pack(fill="x", padx=14, pady=(10,0))

# ── Status card ──────────────────────────────────────────────
# Single-line status. Updated by start_game() and read_serial() to tell
# the user what the system is doing right now.
sc = card(game_frame)
sc.pack(fill="x", padx=16, pady=4)

status_label = tk.Label(sc,
    text="Enter names and press Start",
    font=FONT_STATUS, bg=BG_CARD, fg=TEXT_GRAY, pady=12)
status_label.pack()

# ── Score card ───────────────────────────────────────────────
# Big "0  —  0" display in the centre, flanked by small P1 / P2 labels.
# The em-dash separator and generous padding mimic a sports scoreboard.
score_card = card(game_frame)
score_card.pack(fill="x", padx=16, pady=4)

score_row = tk.Frame(score_card, bg=BG_CARD)
score_row.pack(pady=12)

p1_score_label = tk.Label(score_row, text="P1",
    font=FONT_HEADING, bg=BG_CARD, fg=ACCENT_BLUE)
p1_score_label.pack(side="left", padx=16)

score_label = tk.Label(score_row, text="0  —  0",
    font=FONT_SCORE, bg=BG_CARD, fg=TEXT_WHITE)
score_label.pack(side="left", padx=16)

p2_score_label = tk.Label(score_row, text="P2",
    font=FONT_HEADING, bg=BG_CARD, fg=ACCENT_PINK)
p2_score_label.pack(side="left", padx=16)

# ── History card ─────────────────────────────────────────────
# Round-by-round log for the current match. Just a Listbox that
# read_serial() appends a line to whenever a round resolves.
hc = card(game_frame)
hc.pack(fill="x", padx=16, pady=4)

tk.Label(hc, text="Match History", font=FONT_HEADING,
         bg=BG_CARD, fg=TEXT_GRAY).pack(anchor="w", padx=14, pady=(10,4))

history_list = tk.Listbox(hc,
    font=FONT_SMALL, bg=BG_INPUT, fg=TEXT_WHITE,
    selectbackground=ACCENT_BLUE, selectforeground=BG_DARK,
    relief="flat", bd=0, height=6, highlightthickness=0,
    activestyle="none")
history_list.pack(fill="x", padx=10, pady=(0,10))

# ── Leaderboard card ─────────────────────────────────────────
# Cross-match leaderboard. Each match win bubbles the winner up one
# slot — a primitive "ladder" ranking that's good enough for a lab.
lc = card(game_frame)
lc.pack(fill="x", padx=16, pady=4)

tk.Label(lc, text="🏆  Leaderboard", font=FONT_HEADING,
         bg=BG_CARD, fg=TEXT_GRAY).pack(anchor="w", padx=14, pady=(10,4))

leaderboard_list = tk.Listbox(lc,
    font=FONT_SMALL, bg=BG_INPUT, fg=ACCENT_GOLD,
    selectbackground=ACCENT_GOLD, selectforeground=BG_DARK,
    relief="flat", bd=0, height=3, highlightthickness=0,
    activestyle="none")
leaderboard_list.pack(fill="x", padx=10, pady=(0,10))

# ── Buttons ──────────────────────────────────────────────────
# A 2×2 grid of action buttons. Reset actions are pink to make them
# stand out from the "go" actions and discourage accidental clicks.
btn_frame = tk.Frame(game_frame, bg=BG_DARK)
btn_frame.pack(pady=10)

styled_btn(btn_frame, "Start Game",  lambda: start_game(), ACCENT_BLUE).grid(row=0, column=0, padx=5, pady=4)
styled_btn(btn_frame, "New Game",    lambda: new_game(),   ACCENT_GREEN).grid(row=0, column=1, padx=5, pady=4)
styled_btn(btn_frame, "Reset Board", lambda: reset_leaderboard(),    ACCENT_PINK).grid(row=1, column=0, padx=5, pady=4)
styled_btn(btn_frame, "Reset Records", lambda: reset_player_records(), ACCENT_PINK).grid(row=1, column=1, padx=5, pady=4)

# ============================================================
#  STATS TAB
# ============================================================

tk.Label(stats_frame,
    text="Player Statistics",
    font=FONT_TITLE, bg=BG_DARK, fg=TEXT_WHITE).pack(pady=(14, 2))

tk.Label(stats_frame,
    text="Track performance over time",
    font=FONT_SMALL, bg=BG_DARK, fg=TEXT_GRAY).pack(pady=(0, 10))

# ── Input card ───────────────────────────────────────────────
# Two name fields: the player you're analysing, and an opponent
# (only used by the head-to-head chart).
sic = card(stats_frame)
sic.pack(fill="x", padx=16, pady=4)

si_row = tk.Frame(sic, bg=BG_CARD)
si_row.pack(pady=12, padx=14)

tk.Label(si_row, text="Player", font=FONT_BODY,
         bg=BG_CARD, fg=ACCENT_BLUE).pack(side="left")
stats_player_entry = styled_entry(si_row, width=14)
stats_player_entry.pack(side="left", padx=(6, 20))

tk.Label(si_row, text="vs", font=FONT_BODY,
         bg=BG_CARD, fg=TEXT_GRAY).pack(side="left")
stats_opponent_entry = styled_entry(si_row, width=14)
stats_opponent_entry.pack(side="left", padx=(6, 0))

# ── Chart buttons card ───────────────────────────────────────
# Three different views over the same CSV history:
#   Reaction Times — line chart of one player's reaction over sessions
#   Win Rates      — bar chart, win % vs each opponent
#   Head to Head   — scatter, two players' reactions side-by-side
cc = card(stats_frame)
cc.pack(fill="x", padx=16, pady=4)

tk.Label(cc, text="Visualizations", font=FONT_HEADING,
         bg=BG_CARD, fg=TEXT_GRAY).pack(pady=(10,6))

chart_row = tk.Frame(cc, bg=BG_CARD)
chart_row.pack(pady=(0,10))

styled_btn(chart_row, "Reaction Times", lambda: plot_reaction_times(), ACCENT_BLUE,  width=15).pack(side="left", padx=4)
styled_btn(chart_row, "Win Rates",      lambda: plot_win_rates(),      ACCENT_GREEN, width=12).pack(side="left", padx=4)
styled_btn(chart_row, "Head to Head",   lambda: plot_head_to_head(),   ACCENT_PINK,  width=13).pack(side="left", padx=4)

tk.Label(stats_frame,
    text="Opponent field only needed for Head to Head",
    font=FONT_SMALL, bg=BG_DARK, fg=TEXT_GRAY).pack(pady=2)

# ── Results card ─────────────────────────────────────────────
# Tabular preview of a player's CSV file. Useful for spot-checking that
# saves are happening correctly and reaction times look reasonable.
rc = card(stats_frame)
rc.pack(fill="x", padx=16, pady=4)

tk.Label(rc, text="Recent Results", font=FONT_HEADING,
         bg=BG_CARD, fg=TEXT_GRAY).pack(anchor="w", padx=14, pady=(10,4))

stats_list = tk.Listbox(rc,
    font=FONT_SMALL, bg=BG_INPUT, fg=TEXT_WHITE,
    selectbackground=ACCENT_BLUE, selectforeground=BG_DARK,
    relief="flat", bd=0, height=13, highlightthickness=0,
    activestyle="none")
stats_list.pack(fill="x", padx=10, pady=(0,10))

styled_btn(stats_frame, "Load Records", lambda: load_stats_preview(),
           ACCENT_BLUE, width=18).pack(pady=8)

# ============================================================
#  GAME STATE
# ============================================================

# Module-level state. There are only ever two players and one active match,
# so a handful of globals is simpler than a dedicated class.
p1_score     = 0
p2_score     = 0
round_number = 1
game_over    = False     # True once one side hits 3 wins
game_active  = False     # True while a round is in flight (between START and a result)

# CSV schema for per-player files. Every player's CSV is opened in append
# mode, so the header is only written when the file doesn't exist yet.
CSV_HEADER = ["timestamp", "player", "opponent", "result", "reaction_ms"]

def save_result(player, opponent, result, reaction_ms=""):
    """Append one row to player's CSV. Creates the file with a header
    if it doesn't exist yet."""
    path = player_file(player)
    is_new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(CSV_HEADER)
        writer.writerow([datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                         player, opponent, result, reaction_ms])

def load_records(player):
    """Return every row from a player's CSV as a list of dicts.
    Empty list if the file doesn't exist — caller decides what to do."""
    path = player_file(player)
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))

# ============================================================
#  LEADERBOARD
# ============================================================
# The leaderboard is a plain text file, one name per line, where line 0
# is rank #1. Each match win bubbles the winner up exactly one slot
# (like a ladder), so the leaderboard converges over many matches
# without needing a true Elo-style rating.

def load_leaderboard():
    """Return the leaderboard as a list of names, top rank first."""
    if not os.path.exists(LEADERBOARD_FILE):
        return []
    with open(LEADERBOARD_FILE) as f:
        return [l.strip() for l in f if l.strip()]

def save_leaderboard(board):
    """Write the leaderboard back to disk."""
    with open(LEADERBOARD_FILE, "w") as f:
        f.write("\n".join(board) + "\n")

def update_leaderboard(winner, loser):
    """Add both players if missing, then move the winner up one rank."""
    board = load_leaderboard()
    # New players are appended to the bottom. They have to win matches
    # to climb the board.
    for name in [winner, loser]:
        if name not in board:
            board.append(name)
    # Swap the winner with whoever is one rank above them.
    wi = board.index(winner)
    if wi > 0:
        board[wi], board[wi-1] = board[wi-1], board[wi]
    save_leaderboard(board)
    display_leaderboard()

def display_leaderboard():
    """Refresh the leaderboard listbox from disk."""
    leaderboard_list.delete(0, tk.END)
    for i, name in enumerate(load_leaderboard(), 1):
        leaderboard_list.insert(tk.END, f"  {i}.  {name}")

def reset_leaderboard():
    """Delete the leaderboard file after confirming with the user."""
    if msg.askyesno("Confirm", "Clear leaderboard?"):
        if os.path.exists(LEADERBOARD_FILE):
            os.remove(LEADERBOARD_FILE)
        leaderboard_list.delete(0, tk.END)

def reset_player_records():
    """Nuke every per-player CSV. Doesn't touch the leaderboard."""
    if msg.askyesno("Confirm", "Delete ALL player CSV records?"):
        for f in os.listdir(DATA_DIR):
            if f.endswith(".csv"):
                os.remove(os.path.join(DATA_DIR, f))
        history_list.delete(0, tk.END)
        history_list.insert(tk.END, "  All records deleted.")

# ============================================================
#  GAME CONTROL
# ============================================================

def start_game():
    """Validate names and tell the Arduino to arm the next round."""
    global game_active, game_over
    # If the match is already over, force the user to hit New Game first
    # so they don't accidentally extend the same match.
    if game_over:
        status_label.config(text="Press New Game first.", fg=ACCENT_PINK)
        return
    p1 = p1_entry.get().strip()
    p2 = p2_entry.get().strip()
    if not p1 or not p2:
        status_label.config(text="Enter both player names.", fg=ACCENT_PINK)
        return
    # Same name on both sides would mean both rows of the CSV write to
    # the same file with conflicting results — block it.
    if p1.lower() == p2.lower():
        status_label.config(text="Players must have different names.", fg=ACCENT_PINK)
        return
    game_active = True
    status_label.config(text="⏳  Waiting for buzzer...", fg=TEXT_GRAY)
    if serial_connected:
        ser.write(b"START\n")

def new_game():
    """Reset all match-level state. Doesn't touch CSVs or leaderboard."""
    global p1_score, p2_score, round_number, game_over, game_active
    if serial_connected:
        ser.write(b"RESET\n")
    p1_score = p2_score = 0
    round_number = 1
    game_over = game_active = False
    score_label.config(text="0  —  0")
    status_label.config(text="Enter names and press Start", fg=TEXT_GRAY)

# ============================================================
#  SERIAL READER
# ============================================================

def read_serial():
    """Poll the serial port for one line from the Arduino, then
    re-schedule itself with root.after().

    Why polling and not a background thread: Tk widgets can only be
    touched from the main thread. Using root.after(50, ...) keeps
    everything single-threaded — the GUI stays responsive because the
    Arduino is only ever sending short status lines.
    """
    global p1_score, p2_score, round_number, game_over, game_active

    # While no match is active, just idle. Still re-schedule so we
    # start reading again as soon as start_game() flips game_active.
    if not game_active or game_over:
        root.after(50, read_serial)
        return

    if serial_connected and ser.in_waiting:
        try:
            data = ser.readline().decode(errors="replace").strip()
        except Exception:
            # USB cable yanked mid-read, mojibake, etc.
            # Skip this tick and try again next time.
            root.after(50, read_serial)
            return

        print("Arduino:", data)
        p1 = p1_entry.get().strip()
        p2 = p2_entry.get().strip()
        round_finished = False

        # ── Parse one line of the Arduino wire protocol ──
        if data == "GO":
            # Buzzer just fired. Players are now reacting.
            status_label.config(text="⚡  GO!", fg=ACCENT_BLUE)

        elif data == "TIMEOUT":
            # Nobody pressed in time. Brief pause, then re-arm.
            status_label.config(text="⏱  Timeout — replaying...", fg=TEXT_GRAY)
            root.after(1500, lambda: ser.write(b"START\n"))

        elif data == "P1_FALSE":
            # P1 jumped before GO. P2 gets the point.
            p2_score += 1
            _log(f"  ⚠  Round {round_number}: {p1} false start — {p2} gets the point",
                 p2, p1, "WIN (false start)", p1, p2, "LOSS (false start)")
            round_finished = True

        elif data == "P2_FALSE":
            # Mirror of P1_FALSE.
            p1_score += 1
            _log(f"  ⚠  Round {round_number}: {p2} false start — {p1} gets the point",
                 p1, p2, "WIN (false start)", p2, p1, "LOSS (false start)")
            round_finished = True

        elif data.startswith("P1:"):
            # Format: "P1:<ms>" — P1 won with that reaction time.
            t_ms = int(data.split(":")[1])
            p1_score += 1
            _log(f"  ✓  Round {round_number}: {p1} wins  ({t_ms/1000:.3f}s)",
                 p1, p2, "WIN", p2, p1, "LOSS", t_ms)
            round_finished = True

        elif data.startswith("P2:"):
            # Mirror of P1.
            t_ms = int(data.split(":")[1])
            p2_score += 1
            _log(f"  ✓  Round {round_number}: {p2} wins  ({t_ms/1000:.3f}s)",
                 p2, p1, "WIN", p1, p2, "LOSS", t_ms)
            round_finished = True

        # ── Match-over check (best of 5, first to 3) ──
        if p1_score >= 3:
            history_list.insert(tk.END, f"  🏆  {p1} wins the match!")
            history_list.see(tk.END)
            status_label.config(text=f"🏆  {p1} wins the match!", fg=ACCENT_GREEN)
            update_leaderboard(p1, p2)
            game_over = True; game_active = False

        elif p2_score >= 3:
            history_list.insert(tk.END, f"  🏆  {p2} wins the match!")
            history_list.see(tk.END)
            status_label.config(text=f"🏆  {p2} wins the match!", fg=ACCENT_GREEN)
            update_leaderboard(p2, p1)
            game_over = True; game_active = False

        score_label.config(text=f"{p1_score}  —  {p2_score}")

        # If the round resolved but the match isn't over, auto-arm
        # the next round after a short pause so players can register
        # what just happened.
        if round_finished and not game_over:
            round_number += 1
            status_label.config(text="Next round starting...", fg=TEXT_GRAY)
            root.after(1200, lambda: ser.write(b"START\n"))

    # Re-schedule. 50 ms is fast enough for buzzer-trigger UX and slow
    # enough to keep CPU usage essentially zero.
    root.after(50, read_serial)

def _log(history_msg, winner, w_opp, w_result,
         loser, l_opp, l_result, reaction_ms=""):
    """Helper: append one line to the in-window history list AND
    write one CSV row each for the winner and the loser. Centralised
    so every round outcome produces consistent records on both sides."""
    history_list.insert(tk.END, history_msg)
    history_list.see(tk.END)
    save_result(winner, w_opp, w_result, reaction_ms)
    save_result(loser,  l_opp, l_result)

# ============================================================
#  STATS
# ============================================================

def load_stats_preview():
    """Fill the 'Recent Results' listbox with the last 50 CSV rows
    for the named player. Read-only preview — does not modify state."""
    name = stats_player_entry.get().strip()
    if not name:
        msg.showinfo("Stats", "Enter a player name.")
        return
    records = load_records(name)
    stats_list.delete(0, tk.END)
    if not records:
        stats_list.insert(tk.END, f"  No records found for '{name}'.")
        return
    # Fake column header + horizontal rule using box-drawing characters.
    # Cheap way to make a Listbox look like a small data table.
    stats_list.insert(tk.END, f"  {'Timestamp':<22} {'Opponent':<14} {'Result':<22} {'ms'}")
    stats_list.insert(tk.END, "  " + "─" * 62)
    for r in records[-50:]:
        stats_list.insert(tk.END,
            f"  {r['timestamp']:<22} {r['opponent']:<14} {r['result']:<22} {r['reaction_ms']}")

# Match matplotlib's chrome to the rest of the app: light text on dark.
plt.style.use("dark_background")

def plot_reaction_times():
    """Line chart of one player's reaction times over their entire
    history. Adds a 5-round rolling average so improvement trends
    pop out from per-round noise."""
    name = stats_player_entry.get().strip()
    if not name:
        msg.showinfo("Stats", "Enter a player name."); return
    records = load_records(name)
    # Only WIN rows have a numeric reaction_ms (LOSS rows are blank,
    # since the loser by definition didn't react in time).
    wins = [r for r in records if r["result"] == "WIN" and r["reaction_ms"]]
    if not wins:
        msg.showinfo("Stats", f"No reaction times for '{name}' yet."); return
    times_ms  = [int(r["reaction_ms"]) for r in wins]
    timestamps = [datetime.datetime.strptime(r["timestamp"], "%Y-%m-%d %H:%M:%S") for r in wins]
    fig, ax = plt.subplots(figsize=(9, 5), facecolor=BG_DARK)
    ax.set_facecolor(BG_CARD)
    ax.plot(timestamps, times_ms, marker="o", lw=2, color=ACCENT_BLUE, label=name, ms=6)
    # Rolling average only kicks in once we have at least 5 points,
    # otherwise the first values would just mirror the raw line.
    if len(times_ms) >= 5:
        rolling = [sum(times_ms[max(0,i-4):i+1])/len(times_ms[max(0,i-4):i+1]) for i in range(len(times_ms))]
        ax.plot(timestamps, rolling, lw=2, ls="--", color=ACCENT_PINK, label="5-round avg")
    # Two-line date format: "DD MMM\nHH:MM". Stacking saves horizontal space.
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %b\n%H:%M"))
    fig.autofmt_xdate()
    ax.set_xlabel("Session", color=TEXT_GRAY)
    ax.set_ylabel("Reaction time (ms)", color=TEXT_GRAY)
    ax.set_title(f"{name} — Reaction Times", color=TEXT_WHITE, fontsize=13)
    ax.tick_params(colors=TEXT_GRAY)
    ax.legend(facecolor=BG_CARD, edgecolor=BORDER, labelcolor=TEXT_WHITE)
    ax.grid(axis="y", alpha=0.15, color=BORDER)
    plt.tight_layout(); plt.show()

def plot_win_rates():
    """Bar chart of one player's win % against each opponent they've
    ever played. Sample size (n=) is shown above each bar — a 100%
    win rate over n=1 game is not the same as 100% over n=20."""
    name = stats_player_entry.get().strip()
    if not name:
        msg.showinfo("Stats", "Enter a player name."); return
    records = load_records(name)
    if not records:
        msg.showinfo("Stats", f"No records for '{name}'."); return
    # Count wins / losses per opponent. defaultdict(int) saves us from
    # writing "if opp not in d: d[opp] = 0" guards everywhere.
    wins_vs = defaultdict(int); losses_vs = defaultdict(int)
    for r in records:
        opp = r["opponent"]
        # Use startswith() so "WIN" and "WIN (false start)" both count.
        if r["result"].startswith("WIN"):   wins_vs[opp]   += 1
        elif r["result"].startswith("LOSS"): losses_vs[opp] += 1
    opponents = sorted(set(list(wins_vs) + list(losses_vs)))
    win_rates = []; totals = []
    for opp in opponents:
        w = wins_vs[opp]; l = losses_vs[opp]; total = w + l
        totals.append(total)
        win_rates.append((w/total*100) if total else 0)
    fig, ax = plt.subplots(figsize=(8, 5), facecolor=BG_DARK)
    ax.set_facecolor(BG_CARD)
    bars = ax.bar(opponents, win_rates, color=ACCENT_GREEN, edgecolor=BG_DARK, width=0.5)
    # Annotate each bar with both the % and the sample size.
    for bar, total, wr in zip(bars, totals, win_rates):
        ax.text(bar.get_x()+bar.get_width()/2, bar.get_height()+1,
                f"{wr:.0f}%\n(n={total})", ha="center", va="bottom", fontsize=9, color=TEXT_WHITE)
    # Reference line at 50% — visually separates "winning" from "losing" head-to-heads.
    ax.axhline(50, color=TEXT_GRAY, ls="--", lw=1)
    ax.set_ylim(0, 110)  # 110 leaves room for the text labels above 100%
    ax.set_xlabel("Opponent", color=TEXT_GRAY)
    ax.set_ylabel("Win rate (%)", color=TEXT_GRAY)
    ax.set_title(f"{name} — Win Rates", color=TEXT_WHITE, fontsize=13)
    ax.tick_params(colors=TEXT_GRAY)
    ax.grid(axis="y", alpha=0.15, color=BORDER)
    plt.tight_layout(); plt.show()

def plot_head_to_head():
    """Scatter of two players' reaction times in their rounds against
    each other. Reads both players' CSVs and filters to only the rows
    where the opponent matches — this is how we cross-reference two
    independent files into a single chart."""
    name = stats_player_entry.get().strip()
    opp  = stats_opponent_entry.get().strip()
    if not name or not opp:
        msg.showinfo("Stats", "Enter both player and opponent."); return
    # Pull each player's WIN rows from THEIR file, where opponent matches.
    # We can't just read one player's file because a player's LOSS row
    # has a blank reaction_ms — the winning time lives in the other CSV.
    rec_a = [r for r in load_records(name) if r["opponent"]==opp and r["result"]=="WIN" and r["reaction_ms"]]
    rec_b = [r for r in load_records(opp)  if r["opponent"]==name and r["result"]=="WIN" and r["reaction_ms"]]
    if not rec_a and not rec_b:
        msg.showinfo("Stats", "No head-to-head data yet."); return
    fig, ax = plt.subplots(figsize=(9, 5), facecolor=BG_DARK)
    ax.set_facecolor(BG_CARD)
    def plot_p(records, label, color):
        """Inner helper so we don't repeat the scatter+line code twice."""
        if not records: return
        times = [int(r["reaction_ms"]) for r in records]
        ts = [datetime.datetime.strptime(r["timestamp"], "%Y-%m-%d %H:%M:%S") for r in records]
        # Scatter on top (zorder=3), faint line underneath for trend.
        ax.scatter(ts, times, label=f"{label} wins", color=color, zorder=3, s=60)
        ax.plot(ts, times, color=color, lw=1, alpha=0.5)
    plot_p(rec_a, name, ACCENT_BLUE)
    plot_p(rec_b, opp,  ACCENT_PINK)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %b\n%H:%M"))
    fig.autofmt_xdate()
    ax.set_xlabel("Session", color=TEXT_GRAY)
    ax.set_ylabel("Reaction time (ms)", color=TEXT_GRAY)
    ax.set_title(f"{name} vs {opp}", color=TEXT_WHITE, fontsize=13)
    ax.tick_params(colors=TEXT_GRAY)
    ax.legend(facecolor=BG_CARD, edgecolor=BORDER, labelcolor=TEXT_WHITE)
    ax.grid(axis="y", alpha=0.15, color=BORDER)
    plt.tight_layout(); plt.show()

# ============================================================
#  RUN
# ============================================================

# Show the saved leaderboard before the user has interacted with anything.
display_leaderboard()
# Kick off the serial-polling loop. From here on it re-schedules itself
# via root.after() every 50 ms.
read_serial()
# Block on the Tk event loop. The script doesn't return from here until
# the window is closed.
root.mainloop()
