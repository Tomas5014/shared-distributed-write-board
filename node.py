"""
node.py — Núcleo de um nó do SDWB
===================================
Cada processo cliente do SDWB é um "nó" que pode, a qualquer momento, estar
desempenhando o papel de Coordenador de um quadro (se o criou ou venceu uma
eleição) além de SEMPRE poder agir como cliente comum de um quadro (inclusive
o próprio quadro que ele hospeda — "o computador onde o coordenador está
rodando é só mais um cliente").

Esta classe não depende de Tkinter; ela expõe callbacks (`on_board_update`,
`on_members_update`, `on_status`) que a camada de interface (app.py) registra
para atualizar a tela. Isso mantém a lógica de rede/distribuição testável e
separada da interface, como pede o enunciado (1.1 — "interface é do cliente").
"""

import socket
import threading
import time
import uuid
from typing import Callable, Optional

from board_state import BoardState
import protocol as P


def _safe_close(sock: Optional[socket.socket]) -> None:
    """Fecha um socket de forma que acorde de forma confiável qualquer outra
    thread bloqueada em recv() nele. Em Linux, close() sozinho NÃO garante
    isso (é um problema clássico de sockets multithread); shutdown() antes
    de close() resolve, fazendo o recv() bloqueado retornar imediatamente
    com EOF (0 bytes)."""
    if sock is None:
        return
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


