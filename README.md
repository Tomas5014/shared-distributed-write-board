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
              │   • Aplica ACTION_APPLY (réplica)    │
              │   • Heartbeat com o coordenador      │
              │   • Detecta falha → eleição Bully    │
              │                                      │
              │  Papel de COORDENADOR (se ganhou     │
              │  eleição ou criou o quadro):         │
              │   • Estado canônico (coord_board)    │
              │   • Tabela autoritativa (coord_members)│
              │   • Primário: ordena/valida/aplica   │
              │     e propaga ACTION_APPLY            │
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
| `LEAVE` | — |

### 2.3 Coordenador → Cliente

| Tipo | Campos |
|------|--------|
| `STATE_SYNC` | `client_id`, `board`, `members` |
| `HEARTBEAT` | — |
| `CLIENT_JOINED` / `CLIENT_LEFT` | `members` |
| `ACTION_APPLY` | `action`, `payload`, `client_id`, `result` |
| `ERROR` | `error` (ação rejeitada por conflito de exclusão mútua) |

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

## 5. Propagação de Ações — Replicação Primário-Backup (node.py)

> **Nota de escopo:** o enunciado original pedia *Two-Phase Commit (2PC)* para
> transações atômicas de múltiplos objetos, mas essa exigência **foi removida
> pelo professor**. Como não há mais transações multi-objeto, adotamos um
> esquema mais simples e adequado: **replicação primário-backup com
> sequenciador central**. O coordenador é o **primário** (réplica autoritativa);
> os clientes são **backups** (réplicas passivas).

Usado em **todas** as ações de desenho/seleção/colorir/remover/desselecionar.

```
    Requisitante    Coordenador (primário)     Backup 1   Backup N
         │               │                        │          │
         │─ACTION_REQUEST▶│                        │          │
         │            [action_lock]                │          │
         │            valida (exclusão mútua)       │          │
         │               │                          │          │
         │   [válida] aplica em coord_board         │          │
         │◀──ACTION_APPLY─│──ACTION_APPLY──────────▶──────────▶│
         │   [aplica]     │        [aplica]      [aplica]      │
         │               │                          │          │
         │   [conflito] ──┐                          │          │
         │◀──ERROR────────┘  (só ao requisitante; nada propaga) │
```

**Sequenciador central / ordenação total:** o coordenador processa cada
`ACTION_REQUEST` sob `action_lock` (RLock). Ações concorrentes de clientes
diferentes são serializadas — a segunda enxerga o efeito da primeira. Ações de
um mesmo cliente preservam ordem FIFO porque são lidas e aplicadas inline na
conexão daquele cliente.

**Exclusão mútua:** validada em `board_state.validate()` antes de aplicar. Se o
objeto já está selecionado por outro cliente, o coordenador responde apenas
`ERROR` ao requisitante e **não propaga nada** — o estado canônico não muda.

**Consistência das réplicas:** como só o primário muta o estado e propaga as
ações já confirmadas, na mesma ordem total, todas as réplicas convergem para o
mesmo estado. Não há votação nem fase de *abort*: uma ação válida sempre é
aplicada por todos; uma inválida não é aplicada por ninguém.

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
| `coord_board` | Coordenador | Estado canônico (primário); modificado ao validar/aplicar a ação |
| `board` | Cliente | Réplica local (backup); atualizada apenas via `ACTION_APPLY` recebido |
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

### Passo 3 — Dois PCs ligados direto por cabo Ethernet

Cenário sem roteador/DHCP. Exemplo: **PC-A = `192.168.50.1`** (roda o Serviço de Nomes) e **PC-B = `192.168.50.2`**.

**Pré-requisitos (nos dois):** o código do projeto na máquina, Python 3.10+ e Tkinter
(`sudo apt install python3-tk` no Debian/Ubuntu).

**1. Cabo + IPs estáticos na mesma sub-rede** (Ethernet moderno tem Auto-MDI-X, cabo
comum serve). Descubra o nome da interface com `ip -br link` (ex.: `enp3s0`) e troque abaixo:

```bash
# No PC-A
sudo ip addr add 192.168.50.1/24 dev enp3s0
sudo ip link set enp3s0 up
# No PC-B
sudo ip addr add 192.168.50.2/24 dev enp3s0
sudo ip link set enp3s0 up
```
> Comandos temporários (somem ao reiniciar); suficientes para a demonstração.

**2. Testar a conexão** (não prossiga enquanto o ping não responder):
```bash
# No PC-A
ping 192.168.50.2
```

**3. Serviço de Nomes — só no PC-A:**
```bash
export SDWB_NS_HOST=192.168.50.1
export SDWB_NS_PORT=9999
export SDWB_MY_IP=192.168.50.1
python3 name_service.py            # deve imprimir: ouvindo em 0.0.0.0:9999
```

**4. Cliente no PC-A (outro terminal)** — clica em CRIAR NOVO QUADRO:
```bash
export SDWB_NS_HOST=192.168.50.1
export SDWB_NS_PORT=9999
export SDWB_MY_IP=192.168.50.1
python3 app.py
```

**5. Cliente no PC-B** — clica em INGRESSAR EM QUADRO EXISTENTE:
```bash
export SDWB_NS_HOST=192.168.50.1   # aponta para o NS, que está no PC-A
export SDWB_NS_PORT=9999
export SDWB_MY_IP=192.168.50.2     # o MEU ip nesta máquina
python3 app.py
```

**`SDWB_MY_IP` é a variável-chave deste cenário.** Cada nó precisa anunciar ao Serviço
de Nomes / coordenador um IP que a *outra* máquina consiga discar. Numa ligação direta
sem gateway a detecção automática pode falhar e cair em `127.0.0.1` (inútil para o outro
PC). Por isso defina `SDWB_MY_IP` explicitamente em cada máquina com o IP daquela placa —
o código usa essa variável antes de qualquer heurística.

**Firewall:** as portas dos nós são aleatórias a cada execução (o SO escolhe uma livre),
então não há porta fixa a liberar além da do NS. Num cabo direto entre máquinas confiáveis,
libere a sub-rede ou desative o firewall durante o teste:
```bash
sudo ufw allow from 192.168.50.0/24    # ou, para o teste: sudo ufw disable
```
No Windows, libere o `python.exe` no Firewall para redes privadas (ou desative-o na rede privada).

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
Exercita todos os mecanismos sem Tkinter: cria 3 nós, testa a propagação por replicação primário-backup, exclusão mútua, eleição Bully, recuperação de estado e regra do coordenador sozinho. Resultado esperado: **20/20 PASS**.

---

## 12. Estrutura de Arquivos

```
sdwb/
├── protocol.py        Constantes de mensagens, framing TCP, helpers de socket
├── board_state.py     Estado do quadro: objetos, validação, aplicação de ações
├── name_service.py    Serviço de Nomes (processo separado, endereço fixo)
├── node.py            Núcleo do nó: coordenador + cliente + replicação + eleição Bully
├── app.py             Interface gráfica Tkinter (cliente)
└── test_headless.py   Bateria de testes automatizados (sem GUI)
```
