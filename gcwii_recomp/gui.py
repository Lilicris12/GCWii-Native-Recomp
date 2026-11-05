"""Desktop application (Tkinter - ships with Python on Windows and macOS;
on Linux install your distribution's ``python3-tk`` package)."""
from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import traceback

from . import __version__

DISC_TYPES = [("GameCube / Wii images", "*.iso *.gcm *.wii *.dol *.elf"), ("All files", "*.*")]
SYM_TYPES = [("Symbol maps", "*.map *.txt *.csv"), ("All files", "*.*")]


def run_gui(initial_input: str | None = None) -> int:
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk
    except ImportError:
        print("Tkinter is not available. Install it (e.g. `sudo apt install python3-tk`) "
              "or use the command line: python -m gcwii_recomp --help", file=sys.stderr)
        return 1

    from .builder import available_targets, build
    from .disc import open_source
    from .project import ProjectConfig, recompile

    root = tk.Tk()
    root.title(f"GC/Wii Native Recomp {__version__}")
    root.minsize(820, 640)
    if sys.platform.startswith("linux"):
        try:
            ttk.Style().theme_use("clam")
        except tk.TclError:
            pass

    v_input = tk.StringVar()
    v_output = tk.StringVar()
    v_symbols = tk.StringVar()
    v_key = tk.StringVar(value=os.environ.get("GCWII_COMMON_KEY", ""))
    v_copy = tk.BooleanVar(value=False)
    v_extract = tk.BooleanVar(value=False)
    v_fastlibc = tk.BooleanVar(value=False)
    v_comments = tk.BooleanVar(value=True)
    v_status = tk.StringVar(value="Select a disc image you own to begin.")
    status = available_targets()
    v_targets = {t: tk.BooleanVar(value=(t == "native")) for t in ("native", "windows", "linux", "android")}

    msgs: "queue.Queue[tuple[str, object]]" = queue.Queue()
    busy = {"on": False}

    # ---------------- layout ----------------
    frm = ttk.Frame(root, padding=12)
    frm.pack(fill="both", expand=True)
    frm.columnconfigure(1, weight=1)

    def row_entry(r, label, var, browse, show=None):
        ttk.Label(frm, text=label).grid(row=r, column=0, sticky="w", pady=3)
        e = ttk.Entry(frm, textvariable=var, show=show)
        e.grid(row=r, column=1, sticky="ew", padx=6)
        if browse:
            ttk.Button(frm, text="Browse...", command=browse).grid(row=r, column=2, sticky="ew")
        return e

    def pick_input():
        p = filedialog.askopenfilename(title="Choose a GameCube/Wii disc image or DOL", filetypes=DISC_TYPES)
        if p:
            v_input.set(p)
            if not v_output.get():
                base = os.path.splitext(os.path.basename(p))[0]
                v_output.set(os.path.join(os.path.dirname(p), base + "-native"))
            inspect()

    def pick_dir_input():
        p = filedialog.askdirectory(title="Choose an extracted disc folder (contains sys/main.dol)")
        if p:
            v_input.set(p)
            if not v_output.get():
                v_output.set(p.rstrip("/\\") + "-native")
            inspect()

    def pick_output():
        p = filedialog.askdirectory(title="Output folder")
        if p:
            v_output.set(p)

    def pick_symbols():
        p = filedialog.askopenfilename(title="Symbol map (optional)", filetypes=SYM_TYPES)
        if p:
            v_symbols.set(p)

    def pick_key():
        p = filedialog.askopenfilename(title="Wii common key file (keys.bin or 16-byte key)")
        if p:
            v_key.set(p)

    ttk.Label(frm, text="Turn GameCube / Wii discs you own into native programs",
              font=("TkDefaultFont", 12, "bold")).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))
    row_entry(1, "Disc image / DOL", v_input, pick_input)
    ttk.Button(frm, text="Extracted folder...", command=pick_dir_input).grid(row=2, column=2, sticky="ew")
    ttk.Label(frm, text="(ISO, GCM, Wii ISO, DOL, or a Dolphin-extracted folder; "
                        "convert RVZ/WBFS to ISO first)", foreground="#666").grid(row=2, column=1, sticky="w", padx=6)
    row_entry(3, "Output folder", v_output, pick_output)
    row_entry(4, "Symbol map (optional)", v_symbols, pick_symbols)
    row_entry(5, "Wii common key", v_key, pick_key, show="•")

    info = tk.Text(frm, height=7, wrap="none", relief="groove")
    info.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(8, 4))
    info.insert("end", "Disc information appears here.")
    info.configure(state="disabled")

    opts = ttk.LabelFrame(frm, text="Options", padding=6)
    opts.grid(row=7, column=0, columnspan=3, sticky="ew", pady=4)
    ttk.Checkbutton(opts, text="Copy / decrypt disc image into the output (needed for Wii data)",
                    variable=v_copy).grid(row=0, column=0, sticky="w")
    ttk.Checkbutton(opts, text="Extract file system (assets/files)", variable=v_extract).grid(row=1, column=0, sticky="w")
    ttk.Checkbutton(opts, text="Native memcpy/memset/strlen (needs symbols)", variable=v_fastlibc).grid(row=0, column=1, sticky="w", padx=12)
    ttk.Checkbutton(opts, text="Disassembly comments in generated C", variable=v_comments).grid(row=1, column=1, sticky="w", padx=12)

    tg = ttk.LabelFrame(frm, text="Build targets", padding=6)
    tg.grid(row=8, column=0, columnspan=3, sticky="ew", pady=4)
    labels = {"native": "This computer", "windows": "Windows (x64)", "linux": "Linux (x64)", "android": "Android (arm64)"}
    for col, t in enumerate(("native", "windows", "linux", "android")):
        ttk.Checkbutton(tg, text=labels[t], variable=v_targets[t]).grid(row=0, column=col, sticky="w", padx=8)
        ttk.Label(tg, text=status.get(t, ""), foreground="#2a7" if status.get(t, "").startswith("ready") else "#b60",
                  font=("TkDefaultFont", 8)).grid(row=1, column=col, sticky="w", padx=8)

    btns = ttk.Frame(frm)
    btns.grid(row=9, column=0, columnspan=3, sticky="ew", pady=6)
    b_go = ttk.Button(btns, text="Recompile & Build")
    b_go.pack(side="left")
    b_gen = ttk.Button(btns, text="Recompile only")
    b_gen.pack(side="left", padx=6)
    b_open = ttk.Button(btns, text="Open output folder", command=lambda: open_folder(v_output.get()))
    b_open.pack(side="left")
    prog = ttk.Progressbar(btns, mode="determinate", maximum=100)
    prog.pack(side="right", fill="x", expand=True, padx=(12, 0))

    logbox = tk.Text(frm, height=10, wrap="word", bg="#111", fg="#ddd", insertbackground="#ddd")
    logbox.grid(row=10, column=0, columnspan=3, sticky="nsew")
    sb = ttk.Scrollbar(frm, command=logbox.yview)
    sb.grid(row=10, column=3, sticky="ns")
    logbox.configure(yscrollcommand=sb.set)
    frm.rowconfigure(10, weight=1)
    ttk.Label(frm, textvariable=v_status).grid(row=11, column=0, columnspan=3, sticky="w", pady=(6, 0))

    # ---------------- behaviour ----------------
    def set_info(text: str) -> None:
        info.configure(state="normal")
        info.delete("1.0", "end")
        info.insert("end", text)
        info.configure(state="disabled")

    def inspect() -> None:
        path = v_input.get().strip()
        if not path:
            return
        try:
            src = open_source(path, v_key.get().strip() or None)
            set_info(src.describe())
            v_status.set(f"Ready: {src.title or src.game_id or os.path.basename(path)}")
            if src.platform == "wii":
                v_copy.set(True)
        except Exception as exc:  # noqa: BLE001 - surface to the user
            set_info(f"Cannot read this input:\n{exc}")
            v_status.set("Input not usable yet.")

    def open_folder(path: str) -> None:
        if not path or not os.path.isdir(path):
            messagebox.showinfo("Output", "The output folder does not exist yet.")
            return
        if sys.platform.startswith("win"):
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])

    def log(line: str) -> None:
        msgs.put(("log", line))

    def worker(do_build: bool) -> None:
        try:
            cfg = ProjectConfig(input=v_input.get().strip(), output=v_output.get().strip(),
                                symbols=v_symbols.get().strip(), common_key=v_key.get().strip(),
                                copy_disc=v_copy.get(), extract_files=v_extract.get(),
                                fast_libc=v_fastlibc.get(), comments=v_comments.get())
            res = recompile(cfg, log=log, progress=lambda f: msgs.put(("progress", f * (60 if do_build else 100))))
            built = []
            if do_build:
                targets = [t for t, v in v_targets.items() if v.get()]
                for n, t in enumerate(targets):
                    msgs.put(("status", f"Building for {labels[t]}..."))
                    try:
                        built.append(build(res.output, t, log=log))
                    except Exception as exc:  # noqa: BLE001
                        log(f"!! {labels[t]} build failed: {exc}")
                    msgs.put(("progress", 60 + 40 * (n + 1) / max(len(targets), 1)))
            summary = (f"{res.functions} functions recompiled ({res.coverage * 100:.1f}% code coverage)"
                       + (f", built {len(built)} target(s)" if do_build else ""))
            msgs.put(("done", summary))
        except Exception as exc:  # noqa: BLE001
            log(traceback.format_exc())
            msgs.put(("error", str(exc)))

    def start(do_build: bool) -> None:
        if busy["on"]:
            return
        if not v_input.get().strip():
            messagebox.showwarning("Input", "Choose a disc image or DOL first.")
            return
        busy["on"] = True
        b_go.state(["disabled"])
        b_gen.state(["disabled"])
        logbox.delete("1.0", "end")
        prog["value"] = 0
        v_status.set("Working...")
        threading.Thread(target=worker, args=(do_build,), daemon=True).start()

    b_go.configure(command=lambda: start(True))
    b_gen.configure(command=lambda: start(False))

    def pump() -> None:
        try:
            while True:
                kind, val = msgs.get_nowait()
                if kind == "log":
                    logbox.insert("end", str(val) + "\n")
                    logbox.see("end")
                elif kind == "progress":
                    prog["value"] = float(val)
                elif kind == "status":
                    v_status.set(str(val))
                elif kind in ("done", "error"):
                    busy["on"] = False
                    b_go.state(["!disabled"])
                    b_gen.state(["!disabled"])
                    if kind == "done":
                        prog["value"] = 100
                        v_status.set("Done: " + str(val))
                    else:
                        v_status.set("Failed: " + str(val))
                        messagebox.showerror("Recompilation failed", str(val))
        except queue.Empty:
            pass
        root.after(80, pump)

    pump()
    if initial_input and os.path.exists(initial_input):
        v_input.set(os.path.abspath(initial_input))
        base = os.path.splitext(os.path.basename(initial_input.rstrip("/\\")))[0]
        v_output.set(os.path.join(os.path.dirname(os.path.abspath(initial_input)), base + "-native"))
        inspect()
    root.mainloop()
    return 0
