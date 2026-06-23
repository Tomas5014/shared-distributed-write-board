"""
test_headless.py — Validação automatizada da lógica de rede do SDWB (sem GUI)
================================================================================
Roda o Serviço de Nomes e vários Nodes dentro do mesmo processo Python
(threads + sockets reais em localhost) e exercita:
  1. Criação de quadro + ingresso de múltiplos clientes + sincronização de estado
  2. Propagação de desenhos (2PC) entre réplicas
  3. Exclusão mútua em SELECT concorrente
  4. Detecção de falha do coordenador + eleição do Valentão + atualização do NS
  5. Continuidade das operações com o novo coordenador
  6. Regra "coordenador sozinho que sai mata o quadro"
"""

import sys
import threading
import time

sys.path.insert(0, "/home/claude/sdwb")

import protocol as P
from name_service import NameService
from node import Node

PASS, FAIL = "PASS", "FAIL"
results = []


def check(label, cond):
    results.append((label, PASS if cond else FAIL))
    print(f"[{PASS if cond else FAIL}] {label}")


def wait_until(fn, timeout=5.0, interval=0.1):
    start = time.time()
    while time.time() - start < timeout:
        if fn():
            return True
        time.sleep(interval)
    return fn()


def make_node(name):
    n = Node(display_name=name)
    n.on_status = lambda m, nm=name: print(f"    [{nm}] {m}")
    return n


