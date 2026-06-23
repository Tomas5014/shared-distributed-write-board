"""
board_state.py — Estado do Quadro Branco (replicado em coordenador + clientes)
================================================================================
Cada objeto do quadro é uma linha ou um quadrado, identificado por um id único.
`validate()` é uma checagem somente-leitura (usada na fase de PREPARE do 2PC e
para a checagem de exclusão mútua no coordenador). `apply()` muda o estado e é
chamado depois que a transação foi confirmada (TX_COMMIT), tanto no coordenador
quanto em cada réplica cliente.
"""

from typing import Optional

COLOR_DEFAULT = "#1e1e1e"
PALETTE = ["#e63946", "#1d4ed8"]  # as duas cores disponíveis para "colorir"


class BoardState:
    def __init__(self):
        self.objects: dict[str, dict] = {}   # id -> {kind, points, color, selected_by}
        self._seq = 0

    # ───────────────────────────── serialização ─────────────────────────────

    def to_dict(self) -> dict:
        return {"objects": self.objects, "seq": self._seq}

    @classmethod
    def from_dict(cls, data: dict) -> "BoardState":
        bs = cls()
        bs.objects = data.get("objects", {})
        bs._seq = data.get("seq", 0)
        return bs

    # ───────────────────────────── geometria ─────────────────────────────

    @staticmethod
    def _normalize_square(p1, p2):
        """Recebe dois pontos clicados e devolve dois cantos opostos de um
        quadrado de verdade (lados iguais), preservando a direção do clique."""
        x1, y1 = p1
        x2, y2 = p2
        dx, dy = x2 - x1, y2 - y1
        side = max(abs(dx), abs(dy)) or 1
        sx = 1 if dx >= 0 else -1
        sy = 1 if dy >= 0 else -1
        return [x1, y1], [x1 + side * sx, y1 + side * sy]

    # ─────────────────────────── validação (somente leitura) ───────────────

    def validate(self, action: str, payload: dict, client_id: int) -> tuple[bool, Optional[str]]:
        """Checagem de exclusão mútua / pré-condições. Não muda o estado."""
        if action in ("LINE", "SQUARE"):
            pts = payload.get("points")
            if not pts or len(pts) != 2:
                return False, "pontos inválidos para criação de objeto"
            return True, None

        obj_id = payload.get("object_id")
        obj = self.objects.get(obj_id)
        if obj is None:
            return False, f"objeto {obj_id} não existe"

        if action == "SELECT":
            if obj["selected_by"] is not None and obj["selected_by"] != client_id:
                return False, "objeto já selecionado por outro cliente"
            return True, None

        if action == "DESELECT":
            if obj["selected_by"] != client_id:
                return False, "você não selecionou este objeto"
            return True, None

        if action in ("COLOR", "REMOVE"):
            if obj["selected_by"] != client_id:
                return False, "selecione o objeto antes de colorir/remover"
            return True, None

        return False, f"ação desconhecida: {action}"

    # ─────────────────────────────── aplicação ───────────────────────────────

    def apply(self, action: str, payload: dict, client_id: int) -> dict:
        """Aplica a ação já validada/confirmada. Retorna um resumo do efeito
        (usado para log / depuração); o estado em si é mutado in-place."""
        if action == "LINE":
            self._seq += 1
            oid = f"obj-{self._seq}"
            self.objects[oid] = {
                "kind": "line",
                "points": payload["points"],
                "color": COLOR_DEFAULT,
                "selected_by": None,
            }
            return {"object_id": oid, "object": self.objects[oid]}

        if action == "SQUARE":
            self._seq += 1
            oid = f"obj-{self._seq}"
            p1, p2 = self._normalize_square(*payload["points"])
            self.objects[oid] = {
                "kind": "square",
                "points": [p1, p2],
                "color": COLOR_DEFAULT,
                "selected_by": None,
            }
            return {"object_id": oid, "object": self.objects[oid]}

        obj_id = payload.get("object_id")
        obj = self.objects.get(obj_id)
        if obj is None:
            return {"object_id": obj_id, "error": "objeto não existe mais"}

        if action == "SELECT":
            obj["selected_by"] = client_id
            return {"object_id": obj_id, "object": obj}

        if action == "DESELECT":
            obj["selected_by"] = None
            return {"object_id": obj_id, "object": obj}

        if action == "COLOR":
            obj["color"] = payload["color"]
            return {"object_id": obj_id, "object": obj}

        if action == "REMOVE":
            del self.objects[obj_id]
            return {"object_id": obj_id, "removed": True}

        return {"error": f"ação desconhecida: {action}"}

    # ─────────────────────────── utilitário de hit-test ───────────────────────────

    def hit_test(self, x: float, y: float, tolerance: float = 6.0) -> Optional[str]:
        """Encontra o objeto (mais recente primeiro) sob o ponto (x, y)."""
        for oid in reversed(list(self.objects.keys())):
            obj = self.objects[oid]
            (x1, y1), (x2, y2) = obj["points"]
            if obj["kind"] == "square":
                left, right = sorted([x1, x2])
                top, bottom = sorted([y1, y2])
                if left - tolerance <= x <= right + tolerance and top - tolerance <= y <= bottom + tolerance:
                    return oid
            else:  # line: distância do ponto ao segmento
                if _dist_point_segment(x, y, x1, y1, x2, y2) <= tolerance:
                    return oid
        return None


def _dist_point_segment(px, py, x1, y1, x2, y2) -> float:
    dx, dy = x2 - x1, y2 - y1
    if dx == 0 and dy == 0:
        return ((px - x1) ** 2 + (py - y1) ** 2) ** 0.5
    t = max(0, min(1, ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)))
    cx, cy = x1 + t * dx, y1 + t * dy
    return ((px - cx) ** 2 + (py - cy) ** 2) ** 0.5
