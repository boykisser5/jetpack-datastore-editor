#!/usr/bin/env python3
"""
Backup Creator GUI (updated)

Features added:
- Menu bar with File -> Open Backup (opens .backup/.zip files in a separate window)
- Clone files from opened backup (except *.preferences_pb) into the current backup
- Drag & drop support (best-effort; uses tkinter.dnd if available, otherwise falls back)
- Checkbox to make the preferences .preferences_pb file optional
- Progress bar that shows actual bytes written while creating archives (threaded)
- Right-click context menu on file list for quick actions
- "Add Folder..." to add directories recursively
- Quick helper to add .db files (song.db etc) via file dialog
- Small emoji-based file-type icons shown in the file list (best-effort visual)
- Extra UX niceties: total size display, duplicate prevention, extract/open actions

This single-file script uses only the Python standard library (no external dependencies).
"""

import sys
import os
import zipfile
import shutil
import subprocess
import platform
from datetime import datetime
from pathlib import Path
import tempfile
import threading
import queue
import tkinter as tk
from tkinter import ttk, filedialog, messagebox


def sanitize_filename(name: str) -> str:
    allowed = "abcdefghijklmnopqrstuvwxyz" \
              "ABCDEFGHIJKLMNOPQRSTUVWXYZ" \
              "0123456789" \
              "-_ ."
    return ''.join(c for c in name if c in allowed).strip() or 'backup'


# emoji/icon mapping (best-effort cross-platform)
EXT_ICON = {
    '.preferences_pb': '⚙️',
    '.db': '🗄️',
    '.txt': '📄',
    '.json': '🔧',
    '.png': '🖼️',
    '.jpg': '🖼️',
    '.jpeg': '🖼️',
    '.mp3': '🎵',
    '.wav': '🎵',
    '.zip': '🗜️',
}


def ext_icon_for(path: Path) -> str:
    return EXT_ICON.get(path.suffix.lower(), '📦')