def main():
    # ---- 1. Serviço de Nomes ----
    ns = NameService("127.0.0.1", P.NS_PORT)
    threading.Thread(target=ns.start, daemon=True).start()
    time.sleep(0.3)

    # ---- 2. Cria quadro + 2 clientes ingressam ----
    A = make_node("Alice")
    ok, err = A.create_board("turma1")
    check("Coordenador (Alice) cria e registra o quadro", ok)

    B = make_node("Bob")
    ok, err = B.join_board("turma1", A.my_ip, A.my_port)
    check("Bob ingressa no quadro", ok)

    C = make_node("Carol")
    ok, err = C.join_board("turma1", A.my_ip, A.my_port)
    check("Carol ingressa no quadro", ok)

    time.sleep(0.3)
    check("Todos veem 3 membros (sincronização de entrada)",
          len(A.members) == 3 and len(B.members) == 3 and len(C.members) == 3)

    # ---- 3. Desenho propaga via 2PC ----
    A.do_action("LINE", {"points": [[10, 10], [100, 100]]})
    propagated = wait_until(lambda: len(B.board.objects) == 1 and len(C.board.objects) == 1)
    check("Linha desenhada por Alice propaga para Bob e Carol (2PC commit)", propagated)

    obj_id = next(iter(A.board.objects.keys()))
    check("Objeto replicado é idêntico (mesmo id) nas 3 réplicas",
          obj_id in B.board.objects and obj_id in C.board.objects)

    # ---- 4. Quadrado (segunda forma geométrica) ----
    B.do_action("SQUARE", {"points": [[200, 200], [260, 240]]})
    sq_ok = wait_until(lambda: len(A.board.objects) == 2 and len(C.board.objects) == 2)
    check("Quadrado desenhado por Bob propaga (lados normalizados)", sq_ok)
    sq_id = [oid for oid, o in A.board.objects.items() if o["kind"] == "square"][0]
    (x1, y1), (x2, y2) = A.board.objects[sq_id]["points"]
    check("Quadrado tem lados iguais (é um quadrado de verdade)", abs(x2 - x1) == abs(y2 - y1))

    # ---- 5. Exclusão mútua: Bob e Carol tentam selecionar o mesmo objeto ----
    results_sel = {}

    def try_select(node, label):
        node.do_action("SELECT", {"object_id": obj_id})
        results_sel[label] = node

    t1 = threading.Thread(target=try_select, args=(B, "bob"))
    t2 = threading.Thread(target=try_select, args=(C, "carol"))
    t1.start(); t2.start()
    t1.join(); t2.join()
    time.sleep(0.5)

    selected_by = A.board.objects[obj_id]["selected_by"]
    check("Exclusão mútua: exatamente um dos dois conseguiu selecionar o objeto",
          selected_by in (int(B.client_id), int(C.client_id)))

    winner = B if selected_by == int(B.client_id) else C
    loser = C if winner is B else B
    check(f"O vencedor da seleção foi consistente nas 3 réplicas (id={selected_by})",
          A.board.objects[obj_id]["selected_by"] == selected_by
          and B.board.objects[obj_id]["selected_by"] == selected_by
          and C.board.objects[obj_id]["selected_by"] == selected_by)

    # vencedor colore o objeto
    winner.do_action("COLOR", {"object_id": obj_id, "color": "#1d4ed8"})
    colored = wait_until(lambda: A.board.objects[obj_id]["color"] == "#1d4ed8")
    check("Vencedor consegue colorir o objeto selecionado", colored)

    winner.do_action("DESELECT", {"object_id": obj_id})
    time.sleep(0.3)

    # ---- 6. Detecção de falha + eleição do coordenador ----
    print("\n--- Simulando queda do coordenador (Alice) ---")
    time.sleep(0.5)
    old_coord_id_b = B.client_id
    A.simulate_crash()

    elected = wait_until(lambda: B.is_coordinator or C.is_coordinator, timeout=P.HEARTBEAT_TIMEOUT + P.ELECTION_TIMEOUT + 5)
    check("Um novo coordenador foi eleito após a queda de Alice", elected)

    new_coord = B if B.is_coordinator else (C if C.is_coordinator else None)
    other = C if new_coord is B else B
    check("O vencedor da eleição é o de MAIOR id (Algoritmo do Valentão)",
          new_coord is not None and int(new_coord.client_id) == max(int(B.client_id), int(C.client_id)))

    reconnected = wait_until(lambda: other.coord_ip == new_coord.my_ip and other.coord_port == new_coord.my_port)
    check("O sobrevivente reconectou ao novo coordenador", reconnected)

    # Serviço de Nomes deve refletir o novo endereço
    boards = A.list_boards() if A.running else other.list_boards()
    ns_entry = next((b for b in boards if b["name"] == "turma1"), None)
    check("Serviço de Nomes foi atualizado com o endereço do novo coordenador",
          ns_entry is not None and ns_entry["ip"] == new_coord.my_ip and ns_entry["port"] == new_coord.my_port)

    # estado do quadro foi recuperado (objetos criados antes da queda continuam lá)
    check("Novo coordenador recuperou o estado do quadro (objetos preservados)",
          len(new_coord.board.objects) == 2)
    check("Novo coordenador recuperou a lista de integrantes (membro remanescente presente)",
          other.client_id in new_coord.members)

    # ---- 7. Sistema continua operando após a eleição ----
    new_coord.do_action("LINE", {"points": [[5, 5], [50, 5]]})
    cont_ok = wait_until(lambda: len(other.board.objects) == 3)
    check("Sistema continua operando normalmente após a eleição (novo desenho propaga)", cont_ok)

    # ---- 8. Regra: coordenador sozinho que sai mata o quadro ----
    print("\n--- Testando regra: coordenador sozinho que sai mata o quadro ---")
    # libera os nós do cenário anterior (no sistema real cada nó é um
    # PROCESSO separado; aqui, no teste headless de um único processo,
    # encerramos suas threads para não disputar CPU/GIL com o próximo cenário)
    for n in (A, B, C):
        try:
            n.shutdown()
        except Exception:
            pass
    time.sleep(1.5)

    D = make_node("Dave")
    ok, _ = D.create_board("quadro_solo")
    check("Dave cria quadro 'quadro_solo' sozinho", ok)
    D.leave_board()
    boards2 = wait_until(lambda: not any(b["name"] == "quadro_solo" for b in D.list_boards()), timeout=30.0)
    check("Quadro 'quadro_solo' foi removido do Serviço de Nomes ao sair sozinho", boards2)

    # ---- resumo ----
    print("\n================ RESUMO ================")
    n_fail = sum(1 for _, r in results if r == FAIL)
    for label, r in results:
        print(f"[{r}] {label}")
    print(f"\nTotal: {len(results)}  |  Falhas: {n_fail}")
    sys.exit(1 if n_fail else 0)


if __name__ == "__main__":
    main()
