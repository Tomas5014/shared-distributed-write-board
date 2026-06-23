# Relatório Técnico — SDWB (Shared Distributed Write Board)

**Disciplina:** Sistemas Distribuídos  
**Linguagem:** Python 3.10+  
**Comunicação:** Sockets TCP (sem middleware externo)

---

## 1. Arquitetura e Componentes

O sistema é composto por três tipos de processo:

```
┌─────────────────────────────────────────────────────────────────────┐
│ name_service.py          IP e porta FIXOS (SDWB_NS_HOST/NS_PORT)   │
│  • Tabela: (NomeQuadro, IP, Porta) dos coordenadores               │
│  • Único componente que não falha                                   │
└───────────────────────────┬─────────────────────────────────────────┘
                            │ REGISTER / UPDATE / LIST / REMOVE
              ┌─────────────▼──────────────────────┐
              │ node.py  —  Processo cliente/nó     │
              │                                      │
              │  Papel de CLIENTE (sempre):          │
              │   • Réplica local: board + members   │
              │   • Envia ACTION_REQUEST              │
              │   • Participa de 2PC (vota)          │
              │   • Heartbeat com o coordenador      │
              │   • Detecta falha → eleição Bully    │
              │                                      │
              │  Papel de COORDENADOR (se ganhou     │
              │  eleição ou criou o quadro):         │
              │   • Estado canônico (coord_board)    │
              │   • Tabela autoritativa (coord_members)│
              │   • Orquestra 2PC                    │
              │   • Envia Heartbeat aos clientes     │
              └──────────────────────────────────────┘
              GUI: app.py (Tkinter, processo separado do coordenador)
```

---

## 2. Protocolo de Mensagens (protocol.py)

Framing TCP: `[4 bytes big-endian = tamanho] + [payload UTF-8 JSON]`

### 2.1 Cliente ↔ Serviço de Nomes

| Tipo | Direção | Campos obrigatórios |
|------|---------|---------------------|
| `REGISTER_BOARD` | → NS | `name`, `ip`, `port` |
| `UPDATE_BOARD` | → NS | `name`, `ip`, `port` |
| `REMOVE_BOARD` | → NS | `name` |
| `LIST_BOARDS` | → NS | — |
| `OK` | NS → | `boards` (em LIST) |
| `ERROR` | NS → | `error` |

### 2.2 Cliente → Coordenador

| Tipo | Campos |
|------|--------|
| `JOIN` | `client_id` (None se novo), `ip`, `port`, `name` |
| `ACTION_REQUEST` | `action`, `payload` |
| `HEARTBEAT_ACK` | — |
| `VOTE_COMMIT` / `VOTE_ABORT` | `tx_id` |
| `LEAVE` | — |

### 2.3 Coordenador → Cliente

| Tipo | Campos |
|------|--------|
| `STATE_SYNC` | `client_id`, `board`, `members` |
| `HEARTBEAT` | — |
| `CLIENT_JOINED` / `CLIENT_LEFT` | `members` |
| `PREPARE` | `tx_id`, `action`, `payload`, `client_id` |
| `TX_COMMIT` | `tx_id`, `action`, `payload`, `client_id`, `result` |
| `TX_ABORT` | `tx_id`, `reason` |
| `ERROR` | `error` |

### 2.4 Nó ↔ Nó (Eleição)

| Tipo | Campos |
|------|--------|
| `ELECTION` | — |
| `ELECTION_OK` | `coordinator_ip`, `coordinator_port` (opcional, se já sou coord.) |
| `COORDINATOR_WIN` | `ip`, `port`, `members` |
| `COORDINATOR_ACK` | — |

---

## 3. Serviço de Nomes (name_service.py)

Processo separado e independente. Endereço configurável via variáveis de ambiente:

```bash
SDWB_NS_HOST=192.168.1.100   # padrão: 127.0.0.1
SDWB_NS_PORT=9999             # padrão: 9999
```

