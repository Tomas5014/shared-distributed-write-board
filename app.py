"""
app.py — Interface do Cliente SDWB (Tkinter)
==============================================
Ponto de entrada do cliente. Cada terminal roda este script.

Uso:
    python3 app.py
    (opcionalmente configure SDWB_NS_HOST / SDWB_NS_PORT no ambiente para
     apontar para o Serviço de Nomes, se ele estiver em outra máquina)
"""

import tkinter as tk
from tkinter import simpledialog, messagebox, ttk

from board_state import PALETTE
from node import Node

CANVAS_W, CANVAS_H = 760, 480


class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("SDWB — Shared Distributed Write Board")
        self.root.geometry("900x650")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        name = simpledialog.askstring("Identificação", "Seu nome de usuário:", parent=self.root) or "anon"
        self.node = Node(display_name=name.strip() or "anon")

        self.tool = None
        self.pending_points: list[tuple[int, int]] = []
        self.tool_buttons: dict[str, tk.Button] = {}

        self._build_menu_screen()
        self.root.mainloop()

    # ════════════════════════════════════════════════════════════════════
    # TELA: MENU PRINCIPAL
    # ════════════════════════════════════════════════════════════════════

    def _clear_root(self):
        for w in self.root.winfo_children():
            w.destroy()

    def _build_menu_screen(self):
        self._clear_root()
        self.node.on_board_update = lambda: None
        self.node.on_members_update = lambda: None
        self.node.on_status = lambda m: None
        self.node.on_coordinator_changed = lambda: None
        self.node.on_board_killed = lambda: None

        frame = tk.Frame(self.root)
        frame.pack(expand=True)

        tk.Label(frame, text="SDWB", font=("Helvetica", 32, "bold")).pack(pady=(40, 0))
        tk.Label(frame, text="Shared Distributed Write Board", font=("Helvetica", 12)).pack(pady=(0, 30))

        if self.node.is_coordinator and self.node.board_name:
            tk.Label(frame, text=f"(hospedando '{self.node.board_name}' em segundo plano)",
                     fg="#555").pack(pady=(0, 10))

        tk.Button(frame, text="CRIAR NOVO QUADRO", font=("Helvetica", 14), width=28, height=2,
                  command=self._on_create_clicked).pack(pady=10)
        tk.Button(frame, text="INGRESSAR EM QUADRO EXISTENTE", font=("Helvetica", 14), width=28, height=2,
                  command=self._on_join_clicked).pack(pady=10)

        tk.Label(frame, text=f"meu endereço: {self.node.my_ip}:{self.node.my_port}",
                 fg="#888", font=("Helvetica", 9)).pack(pady=(30, 0))

    def _on_create_clicked(self):
        name = simpledialog.askstring("Criar Quadro", "Nome do novo quadro:", parent=self.root)
        if not name:
            return
        ok, err = self.node.create_board(name)
        if not ok:
            messagebox.showerror("Erro ao criar quadro", err)
            return
        self._build_board_screen()

    def _on_join_clicked(self):
        boards = self.node.list_boards()
        if not boards:
            messagebox.showinfo("Quadros disponíveis", "Nenhum quadro disponível no momento.")
            return
        chosen = self._pick_board_dialog(boards)
        if chosen is None:
            return
        ok, err = self.node.join_board(chosen["name"], chosen["ip"], chosen["port"])
        if not ok:
            messagebox.showerror("Erro ao ingressar", err)
            return
        self._build_board_screen()

    def _pick_board_dialog(self, boards: list[dict]):
        win = tk.Toplevel(self.root)
        win.title("Quadros disponíveis")
        win.geometry("420x320")
        win.grab_set()

        tk.Label(win, text="Escolha um quadro para ingressar:", font=("Helvetica", 11)).pack(pady=8)
        listbox = tk.Listbox(win, font=("Helvetica", 11))
        listbox.pack(fill="both", expand=True, padx=10, pady=5)
        for b in boards:
            listbox.insert(tk.END, f"{b['name']}   ({b['ip']}:{b['port']})")

        result = {"value": None}

        def confirm():
            sel = listbox.curselection()
            if sel:
                result["value"] = boards[sel[0]]
            win.destroy()

        btn_frame = tk.Frame(win)
        btn_frame.pack(pady=8)
        tk.Button(btn_frame, text="Ingressar", width=12, command=confirm).pack(side="left", padx=5)
        tk.Button(btn_frame, text="Cancelar", width=12, command=win.destroy).pack(side="left", padx=5)
        listbox.bind("<Double-Button-1>", lambda e: confirm())

        self.root.wait_window(win)
        return result["value"]

    # ════════════════════════════════════════════════════════════════════
    # TELA: QUADRO
    # ════════════════════════════════════════════════════════════════════

    def _build_board_screen(self):
        self._clear_root()
        self.tool = None
        self.pending_points = []

        # ---- callbacks de rede -> thread principal da GUI ----
        self.node.on_board_update = lambda: self.root.after(0, self._redraw)
        self.node.on_members_update = lambda: self.root.after(0, self._refresh_members)
        self.node.on_status = lambda m: self.root.after(0, lambda msg=m: self._set_status(msg))
        self.node.on_coordinator_changed = lambda: self.root.after(0, self._refresh_members)
        self.node.on_board_killed = lambda: self.root.after(0, self._forced_back_to_menu)

        top = tk.Frame(self.root)
        top.pack(fill="x", pady=4)
        tk.Label(top, text=f"Quadro: {self.node.board_name}", font=("Helvetica", 14, "bold")).pack(side="left", padx=10)
        tk.Button(top, text="Sair do Quadro", command=self._on_leave).pack(side="right", padx=10)

        toolbar = tk.Frame(self.root)
        toolbar.pack(fill="x", pady=2)

        self.tool_buttons = {}
        self._add_tool_button(toolbar, "Linha", "LINE")
        self._add_tool_button(toolbar, "Quadrado", "SQUARE")
        self._add_tool_button(toolbar, "Selecionar", "SELECT")
        tk.Button(toolbar, text="Colorir (cor 1)", bg=PALETTE[0], fg="white",
                  command=lambda: self._on_color(PALETTE[0])).pack(side="left", padx=4)
        tk.Button(toolbar, text="Colorir (cor 2)", bg=PALETTE[1], fg="white",
                  command=lambda: self._on_color(PALETTE[1])).pack(side="left", padx=4)
        tk.Button(toolbar, text="Remover", command=self._on_remove).pack(side="left", padx=4)

        body = tk.Frame(self.root)
        body.pack(fill="both", expand=True, padx=8, pady=4)

        self.canvas = tk.Canvas(body, width=CANVAS_W, height=CANVAS_H, bg="white",
                                 highlightthickness=1, highlightbackground="#ccc")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.bind("<Button-1>", self._on_canvas_click)

        side = tk.Frame(body, width=180)
        side.pack(side="right", fill="y", padx=(8, 0))
        tk.Label(side, text="Participantes", font=("Helvetica", 11, "bold")).pack(anchor="w")
        self.members_list = tk.Listbox(side, font=("Helvetica", 10))
        self.members_list.pack(fill="both", expand=True)

        self.status_var = tk.StringVar(value="Pronto.")
        tk.Label(self.root, textvariable=self.status_var, anchor="w", fg="#333",
                 bd=1, relief="sunken").pack(fill="x", side="bottom")

        self._redraw()
        self._refresh_members()

    def _add_tool_button(self, parent, label, tool_key):
        btn = tk.Button(parent, text=label, command=lambda: self._select_tool(tool_key))
        btn.pack(side="left", padx=4)
        self.tool_buttons[tool_key] = btn

    def _select_tool(self, tool_key):
        self.tool = tool_key
        self.pending_points = []
        for key, btn in self.tool_buttons.items():
            btn.config(relief="sunken" if key == tool_key else "raised")
        self._set_status(f"Ferramenta selecionada: {tool_key}. "
                          + ("Clique 2 pontos no quadro." if tool_key in ("LINE", "SQUARE")
                             else "Clique em um objeto para selecioná-lo."))

    def _on_canvas_click(self, event):
        x, y = event.x, event.y
        if self.tool in ("LINE", "SQUARE"):
            self.pending_points.append((x, y))
            if len(self.pending_points) == 1:
                self._set_status("Primeiro ponto marcado. Clique no segundo ponto.")
            elif len(self.pending_points) == 2:
                self.node.do_action(self.tool, {"points": self.pending_points})
                self.pending_points = []
        elif self.tool == "SELECT":
            oid = self.node.board.hit_test(x, y)
            if oid is None:
                self._set_status("Nenhum objeto neste ponto.")
            else:
                self.node.do_action("SELECT", {"object_id": oid})
        else:
            self._set_status("Escolha uma ferramenta primeiro (Linha, Quadrado ou Selecionar).")

    def _current_selected_object(self):
        if self.node.client_id is None:
            return None
        my_id = int(self.node.client_id)
        for oid, obj in self.node.board.objects.items():
            if obj.get("selected_by") == my_id:
                return oid
        return None

    def _on_color(self, color_hex):
        oid = self._current_selected_object()
        if oid is None:
            messagebox.showinfo("Colorir", "Selecione um objeto primeiro (botão Selecionar).")
            return
        self.node.do_action("COLOR", {"object_id": oid, "color": color_hex})

    def _on_remove(self):
        oid = self._current_selected_object()
        if oid is None:
            messagebox.showinfo("Remover", "Selecione um objeto primeiro (botão Selecionar).")
            return
        self.node.do_action("REMOVE", {"object_id": oid})

    def _on_leave(self):
        self.node.leave_board()
        self._build_menu_screen()

    def _forced_back_to_menu(self):
        messagebox.showinfo("Quadro encerrado", "Este quadro foi encerrado (sem mais participantes).")
        self._build_menu_screen()

    # ---- redesenho / sincronização ----

    def _redraw(self):
        if not hasattr(self, "canvas"):
            return
        self.canvas.delete("all")
        my_id = int(self.node.client_id) if self.node.client_id is not None else None
        for oid, obj in self.node.board.objects.items():
            (x1, y1), (x2, y2) = obj["points"]
            color = obj["color"]
            sel_by = obj.get("selected_by")
            if obj["kind"] == "line":
                self.canvas.create_line(x1, y1, x2, y2, fill=color, width=3)
            else:
                self.canvas.create_rectangle(x1, y1, x2, y2, outline=color, width=3)
            if sel_by is not None:
                hl = "#2ecc71" if sel_by == my_id else "#f39c12"
                left, right = sorted([x1, x2])
                top, bottom = sorted([y1, y2])
                pad = 4
                self.canvas.create_rectangle(left - pad, top - pad, right + pad, bottom + pad,
                                              outline=hl, width=2, dash=(4, 2))

    def _refresh_members(self):
        if not hasattr(self, "members_list"):
            return
        self.members_list.delete(0, tk.END)
        for cid, info in sorted(self.node.members.items(), key=lambda kv: int(kv[0])):
            is_coord = (info["ip"] == self.node.coord_ip and info["port"] == self.node.coord_port)
            is_me = (cid == self.node.client_id)
            label = f"#{cid} {info.get('name', 'anon')}"
            if is_coord:
                label += "  [coordenador]"
            if is_me:
                label += "  (eu)"
            self.members_list.insert(tk.END, label)

    def _set_status(self, msg: str):
        if hasattr(self, "status_var"):
            self.status_var.set(msg)

    # ════════════════════════════════════════════════════════════════════

    def _on_close(self):
        try:
            self.node.shutdown()
        except Exception:
            pass
        self.root.destroy()


if __name__ == "__main__":
    App()