class Node:
    def __init__(self, display_name: str = "anon"):
        self.display_name = display_name
        self.my_ip = P.local_ip()
        self.my_port = P.free_port()

        # ---- papel de CLIENTE (participante de um quadro) ----
        self.client_id: Optional[str] = None
        self.coord_ip: Optional[str] = None
        self.coord_port: Optional[int] = None
        self.sock: Optional[socket.socket] = None
        self.sock_lock = threading.Lock()
        self.board = BoardState()          # réplica local do estado (visão de cliente, usada pela GUI)
        self.members: dict[str, dict] = {} # réplica local da tabela de membros (lida pela GUI, eleição)
        self._intentional_disconnect = False
        self._got_new_coordinator = threading.Event()

        # ---- papel de COORDENADOR (se este nó hospeda um quadro) ----
        self.is_coordinator = False
        self.coord_board = BoardState()           # estado CANÔNICO (só tocado pela lógica de 2PC)
        self.coord_members: dict[str, dict] = {}  # tabela AUTORITATIVA de membros (só tocada pelo coordenador)
        self.board_name: Optional[str] = None
        self.next_client_id = 1
        self.conns: dict[str, socket.socket] = {}
        self.conn_locks: dict[str, threading.Lock] = {}
        self.last_ack: dict[str, float] = {}
        self.tx_lock = threading.RLock()            # serializa ACTION_REQUEST -> exclusão mútua total
        self.members_lock = threading.RLock()
        self.pending_tx: dict[str, dict] = {}

        # ---- eleição ----
        self.election_lock = threading.Lock()
        self.election_in_progress = False

        self.running = True
        self._listener_sock: Optional[socket.socket] = None
        self._reconnect_lock = threading.Lock()  # previne reconexões concorrentes

        # ---- callbacks para a UI ----
        self.on_board_update: Callable[[], None] = lambda: None
        self.on_members_update: Callable[[], None] = lambda: None
        self.on_status: Callable[[str], None] = lambda msg: None
        self.on_coordinator_changed: Callable[[], None] = lambda: None
        self.on_board_killed: Callable[[], None] = lambda: None

        threading.Thread(target=self._listener_loop, daemon=True).start()

    # ════════════════════════════════════════════════════════════════════
    # API pública usada pela GUI
    # ════════════════════════════════════════════════════════════════════

    def list_boards(self) -> list[dict]:
        reply = P.one_shot(P.NS_HOST, P.NS_PORT, {"type": P.LIST_BOARDS})
        if reply and reply.get("type") == P.OK:
            return reply["boards"]
        return []

    def create_board(self, name: str) -> tuple[bool, str]:
        if not name or not name.strip():
            return False, "nome do quadro não pode ser vazio"
        name = name.strip()
        self.board_name = name
        self.is_coordinator = True
        with self.members_lock:
            self.coord_members = {}
            self.conns = {}
            self.conn_locks = {}
            self.last_ack = {}
        self.next_client_id = 1
        self.coord_board = BoardState()

        reply = P.one_shot(P.NS_HOST, P.NS_PORT,
                            {"type": P.REGISTER_BOARD, "name": name,
                             "ip": self.my_ip, "port": self.my_port})
        if reply is None or reply.get("type") != P.OK:
            self.is_coordinator = False
            self.board_name = None
            err = reply.get("error") if reply else "Serviço de Nomes não respondeu"
            return False, err

        self.coord_ip, self.coord_port = self.my_ip, self.my_port
        threading.Thread(target=self._coord_heartbeat_loop, daemon=True).start()

        self.client_id = None
        ok, err = self._reconnect_to_coordinator()
        return ok, err

    def join_board(self, name: str, ip: str, port: int) -> tuple[bool, str]:
        self.is_coordinator = False
        self.board_name = name
        self.coord_ip, self.coord_port = ip, port
        self.client_id = None
        ok, err = self._reconnect_to_coordinator()
        return ok, err

    def leave_board(self):
        self._intentional_disconnect = True
        try:
            self._client_send({"type": P.LEAVE})
        except Exception:
            pass
        # Importante: NÃO fechamos self.sock aqui imediatamente. Se houver
        # qualquer mensagem ainda não lida no buffer de entrada (ex.: um
        # HEARTBEAT que chegou um instante antes), fechar agora poderia fazer
        # o kernel mandar um RST em vez de um FIN limpo, arriscando perder o
        # LEAVE que acabamos de enviar. Em vez disso, só soltamos nossa
        # referência: o coordenador, ao processar o LEAVE, fecha a conexão
        # do lado dele (_coord_remove_client); nossa própria reader loop
        # então vê EOF naturalmente e termina sozinha (sem disparar eleição,
        # graças a _intentional_disconnect e a este self.sock já ser None).
        with self.sock_lock:
            self.sock = None
        self.client_id = None
        self.board = BoardState()
        self.members = {}
        if not self.is_coordinator:
            self.board_name = None

    def do_action(self, action: str, payload: dict):
        """Envia uma transação de desenho/seleção/cor/remoção ao coordenador."""
        try:
            self._client_send({"type": P.ACTION_REQUEST, "action": action, "payload": payload})
        except Exception as e:
            self.on_status(f"Falha ao enviar ação: {e}")

    def shutdown(self):
        self.running = False
        try:
            self.leave_board()
        except Exception:
            pass

    def simulate_crash(self):
        """Fins de TESTE/DEMONSTRAÇÃO: simula a queda abrupta deste processo
        (ex.: 'desligar o PC'), sem enviar LEAVE — usado para o cenário de
        teste obrigatório 'Morte do Coordenador'. Os outros nós devem
        detectar via heartbeat e disparar a eleição do Valentão."""
        self.running = False
        if self._listener_sock:
            _safe_close(self._listener_sock)
        with self.sock_lock:
            if self.sock:
                _safe_close(self.sock)
        for conn in list(self.conns.values()):
            _safe_close(conn)

    # ════════════════════════════════════════════════════════════════════
    # Listener unificado: recebe JOINs (se sou coordenador), mensagens de
    # eleição (sempre) e sondagens do Serviço de Nomes (sempre)
    # ════════════════════════════════════════════════════════════════════

    def _listener_loop(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", self.my_port))
        srv.listen(64)
        self._listener_sock = srv
        while self.running:
            try:
                conn, addr = srv.accept()
            except OSError:
                break
            threading.Thread(target=self._handle_incoming, args=(conn, addr), daemon=True).start()

    def _handle_incoming(self, conn: socket.socket, addr):
        msg = P.recv_msg(conn)
        if msg is None:
            conn.close()
            return
        mtype = msg.get("type")

        if mtype == P.NS_PROBE:
            P.send_msg(conn, {"type": P.OK})
            conn.close()
            return

        if mtype == P.ELECTION:
            reply = {"type": P.ELECTION_OK}
            if self.is_coordinator:
                # Resiliência extra: se eu já sou o coordenador (por exemplo,
                # o anúncio COORDINATOR_WIN que mandei se perdeu), já aviso
                # aqui mesmo, na resposta da eleição, em vez de depender só
                # do broadcast fire-and-forget.
                reply["coordinator_ip"] = self.my_ip
                reply["coordinator_port"] = self.my_port
            P.send_msg(conn, reply)
            conn.close()
            self._start_election()  # eleição em cascata: também disputo contra IDs maiores
            return

        if mtype == P.COORDINATOR_WIN:
            # Responde com ACK imediatamente (antes de processar) para que o
            # coordenador saiba que a mensagem foi entregue.
            P.send_msg(conn, {"type": P.COORDINATOR_ACK})
            conn.close()
            self._handle_coordinator_win(msg)
            return

        if mtype == P.JOIN:
            if not self.is_coordinator:
                P.send_msg(conn, {"type": P.ERROR_MSG, "error": "este nó não é o coordenador"})
                conn.close()
                return
            self._coord_handle_join(conn, msg)
            return

        conn.close()

    # ════════════════════════════════════════════════════════════════════
    # PAPEL DE COORDENADOR
    # ════════════════════════════════════════════════════════════════════

    def _coord_handle_join(self, conn: socket.socket, msg: dict):
        ip, port, name = msg["ip"], msg["port"], msg.get("name", "anon")
        existing_id = msg.get("client_id")
        with self.members_lock:
            if existing_id is not None and str(existing_id) in self.coord_members:
                cid = str(existing_id)            # reconexão (pós-eleição ou queda de socket)
            else:
                cid = str(self.next_client_id)
                self.next_client_id += 1
            self.coord_members[cid] = {"ip": ip, "port": port, "name": name}
            self.conns[cid] = conn
            self.conn_locks[cid] = threading.Lock()
            self.last_ack[cid] = time.time()
            members_snapshot = dict(self.coord_members)

        P.send_msg(conn, {
            "type": P.STATE_SYNC, "client_id": int(cid),
            "board": self.coord_board.to_dict(), "members": members_snapshot,
        })
        self._coord_broadcast({"type": P.CLIENT_JOINED, "members": members_snapshot}, exclude=cid)
        self.on_members_update()
        self._coord_client_reader_loop(cid, conn)

    def _coord_client_reader_loop(self, cid: str, conn: socket.socket):
        while self.running and self.is_coordinator:
            msg = P.recv_msg(conn)
            if msg is None:
                break
            mtype = msg.get("type")
            if mtype == P.ACTION_REQUEST:
                # IMPORTANTE: processar em thread própria, sem bloquear este loop.
                # _coord_handle_action_request pode ficar até VOTE_TIMEOUT segundos
                # esperando votos de OUTROS clientes — inclusive deste, para
                # transações de terceiros. Se processássemos aqui (na mesma thread
                # que lê as respostas de voto desta conexão), uma ação própria do
                # cliente travaria a leitura do voto dele para a transação de outro
                # cliente, gerando um deadlock de "head-of-line blocking".
                threading.Thread(target=self._coord_handle_action_request,
                                  args=(cid, msg), daemon=True).start()
            elif mtype == P.HEARTBEAT_ACK:
                self.last_ack[cid] = time.time()
            elif mtype == P.VOTE_COMMIT or mtype == P.VOTE_ABORT:
                self._coord_record_vote(msg["tx_id"], cid, mtype)
            elif mtype == P.LEAVE:
                break
        # Só limpa e notifica os outros se ainda estamos em operação normal.
        # Se self.running == False significa que é um simulate_crash() / shutdown()
        # — nesse caso NÃO devemos chamar _coord_remove_client, pois isso
        # acionaria _kill_hosted_board e removeria o quadro do Serviço de Nomes
        # ANTES que os demais nós detectem a falha e façam a eleição do novo
        # coordenador.  O NS ficaria em branco e o quadro seria perdido.
        if self.running:
            self._coord_remove_client(cid)

    def _coord_send(self, cid: str, msg: dict) -> bool:
        conn = self.conns.get(cid)
        lock = self.conn_locks.get(cid)
        if conn is None or lock is None:
            return False
        try:
            with lock:
                P.send_msg(conn, msg)
            return True
        except Exception:
            return False

    def _coord_broadcast(self, msg: dict, exclude: Optional[str] = None):
        for cid in list(self.conns.keys()):
            if cid == exclude:
                continue
            self._coord_send(cid, msg)

    def _coord_remove_client(self, cid: str):
        with self.members_lock:
            existed = cid in self.coord_members
            self.coord_members.pop(cid, None)
            conn = self.conns.pop(cid, None)
            self.conn_locks.pop(cid, None)
            self.last_ack.pop(cid, None)
            remaining = len(self.coord_members)
            members_snapshot = dict(self.coord_members)
        if not existed:
            return
        _safe_close(conn)
        self._coord_broadcast({"type": P.CLIENT_LEFT, "members": members_snapshot})
        self.on_members_update()
        if remaining == 0:
            self._kill_hosted_board()

    def _kill_hosted_board(self):
        """Regra das anotações: coordenador sozinho que sai/cai mata o quadro."""
        name = self.board_name
        if name is None:
            return
        P.one_shot(P.NS_HOST, P.NS_PORT, {"type": P.REMOVE_BOARD, "name": name})
        self.is_coordinator = False
        self.board_name = None
        self.coord_board = BoardState()
        self.on_status(f"Quadro '{name}' encerrado (ficou sem participantes).")
        self.on_board_killed()

    def _coord_heartbeat_loop(self):
        while self.running and self.is_coordinator:
            time.sleep(P.HEARTBEAT_INTERVAL)
            if not self.is_coordinator:
                break
            with self.members_lock:
                cids = list(self.coord_members.keys())
            now = time.time()
            for cid in cids:
                self._coord_send(cid, {"type": P.HEARTBEAT})
            for cid in cids:
                last = self.last_ack.get(cid, now)
                if now - last > P.HEARTBEAT_TIMEOUT:
                    self.on_status(f"Cliente {cid} não respondeu a tempo — removendo.")
                    self._coord_remove_client(cid)

    # ---- 2PC (Commit em Duas Fases) ----

    def _coord_handle_action_request(self, cid: str, msg: dict):
        action, payload = msg["action"], msg["payload"]
        with self.tx_lock:   # garante ordenação total -> exclusão mútua entre transações concorrentes
            ok, err = self.coord_board.validate(action, payload, int(cid))
            if not ok:
                self._coord_send(cid, {"type": P.ERROR_MSG, "error": err})
                return

            tx_id = str(uuid.uuid4())
            with self.members_lock:
                participants = [c for c in self.coord_members.keys() if c != cid]
            event = threading.Event()
            self.pending_tx[tx_id] = {"event": event, "votes": {}, "expected": set(participants)}

            # ---- Fase 1: PREPARE ----
            for pcid in participants:
                self._coord_send(pcid, {
                    "type": P.PREPARE, "tx_id": tx_id, "action": action,
                    "payload": payload, "client_id": int(cid),
                })
            if participants:
                event.wait(P.VOTE_TIMEOUT)

            entry = self.pending_tx.pop(tx_id, {"votes": {}, "expected": set(participants)})
            votes, expected = entry["votes"], entry["expected"]
            aborted = (any(v == P.VOTE_ABORT for v in votes.values())
                       or len(votes) < len(expected))

            if aborted:
                reason = "conflito detectado durante a votação" if votes else "participante não respondeu a tempo"
                self._coord_send(cid, {"type": P.TX_ABORT, "tx_id": tx_id, "reason": reason})
                return

            # ---- Fase 2: COMMIT ----
            # Aplica somente ao estado CANÔNICO (coord_board). A réplica de cliente
            # deste mesmo nó (self.board, usada pela GUI) só é atualizada quando o
            # TX_COMMIT chega pela conexão de loopback, em _client_reader_loop —
            # exatamente o mesmo caminho usado por todos os outros clientes. Isso
            # evita aplicar a ação duas vezes no nó que hospeda o coordenador.
            result = self.coord_board.apply(action, payload, int(cid))
            commit_msg = {"type": P.TX_COMMIT, "tx_id": tx_id, "action": action,
                          "payload": payload, "client_id": int(cid), "result": result}
            self._coord_send(cid, commit_msg)
            self._coord_broadcast(commit_msg, exclude=cid)

    def _coord_record_vote(self, tx_id: str, cid: str, vote: str):
        entry = self.pending_tx.get(tx_id)
        if entry is None:
            return
        entry["votes"][cid] = vote
        if len(entry["votes"]) >= len(entry["expected"]):
            entry["event"].set()

    # ════════════════════════════════════════════════════════════════════
    # PAPEL DE CLIENTE
    # ════════════════════════════════════════════════════════════════════

    def _client_send(self, msg: dict):
        with self.sock_lock:
            if self.sock is None:
                raise ConnectionError("sem conexão com o coordenador")
            P.send_msg(self.sock, msg)

    def _reconnect_to_coordinator(self) -> tuple[bool, str]:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(5.0)
            s.connect((self.coord_ip, self.coord_port))
            P.send_msg(s, {
                "type": P.JOIN,
                "client_id": int(self.client_id) if self.client_id is not None else None,
                "ip": self.my_ip, "port": self.my_port, "name": self.display_name,
            })
            reply = P.recv_msg(s)
            if reply is None or reply.get("type") != P.STATE_SYNC:
                err = reply.get("error") if reply else "sem resposta do coordenador"
                s.close()
                return False, err
            s.settimeout(None)
        except Exception as e:
            return False, str(e)

        with self.sock_lock:
            old = self.sock
            self.sock = s
        _safe_close(old)

        self.client_id = str(reply["client_id"])
        self.board = BoardState.from_dict(reply["board"])
        self.members = reply["members"]
        self._intentional_disconnect = False
        threading.Thread(target=self._client_reader_loop, daemon=True).start()
        self.on_board_update()
        self.on_members_update()
        self.on_status(f"Conectado ao coordenador em {self.coord_ip}:{self.coord_port}")
        return True, ""

    def _client_reader_loop(self):
        sock = self.sock
        while self.running:
            msg = P.recv_msg(sock)
            if msg is None:
                break
            mtype = msg.get("type")

            if mtype == P.HEARTBEAT:
                try:
                    self._client_send({"type": P.HEARTBEAT_ACK})
                except Exception:
                    break

            elif mtype == P.PREPARE:
                ok, _err = self.board.validate(msg["action"], msg["payload"], msg["client_id"])
                vote = P.VOTE_COMMIT if ok else P.VOTE_ABORT
                try:
                    self._client_send({"type": vote, "tx_id": msg["tx_id"]})
                except Exception:
                    break

            elif mtype == P.TX_COMMIT:
                self.board.apply(msg["action"], msg["payload"], msg["client_id"])
                self.on_board_update()

            elif mtype == P.TX_ABORT:
                self.on_status(f"Ação rejeitada: {msg.get('reason', '')}")

            elif mtype in (P.CLIENT_JOINED, P.CLIENT_LEFT):
                self.members = msg["members"]
                self.on_members_update()

            elif mtype == P.ERROR_MSG:
                self.on_status(f"Erro: {msg.get('error', '')}")

        # conexão encerrada
        if self.running and not self._intentional_disconnect and sock is self.sock:
            self._on_coordinator_failure()

    # ════════════════════════════════════════════════════════════════════
    # DETECÇÃO DE FALHA + ELEIÇÃO (Algoritmo do Valentão / Bully)
    # ════════════════════════════════════════════════════════════════════

    def _on_coordinator_failure(self):
        self.on_status("Coordenador não responde! Iniciando eleição...")
        # remove a entrada do coordenador morto da réplica de cliente
        with self.members_lock:
            dead = [c for c, info in self.members.items()
                    if info["ip"] == self.coord_ip and info["port"] == self.coord_port]
            for c in dead:
                self.members.pop(c, None)
        self.on_members_update()
        self._start_election()

    def _start_election(self):
        with self.election_lock:
            if self.election_in_progress or self.client_id is None or self.is_coordinator:
                return
            self.election_in_progress = True
        threading.Thread(target=self._run_election, daemon=True).start()

    def _run_election(self):
        try:
            while self.running and not self.is_coordinator:
                # Limpa o evento ANTES de contatar quem quer que seja: se isso
                # for feito depois (ex.: logo antes do wait()), um anúncio
                # COORDINATOR_WIN que chegue rápido demais (entre o envio do
                # ELECTION e o clear()) seria descartado, e o wait() abaixo
                # ficaria esperando à toa o timeout inteiro (sinal perdido).
                self._got_new_coordinator.clear()

                my_id = int(self.client_id)
                with self.members_lock:
                    # usa a réplica de cliente: é o que conhecemos sobre os
                    # participantes do quadro depois que o coordenador caiu
                    higher = [(c, dict(info)) for c, info in self.members.items() if int(c) > my_id]

                responded = False
                for c, info in higher:
                    reply = P.one_shot(info["ip"], info["port"], {"type": P.ELECTION}, timeout=P.ELECTION_CONTACT_TIMEOUT)
                    if reply and reply.get("type") == P.ELECTION_OK:
                        responded = True
                        if reply.get("coordinator_ip"):
                            # o respondente já é o coordenador (provavelmente
                            # de uma rodada anterior) — adota direto, sem
                            # depender só do broadcast COORDINATOR_WIN
                            self._adopt_new_coordinator(reply["coordinator_ip"], reply["coordinator_port"])

                if self.is_coordinator or self._got_new_coordinator.is_set():
                    return  # um anúncio já chegou enquanto contactávamos os outros

                if not responded:
                    self._become_coordinator()
                    return

                self.on_status("Aguardando anúncio do novo coordenador...")
                won = self._got_new_coordinator.wait(P.ELECTION_TIMEOUT)
                if won or self.is_coordinator:
                    return
                # Timeout: antes de tentar de novo, consulta o NS. Se o
                # coordenador já foi atualizado por outro nó que ganhou a
                # eleição enquanto aguardávamos (COORDINATOR_WIN se perdeu),
                # adota direto sem re-disputar e sem se auto-proclamar.
                boards = P.one_shot(P.NS_HOST, P.NS_PORT,
                                     {"type": P.LIST_BOARDS}, timeout=2.0)
                if boards and boards.get("type") == P.OK:
                    entry = next((b for b in boards["boards"] if b["name"] == self.board_name), None)
                    if entry and (entry["ip"] != self.coord_ip or entry["port"] != self.coord_port):
                        # O NS já tem um novo endereço — é o novo coordenador
                        self._adopt_new_coordinator(entry["ip"], entry["port"])
                        return
                # NS não mudou ou não respondeu → tenta nova rodada de eleição
        finally:
            with self.election_lock:
                self.election_in_progress = False

    def _become_coordinator(self):
        self.is_coordinator = True
        self.coord_ip, self.coord_port = self.my_ip, self.my_port
        # recupera o estado a partir da minha própria réplica (mantida em dia
        # via TX_COMMIT) — satisfaz o requisito de recuperação de estado pelo
        # novo coordenador. Cópia profunda via serialização: evita que o
        # estado canônico e a réplica de cliente apontem para o mesmo objeto.
        self.coord_board = BoardState.from_dict(self.board.to_dict())

        with self.members_lock:
            # Inicializa a tabela AUTORITATIVA a partir da réplica de cliente
            # (é o que o novo coordenador sabe sobre quem está no quadro).
            self.coord_members = dict(self.members)
            self.conns = {}
            self.conn_locks = {}
            self.last_ack = {}
            self.next_client_id = (max((int(c) for c in self.members.keys()), default=0)) + 1
            members_snapshot = dict(self.members)

        threading.Thread(target=self._coord_heartbeat_loop, daemon=True).start()

        reply = P.one_shot(P.NS_HOST, P.NS_PORT, {
            "type": P.UPDATE_BOARD, "name": self.board_name,
            "ip": self.my_ip, "port": self.my_port,
        })
        if reply is None or reply.get("type") != P.OK:
            self.on_status("Aviso: não foi possível atualizar o Serviço de Nomes.")

        for c, info in members_snapshot.items():
            if c == self.client_id:
                continue
            # Entrega confiável: aguarda ACK por até 3 s; sem resposta, o
            # nó descobrirá o novo coordenador via NS na próxima iteração
            # da eleição ou na reconexão.
            P.one_shot(info["ip"], info["port"], {
                "type": P.COORDINATOR_WIN, "ip": self.my_ip, "port": self.my_port,
                "members": members_snapshot,
            }, timeout=3.0)

        self.on_status(f"Vitória na eleição! Agora sou o coordenador do quadro '{self.board_name}'.")
        self.on_coordinator_changed()
        self._reconnect_to_coordinator()

    def _adopt_new_coordinator(self, ip: str, port: int, members: Optional[dict] = None):
        """Ponto único onde um nó toma conhecimento de quem é o coordenador
        atual — seja por ter recebido o broadcast COORDINATOR_WIN, seja por
        ter descoberto isso na própria resposta de uma mensagem ELECTION
        (rede de segurança caso o broadcast fire-and-forget se perca).
        É idempotente: chamadas duplicadas para o mesmo endereço são ignoradas,
        evitando o bug de dupla-reconexão que causaria split-brain."""
        with self.members_lock:
            # Idempotência: se já é o coordenador atual E temos socket ativo,
            # apenas atualiza membros (se fornecidos) e sinaliza o event.
            already = (self.coord_ip == ip and self.coord_port == port
                       and self.sock is not None)
        if already:
            if members is not None:
                self.members = members
                self.on_members_update()
            self._got_new_coordinator.set()
            return

        self.coord_ip, self.coord_port = ip, port
        if members is not None:
            self.members = members
        self.is_coordinator = (ip == self.my_ip and port == self.my_port)
        self.on_members_update()
        self.on_status(f"Novo coordenador: {ip}:{port}")
        self.on_coordinator_changed()
        self._got_new_coordinator.set()
        if not self.is_coordinator:
            threading.Thread(target=self._safe_reconnect, daemon=True).start()

    def _safe_reconnect(self):
        """Wrapper com lock para evitar que dois threads disparem reconexão
        simultânea (o que causaria dupla conexão no coordenador)."""
        if not self._reconnect_lock.acquire(blocking=False):
            return  # já há uma reconexão em andamento
        try:
            self._reconnect_to_coordinator()
        finally:
            self._reconnect_lock.release()

    def _handle_coordinator_win(self, msg: dict):
        self._adopt_new_coordinator(msg["ip"], msg["port"], msg.get("members"))