Mantém **apenas** a tabela `(NomeQuadro → {ip, porta})`. Não conhece o conteúdo dos quadros. Faz varredura periódica (a cada 15 s) sondando os coordenadores registrados; após 2 falhas consecutivas sem resposta, remove o quadro órfão da tabela.

---

## 4. Protocolo de Entrada (Onboarding)

```
Cliente Novo          Serviço de Nomes        Coordenador
     │                       │                     │
     │──LIST_BOARDS──────────▶│                     │
     │◀──[lista de quadros]───│                     │
     │   (escolhe um quadro)  │                     │
     │                        │                     │
     │──JOIN (ip, port, name)─────────────────────▶│
     │◀──STATE_SYNC (client_id, board, members)────│
     │                        │                     │
     │                        │──CLIENT_JOINED(members)──▶ outros clientes
```

O `STATE_SYNC` contém o estado completo do quadro (`board_state.to_dict()`) e a lista de todos os participantes — garantindo que o novo cliente comece sincronizado, mesmo que desenhos tenham sido feitos antes de sua entrada.

---

## 5. Protocolo 2PC — Commit em Duas Fases (node.py)

Usado em **todas** as ações de desenho/seleção/colorir/remover. Garante que a ação ou é aplicada em **todos** os nós ou em **nenhum**.

```
    Requisitante    Coordenador    Participante 1   Participante N
         │               │               │               │
         │─ACTION_REQUEST▶│               │               │
         │               │──PREPARE──────▶───────────────▶│
         │               │◀──VOTE_COMMIT──◀──VOTE_COMMIT──│
         │               │   (ou ABORT)      (ou ABORT)   │
         │               │                                 │
         │     [todos COMMIT]                              │
         │◀──TX_COMMIT────│──TX_COMMIT────▶───────────────▶│
         │   [aplica]     │   [aplica]    [aplica]  [aplica]│
         │               │                                 │
         │     [algum ABORT ou timeout]                    │
         │◀──TX_ABORT─────│                                 │
```

**Exclusão mútua:** O coordenador usa `tx_lock` (RLock) para serializar ACTION_REQUESTs, garantindo que transações concorrentes sejam ordenadas totalmente. Detectada na fase de validação (`board_state.validate()`): se um objeto já está selecionado por outro cliente, `VOTE_ABORT` é emitido imediatamente.

**Deadlock de head-of-line evitado:** Cada `ACTION_REQUEST` é processado em uma thread separada no coordenador, para que a leitura de votos de outros clientes (na mesma conexão) não seja bloqueada pela espera de votos para a transação atual.

---

## 6. Detecção de Falhas — Heartbeat Centralizado

O coordenador envia `HEARTBEAT` a cada **T = 4 s** para todos os clientes. Os clientes respondem com `HEARTBEAT_ACK`. Se um cliente não responder em **2T = 8 s**, o coordenador o remove da lista e notifica os demais via `CLIENT_LEFT`.

Clientes detectam falha do coordenador quando a conexão TCP retorna EOF — evento imediato, não dependente do timeout de heartbeat.

---

## 7. Eleição — Algoritmo do Valentão (Bully)

Disparada quando um cliente detecta que o coordenador caiu.

```
 Ids: A=1(coord), B=2, C=3.   A falha.

 B detecta falha → envia ELECTION para C (id=3 > id=2)
 C detecta falha → higher=[] → declara-se vencedor
 C responde ELECTION_OK a B (com coordinator_ip=C)
 B recebe coordinator_ip → _adopt_new_coordinator(C) → reconecta
 C → UPDATE_BOARD no NS com novo endereço
 C → envia COORDINATOR_WIN para B (confiável, com ACK)
```

**Resiliência implementada:**

| Problema | Solução |
|----------|---------|
| COORDINATOR_WIN perdido | ELECTION_OK inclui `coordinator_ip` se o respondente já é coordenador |
| COORDINATOR_WIN perdido (timeout) | Após ELECTION_TIMEOUT, consulta o NS antes de se auto-proclamar |
| Split-brain por dupla-reconexão | `_adopt_new_coordinator` é idempotente + `_reconnect_lock` |
| Crash gera remoção prematura do quadro do NS | `_coord_client_reader_loop` não chama `_coord_remove_client` se `not self.running` |

