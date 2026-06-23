"""
name_service.py — Serviço de Nomes (Páginas Amarelas do SDWB)
================================================================
Processo separado e independente. É o ÚNICO componente com IP/porta fixos.
Mantém SOMENTE a tabela (NomeDoQuadro, IP, Porta) do Coordenador atual de
cada quadro. Não guarda nem precisa saber nada sobre o conteúdo dos quadros.

Premissa do trabalho: o Serviço de Nomes nunca falha. Para reforçar a
resiliência dos quadros mesmo assim, ele faz uma varredura periódica
(sweep) testando se o coordenador registrado de cada quadro ainda responde;
se um quadro ficar "órfão" (coordenador caiu e estava sozinho, sem ninguém
para detectar e reportar a falha) ele é removido da tabela depois de
algumas tentativas, evitando lixo acumulado.

Uso:
    python3 name_service.py [porta]
"""

import socket
import sys
import threading
import time

from protocol import (
    NS_HOST, NS_PORT, OK, ERROR_MSG, NS_PROBE,
    REGISTER_BOARD, LIST_BOARDS, UPDATE_BOARD, REMOVE_BOARD,
    send_msg, recv_msg, one_shot,
)

SWEEP_INTERVAL = 15      # segundos entre varreduras de boards órfãos
SWEEP_FAILS_TO_REMOVE = 2


class NameService:
    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self.lock = threading.Lock()
        # nome_do_quadro -> {"ip":..., "port":..., "fails": 0}
        self.boards: dict[str, dict] = {}

    # ───────────────────────────── servidor ─────────────────────────────

    def start(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.host, self.port))
        srv.listen(64)
        print(f"[NameService] ouvindo em {self.host}:{self.port}  (endereço FIXO do sistema)")

        threading.Thread(target=self._sweep_loop, daemon=True).start()

        while True:
            conn, addr = srv.accept()
            threading.Thread(target=self._handle, args=(conn, addr), daemon=True).start()

    def _handle(self, conn: socket.socket, addr):
        try:
            msg = recv_msg(conn)
            if msg is None:
                return
            reply = self._dispatch(msg)
            send_msg(conn, reply)
        except Exception as e:
            try:
                send_msg(conn, {"type": ERROR_MSG, "error": str(e)})
            except Exception:
                pass
        finally:
            conn.close()

    def _dispatch(self, msg: dict) -> dict:
        mtype = msg.get("type")

        if mtype == REGISTER_BOARD:
            name, ip, port = msg["name"], msg["ip"], msg["port"]
            with self.lock:
                if name in self.boards:
                    return {"type": ERROR_MSG, "error": f"quadro '{name}' já existe"}
                self.boards[name] = {"ip": ip, "port": port, "fails": 0}
            print(f"[NameService] quadro registrado: {name} -> {ip}:{port}")
            return {"type": OK}

        if mtype == UPDATE_BOARD:
            # usado pelo novo coordenador eleito após uma falha, para atualizar seu endereço
            name, ip, port = msg["name"], msg["ip"], msg["port"]
            with self.lock:
                self.boards[name] = {"ip": ip, "port": port, "fails": 0}
            print(f"[NameService] quadro atualizado (nova eleição): {name} -> {ip}:{port}")
            return {"type": OK}

        if mtype == REMOVE_BOARD:
            name = msg["name"]
            with self.lock:
                self.boards.pop(name, None)
            print(f"[NameService] quadro removido: {name}")
            return {"type": OK}

        if mtype == LIST_BOARDS:
            with self.lock:
                boards = [{"name": n, "ip": b["ip"], "port": b["port"]}
                          for n, b in self.boards.items()]
            return {"type": OK, "boards": boards}

        return {"type": ERROR_MSG, "error": f"tipo de mensagem desconhecido: {mtype}"}

    # ─────────────────────────── varredura de órfãos ───────────────────────────

    def _sweep_loop(self):
        while True:
            time.sleep(SWEEP_INTERVAL)
            with self.lock:
                items = list(self.boards.items())
            for name, info in items:
                reply = one_shot(info["ip"], info["port"], {"type": NS_PROBE}, timeout=3.0)
                with self.lock:
                    if name not in self.boards:
                        continue
                    if reply is None:
                        self.boards[name]["fails"] += 1
                        if self.boards[name]["fails"] >= SWEEP_FAILS_TO_REMOVE:
                            print(f"[NameService] quadro órfão removido por inatividade: {name}")
                            del self.boards[name]
                    else:
                        self.boards[name]["fails"] = 0


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else NS_PORT
    NameService(NS_HOST if NS_HOST else "0.0.0.0", port).start()