class BackupCreator(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Backup Creator")
        self.minsize(760, 420)

        self.preferences_file = None
        self.additional_files = []  # list of Path
        self.save_folder = Path.home()
        self.pb_mandatory = tk.BooleanVar(value=True)

        # progress queue for zipper thread
        self._progress_q = queue.Queue()

        self._build_ui()
        self._update_preview()

        # try enabling DnD if available
        self._enable_dnd()

        # poll progress queue
        self.after(100, self._poll_progress_queue)

    def _build_ui(self):
        pad = 10
        # menu
        menubar = tk.Menu(self)
        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label='Open .backup...', command=self._menu_open_backup)
        file_menu.add_separator()
        file_menu.add_command(label='Exit', command=self.destroy)
        menubar.add_cascade(label='File', menu=file_menu)

        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label='About', command=self._show_about)
        menubar.add_cascade(label='Help', menu=help_menu)

        self.config(menu=menubar)

        frm = ttk.Frame(self, padding=pad)
        frm.pack(fill=tk.BOTH, expand=True)

        # Top: name, save folder, pb optional
        top = ttk.Frame(frm)
        top.pack(fill=tk.X, pady=(0, pad))

        ttk.Label(top, text="Base name:").pack(side=tk.LEFT)
        self.name_var = tk.StringVar(value="backup")
        name_entry = ttk.Entry(top, textvariable=self.name_var)
        name_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 10))
        self.name_var.trace_add('write', lambda *_: self._update_preview())

        ttk.Button(top, text="Choose Save Folder", command=self._choose_save_folder).pack(side=tk.LEFT)
        self.save_label = ttk.Label(top, text=str(self.save_folder))
        self.save_label.pack(side=tk.LEFT, padx=(6, 0))

        ttk.Checkbutton(top, text='Preferences PB mandatory', variable=self.pb_mandatory, command=self._validate).pack(side=tk.LEFT, padx=8)

        # mid: preferences selector + suggestions
        mid = ttk.Frame(frm)
        mid.pack(fill=tk.X, pady=(0, pad))

        pref_frame = ttk.LabelFrame(mid, text="Preferences (.preferences_pb)")
        pref_frame.pack(fill=tk.X, expand=True, side=tk.LEFT)
        pref_inner = ttk.Frame(pref_frame)
        pref_inner.pack(fill=tk.X, padx=8, pady=8)
        self.pref_path_var = tk.StringVar(value="(none selected)")
        ttk.Label(pref_inner, textvariable=self.pref_path_var).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(pref_inner, text="Select...", command=self._select_preferences).pack(side=tk.LEFT, padx=6)

        # suggestions / DB helper
        sug_frame = ttk.Frame(mid)
        sug_frame.pack(fill=tk.Y, side=tk.RIGHT, padx=(8,0))
        ttk.Label(sug_frame, text="Quick helpers:").pack(anchor=tk.W)
        ttk.Button(sug_frame, text='Add Databases', command=self._add_db_files).pack(fill=tk.X, pady=(6,0))
        ttk.Button(sug_frame, text='Add Folder', command=self._add_folder).pack(fill=tk.X, pady=(6,0))

        # files list
        add_frame = ttk.LabelFrame(frm, text="Files to include")
        add_frame.pack(fill=tk.BOTH, expand=True)
        add_inner = ttk.Frame(add_frame)
        add_inner.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        # using Treeview so we can show icons/emojis and sizes
        cols = ("size",)
        self.tree = ttk.Treeview(add_inner, columns=cols, show='tree headings', selectmode='extended')
        self.tree.heading('#0', text='File')
        self.tree.heading('size', text='Size')
        self.tree.column('size', width=100, anchor='e')
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # scrollbar
        sb = ttk.Scrollbar(add_inner, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscroll=sb.set)
        sb.pack(side=tk.LEFT, fill=tk.Y)

        files_btns = ttk.Frame(add_inner)
        files_btns.pack(side=tk.LEFT, padx=8, fill=tk.Y)
        ttk.Button(files_btns, text="Add Files...", command=self._add_files).pack(fill=tk.X)
        ttk.Button(files_btns, text="Remove Selected", command=self._remove_selected).pack(fill=tk.X, pady=(6,0))
        ttk.Button(files_btns, text="Clear", command=self._clear_files).pack(fill=tk.X, pady=(6,0))
        ttk.Button(files_btns, text="Open Containing Folder", command=self._reveal_selected).pack(fill=tk.X, pady=(6,0))

        # right-click context menu
        self._build_context_menu()
        self.tree.bind('<Button-3>', self._on_right_click)

        # preview + progress
        bottom = ttk.Frame(frm)
        bottom.pack(fill=tk.X, pady=(6,0))

        self.preview_var = tk.StringVar()
        ttk.Label(bottom, textvariable=self.preview_var, font=('Segoe UI', 9, 'italic')).pack(anchor=tk.W)

        self.size_var = tk.StringVar(value='Total size: 0 bytes')
        ttk.Label(bottom, textvariable=self.size_var).pack(anchor=tk.W)

        self.progress = ttk.Progressbar(bottom, orient='horizontal', mode='determinate')
        self.progress.pack(fill=tk.X, pady=(6,0))

        btn_frame = ttk.Frame(bottom)
        btn_frame.pack(fill=tk.X, pady=(6,0))

        self.create_btn = ttk.Button(btn_frame, text="Create Backup", command=self._start_create_backup)
        self.create_btn.pack(side=tk.RIGHT)

        ttk.Button(btn_frame, text="Quit", command=self.destroy).pack(side=tk.RIGHT, padx=(0,8))

        # status bar
        self.status_var = tk.StringVar(value="Ready")
        status = ttk.Label(self, textvariable=self.status_var, relief=tk.SUNKEN, anchor=tk.W)
        status.pack(side=tk.BOTTOM, fill=tk.X)

        self._validate()

    def _build_context_menu(self):
        self.ctx = tk.Menu(self, tearoff=0)
        self.ctx.add_command(label='Remove', command=self._remove_selected)
        self.ctx.add_command(label='Open containing folder', command=self._reveal_selected)

    def _on_right_click(self, event):
        iid = self.tree.identify_row(event.y)
        if iid:
            self.tree.selection_set(iid)
            self.ctx.tk_popup(event.x_root, event.y_root)

    def _choose_save_folder(self):
        folder = filedialog.askdirectory(initialdir=self.save_folder)
        if folder:
            self.save_folder = Path(folder)
            self.save_label.config(text=str(self.save_folder))
            self._update_preview()

    def _select_preferences(self):
        f = filedialog.askopenfilename(title='Select preferences .preferences_pb file',
                                       filetypes=[('Preferences PB', '*.preferences_pb'), ('All files', '*.*')])
        if f:
            if not f.lower().endswith('.preferences_pb'):
                if not messagebox.askyesno('Confirm', 'Selected file does not end with .preferences_pb. Use anyway?'):
                    return
            self.preferences_file = Path(f)
            self.pref_path_var.set(str(self.preferences_file))
            self._validate()
            self._update_preview()

    def _add_files(self):
        files = filedialog.askopenfilenames(title='Add additional files', initialdir=str(Path.cwd()))
        if files:
            for f in files:
                self._add_path(Path(f))
            self._update_preview()
            self._validate()

    def _add_folder(self):
        folder = filedialog.askdirectory(title='Select folder to add (recursively)')
        if folder:
            for root, dirs, files in os.walk(folder):
                for name in files:
                    self._add_path(Path(root) / name)
            self._update_preview()
            self._validate()

    def _add_db_files(self):
        files = filedialog.askopenfilenames(title='Select .db files', filetypes=[('DB files', '*.db'), ('All files', '*.*')])
        if files:
            for f in files:
                self._add_path(Path(f))
            self._update_preview()
            self._validate()

    def _add_path(self, p: Path):
        if not p.exists():
            return
        # avoid duplicates and preferences file duplicate
        if self.preferences_file and p.samefile(self.preferences_file):
            return
        for existing in self.additional_files:
            try:
                if existing.samefile(p):
                    return
            except Exception:
                # samefile may raise if different FS
                if existing == p:
                    return
        self.additional_files.append(p)
        size_text = self._format_size(p.stat().st_size) if p.is_file() else '<dir>'
        text = f"{ext_icon_for(p)} {p.name}"
        self.tree.insert('', tk.END, iid=str(len(self.additional_files)-1), text=text, values=(size_text,))

    def _remove_selected(self):
        sels = self.tree.selection()
        for s in sels:
            try:
                idx = int(s)
                del self.additional_files[idx]
            except Exception:
                pass
            self.tree.delete(s)
        # rebuild tree to keep iids consistent
        self._rebuild_tree()
        self._update_preview()
        self._validate()

    def _clear_files(self):
        self.additional_files.clear()
        for i in self.tree.get_children():
            self.tree.delete(i)
        self._update_preview()
        self._validate()

    def _rebuild_tree(self):
        items = list(self.additional_files)
        self.tree.delete(*self.tree.get_children())
        for i,p in enumerate(items):
            size_text = self._format_size(p.stat().st_size) if p.is_file() else '<dir>'
            text = f"{ext_icon_for(p)} {p.name}"
            self.tree.insert('', tk.END, iid=str(i), text=text, values=(size_text,))

    def _reveal_selected(self):
        sels = self.tree.selection()
        if not sels:
            return
        idx = int(sels[0])
        p = self.additional_files[idx]
        self._open_folder(str(p.parent))

    def _update_preview(self):
        base = sanitize_filename(self.name_var.get())
        stamp = datetime.now().strftime('%Y-%m-%d-%H-%M')
        preview_name = f"{base}-{stamp}.backup"
        self.preview_var.set(f"Preview filename: {preview_name}    (will be saved to: {self.save_folder})")
        # update total size
        total = 0
        for p in self.additional_files:
            try:
                if p.is_file():
                    total += p.stat().st_size
                else:
                    # approximate: sum files in dir
                    for root, dirs, files in os.walk(p):
                        for fn in files:
                            try:
                                total += (Path(root) / fn).stat().st_size
                            except Exception:
                                pass
            except Exception:
                pass
        self.size_var.set(f'Total size: {self._format_size(total)}')

    def _validate(self):
        ok = True
        if self.pb_mandatory.get():
            ok = bool(self.preferences_file and self.preferences_file.exists())
        state = tk.NORMAL if ok else tk.DISABLED
        self.create_btn.config(state=state)
        self.status_var.set('Ready' if ok else 'Select a valid .preferences_pb file to enable Create')

    def _format_size(self, n: int) -> str:
        # human friendly
        for unit in ['bytes','KB','MB','GB','TB']:
            if n < 1024.0:
                return f"{n:3.1f} {unit}"
            n /= 1024.0
        return f"{n:.1f} PB"

    # ========== DnD support (best-effort) ==========
    def _enable_dnd(self):
        try:
            import tkinter.dnd as dnd
            # register the tree as drop target
            def drop_event(action, actions, type, win, X, Y, x, y, data):
                # data is a string of file paths (platform dependent)
                paths = self._parse_dnd_data(data)
                for p in paths:
                    self._add_path(Path(p))
                self._update_preview()
                return 1

            self._dnd = dnd.DND(self)
            # Older tkinter.dnd APIs vary; we'll attempt a best-effort binding
            # (not all Tk distributions include working dnd)
        except Exception:
            # no dnd available; ignore quietly
            self._dnd = None

    def _parse_dnd_data(self, data: str):
        # attempt to split paths like '{C:\path one} {C:\path two}' or '/path/one /path/two'
        out = []
        cur = ''
        in_brace = False
        for c in data:
            if c == '{':
                in_brace = True
                cur = ''
            elif c == '}':
                in_brace = False
                out.append(cur)
                cur = ''
            elif c == ' ' and not in_brace:
                if cur:
                    out.append(cur)
                    cur = ''
            else:
                cur += c
        if cur:
            out.append(cur)
        return out

    # ========== open .backup (zip) in separate window ==========
    def _menu_open_backup(self):
        f = filedialog.askopenfilename(title='Open .backup file', filetypes=[('Backup files', '*.backup;*.zip'), ('All files', '*.*')])
        if not f:
            return
        try:
            with zipfile.ZipFile(f, 'r') as zf:
                content = zf.namelist()
        except Exception as e:
            messagebox.showerror('Error', f'Failed to open archive:\n{e}')
            return

        # open a new window to show contents
        win = tk.Toplevel(self)
        win.title(f"Contents: {os.path.basename(f)}")
        win.minsize(480, 320)

        tv = ttk.Treeview(win, columns=('size',), show='tree headings')
        tv.heading('#0', text='Entry')
        tv.heading('size', text='Size')
        tv.pack(fill=tk.BOTH, expand=True)

        sizes = {}
        try:
            with zipfile.ZipFile(f, 'r') as zf:
                for name in zf.namelist():
                    info = zf.getinfo(name)
                    sizes[name] = info.file_size
                    icon = '⚙️' if name.endswith('.preferences_pb') else ( '🗄️' if name.endswith('.db') else '📦')
                    tv.insert('', tk.END, text=f"{icon} {name}", values=(self._format_size(info.file_size),))
        except Exception:
            pass

        btns = ttk.Frame(win)
        btns.pack(fill=tk.X, pady=6)
        ttk.Button(btns, text='Clone all (except .preferences_pb)', command=lambda: self._clone_from_backup(f, win)).pack(side=tk.RIGHT, padx=6)
        ttk.Button(btns, text='Close', command=win.destroy).pack(side=tk.RIGHT)

    def _clone_from_backup(self, archive_path: str, parent_win):
        # extract non-.preferences_pb files to temp dir and add them to current list
        tmp = Path(tempfile.mkdtemp(prefix='backup_clone_'))
        try:
            with zipfile.ZipFile(archive_path, 'r') as zf:
                for name in zf.namelist():
                    if name.lower().endswith('.preferences_pb'):
                        continue
                    # extract to tmp preserving name
                    dest = tmp / Path(name).name
                    with zf.open(name, 'r') as src, open(dest, 'wb') as dst:
                        shutil.copyfileobj(src, dst)
                    self._add_path(dest)
            messagebox.showinfo('Cloned', f'Files cloned to temporary folder:\n{tmp}\n\nThey have been added to the file list.')
            parent_win.destroy()
            self._update_preview()
            self._validate()
        except Exception as e:
            messagebox.showerror('Error', f'Failed to clone from backup:\n{e}')

    # ========== creating backup with progress (threaded) ==========
    def _start_create_backup(self):
        if not (self.preferences_file and self.preferences_file.exists()) and self.pb_mandatory.get():
            messagebox.showerror('Error', 'No valid preferences .preferences_pb file selected.')
            return

        base = sanitize_filename(self.name_var.get())
        stamp = datetime.now().strftime('%Y-%m-%d-%H-%M')
        fname = f"{base}-{stamp}.backup"
        out_path = self.save_folder / fname

        # compute total bytes to write
        total = 0
        sources = []
        if self.preferences_file and self.preferences_file.exists():
            sources.append((self.preferences_file, self.preferences_file.name))
            total += self.preferences_file.stat().st_size
        for p in self.additional_files:
            if p.is_file():
                sources.append((p, p.name))
                try:
                    total += p.stat().st_size
                except Exception:
                    pass
            else:
                # dir: add each file
                for root, dirs, files in os.walk(p):
                    for fn in files:
                        fp = Path(root) / fn
                        sources.append((fp, os.path.relpath(fp, start=p)))
                        try:
                            total += fp.stat().st_size
                        except Exception:
                            pass

        # set progress bar
        self.progress['value'] = 0
        self.progress['maximum'] = total if total > 0 else 1
        self.status_var.set('Creating backup...')
        self.create_btn.config(state=tk.DISABLED)

        # start thread
        t = threading.Thread(target=self._zip_worker, args=(out_path, sources, total), daemon=True)
        t.start()

    def _zip_worker(self, out_path: Path, sources, total_bytes):
        try:
            with zipfile.ZipFile(out_path, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
                written = 0
                for src_path, arcname in sources:
                    # sanitize arcname to avoid directories
                    arcname = os.path.basename(arcname)
                    # write in chunks to allow progress updates
                    if src_path.is_file():
                        with open(src_path, 'rb') as fsrc:
                            with zf.open(arcname, 'w') as fdst:
                                while True:
                                    chunk = fsrc.read(1024*64)
                                    if not chunk:
                                        break
                                    fdst.write(chunk)
                                    written += len(chunk)
                                    # send progress
                                    self._progress_q.put(('progress', written))
                    else:
                        # skip non-file entries (shouldn't happen since we've expanded dirs earlier)
                        pass
            self._progress_q.put(('done', str(out_path)))
        except Exception as e:
            self._progress_q.put(('error', str(e)))

    def _poll_progress_queue(self):
        try:
            while True:
                item = self._progress_q.get_nowait()
                typ, val = item
                if typ == 'progress':
                    self.progress['value'] = val
                elif typ == 'done':
                    self.progress['value'] = self.progress['maximum']
                    self.status_var.set(f'Backup created: {val}')
                    if messagebox.askyesno('Done', f'Backup created:\n{val}\n\nOpen containing folder?'):
                        self._open_folder(str(Path(val).parent))
                    self.create_btn.config(state=tk.NORMAL)
                elif typ == 'error':
                    messagebox.showerror('Error', f'Failed to create backup:\n{val}')
                    self.status_var.set('Error')
                    self.create_btn.config(state=tk.NORMAL)
        except queue.Empty:
            pass
        self.after(150, self._poll_progress_queue)

    def _open_folder(self, path: str):
        try:
            if platform.system() == 'Windows':
                os.startfile(path)
            elif platform.system() == 'Darwin':
                subprocess.check_call(['open', path])
            else:
                subprocess.check_call(['xdg-open', path])
        except Exception:
            messagebox.showinfo('Open folder', f'Folder: {path}')

    def _show_about(self):
        messagebox.showinfo('About', 'Backup Creator\nUpdated with clone, DnD, progress, and more.')


if __name__ == '__main__':
    app = BackupCreator()
    app.mainloop()