---

## 8. Estado do Quadro — board_state.py

Cada objeto tem: `kind` (line/square), `points` ([p1, p2]), `color`, `selected_by` (None ou client_id).

Dois objetos geométricos disponíveis:
- **Linha reta:** dois pontos clicados → segmento.
- **Quadrado:** dois pontos clicados → lados normalizados para `side = max(|dx|, |dy|)`.

Hit-test para seleção: distância ponto-segmento para linhas; bounding-box expandida para quadrados.

---

## 9. Separação de Estado Canônico e Réplica

Para evitar que o nó que hospeda o coordenador aplique ações duas vezes:

| Atributo | Dono | Descrição |
|----------|------|-----------|
| `coord_board` | Coordenador | Estado canônico; modificado pelo 2PC |
| `board` | Cliente | Réplica local; atualizada apenas via `TX_COMMIT` recebido |
| `coord_members` | Coordenador | Tabela autoritativa de membros |
| `members` | Cliente | Réplica da lista; atualizada via `STATE_SYNC` / `CLIENT_JOINED` / `CLIENT_LEFT` |

---

## 10. Regras Especiais (das anotações)

- **Coordenador sai (sem desligar o PC):** O serviço de coordenador continua rodando no mesmo processo. Apenas a interface de cliente é encerrada. O quadro persiste.
- **Coordenador desliga o PC (`simulate_crash`):** Os outros clientes detectam via EOF, iniciam eleição Bully, o vencedor atualiza o NS.
- **Coordenador sozinho sai ou cai:** `_kill_hosted_board()` é chamado → NS remove o quadro → quadro é destruído.
- **Serviço de Nomes:** Nunca falha. Endereço fixo via variáveis de ambiente.

---

## 11. Como Executar

### Passo 1 — Serviço de Nomes (em qualquer máquina, endereço fixo)
```bash
python3 name_service.py
# ou em outra porta:
python3 name_service.py 9999
```

### Passo 2 — Clientes (em cada terminal/máquina)
```bash
# Se o NS estiver em outra máquina:
export SDWB_NS_HOST=192.168.1.100
export SDWB_NS_PORT=9999

python3 app.py
```

O primeiro cliente que clicar em **CRIAR NOVO QUADRO** passa a hospedar o coordenador daquele quadro. Os demais clicam em **INGRESSAR EM QUADRO EXISTENTE**, selecionam o quadro na lista e entram.

### Cenários de Teste Obrigatórios

| Cenário | Como testar |
|---------|-------------|
| Entrada Dinâmica | Abra app.py em 3 terminais; o 1º cria, os outros ingressam |
| Concorrência Transacional | Dois clientes clicam em "Selecionar" no mesmo objeto simultaneamente; um deles recebe mensagem de erro |
| Morte do Coordenador | Feche (Ctrl+C) o terminal onde está o coordenador; os demais realizam eleição automaticamente |

### Teste automatizado (sem GUI)
```bash
python3 test_headless.py
```
Exercita todos os mecanismos sem Tkinter: cria 3 nós, testa 2PC, exclusão mútua, eleição Bully, recuperação de estado e regra do coordenador sozinho. Resultado esperado: **20/20 PASS**.

---

## 12. Estrutura de Arquivos

```
sdwb/
├── protocol.py        Constantes de mensagens, framing TCP, helpers de socket
├── board_state.py     Estado do quadro: objetos, validação, aplicação de ações
├── name_service.py    Serviço de Nomes (processo separado, endereço fixo)
├── node.py            Núcleo do nó: coordenador + cliente + 2PC + eleição Bully
├── app.py             Interface gráfica Tkinter (cliente)
└── test_headless.py   Bateria de testes automatizados (sem GUI)
```
