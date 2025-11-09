import tkinter as tk
from tkinter import ttk, messagebox
import serial, serial.tools.list_ports
import threading, time

BAUDRATE = 115200


def list_serial_ports():
    return list(serial.tools.list_ports.comports())


class RampsGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("RAMPS Simple Controller")
        self.geometry("780x540")
        self.resizable(False, False)

        # Série
        self.ser = None
        self.rx_thread = None
        self.rx_running = False

        # Vars UI
        self.var_port = tk.StringVar()
        self.var_steps = tk.StringVar(value="200")
        self.var_speed = tk.StringVar(value="800")  # µs entre pas
        self.var_hotend = tk.StringVar(value="--.-")
        self.var_bed = tk.StringVar(value="--.-")
        self.var_status = tk.StringVar(value="Déconnecté")
        self.var_auto = tk.BooleanVar(value=True)

        self._build_ui()
        self._refresh_ports()
        self.after(1000, self._temp_poll_loop)

    # ----- UI -----
    def _build_ui(self):
        top = ttk.Frame(self, padding=10)
        top.pack(fill="x")
        ttk.Label(top, text="Port :").pack(side="left")
        self.cmb_port = ttk.Combobox(
            top, textvariable=self.var_port, width=28, state="readonly"
        )
        self.cmb_port.pack(side="left", padx=6)
        ttk.Button(top, text="Rafraîchir", command=self._refresh_ports).pack(
            side="left", padx=4
        )
        self.btn_conn = ttk.Button(top, text="Connexion", command=self.connect)
        self.btn_conn.pack(side="left", padx=4)
        self.btn_disc = ttk.Button(
            top, text="Déconnexion", command=self.disconnect, state="disabled"
        )
        self.btn_disc.pack(side="left", padx=4)
        ttk.Label(top, textvariable=self.var_status).pack(side="right")

        params = ttk.LabelFrame(self, text="Paramètres Mouvement", padding=10)
        params.pack(fill="x", padx=10, pady=6)
        ttk.Label(params, text="Pas :").grid(row=0, column=0, sticky="w")
        ttk.Entry(params, textvariable=self.var_steps, width=10).grid(
            row=0, column=1, padx=6
        )
        ttk.Label(params, text="Vitesse (µs/step) :").grid(row=0, column=2, sticky="w")
        ttk.Entry(params, textvariable=self.var_speed, width=12).grid(
            row=0, column=3, padx=6
        )

        axes = ttk.LabelFrame(self, text="Moteurs", padding=10)
        axes.pack(fill="x", padx=10, pady=6)

        def axis_row(row, label, axis):
            ttk.Label(axes, text=label, width=6).grid(
                row=row, column=0, padx=4, pady=4, sticky="w"
            )
            ttk.Button(
                axes, text="⟵", width=8, command=lambda: self.move(axis, negative=True)
            ).grid(row=row, column=1, padx=3)
            ttk.Button(
                axes, text="⟶", width=8, command=lambda: self.move(axis, negative=False)
            ).grid(row=row, column=2, padx=3)

        axis_row(0, "X", "X")
        axis_row(1, "Y", "Y")
        axis_row(2, "Z", "Z")
        axis_row(3, "E", "E")

        thermo = ttk.LabelFrame(self, text="Températures & Heatbed", padding=10)
        thermo.pack(fill="x", padx=10, pady=6)

        ttk.Label(thermo, text="Hotend :").grid(row=0, column=0, sticky="w")
        ttk.Label(thermo, textvariable=self.var_hotend, width=8).grid(
            row=0, column=1, sticky="w", padx=6
        )
        ttk.Label(thermo, text="Bed :").grid(row=0, column=2, sticky="w")
        ttk.Label(thermo, textvariable=self.var_bed, width=8).grid(
            row=0, column=3, sticky="w", padx=6
        )

        ttk.Button(thermo, text="Lire T?", command=self.read_temps_once).grid(
            row=0, column=4, padx=10
        )
        ttk.Checkbutton(thermo, text="Lecture auto", variable=self.var_auto).grid(
            row=0, column=5
        )

        ttk.Button(thermo, text="Bed ON", command=lambda: self.send("HB ON")).grid(
            row=1, column=0, pady=8
        )
        ttk.Button(thermo, text="Bed OFF", command=lambda: self.send("HB OFF")).grid(
            row=1, column=1, pady=8
        )

        logf = ttk.LabelFrame(self, text="Journal", padding=10)
        logf.pack(fill="both", expand=True, padx=10, pady=6)
        self.txt = tk.Text(logf, height=12, wrap="word")
        self.txt.pack(fill="both", expand=True)

    # ----- Ports -----
    def _refresh_ports(self):
        ports = list_serial_ports()
        values = [f"{p.device} — {p.description}" for p in ports]
        self.cmb_port["values"] = values
        if values:
            self.var_port.set(values[0])

    # ----- Connexion série -----
    def connect(self):
        if self.ser:
            return
        if not self.var_port.get():
            messagebox.showwarning("Port", "Aucun port sélectionné.")
            return
        port = self.var_port.get().split(" — ")[0]
        try:
            self.ser = serial.Serial(port, BAUDRATE, timeout=0.2)
            time.sleep(2)  # boot Arduino
            self.var_status.set(f"Connecté : {port}")
            self.btn_conn.config(state="disabled")
            self.btn_disc.config(state="normal")
            self._start_reader()
            self._log(f"Connecté à {port}")
        except Exception as e:
            self.ser = None
            messagebox.showerror("Connexion", f"{e}")

    def disconnect(self):
        self._stop_reader()
        if self.ser:
            try:
                self.ser.close()
            except:
                pass
        self.ser = None
        self.var_status.set("Déconnecté")
        self.btn_conn.config(state="normal")
        self.btn_disc.config(state="disabled")
        self._log("Déconnecté.")

    # ----- RX thread -----
    def _start_reader(self):
        self.rx_running = True
        self.rx_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self.rx_thread.start()

    def _stop_reader(self):
        self.rx_running = False
        if self.rx_thread and self.rx_thread.is_alive():
            self.rx_thread.join(timeout=0.5)
        self.rx_thread = None

    def _reader_loop(self):
        buf = b""
        while self.rx_running and self.ser:
            try:
                if self.ser.in_waiting:
                    buf += self.ser.read(self.ser.in_waiting)
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        text = line.decode(errors="ignore").strip()
                        if text:
                            self.after(0, self._handle_rx_line, text)
                else:
                    time.sleep(0.05)
            except Exception as e:
                self.after(0, self._log, f"[RX ERR] {e}")
                time.sleep(0.2)

    def _handle_rx_line(self, line: str):
        # Ex: "Hotend: 23.4 C | Bed: 24.1 C"
        if "Hotend:" in line and "Bed:" in line:
            try:
                parts = line.replace("C", "").split("|")
                h = parts[0].split(":")[1].strip()
                b = parts[1].split(":")[1].strip()
                self.var_hotend.set(h)
                self.var_bed.set(b)
            except:
                pass
        self._log("<< " + line)

    # ----- Commandes -----
    def _log(self, msg):
        self.txt.insert("end", msg + "\n")
        self.txt.see("end")

    def send(self, cmd: str):
        if not self.ser:
            self._log("[Non connecté] " + cmd)
            return
        try:
            self.ser.write((cmd.strip() + "\n").encode())
            self._log(">> " + cmd)
        except Exception as e:
            self._log(f"[TX ERR] {e}")

    def move(self, axis: str, negative=False):
        try:
            steps = int(self.var_steps.get())
            speed = int(self.var_speed.get())
        except ValueError:
            messagebox.showwarning(
                "Paramètres", "Pas et vitesse doivent être numériques."
            )
            return
        if steps <= 0 or speed <= 0:
            messagebox.showwarning("Paramètres", "Pas et vitesse doivent être > 0.")
            return
        s = -steps if negative else steps
        self.send(f"M{axis} {s} {speed}")

    def read_temps_once(self):
        self.send("T?")

    def _temp_poll_loop(self):
        if self.var_auto.get() and self.ser:
            self.send("T?")
        self.after(1000, self._temp_poll_loop)


if __name__ == "__main__":
    app = RampsGUI()
    app.mainloop()
