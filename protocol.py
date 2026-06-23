"""
protocol.py — SDWB Protocol Constants & Message I/O
====================================================
Framing: 4-byte big-endian unsigned int (payload length) + UTF-8 JSON payload.
"""

import json
import os
import socket
import struct
from typing import Optional

# ─────────────────────────────────────────────────────────────────────────────
# Message type constants
# ─────────────────────────────────────────────────────────────────────────────

# Cliente ↔ Serviço de Nomes
REGISTER_BOARD  = "REGISTER_BOARD"
LIST_BOARDS     = "LIST_BOARDS"
UPDATE_BOARD    = "UPDATE_BOARD"
REMOVE_BOARD    = "REMOVE_BOARD"

# Cliente → Coordenador
JOIN            = "JOIN"
LEAVE           = "LEAVE"
ACTION_REQUEST  = "ACTION_REQUEST"   # pedido de transação (select/deselect/color/remove/line/square)
HEARTBEAT       = "HEARTBEAT"

# Coordenador → Cliente
HEARTBEAT_ACK   = "HEARTBEAT_ACK"
STATE_SYNC      = "STATE_SYNC"
CLIENT_JOINED   = "CLIENT_JOINED"
CLIENT_LEFT     = "CLIENT_LEFT"
ERROR_MSG       = "ERROR"
OK              = "OK"

# Protocolo de Commit em Duas Fases (2PC) — Coordenador ⇄ Clientes (participantes)
PREPARE         = "PREPARE"        # coordenador -> participantes: vote nesta ação
VOTE_COMMIT     = "VOTE_COMMIT"    # participante -> coordenador
VOTE_ABORT      = "VOTE_ABORT"     # participante -> coordenador
TX_COMMIT       = "TX_COMMIT"      # coordenador -> todos: ação confirmada, aplicar
TX_ABORT        = "TX_ABORT"       # coordenador -> todos (ou só ao requisitante): ação cancelada

# Nó ↔ Nó  (eleição)
ELECTION          = "ELECTION"
ELECTION_OK       = "ELECTION_OK"
COORDINATOR_WIN   = "COORDINATOR_WIN"
COORDINATOR_ACK   = "COORDINATOR_ACK"   # ACK de quem recebeu o COORDINATOR_WIN

# Serviço de Nomes -> qualquer nó (varredura de quadros órfãos)
NS_PROBE        = "NS_PROBE"

# Sub-tipos de DRAW_ACTION
ACTION_LINE     = "LINE"
ACTION_SQUARE   = "SQUARE"
ACTION_COLOR    = "COLOR"
ACTION_REMOVE   = "REMOVE"
ACTION_SELECT   = "SELECT"
ACTION_DESELECT = "DESELECT"

# ─────────────────────────────────────────────────────────────────────────────
# Configuração de rede
# (IP do Serviço de Nomes é o único endereço fixo do sistema)
# ─────────────────────────────────────────────────────────────────────────────

NS_HOST            = os.environ.get("SDWB_NS_HOST", "127.0.0.1")
NS_PORT            = int(os.environ.get("SDWB_NS_PORT", "9999"))

HEARTBEAT_INTERVAL = 4    # T: segundos entre envios de heartbeat
HEARTBEAT_TIMEOUT  = 8    # 2T: segundos sem resposta -> peer considerado morto
ELECTION_TIMEOUT   = 6    # segundos aguardando COORDINATOR_WIN antes de reiniciar eleição
ELECTION_CONTACT_TIMEOUT = 4   # segundos aguardando resposta a uma mensagem ELECTION
VOTE_TIMEOUT        = 4   # segundos aguardando votos de cada participante no 2PC

# ─────────────────────────────────────────────────────────────────────────────
# I/O de mensagens
# ─────────────────────────────────────────────────────────────────────────────

def send_msg(sock: socket.socket, data: dict) -> None:
    """Envia mensagem JSON prefixada com tamanho (4 bytes big-endian)."""
    payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
    sock.sendall(struct.pack("!I", len(payload)) + payload)


def recv_msg(sock: socket.socket) -> Optional[dict]:
    """Recebe mensagem JSON prefixada com tamanho. Retorna None em caso de erro/EOF."""
    raw = _recv_all(sock, 4)
    if raw is None:
        return None
    length  = struct.unpack("!I", raw)[0]
    payload = _recv_all(sock, length)
    if payload is None:
        return None
    try:
        return json.loads(payload.decode("utf-8"))
    except json.JSONDecodeError:
        return None


def _recv_all(sock: socket.socket, n: int) -> Optional[bytes]:
    buf = b""
    while len(buf) < n:
        try:
            chunk = sock.recv(n - len(buf))
        except OSError:
            return None
        if not chunk:
            return None
        buf += chunk
    return buf


def fire_and_forget(host: str, port: int, msg: dict, timeout: float = 2.0) -> None:
    """Conecta, envia uma mensagem e fecha sem esperar resposta."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((host, port))
        send_msg(s, msg)
        s.close()
    except Exception:
        pass


def one_shot(host: str, port: int, msg: dict,
             timeout: float = 5.0) -> Optional[dict]:
    """Conecta, envia uma mensagem, recebe uma resposta e fecha a conexão."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((host, port))
        send_msg(s, msg)
        reply = recv_msg(s)
        s.close()
        return reply
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Utilitários de rede
# ─────────────────────────────────────────────────────────────────────────────

def free_port() -> int:
    """Pede ao SO uma porta TCP livre."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def local_ip() -> str:
    """Retorna o IP LAN desta máquina."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"
