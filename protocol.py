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

# Replicação primário-backup (sequenciador central) — Coordenador -> Clientes
# O coordenador é o PRIMÁRIO: serializa, valida (exclusão mútua) e aplica a ação
# no estado canônico, então propaga o resultado às réplicas (backups). Não há
# fase de votação — em caso de conflito o coordenador responde apenas ERROR ao
# requisitante e nada é propagado.
ACTION_APPLY    = "ACTION_APPLY"   # coordenador -> todos: aplique esta ação confirmada

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
NS_BIND_HOST       = os.environ.get("SDWB_NS_BIND", "0.0.0.0")
# NS_HOST  = endereço que TODOS (cliente e o próprio NS) usam para DISCAR o NS.
# NS_BIND_HOST = endereço em que o PROCESSO do NS efetivamente ESCUTA.
# Por padrão escuta em todas as interfaces (0.0.0.0), mesmo que NS_HOST
# aponte para um IP específico — assim "esquecer" de configurar o bind não
# deixa o serviço acessível só em loopback.

HEARTBEAT_INTERVAL = 4    # T: segundos entre envios de heartbeat
HEARTBEAT_TIMEOUT  = 8    # 2T: segundos sem resposta -> peer considerado morto
ELECTION_TIMEOUT   = 6    # segundos aguardando COORDINATOR_WIN antes de reiniciar eleição
ELECTION_CONTACT_TIMEOUT = 4   # segundos aguardando resposta a uma mensagem ELECTION

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
    """Retorna o IP LAN desta máquina.

    Em redes sem rota de internet (ex.: dois PCs ligados direto por cabo
    Ethernet, sem gateway), a técnica clássica de "conectar via UDP a
    8.8.8.8 e ver qual IP local o SO escolheu" pode falhar, pois não há
    rota até esse endereço — caindo no fallback 127.0.0.1, que é inútil
    para qualquer outra máquina. Para evitar isso, tentamos várias
    estratégias em ordem, e nunca aceitamos 127.0.0.1 se houver alternativa.
    """
    override = os.environ.get("SDWB_MY_IP")
    if override:
        return override

    candidates = []

    # 1) Tenta rotear em direção ao próprio Serviço de Nomes — funciona
    #    mesmo sem gateway de internet, desde que NS_HOST esteja configurado
    #    para um IP real (não 127.0.0.1) nesta máquina.
    if NS_HOST and NS_HOST != "127.0.0.1":
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect((NS_HOST, NS_PORT))
            candidates.append(s.getsockname()[0])
            s.close()
        except Exception:
            pass

    # 2) Truque clássico via DNS público (só funciona com internet/gateway)
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        candidates.append(s.getsockname()[0])
        s.close()
    except Exception:
        pass

    for ip in candidates:
        if ip and not ip.startswith("127."):
            return ip

    # 3) Último recurso: resolve o próprio hostname
    try:
        ip = socket.gethostbyname(socket.gethostname())
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass

    return "127.0.0.1"
