"""
Lab 7 — RFID Tag Database Viewer (PC-side GUI)

Listens for RFID scans coming from the Arduino over serial and stores
them in a local SQLite database. Provides a PyQt6 GUI for inspecting
the table, searching, and clearing it.

Architecture:
    SerialReaderThread (QThread)
        Reads lines from the serial port in the background, emits
        Qt signals when a tag is scanned. Runs off the GUI thread so
        the UI never blocks on I/O.

    DatabaseManager
        Thin SQLite wrapper. Each call opens its own connection because
        sqlite3 connections aren't safe to share across threads, and the
        operations here are infrequent enough that connection setup is
        negligible.

    RFIDDatabaseGUI (QMainWindow)
        The main window. Owns the DB manager, owns the serial thread,
        wires every signal to a slot, and refreshes the table after
        each scan.

Wire protocol (Arduino -> PC):
    DATA_PACKET:<uid_hex>      One line per successful RFID read in the
                               unlocked state. The UID is uppercase hex,
                               no separators.

    Anything else: treated as a free-form log line and shown in the
    Serial Log panel.
"""

import sys
import sqlite3
from datetime import datetime

import serial
import serial.tools.list_ports

from PyQt6.QtCore import QThread, pyqtSignal, Qt
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
    QHeaderView,
    QGroupBox,
)


# Constants kept at module level so they're easy to find and change.
DATABASE_FILE = "rfid_database.db"
BAUD_RATE = 9600
DATA_PREFIX = "DATA_PACKET:"   # Lines starting with this are tag scans;
                               # everything else is a free-form log line.


# ========================= DATABASE MANAGER =========================

