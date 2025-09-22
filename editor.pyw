#!/usr/bin/env python3

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog
import struct
import os
import sys

def read_varint(data, offset):
    """Return (value, new_offset) for protobuf varint"""
    result = 0
    shift = 0
    pos = offset
    while True:
        if pos >= len(data):
            raise ValueError("Unexpected end of data while reading varint")
        b = data[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
        if shift > 70:
            raise ValueError("Varint too big")
    return result, pos

def encode_varint(value):
    """Encode integer as protobuf varint bytes"""
    if value < 0:
        value &= (1 << 64) - 1
    parts = []
    while True:
        to_write = value & 0x7F
        value >>= 7
        if value:
            parts.append(to_write | 0x80)
        else:
            parts.append(to_write)
            break
    return bytes(parts)

def read_fixed32(data, offset):
    if offset + 4 > len(data):
        raise ValueError("Not enough data for fixed32")
    val = struct.unpack_from('<I', data, offset)[0]
    return val, offset + 4

def read_fixed64(data, offset):
    if offset + 8 > len(data):
        raise ValueError("Not enough data for fixed64")
    val = struct.unpack_from('<Q', data, offset)[0]
    return val, offset + 8

def encode_fixed32(value):
    return struct.pack('<I', value & 0xffffffff)

def encode_fixed64(value):
    return struct.pack('<Q', value & 0xffffffffffffffff)

# ----------------
# Value parser/encoder for Value message (the "oneof" types)
# ----------------

WIRE_VARINT = 0
WIRE_64BIT = 1
WIRE_LENGTH = 2
WIRE_32BIT = 5

def parse_value_message(data):
    offset = 0
    found = {}
    while offset < len(data):
        tag, offset = read_varint(data, offset)
        field_num = tag >> 3
        wire = tag & 0x7
        if field_num == 1 and wire == WIRE_VARINT:
            v, offset = read_varint(data, offset)
            found['boolean'] = bool(v)
        elif field_num == 2 and wire == WIRE_32BIT:
            raw, offset = read_fixed32(data, offset)
            f = struct.unpack('<f', struct.pack('<I', raw))[0]
            found['float'] = f
        elif field_num == 3 and wire == WIRE_VARINT:
            v, offset = read_varint(data, offset)
            found['integer'] = v if v <= 0x7fffffff else v - (1 << 32)
        elif field_num == 4 and wire == WIRE_VARINT:
            v, offset = read_varint(data, offset)
            found['long'] = v if v <= 0x7fffffffffffffff else v - (1 << 64)
        elif field_num == 5 and wire == WIRE_LENGTH:
            ln, offset = read_varint(data, offset)
            if offset + ln > len(data):
                raise ValueError("Truncated string")
            s = data[offset:offset+ln].decode('utf-8', errors='replace')
            offset += ln
            found['string'] = s
        elif field_num == 6 and wire == WIRE_LENGTH:
            ln, offset = read_varint(data, offset)
            if offset + ln > len(data):
                raise ValueError("Truncated string_set")
            sub = data[offset:offset+ln]
            offset += ln
            suboff = 0
            arr = []
            while suboff < len(sub):
                stag, suboff = read_varint(sub, suboff)
                sfield = stag >> 3
                swire = stag & 0x7
                if sfield == 1 and swire == WIRE_LENGTH:
                    sln, suboff = read_varint(sub, suboff)
                    arr.append(sub[suboff:suboff+sln].decode('utf-8', errors='replace'))
                    suboff += sln
                else:
                    if swire == WIRE_VARINT:
                        _, suboff = read_varint(sub, suboff)
                    elif swire == WIRE_64BIT:
                        suboff += 8
                    elif swire == WIRE_LENGTH:
                        l, suboff = read_varint(sub, suboff)
                        suboff += l
                    elif swire == WIRE_32BIT:
                        suboff += 4
                    else:
                        break
            found['string_set'] = arr
        elif field_num == 7 and wire == WIRE_64BIT:
            raw, offset = read_fixed64(data, offset)
            d = struct.unpack('<d', struct.pack('<Q', raw))[0]
            found['double'] = d
        else:
            if wire == WIRE_VARINT:
                _, offset = read_varint(data, offset)
            elif wire == WIRE_64BIT:
                offset += 8
            elif wire == WIRE_LENGTH:
                ln, offset = read_varint(data, offset)
                offset += ln
            elif wire == WIRE_32BIT:
                offset += 4
            else:
                raise ValueError("Unknown wire type encountered")
    for t in ['boolean','float','integer','long','string','string_set','double']:
        if t in found:
            return t, found[t]
    return None, None

def encode_value_message(type_name, value):
    parts = []
    if type_name == 'boolean':
        parts.append(encode_varint((1 << 3) | WIRE_VARINT))
        parts.append(encode_varint(1 if value else 0))
    elif type_name == 'float':
        parts.append(encode_varint((2 << 3) | WIRE_32BIT))
        parts.append(encode_fixed32(struct.unpack('<I', struct.pack('<f', float(value)))[0]))
    elif type_name == 'integer':
        parts.append(encode_varint((3 << 3) | WIRE_VARINT))
        parts.append(encode_varint(int(value)))
    elif type_name == 'long':
        parts.append(encode_varint((4 << 3) | WIRE_VARINT))
        parts.append(encode_varint(int(value)))
    elif type_name == 'string':
        b = str(value).encode('utf-8')
        parts.append(encode_varint((5 << 3) | WIRE_LENGTH))
        parts.append(encode_varint(len(b)))
        parts.append(b)
    elif type_name == 'string_set':
        inner = []
        for s in value:
            sb = str(s).encode('utf-8')
            inner.append(encode_varint((1 << 3) | WIRE_LENGTH))
            inner.append(encode_varint(len(sb)))
            inner.append(sb)
        inner_bytes = b''.join(inner)
        parts.append(encode_varint((6 << 3) | WIRE_LENGTH))
        parts.append(encode_varint(len(inner_bytes)))
        parts.append(inner_bytes)
    elif type_name == 'double':
        parts.append(encode_varint((7 << 3) | WIRE_64BIT))
        parts.append(encode_fixed64(struct.unpack('<Q', struct.pack('<d', float(value)))[0]))
    else:
        raise ValueError("Unknown type for encoding: " + str(type_name))
    return b''.join(parts)

# ----------------
# Top-level PreferenceMap parsing/encoding
# ----------------

def parse_preferences_pb(raw_bytes):
    offset = 0
    res = {}
    while offset < len(raw_bytes):
        tag, offset = read_varint(raw_bytes, offset)
        field_num = tag >> 3
        wire = tag & 0x7
        if field_num == 1 and wire == WIRE_LENGTH:
            length, offset = read_varint(raw_bytes, offset)
            entry = raw_bytes[offset:offset+length]
            offset += length
            eoff = 0
            key = None
            val_bytes = None
            while eoff < len(entry):
                etag, eoff = read_varint(entry, eoff)
                efnum = etag >> 3
                ewire = etag & 0x7
                if efnum == 1 and ewire == WIRE_LENGTH:
                    klen, eoff = read_varint(entry, eoff)
                    key = entry[eoff:eoff+klen].decode('utf-8', errors='replace')
                    eoff += klen
                elif efnum == 2 and ewire == WIRE_LENGTH:
                    vlen, eoff = read_varint(entry, eoff)
                    val_bytes = entry[eoff:eoff+vlen]
                    eoff += vlen
                else:
                    if ewire == WIRE_VARINT:
                        _, eoff = read_varint(entry, eoff)
                    elif ewire == WIRE_64BIT:
                        eoff += 8
                    elif ewire == WIRE_LENGTH:
                        l, eoff = read_varint(entry, eoff)
                        eoff += l
                    elif ewire == WIRE_32BIT:
                        eoff += 4
                    else:
                        raise ValueError("Unknown wire inside map entry")
            if key is not None:
                if val_bytes is None:
                    res[key] = (None, None)
                else:
                    typ, val = parse_value_message(val_bytes)
                    res[key] = (typ, val)
        else:
            if wire == WIRE_VARINT:
                _, offset = read_varint(raw_bytes, offset)
            elif wire == WIRE_64BIT:
                offset += 8
            elif wire == WIRE_LENGTH:
                l, offset = read_varint(raw_bytes, offset)
                offset += l
            elif wire == WIRE_32BIT:
                offset += 4
            else:
                raise ValueError("Unknown top-level wire")
    return res

def encode_preferences_dict(pref_dict):
    entries = []
    for key, (tname, value) in pref_dict.items():
        kbytes = key.encode('utf-8')
        entry_parts = []
        entry_parts.append(encode_varint((1 << 3) | WIRE_LENGTH))
        entry_parts.append(encode_varint(len(kbytes)))
        entry_parts.append(kbytes)
        if tname is not None:
            vbytes = encode_value_message(tname, value)
            entry_parts.append(encode_varint((2 << 3) | WIRE_LENGTH))
            entry_parts.append(encode_varint(len(vbytes)))
            entry_parts.append(vbytes)
        entry_bytes = b''.join(entry_parts)
        entries.append(encode_varint((1 << 3) | WIRE_LENGTH))
        entries.append(encode_varint(len(entry_bytes)))
        entries.append(entry_bytes)
    return b''.join(entries)

# ----------------
# GUI
# ----------------

class PrefEditorApp:
    def __init__(self, root):
        self.root = root

        # use default OS theme by not forcing a specific ttk theme
        style = ttk.Style(root)
        # (no explicit theme_use call — let ttk choose the OS-default theme)
        style.configure("Header.TLabel", font=("Segoe UI", 10, "bold"))
        style.configure("Status.TLabel", foreground="#555555")
        style.configure("Tip.TLabel", foreground="#333333", font=("Segoe UI", 9))

        root.title("Preferences DataStore Editor")
        root.geometry("800x600")
        root.resizable(False, False)

        # Try to load and set the window icon (editor.png) if present
        icon_path = os.path.join(os.getcwd(), "editor.png")
        self._icon_img = None
        try:
            if os.path.exists(icon_path):
                self._icon_img = tk.PhotoImage(file=icon_path)
                # apply as application icon
                try:
                    root.iconphoto(True, self._icon_img)
                except Exception:
                    # older platforms may behave differently; ignore if fails
                    pass
        except Exception:
            # ignore icon loading errors
            self._icon_img = None

        self.filename = None
        self.pref_dict = {}  # key -> (type_name, value)

        # Menu
        menubar = tk.Menu(root)
        filemenu = tk.Menu(menubar, tearoff=0)
        filemenu.add_command(label="New...", command=self.menu_new, accelerator="Ctrl+N")
        filemenu.add_command(label="Open...", command=self.open_file, accelerator="Ctrl+O")
        filemenu.add_separator()
        filemenu.add_command(label="Save", command=self.save_file, accelerator="Ctrl+S")
        filemenu.add_command(label="Save As...", command=self.save_file_as)
        filemenu.add_separator()
        filemenu.add_command(label="Exit", command=self.quit)
        menubar.add_cascade(label="File", menu=filemenu)

        helpmenu = tk.Menu(menubar, tearoff=0)
        helpmenu.add_command(label="Tips & About", command=self.show_about)
        menubar.add_cascade(label="Help", menu=helpmenu)

        root.config(menu=menubar)
        root.bind_all("<Control-n>", lambda e: self.menu_new())
        root.bind_all("<Control-o>", lambda e: self.open_file())
        root.bind_all("<Control-s>", lambda e: self.save_file())

        # layout frames
        main = ttk.Frame(root, padding=(10,10))
        main.pack(fill='both', expand=True)

        left = ttk.Frame(main, width=320)
        left.pack(side='left', fill='y')

        # keys header + +/- buttons
        header_frame = ttk.Frame(left)
        header_frame.pack(fill='x', pady=(2,4))
        ttk.Label(header_frame, text="Keys", style="Header.TLabel").pack(side='left', anchor='w')
        self.plus_btn = ttk.Button(header_frame, text="+", width=3, command=self.add_key, state='disabled')
        self.plus_btn.pack(side='left', padx=(8,2))
        self.minus_btn = ttk.Button(header_frame, text="-", width=3, command=self.remove_key, state='disabled')
        self.minus_btn.pack(side='left', padx=2)

        # listbox with vertical + horizontal scrollbars inside its frame (grid layout)
        listbox_frame = ttk.Frame(left, relief='flat')
        listbox_frame.pack(fill='both', expand=True, pady=(4,4))

        vscroll = ttk.Scrollbar(listbox_frame, orient='vertical')
        hscroll = ttk.Scrollbar(listbox_frame, orient='horizontal')

        self.listbox = tk.Listbox(listbox_frame, width=45, height=28, activestyle='dotbox',
                                  yscrollcommand=vscroll.set, xscrollcommand=hscroll.set, selectmode='browse')
        self.listbox.grid(row=0, column=0, sticky='nsew')
        vscroll.grid(row=0, column=1, sticky='ns')
        hscroll.grid(row=1, column=0, sticky='ew')

        listbox_frame.grid_rowconfigure(0, weight=1)
        listbox_frame.grid_columnconfigure(0, weight=1)

        vscroll.config(command=self.listbox.yview)
        hscroll.config(command=self.listbox.xview)

        self.listbox.bind('<<ListboxSelect>>', self.on_select)

        # small actions row (only reload kept here; menu handles New/Open/Save)
        btn_frame = ttk.Frame(left)
        btn_frame.pack(fill='x', pady=(6,0))
        ttk.Button(btn_frame, text="Reload", command=self.reload_file).pack(side='left', padx=2)

        # right: details + edit
        right = ttk.Frame(main)
        right.pack(side='right', fill='both', expand=True)

        meta_frame = ttk.Frame(right)
        meta_frame.pack(fill='x')
        self.file_label = ttk.Label(meta_frame, text="No file loaded", style="Status.TLabel")
        self.file_label.pack(anchor='w')

        sep = ttk.Separator(right, orient='horizontal')
        sep.pack(fill='x', pady=6)

        form = ttk.Frame(right)
        form.pack(fill='both', expand=True)

        ttk.Label(form, text="Key:").grid(row=0, column=0, sticky='nw', padx=(0,6))
        self.key_var = tk.StringVar()
        self.key_entry = ttk.Entry(form, textvariable=self.key_var, width=48, state='disabled')
        self.key_entry.grid(row=0, column=1, sticky='we', pady=2)

        ttk.Label(form, text="Type:").grid(row=1, column=0, sticky='nw', padx=(0,6))
        self.type_var = tk.StringVar()
        self.type_combo = ttk.Combobox(form, textvariable=self.type_var, state='disabled', values=[
            'string','boolean','integer','long','float','double','string_set'
        ])
        self.type_combo.grid(row=1, column=1, sticky='we', pady=2)

        ttk.Label(form, text="Value:").grid(row=2, column=0, sticky='nw', padx=(0,6))
        self.value_text = tk.Text(form, height=18, width=48, state='disabled', wrap='none')
        self.value_text.grid(row=2, column=1, sticky='we', pady=2)

        # small vertical scrollbar for the value text
        val_vscroll = ttk.Scrollbar(form, orient='vertical', command=self.value_text.yview)
        self.value_text.config(yscrollcommand=val_vscroll.set)
        val_vscroll.grid(row=2, column=2, sticky='ns', padx=(4,0))

        hint = ("Tips:\n"
                " • Booleans: 'true' or 'false'.\n"
                " • Integers/Longs: plain integers (no commas).\n"
                " • Float/Double: numeric decimal (e.g. 3.14).\n"
                " • string_set: put one item per line.\n"
                " • Always backup your file (a .bak is automatically made on first save).\n"
                " • Use File → New / Open / Save from the menu.\n")
        self.tip_label = ttk.Label(right, text=hint, style="Tip.TLabel", justify='left')
        self.tip_label.pack(anchor='w', pady=(8,0))

        action_frame = ttk.Frame(right)
        action_frame.pack(fill='x', pady=(8,0))
        self.apply_btn = ttk.Button(action_frame, text="Apply to Selected Key", command=self.apply_to_selected, state='disabled')
        self.apply_btn.pack(side='left', padx=4)
        self.apply_save_btn = ttk.Button(action_frame, text="Apply & Save", command=self.apply_and_save, state='disabled')
        self.apply_save_btn.pack(side='left', padx=4)

        # status bar
        self.status_label = ttk.Label(root, text="Ready", anchor='w', style="Status.TLabel")
        self.status_label.pack(side='bottom', fill='x')

        # load known keys/types from PreferenceKeys.txt if present (suggestions)
        self.known_types = {}
        self.load_known_keys_file()

        # set initial widgets state (no file)
        self.update_widget_states(file_loaded=False)

    def load_known_keys_file(self):
        fname = os.path.join(os.getcwd(), "PreferenceKeys.txt")
        if not os.path.exists(fname):
            return
        try:
            with open(fname, 'r', encoding='utf-8') as f:
                data = f.read()
        except Exception:
            return
        import re
        for m in re.finditer(r'booleanPreferencesKey\(\s*"([^"]+)"\s*\)', data):
            self.known_types[m.group(1)] = 'boolean'
        for m in re.finditer(r'floatPreferencesKey\(\s*"([^"]+)"\s*\)', data):
            self.known_types[m.group(1)] = 'float'
        for m in re.finditer(r'intPreferencesKey\(\s*"([^"]+)"\s*\)', data):
            self.known_types[m.group(1)] = 'integer'
        for m in re.finditer(r'stringPreferencesKey\(\s*"([^"]+)"\s*\)', data):
            self.known_types[m.group(1)] = 'string'
        if self.known_types:
            self.status_label.config(text="Loaded PreferenceKeys.txt suggestions")

    # ---------------- Menu actions ----------------
    def menu_new(self):
        target = filedialog.asksaveasfilename(title="Create new .preferences_pb file",
                                              defaultextension=".preferences_pb",
                                              filetypes=[("preferences_pb","*.preferences_pb"), ("All files","*.*")])
        if not target:
            return
        try:
            with open(target, 'wb') as f:
                f.write(b'')
            self.filename = target
            self.pref_dict = {}
            self.refresh_listbox()
            self.file_label.config(text=f"New: {os.path.basename(self.filename)}  (0 keys)")
            self.update_widget_states(file_loaded=True)
            self.status_label.config(text=f"Created new file: {self.filename}")
        except Exception as e:
            messagebox.showerror("Error", f"Could not create file: {e}")

    def show_about(self):
        d = tk.Toplevel(self.root)
        d.title("Tips & About")
        d.transient(self.root)
        d.resizable(False, False)
        d.withdraw()  # build off-screen to avoid flicker while packing

        # main text
        text = (
            "Preferences DataStore Editor\n\n"
            "• Use File → New to create a new file.\n"
            "• Open a .preferences_pb to edit keys.\n"
            "• Add / Remove keys with the + / - buttons.\n"
            "• Apply updates and Save when ready.\n\n"
            "Note: Always keep a backup. This tool uses a minimal protobuf reader/writer tailored "
            "to the Preferences DataStore format."
        )
        lbl = ttk.Label(d, text=text, justify='left', anchor='nw')
        lbl.pack(fill='both', padx=12, pady=(12,6))

        # bottom row: image on left, spacer, OK button on right
        bottom = ttk.Frame(d)
        bottom.pack(fill='x', padx=8, pady=8)

        img_path = os.path.join(os.getcwd(), "me.jpg")
        about_img_lbl = None
        if os.path.exists(img_path):
            try:
                # keep reference so it doesn't get GC'd
                self._about_img = tk.PhotoImage(file=img_path)
                about_img_lbl = ttk.Label(bottom, image=self._about_img)
                about_img_lbl.pack(side='left', anchor='w', padx=(0,8))
            except Exception:
                about_img_lbl = None

        # filler frame to push the OK button to the right
        filler = ttk.Frame(bottom)
        filler.pack(side='left', fill='x', expand=True)

        ok_btn = ttk.Button(bottom, text="OK", command=d.destroy)
        ok_btn.pack(side='right')

        d.deiconify()
        d.grab_set()
        self.root.wait_window(d)

    # ---------------- file ops ----------------
    def open_file(self):
        fn = filedialog.askopenfilename(title="Open preferences_pb file",
                                        filetypes=[("preferences_pb","*.preferences_pb"),("All files","*.*")])
        if not fn:
            return
        try:
            with open(fn, 'rb') as f:
                raw = f.read()
            parsed = parse_preferences_pb(raw)
        except Exception as e:
            messagebox.showerror("Error", f"Failed to parse file: {e}")
            return
        self.filename = fn
        self.pref_dict = parsed
        self.refresh_listbox()
        self.file_label.config(text=f"Loaded: {os.path.basename(fn)}  ({len(self.pref_dict)} keys)")
        self.update_widget_states(file_loaded=True)
        self.status_label.config(text=f"Opened: {fn}")

    def reload_file(self):
        if not self.filename:
            return
        try:
            with open(self.filename, 'rb') as f:
                raw = f.read()
            self.pref_dict = parse_preferences_pb(raw)
            self.refresh_listbox()
            messagebox.showinfo("Reloaded", "Reloaded and parsed file.")
            self.status_label.config(text=f"Reloaded: {self.filename}")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to reload: {e}")

    def save_file_as(self):
        if not self.filename:
            default = "settings.preferences_pb"
        else:
            default = os.path.basename(self.filename)
        fn = filedialog.asksaveasfilename(title="Save preferences_pb as",
                                          initialfile=default,
                                          defaultextension=".preferences_pb",
                                          filetypes=[("preferences_pb","*.preferences_pb"), ("All files","*.*")])
        if not fn:
            return
        self.filename = fn
        self.save_file()

    def refresh_listbox(self):
        self.listbox.delete(0, 'end')
        for k in sorted(self.pref_dict.keys()):
            typ, val = self.pref_dict[k]
            disp = f"{k}  [{typ if typ else 'unknown'}]  {repr(val)}"
            self.listbox.insert('end', disp)

    def on_select(self, event=None):
        sel = self.listbox.curselection()
        if not sel:
            self.minus_btn.config(state='disabled')
            return
        idx = sel[0]
        entry = self.listbox.get(idx)
        key = entry.split("  [")[0]
        typ, val = self.pref_dict.get(key, (None, None))
        self.key_var.set(key)
        if typ is None:
            suggestion = self.known_types.get(key)
            if suggestion:
                typ = suggestion
        self.type_var.set(typ if typ else '')
        self.value_text.config(state='normal')
        self.value_text.delete('1.0','end')
        if typ == 'string_set' and isinstance(val, (list,tuple)):
            self.value_text.insert('1.0', "\n".join(val))
        elif typ is None:
            if val is None:
                self.value_text.insert('1.0', '')
            else:
                self.value_text.insert('1.0', str(val))
        else:
            self.value_text.insert('1.0', str(val))
        self.minus_btn.config(state='normal')

    def add_key(self):
        if self.filename is None:
            messagebox.showinfo("No file", "Create or open a file before adding keys.")
            return
        k = simpledialog.askstring("Add Key", "Enter new preference key name:")
        if not k:
            return
        if k in self.pref_dict:
            messagebox.showerror("Error", "Key already exists.")
            return
        t = simpledialog.askstring("Type", "Enter type (string,boolean,integer,long,float,double,string_set):",
                                   initialvalue=self.known_types.get(k,'string'))
        if not t:
            return
        t = t.strip()
        if t not in ['string','boolean','integer','long','float','double','string_set']:
            messagebox.showerror("Error", "Type not valid.")
            return
        if t == 'string_set':
            v = []
        elif t == 'boolean':
            v = False
        elif t in ('integer','long'):
            v = 0
        elif t in ('float','double'):
            v = 0.0
        else:
            v = ""
        self.pref_dict[k] = (t, v)
        self.refresh_listbox()
        keys = sorted(self.pref_dict.keys())
        idx = keys.index(k)
        self.listbox.selection_clear(0, 'end')
        self.listbox.selection_set(idx)
        self.listbox.see(idx)
        self.on_select()
        self.status_label.config(text=f"Added key: {k}")

    def remove_key(self):
        sel = self.listbox.curselection()
        if not sel:
            messagebox.showerror("Error", "No key selected")
            return
        idx = sel[0]
        entry = self.listbox.get(idx)
        key = entry.split("  [")[0]
        if messagebox.askyesno("Confirm", f"Remove key '{key}'?"):
            self.pref_dict.pop(key, None)
            self.refresh_listbox()
            self.key_var.set('')
            self.type_var.set('')
            self.value_text.config(state='normal')
            self.value_text.delete('1.0','end')
            self.value_text.config(state='disabled')
            self.minus_btn.config(state='disabled')
            self.status_label.config(text=f"Removed key: {key}")

    def apply_to_selected(self):
        key = self.key_var.get().strip()
        if not key:
            messagebox.showerror("Error", "Key cannot be empty")
            return
        t = self.type_var.get().strip()
        if t not in ['string','boolean','integer','long','float','double','string_set']:
            messagebox.showerror("Error", "Invalid type selected")
            return
        raw = self.value_text.get('1.0','end').rstrip('\n')
        try:
            if t == 'string_set':
                vals = [line for line in raw.splitlines() if line.strip()!='']
                v = vals
            elif t == 'boolean':
                lo = raw.strip().lower()
                if lo in ('true','1','yes','y','on'):
                    v = True
                elif lo in ('false','0','no','n','off',''):
                    v = False
                else:
                    raise ValueError("Boolean must be true/false")
            elif t in ('integer','long'):
                v = int(raw.strip() or "0")
            elif t in ('float','double'):
                v = float(raw.strip() or "0")
            else:
                v = raw
        except Exception as e:
            messagebox.showerror("Parse error", f"Could not parse value: {e}")
            return
        self.pref_dict[key] = (t, v)
        self.refresh_listbox()
        self.status_label.config(text=f"Updated key: {key}")
        messagebox.showinfo("Updated", f"Updated key '{key}'")

    def apply_and_save(self):
        self.apply_to_selected()
        self.save_file()

    def save_file(self):
        if not self.filename:
            messagebox.showerror("Error", "No file selected for saving")
            return
        backup = self.filename + ".bak"
        try:
            if not os.path.exists(backup):
                with open(self.filename, 'rb') as f:
                    with open(backup, 'wb') as b:
                        b.write(f.read())
            out = encode_preferences_dict(self.pref_dict)
            with open(self.filename, 'wb') as f:
                f.write(out)
            messagebox.showinfo("Saved", f"Saved to {self.filename} (backup created at {backup})")
            self.status_label.config(text=f"Saved: {self.filename}")
        except Exception as e:
            messagebox.showerror("Error saving", str(e))

    def quit(self):
        if messagebox.askokcancel("Quit", "Quit without saving?"):
            self.root.quit()

    # Enable/disable widgets depending on whether a file is loaded
    def update_widget_states(self, file_loaded: bool):
        state_btn = 'normal' if file_loaded else 'disabled'
        self.plus_btn.config(state=state_btn)
        self.minus_btn.config(state=state_btn if self.listbox.curselection() else 'disabled')
        self.key_entry.config(state='normal' if file_loaded else 'disabled')
        self.type_combo.config(state='readonly' if file_loaded else 'disabled')
        self.value_text.config(state='normal' if file_loaded else 'disabled')
        self.apply_btn.config(state='normal' if file_loaded else 'disabled')
        self.apply_save_btn.config(state='normal' if file_loaded else 'disabled')
        if not file_loaded:
            self.listbox.delete(0, 'end')
            self.file_label.config(text="No file loaded")
            self.status_label.config(text="No file loaded")

def main():
    # Detach Windows console to avoid terminal window (if running with python.exe).
    # If you prefer to keep the console, remove this block or run the script with pythonw.exe / .pyw.
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.kernel32.FreeConsole()
        except Exception:
            pass

    root = tk.Tk()
    app = PrefEditorApp(root)
    root.mainloop()

if __name__ == "__main__":
    main()