class DatabaseManager:
    """
    Thin wrapper over the SQLite tags table.

    Connection-per-call is intentional: SQLite connections aren't safe
    to share across threads, and the operations here are tiny and
    infrequent (one INSERT/UPDATE per RFID tap), so the overhead of
    opening a connection each time doesn't matter.
    """

    def __init__(self, db_file=DATABASE_FILE):
        self.db_file = db_file
        self.initialize_database()

    def connect(self):
        """Open a fresh SQLite connection. Caller is responsible for closing."""
        return sqlite3.connect(self.db_file)

    def initialize_database(self):
        """Create the tags table if it doesn't exist yet. Safe to call
        on every startup — IF NOT EXISTS makes this idempotent."""
        conn = self.connect()
        cursor = conn.cursor()

        # Schema notes:
        #   id          — surrogate primary key, used to derive tag_id
        #   tag_id      — friendly label like "TAG-001". UNIQUE so the
        #                 friendly name can't collide.
        #   rfid_uid    — the raw hex UID from the RC522. UNIQUE because
        #                 each physical tag has exactly one row.
        #   scan_count  — running total. Starts at 1 on insert, +1 per re-scan.
        #   first_seen  — timestamp of the very first scan.
        #   last_seen   — timestamp of the most recent scan.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS tags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tag_id TEXT UNIQUE,
                rfid_uid TEXT UNIQUE NOT NULL,
                scan_count INTEGER NOT NULL DEFAULT 1,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL
            )
        """)

        conn.commit()
        conn.close()

    def add_or_update_tag(self, rfid_uid):
        """
        Upsert one tag scan.

        If the UID already exists: bump scan_count, update last_seen.
        If it's new: insert a row, then set tag_id to "TAG-NNN" where
        NNN is the surrogate id zero-padded to 3 digits.

        Returns a dict describing what happened, so the caller can
        show different messages for new vs existing tags.
        """
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        conn = self.connect()
        cursor = conn.cursor()

        # Look up by UID — the natural key. Use UID rather than tag_id
        # because tag_id is derived from the surrogate id, which we
        # don't have at scan time.
        cursor.execute(
            "SELECT id, tag_id, scan_count FROM tags WHERE rfid_uid = ?",
            (rfid_uid,)
        )

        existing_tag = cursor.fetchone()

        if existing_tag:
            # ── Tag we've seen before: just update the counters. ──
            db_id, tag_id, scan_count = existing_tag
            new_count = scan_count + 1

            cursor.execute("""
                UPDATE tags
                SET scan_count = ?, last_seen = ?
                WHERE rfid_uid = ?
            """, (new_count, now, rfid_uid))

            conn.commit()
            conn.close()

            return {
                "status": "existing",
                "tag_id": tag_id,
                "rfid_uid": rfid_uid,
                "scan_count": new_count
            }

        else:
            # ── Brand new tag: two-step insert. ──
            # Step 1: insert with placeholder tag_id "TEMP" so we
            # can find out what AUTOINCREMENT assigned to the row.
            cursor.execute("""
                INSERT INTO tags (tag_id, rfid_uid, scan_count, first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?)
            """, ("TEMP", rfid_uid, 1, now, now))

            # Step 2: format the friendly id from the surrogate and
            # write it back. Zero-padded to 3 so they sort correctly
            # alphabetically (TAG-001 .. TAG-099 .. TAG-100).
            new_db_id = cursor.lastrowid
            tag_id = f"TAG-{new_db_id:03d}"

            cursor.execute("""
                UPDATE tags
                SET tag_id = ?
                WHERE id = ?
            """, (tag_id, new_db_id))

            conn.commit()
            conn.close()

            return {
                "status": "new",
                "tag_id": tag_id,
                "rfid_uid": rfid_uid,
                "scan_count": 1
            }

    def get_all_tags(self, search_text=""):
        """
        Return every row, optionally filtered by a substring of
        either tag_id or rfid_uid.

        Used by the GUI to repopulate the table on every refresh.
        Ordered by id (insertion order) so new tags appear at the bottom.
        """
        conn = self.connect()
        cursor = conn.cursor()

        if search_text:
            # LIKE with % wildcards on both sides => substring match.
            # Parameterised to avoid any SQL injection in the search box.
            search_pattern = f"%{search_text}%"
            cursor.execute("""
                SELECT tag_id, rfid_uid, scan_count, first_seen, last_seen
                FROM tags
                WHERE tag_id LIKE ? OR rfid_uid LIKE ?
                ORDER BY id ASC
            """, (search_pattern, search_pattern))
        else:
            cursor.execute("""
                SELECT tag_id, rfid_uid, scan_count, first_seen, last_seen
                FROM tags
                ORDER BY id ASC
            """)

        rows = cursor.fetchall()
        conn.close()

        return rows

    def clear_database(self):
        """
        Wipe all rows. Also resets the AUTOINCREMENT counter so the next
        tag gets TAG-001 again — otherwise SQLite remembers the highest
        id ever used and would issue TAG-005 or whatever.
        """
        conn = self.connect()
        cursor = conn.cursor()

        cursor.execute("DELETE FROM tags")
        cursor.execute("DELETE FROM sqlite_sequence WHERE name='tags'")

        conn.commit()
        conn.close()


# ========================= SERIAL READER THREAD =========================

class SerialReaderThread(QThread):
    """
    Background thread that reads lines from the Arduino.

    Reading is blocking (with a 1s timeout), so it MUST run off the GUI
    thread — otherwise the window would freeze every time we wait for a
    byte. Communication back to the GUI is via Qt signals, which Qt
    routes safely across threads.
    """

    # Signals. Qt requires these to be class-level attributes.
    tag_scanned       = pyqtSignal(str)   # emits the UID hex string
    serial_message    = pyqtSignal(str)   # emits free-form log lines
    connection_error  = pyqtSignal(str)   # emits a human-readable error

    def __init__(self, port_name, baud_rate=BAUD_RATE):
        super().__init__()
        self.port_name = port_name
        self.baud_rate = baud_rate
        self.running = False           # flipped to False by stop() to break the loop
        self.serial_connection = None

    def run(self):
        """QThread entry point. Runs on the worker thread once start() is called."""
        try:
            # timeout=1 means readline() returns after at most 1 s of
            # no input — this gives self.running a chance to be checked
            # at least once per second so stop() doesn't have to wait forever.
            self.serial_connection = serial.Serial(
                self.port_name,
                self.baud_rate,
                timeout=1
            )

            self.running = True
            self.serial_message.emit(f"Connected to {self.port_name} at {self.baud_rate} baud.")

            while self.running:
                try:
                    # errors="ignore" so a single bad byte (e.g. from
                    # cable noise) doesn't kill the whole reader.
                    line = self.serial_connection.readline().decode(errors="ignore").strip()

                    if not line:
                        # readline() returned empty — either timeout or
                        # just a blank line. Loop back and keep going.
                        continue

                    self.serial_message.emit(f"Arduino: {line}")

                    # Lines beginning with DATA_PACKET: carry a UID.
                    # Anything else is treated as a log line only.
                    if line.startswith(DATA_PREFIX):
                        uid = line.replace(DATA_PREFIX, "").strip().upper()

                        if uid:
                            self.tag_scanned.emit(uid)

                except Exception as e:
                    # Mid-read failure (cable yanked, port closed, etc).
                    # Tell the GUI and break out of the loop — the
                    # finally block will close the port cleanly.
                    self.connection_error.emit(f"Serial read error: {e}")
                    break

        except Exception as e:
            # Open-time failure (port doesn't exist, permission denied).
            self.connection_error.emit(f"Could not open serial port: {e}")

        finally:
            # Always close the port, whether we exited cleanly or via error.
            if self.serial_connection and self.serial_connection.is_open:
                self.serial_connection.close()

            self.serial_message.emit("Serial connection closed.")

    def stop(self):
        """Ask the thread to exit cleanly and block until it does.

        Setting running=False trips the while-loop guard. The 1-second
        readline timeout means we'll exit at most one second later.
        self.wait() then blocks the caller until the QThread fully
        terminates — important so the GUI doesn't proceed to reconnect
        while the old thread still has the port open.
        """
        self.running = False
        self.wait()


# ========================= MAIN WINDOW =========================

class RFIDDatabaseGUI(QMainWindow):
    """
    Main application window.

    Layout (top to bottom):
        [Serial Connection]  port picker, connect/disconnect, status
        [Database Navigation] search box, refresh, clear
        [Tag Table]          5-column QTableWidget of every row
        [Latest Scan]        one-line label, updated on every tag
        [Serial Log]         scrolling log of everything we've heard
    """

    def __init__(self):
        super().__init__()

        # Owned components.
        self.db = DatabaseManager()
        self.serial_thread = None   # only non-None while connected

        self.setWindowTitle("RFID Tag Database Viewer")
        self.setMinimumSize(950, 650)

        self.setup_ui()
        self.load_ports()        # populate the dropdown
        self.refresh_table()     # show existing rows on startup

    def setup_ui(self):
        """Build every widget and lay them out vertically."""
        main_widget = QWidget()
        main_layout = QVBoxLayout()

        # ================= Serial Controls =================
        # Top row: port dropdown, refresh/connect/disconnect buttons,
        # and a status label. Grouped so it's visually one block.
        serial_group = QGroupBox("Serial Connection")
        serial_layout = QHBoxLayout()

        self.port_combo = QComboBox()

        self.refresh_ports_button = QPushButton("Refresh Ports")
        self.refresh_ports_button.clicked.connect(self.load_ports)

        self.connect_button = QPushButton("Connect")
        self.connect_button.clicked.connect(self.connect_serial)

        self.disconnect_button = QPushButton("Disconnect")
        self.disconnect_button.clicked.connect(self.disconnect_serial)
        # Disabled until we actually connect — protects against
        # disconnect-when-already-disconnected misclicks.
        self.disconnect_button.setEnabled(False)

        self.status_label = QLabel("Status: Disconnected")

        serial_layout.addWidget(QLabel("Port:"))
        serial_layout.addWidget(self.port_combo)
        serial_layout.addWidget(self.refresh_ports_button)
        serial_layout.addWidget(self.connect_button)
        serial_layout.addWidget(self.disconnect_button)
        serial_layout.addWidget(self.status_label)

        serial_group.setLayout(serial_layout)

        # ================= Search Controls =================
        # Live search: refresh_table() is called on every keystroke,
        # which is fine because the table is small and the SQLite query
        # is sub-millisecond.
        search_group = QGroupBox("Database Navigation")
        search_layout = QHBoxLayout()

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search by Tag ID or RFID UID...")
        self.search_input.textChanged.connect(self.refresh_table)

        self.refresh_table_button = QPushButton("Refresh Table")
        self.refresh_table_button.clicked.connect(self.refresh_table)

        self.clear_button = QPushButton("Clear Database")
        self.clear_button.clicked.connect(self.clear_database)

        search_layout.addWidget(QLabel("Search:"))
        search_layout.addWidget(self.search_input)
        search_layout.addWidget(self.refresh_table_button)
        search_layout.addWidget(self.clear_button)

        search_group.setLayout(search_layout)

        # ================= Table =================
        # Five columns matching the SELECT in get_all_tags(). The header
        # is set to Stretch so the columns fill the available width
        # evenly — without that, they default to a fixed width and
        # leave a big empty band on the right.
        self.table = QTableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels([
            "Tag ID",
            "RFID UID",
            "Scan Count",
            "First Seen",
            "Last Seen"
        ])

        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        # Read-only: this is a viewer, edits would desync from the DB.
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        # Click anywhere on a row to select the whole row, not just one cell.
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)

        # ================= Latest Scan =================
        # A one-liner that shows the most recent scan's outcome.
        # Acts as a "did the tap register?" confirmation for the user
        # without forcing them to scan the table for the new row.
        latest_group = QGroupBox("Latest Scan")
        latest_layout = QHBoxLayout()

        self.latest_scan_label = QLabel("No tag scanned yet.")
        latest_layout.addWidget(self.latest_scan_label)

        latest_group.setLayout(latest_layout)

        # ================= Log Output =================
        # Append-only text view of everything we've received over serial.
        # Read-only so users can't accidentally clobber it.
        log_group = QGroupBox("Serial Log")
        log_layout = QVBoxLayout()

        self.log_output = QTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.setMinimumHeight(140)

        log_layout.addWidget(self.log_output)
        log_group.setLayout(log_layout)

        # ================= Compose the main layout =================
        main_layout.addWidget(serial_group)
        main_layout.addWidget(search_group)
        main_layout.addWidget(self.table)
        main_layout.addWidget(latest_group)
        main_layout.addWidget(log_group)

        main_widget.setLayout(main_layout)
        self.setCentralWidget(main_widget)

    def load_ports(self):
        """Repopulate the port dropdown from pyserial's enumeration.

        Each item stores the bare device path (e.g. /dev/cu.usbmodem...)
        in userData, while the display text also shows the description
        — handy when there are multiple USB-serial devices plugged in.
        """
        self.port_combo.clear()

        ports = serial.tools.list_ports.comports()

        if not ports:
            # No USB-serial devices visible: show a placeholder and
            # disable Connect so the user can't try anyway.
            self.port_combo.addItem("No ports found")
            self.connect_button.setEnabled(False)
            return

        for port in ports:
            display_text = f"{port.device} - {port.description}"
            self.port_combo.addItem(display_text, port.device)

        self.connect_button.setEnabled(True)

    def connect_serial(self):
        """Spin up a SerialReaderThread on the chosen port and wire its signals."""
        port_name = self.port_combo.currentData()

        if not port_name:
            QMessageBox.warning(self, "No Port Selected", "Please select a valid serial port.")
            return

        # Create the worker, connect its three signals to our slots,
        # and start it. From here on, all serial I/O happens in the
        # worker thread; we only see signal callbacks on the GUI thread.
        self.serial_thread = SerialReaderThread(port_name)
        self.serial_thread.tag_scanned.connect(self.handle_tag_scanned)
        self.serial_thread.serial_message.connect(self.add_log)
        self.serial_thread.connection_error.connect(self.handle_serial_error)

        self.serial_thread.start()

        # Lock the connection controls while connected so the user
        # can't try to connect twice or change the port mid-session.
        self.connect_button.setEnabled(False)
        self.disconnect_button.setEnabled(True)
        self.port_combo.setEnabled(False)
        self.status_label.setText(f"Status: Connected to {port_name}")

    def disconnect_serial(self):
        """Stop the worker thread and re-enable the connection controls."""
        if self.serial_thread:
            # stop() blocks until the thread actually exits, so when
            # we move on it's guaranteed the port has been closed.
            self.serial_thread.stop()
            self.serial_thread = None

        self.connect_button.setEnabled(True)
        self.disconnect_button.setEnabled(False)
        self.port_combo.setEnabled(True)
        self.status_label.setText("Status: Disconnected")

    def handle_tag_scanned(self, uid):
        """Slot for SerialReaderThread.tag_scanned.

        This runs on the GUI thread (Qt marshals the signal across
        threads), so it's safe to touch widgets here.
        """
        # Upsert. Returns a dict telling us whether this was new or
        # an existing tag — used to phrase the latest-scan label.
        result = self.db.add_or_update_tag(uid)

        if result["status"] == "new":
            message = (
                f"New tag saved: {result['tag_id']} | "
                f"UID: {result['rfid_uid']} | "
                f"Count: {result['scan_count']}"
            )
        else:
            message = (
                f"Existing tag updated: {result['tag_id']} | "
                f"UID: {result['rfid_uid']} | "
                f"Count: {result['scan_count']}"
            )

        self.latest_scan_label.setText(message)
        self.add_log(message)
        # Refresh from the DB rather than just inserting a row into the
        # widget — keeps the visible state guaranteed-consistent with
        # what's actually persisted.
        self.refresh_table()

    def refresh_table(self):
        """Re-query the DB and redraw the table from scratch.

        Cheap enough at lab-scale (tens to maybe hundreds of rows)
        that we don't bother diffing. Triggered by:
          - explicit Refresh button
          - every keystroke in the search box
          - every tag scan
        """
        search_text = self.search_input.text().strip()
        rows = self.db.get_all_tags(search_text)

        self.table.setRowCount(len(rows))

        for row_index, row_data in enumerate(rows):
            for column_index, value in enumerate(row_data):
                item = QTableWidgetItem(str(value))
                # Centre all cells. The columns are short fixed-width
                # things (IDs, counts, timestamps), so centred reads
                # better than left-aligned with ragged whitespace.
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.table.setItem(row_index, column_index, item)

    def clear_database(self):
        """Wipe the DB after a yes/no confirmation. Irreversible."""
        confirm = QMessageBox.question(
            self,
            "Clear Database",
            "Are you sure you want to delete all saved RFID records?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )

        if confirm == QMessageBox.StandardButton.Yes:
            self.db.clear_database()
            self.refresh_table()
            self.latest_scan_label.setText("Database cleared.")
            self.add_log("Database cleared.")

    def add_log(self, message):
        """Append one line to the log view with an HH:MM:SS prefix.

        QTextEdit.append() handles scrolling automatically — the view
        sticks to the bottom as long as the user hasn't scrolled up
        themselves.
        """
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_output.append(f"[{timestamp}] {message}")

    def handle_serial_error(self, error_message):
        """Slot for SerialReaderThread.connection_error.

        Logs the error, pops a modal so the user can't miss it, and
        tears the connection down so the UI returns to a clean state.
        """
        self.add_log(error_message)
        QMessageBox.critical(self, "Serial Error", error_message)
        self.disconnect_serial()

    def closeEvent(self, event):
        """Called by Qt when the user clicks the window's close button.

        Make sure the serial thread is stopped before we let the
        window die — otherwise the thread keeps running with a
        reference to a destroyed window and either crashes on the
        next signal or holds the port open until the process exits.
        """
        self.disconnect_serial()
        event.accept()


# ========================= APPLICATION ENTRY POINT =========================

def main():
    """Standard PyQt6 startup: create the app, build the window,
    show it, and hand control to the Qt event loop until it exits."""
    app = QApplication(sys.argv)

    window = RFIDDatabaseGUI()
    window.show()

    # app.exec() blocks until the last window closes. Its return code
    # propagates out to the shell as our exit code.
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
