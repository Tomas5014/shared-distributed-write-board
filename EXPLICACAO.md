# SDWB — Explicação Detalhada do Programa (guia para apresentação)

> Documento didático para entender **todo** o código do *Shared Distributed Write
> Board* e conseguir explicá-lo ao professor. Lê-se de cima para baixo: primeiro
> a visão geral e os conceitos, depois cada arquivo função por função, e por fim
> os fluxos completos passo a passo e um "FAQ de banca".

---

## 1. O que o sistema faz (visão de 30 segundos)

Um **quadro branco colaborativo distribuído**: várias pessoas, cada uma em um
terminal/PC, desenham linhas e quadrados no mesmo quadro e veem em tempo real o
que os outros fazem. Não existe servidor central fixo. Em vez disso:

- Um processo leve, o **Serviço de Nomes** (as "Páginas Amarelas"), só guarda
  *onde* está cada quadro: `(nome, IP, porta)`.
- Um dos próprios nós assume o papel de **Coordenador** do quadro (o "primário"):
  ele guarda o estado oficial e ordena/repassa as ações.
- Se o Coordenador cair, os demais **detectam** a falha e fazem uma **eleição**
  (Algoritmo do Valentão / *Bully*) para escolher um novo Coordenador, que assume
  e atualiza o Serviço de Nomes. O papel "migra" de máquina — daí *Coordenador
  Migrante*.

Conceitos de Sistemas Distribuídos exercitados: **descoberta de serviço**,
**replicação primário-backup**, **ordenação total / exclusão mútua**,
**detecção de falhas (heartbeat)** e **eleição de líder**.

---

## 2. Arquitetura: três tipos de processo

```
        ┌───────────────────────────────────────────────────────────┐
        │  name_service.py   — Serviço de Nomes (endereço FIXO)      │
        │  Tabela: nome do quadro -> (IP, porta) do coordenador      │
        │  Não conhece o conteúdo dos quadros. Premissa: não falha.  │
        └───────────────▲───────────────────────────────────────────┘
                        │  REGISTER / UPDATE / REMOVE / LIST
        ┌───────────────┴───────────────────────────────────────────┐
        │  node.py — um "nó". Cada cliente roda um.                  │
        │                                                            │
        │   SEMPRE age como CLIENTE:                                 │
        │     • réplica local do quadro (self.board)                 │
        │     • envia ACTION_REQUEST, aplica ACTION_APPLY            │
        │     • responde HEARTBEAT, detecta queda do coordenador     │
        │                                                            │
        │   PODE ser COORDENADOR (criou o quadro ou venceu eleição): │
        │     • estado canônico (self.coord_board)                   │
        │     • tabela de membros (self.coord_members)               │
        │     • valida + aplica + propaga as ações                   │
        │     • manda HEARTBEAT, detecta queda de clientes           │
        └────────────────────────────────────────────────────────────┘
              app.py — interface gráfica (Tkinter) que usa um Node
```

**Ponto-chave para a banca:** *node.py não tem nada de Tkinter*. Ele expõe
**callbacks** (`on_board_update`, `on_status`, ...) que a GUI registra. Assim a
lógica distribuída fica isolada e testável sem tela (ver `test_headless.py`).

### Arquivos

| Arquivo | Papel |
|---|---|
| `protocol.py` | Constantes de mensagens, *framing* TCP, helpers de socket, descoberta de IP |
| `board_state.py` | O estado do quadro: objetos, validação (exclusão mútua) e aplicação de ações |
| `name_service.py` | O Serviço de Nomes (processo separado) |
| `node.py` | O coração: cliente + coordenador + replicação + heartbeat + eleição |
| `app.py` | Interface gráfica do cliente |
| `test_headless.py` | Bateria de testes automatizados, sem GUI |

---

## 3. Mapa: requisito do enunciado → onde está no código

| Requisito | Onde |
|---|---|
| Serviço de Nomes (tabela nome/IP/porta) | `name_service.py` |
| IP/porta do coordenador não-hardcoded | descoberta via `LIST_BOARDS`; só o NS tem endereço fixo (`protocol.py` `NS_HOST/NS_PORT`) |
| Criar / Ingressar / exibir quadro | `app.py` + `Node.create_board` / `Node.join_board` |
| Linha, Quadrado, 2 cores, Colorir, Remover | `board_state.py` (`apply`) + toolbar em `app.py` |
| Atualização refletida em todos | `ACTION_APPLY` propagado pelo coordenador |
| Onboarding + sincronização de estado | `JOIN` → `STATE_SYNC` (`_coord_handle_join`) |
| Exclusão mútua (objeto selecionado → erro ao 2º) | `board_state.validate` + `action_lock` |
| Detectar falha do coordenador + eleição Bully | `_client_reader_loop` (EOF) → `_run_election` |
| Vencedor atualiza o Serviço de Nomes | `_become_coordinator` → `UPDATE_BOARD` |
| Detecção de falha de cliente | `_coord_heartbeat_loop` |
| Novo coordenador recupera lista de integrantes | `_become_coordinator` (usa `self.members`) |
| Sockets TCP, sem middleware externo | tudo em `socket` puro |

---

## 4. O protocolo de mensagens

### 4.1 Como uma mensagem viaja (framing)

Tudo é JSON em UTF-8, precedido por **4 bytes** que dizem o tamanho do JSON. Isso
resolve o problema clássico de TCP ser um *fluxo de bytes* sem fronteiras de
mensagem: o leitor primeiro lê 4 bytes (o tamanho `N`), depois lê exatamente `N`
bytes. Implementado em `protocol.send_msg` / `protocol.recv_msg`.

```
[ tamanho: 4 bytes big-endian ][ payload: JSON em UTF-8 ]
```

### 4.2 Catálogo de mensagens

**Cliente ↔ Serviço de Nomes**

| Tipo | Sentido | Campos |
|---|---|---|
| `REGISTER_BOARD` | nó → NS | `name, ip, port` (criar quadro) |
| `UPDATE_BOARD` | nó → NS | `name, ip, port` (novo coordenador após eleição) |
| `REMOVE_BOARD` | nó → NS | `name` (quadro encerrado) |
| `LIST_BOARDS` | nó → NS | — (resposta traz `boards`) |

**Cliente → Coordenador**

| Tipo | Campos |
|---|---|
| `JOIN` | `client_id` (None se novo), `ip`, `port`, `name` |
| `ACTION_REQUEST` | `action`, `payload` (pedido de desenho/seleção/cor/remoção) |
| `HEARTBEAT_ACK` | — (resposta ao heartbeat) |
| `LEAVE` | — (saída voluntária) |

**Coordenador → Cliente(s)**

| Tipo | Campos |
|---|---|
| `STATE_SYNC` | `client_id`, `board`, `members` (estado completo ao entrar) |
| `HEARTBEAT` | — |
| `CLIENT_JOINED` / `CLIENT_LEFT` | `members` (lista atualizada) |
| `ACTION_APPLY` | `action, payload, client_id, result` (ação confirmada, aplique) |
| `ERROR` | `error` (ex.: objeto já selecionado por outro) |

**Nó ↔ Nó (eleição)**

| Tipo | Campos |
|---|---|
| `ELECTION` | — ("estou iniciando eleição") |
| `ELECTION_OK` | opcional `coordinator_ip/port` (se quem responde já é coordenador) |
| `COORDINATOR_WIN` | `ip, port, members` (anúncio do vencedor) |
| `COORDINATOR_ACK` | — (confirmação de recebimento) |

**Serviço de Nomes → nó**

| Tipo | Campos |
|---|---|
| `NS_PROBE` | — (o NS sonda se o coordenador ainda está vivo) |

---

## 5. `protocol.py` — fundação de comunicação

Define constantes (os nomes das mensagens acima) e funções utilitárias.

- **`NS_HOST`, `NS_PORT`** — único endereço fixo do sistema, lido de variáveis de
  ambiente (`SDWB_NS_HOST` / `SDWB_NS_PORT`, padrão `127.0.0.1:9999`). É como todo
  mundo "disca" para o Serviço de Nomes.
- **`NS_BIND_HOST`** (padrão `0.0.0.0`) — em qual interface o processo do NS
  *escuta*. Escutar em `0.0.0.0` = todas as interfaces, evitando o erro comum de o
  NS ficar acessível só em `localhost`.
- **Tempos** (em segundos): `HEARTBEAT_INTERVAL=4` (T), `HEARTBEAT_TIMEOUT=8` (2T),
  `ELECTION_TIMEOUT=6`, `ELECTION_CONTACT_TIMEOUT=4`.

Funções:

- **`send_msg` / `recv_msg`** — o *framing* de 4 bytes descrito acima.
  `recv_msg` devolve `None` em EOF/erro (a conexão caiu) — é assim que detectamos
  desconexões.
- **`_recv_all(sock, n)`** — lê *exatamente* `n` bytes (TCP pode entregar em
  pedaços; este laço junta tudo).
- **`fire_and_forget`** — conecta, manda uma mensagem e fecha (sem esperar
  resposta).
- **`one_shot`** — conecta, manda, **espera uma resposta** e fecha. Usado para
  falar com o NS e para mensagens pontuais de eleição.
- **`free_port`** — pede ao SO uma porta TCP livre (cada nó escuta numa porta
  aleatória; só o NS tem porta fixa).
- **`local_ip`** — descobre o IP da máquina na LAN. Importante para o cenário de
  **dois PCs por cabo**: sem gateway, o truque clássico "conectar em 8.8.8.8"
  falha, então a função tenta várias estratégias e **nunca aceita `127.0.0.1` se
  houver alternativa**. A variável `SDWB_MY_IP` força um IP explícito (recomendado
  no cabo direto).

---

## 6. `board_state.py` — o estado do quadro

Modela o desenho de forma independente de rede. **A mesma classe é usada nas duas
visões**: o estado canônico do coordenador (`coord_board`) e a réplica de cada
cliente (`board`).

Cada objeto é um dicionário:
```python
{ "kind": "line" | "square",
  "points": [[x1,y1], [x2,y2]],
  "color": "#......",
  "selected_by": None | <client_id int> }   # None = livre; senão, travado por aquele cliente
```

- **`PALETTE`** — exatamente **duas cores** (requisito do enunciado).

- **`to_dict` / `from_dict`** — serializam/desserializam o estado inteiro. É como
  o coordenador manda o quadro completo no `STATE_SYNC` e como o novo coordenador
  faz uma **cópia profunda** ao assumir.

- **`_normalize_square(p1, p2)`** — dois cliques viram um **quadrado de verdade**:
  `side = max(|dx|, |dy|)`, preservando a direção do clique. (Sem isso o "quadrado"
  seria um retângulo qualquer.)

- **`validate(action, payload, client_id)` — somente leitura.** É o juiz da
  **exclusão mútua**; não muda nada, só responde `(ok, erro)`:
  - `LINE/SQUARE`: precisa de 2 pontos.
  - `SELECT`: falha se o objeto já está `selected_by` **outro** cliente →
    *"objeto já selecionado por outro cliente"* (a mensagem de erro exigida).
  - `DESELECT`: só quem selecionou pode liberar.
  - `COLOR/REMOVE`: exige que o objeto esteja selecionado **por você** ("selecione
    o objeto antes de colorir/remover") — implementa a regra "selecionar objeto,
    depois operação".

- **`apply(action, payload, client_id)` — muda o estado.** Só é chamado **depois**
  que o coordenador confirmou a ação. Cria linha/quadrado, marca/desmarca seleção,
  troca cor ou remove. Detalhe importante no `SELECT`: ao selecionar um objeto,
  **libera automaticamente** qualquer outro que o mesmo cliente já tivesse
  selecionado → garante "no máximo um objeto selecionado por cliente".

- **`hit_test(x, y)`** — dado um clique, descobre qual objeto está ali (distância
  ponto-segmento para linhas; *bounding box* para quadrados). Percorre do mais
  recente para o mais antigo. Usado pela GUI ao clicar com a ferramenta
  "Selecionar".

> **Por que separar `validate` de `apply`?** `validate` é a checagem barata e sem
> efeitos colaterais (exclusão mútua) feita pelo coordenador *antes* de aceitar.
> `apply` é a mutação determinística que **todas** as réplicas executam igual,
> garantindo que convergem para o mesmo estado.

---

## 7. `name_service.py` — o Serviço de Nomes

Processo **separado e independente**, o único com endereço fixo. Mantém só a
tabela `nome_do_quadro -> {ip, port, fails}`.

- **`start`** — abre o socket TCP, escuta, e para cada conexão dispara uma thread
  `_handle`. Também sobe a thread de *sweep* (varredura).
- **`_dispatch(msg)`** — trata cada tipo:
  - `REGISTER_BOARD`: registra um quadro novo (erro se o nome já existe).
  - `UPDATE_BOARD`: atualiza o endereço (usado pelo **novo coordenador** após uma
    eleição).
  - `REMOVE_BOARD`: remove o quadro (quando ele é encerrado).
  - `LIST_BOARDS`: devolve a lista `[{name, ip, port}, ...]` para o cliente
    escolher onde entrar.
- **`_sweep_loop`** — a cada 15 s, sonda (`NS_PROBE`) cada coordenador registrado;
  depois de 2 falhas seguidas, remove o quadro "órfão" (coordenador caiu sem
  ninguém para detectar). É uma rede de segurança opcional; o enunciado assume que
  o NS nunca falha.

> **Pergunta provável:** *"O NS não armazena o desenho?"* Não. Ele é só a lista
> telefônica: nome → onde encontrar o coordenador. Todo o conteúdo do quadro vive
> no coordenador e nas réplicas.

---

## 8. `node.py` — o coração do sistema

Cada nó tem **dois chapéus**: sempre é **cliente**; às vezes também é
**coordenador**. Os atributos do `__init__` estão divididos justamente assim
(bloco "papel de CLIENTE" e bloco "papel de COORDENADOR").

Ao nascer, o nó já sobe uma thread: **`_listener_loop`** (seu servidor TCP).

### 8.1 Estruturas de dados importantes

Visão de **cliente**:
- `self.board` — réplica local do quadro (o que a GUI desenha).
- `self.members` — réplica da lista de participantes.
- `self.sock` — conexão TCP com o coordenador atual.
- `self.coord_ip/coord_port` — quem é o coordenador agora.

Visão de **coordenador**:
- `self.coord_board` — estado **canônico** (a "verdade").
- `self.coord_members` — tabela **autoritativa** de membros.
- `self.conns[cid]` — socket de cada cliente conectado; `self.conn_locks[cid]`
  serializa o envio por aquele socket; `self.last_ack[cid]` guarda o último ACK
  de heartbeat.

Locks (controle de concorrência):
- `action_lock` — **serializa todas as ações** → dá ordenação total e exclusão
  mútua entre clientes diferentes.
- `members_lock` — protege as tabelas de membros/conexões.
- `sock_lock` — protege a troca do socket de cliente (importante durante
  reconexões/eleição).
- `election_lock` / `_reconnect_lock` — evitam eleições e reconexões duplicadas
  simultâneas.

> **Por que separar `coord_board` de `board` no mesmo nó?** O computador do
> coordenador é "só mais um cliente". Se ele aplicasse a ação tanto no canônico
> quanto na réplica, aplicaria **duas vezes**. Solução: o coordenador só mexe no
> `coord_board`; a réplica `board` dele é atualizada quando o `ACTION_APPLY`
> chega pela conexão de *loopback* — exatamente o mesmo caminho dos outros
> clientes.

### 8.2 API pública (o que a GUI chama)

- **`list_boards()`** — pergunta a lista de quadros ao NS (`LIST_BOARDS`).
- **`create_board(name)`** — vira coordenador: zera as tabelas, registra no NS
  (`REGISTER_BOARD`), sobe o heartbeat e **se conecta ao próprio quadro** como
  cliente (`_reconnect_to_coordinator`).
- **`join_board(name, ip, port)`** — entra como cliente em um quadro existente.
  Tem um cuidado especial (`rejoining_own`): se eu **já sou o coordenador** desse
  quadro (saí para o menu mas continuo hospedando), reentrar **não** pode zerar
  `is_coordinator`, senão derrubaria o atendimento dos outros e o meu próprio
  listener rejeitaria meu `JOIN`.
- **`leave_board()`** — saída **voluntária**: manda `LEAVE` e solta a conexão.
  Não fecha o socket na marra para não arriscar perder o `LEAVE` (ver comentário
  no código sobre RST vs FIN). Se eu for coordenador, **continuo hospedando**.
- **`do_action(action, payload)`** — manda um `ACTION_REQUEST` ao coordenador.
- **`shutdown()`** — encerra o nó (fechar a janela).
- **`simulate_crash()`** — **para demonstração**: simula queda abrupta (fecha
  sockets sem mandar `LEAVE`), para testar o cenário "Morte do Coordenador".

### 8.3 O listener (servidor de cada nó)

- **`_listener_loop`** — escuta em `0.0.0.0:my_port` e despacha cada conexão para
  `_handle_incoming` numa thread.
- **`_handle_incoming`** — lê **uma** mensagem e decide:
  - `NS_PROBE` → responde `OK` (estou vivo).
  - `ELECTION` → responde `ELECTION_OK` (e, se eu já sou coordenador, já informo
    meu endereço); depois **eu também começo uma eleição** (cascata do Bully).
  - `COORDINATOR_WIN` → responde `COORDINATOR_ACK` e adoto o novo coordenador.
  - `JOIN` → se sou coordenador, trato o ingresso; senão, erro.

### 8.4 Papel de COORDENADOR

- **`_coord_handle_join`** — registra o cliente (reaproveita o `client_id` se for
  uma reconexão), responde **`STATE_SYNC`** com o quadro completo + lista de
  membros (é a **sincronização de estado** do onboarding), avisa os demais com
  `CLIENT_JOINED`, e entra no laço de leitura daquele cliente.
- **`_coord_client_reader_loop`** — lê desse cliente: `ACTION_REQUEST` (processado
  **inline**, garantindo ordem FIFO daquele cliente), `HEARTBEAT_ACK`, `LEAVE`.
  Ao sair do laço, se ainda estamos em operação normal (`self.running`), remove o
  cliente. (Se for um *crash* nosso, **não** removemos, para não apagar o quadro
  do NS antes da eleição.)
- **`_coord_handle_action_request`** — **o coração da replicação** (ver §9).
- **`_coord_send` / `_coord_broadcast`** — enviam para um/todos os clientes
  (cada socket protegido pelo seu `conn_lock`).
- **`_coord_remove_client`** — tira o cliente das tabelas, **libera as seleções
  que ele tinha** (`_coord_release_selections_of`, senão o objeto ficaria travado
  para sempre), avisa todos com `CLIENT_LEFT`. Se sobrou **zero** membro, chama
  `_kill_hosted_board`.
- **`_kill_hosted_board`** — regra das anotações: coordenador que fica sozinho e
  sai/cai **encerra o quadro** (remove do NS).
- **`_coord_heartbeat_loop`** — a cada T=4 s manda `HEARTBEAT` a todos; quem não
  responde `HEARTBEAT_ACK` em 2T=8 s é considerado morto e removido (atende ao
  requisito "detecção de falha → atualizar lista de integrantes").

### 8.5 Papel de CLIENTE

- **`_reconnect_to_coordinator`** — abre socket com o coordenador, manda `JOIN`
  (com `client_id` atual, ou `None` se novo), recebe `STATE_SYNC`, troca o socket
  ativo de forma segura e sobe o `_client_reader_loop`.
- **`_client_reader_loop`** — recebe do coordenador:
  - `HEARTBEAT` → responde `HEARTBEAT_ACK`.
  - `ACTION_APPLY` → **aplica na réplica local** e redesenha (`on_board_update`).
  - `CLIENT_JOINED/LEFT` → atualiza a lista.
  - `ERROR` → mostra na barra de status (ex.: seleção negada).
  - **Se `recv_msg` devolve `None` (EOF)** e não foi saída intencional →
    `_on_coordinator_failure()` → começa a **eleição**. É assim que se detecta a
    queda do coordenador.

### 8.6 Detecção de falha + Eleição (Bully) — ver §11 para o passo a passo

Métodos: `_on_coordinator_failure`, `_start_election`, `_run_election`,
`_become_coordinator`, `_adopt_new_coordinator`, `_safe_reconnect`,
`_handle_coordinator_win`. Detalhados na §11.

---

## 9. Como uma ação se propaga (replicação primário-backup)

> **Contexto importante para a banca:** o enunciado original pedia **2PC
> (Two-Phase Commit)**, mas o professor **removeu** esse requisito. Como não há
> mais transações de múltiplos objetos, trocamos por um mecanismo mais simples e
> adequado: **replicação primário-backup com sequenciador central**. O
> coordenador é o **primário**; os clientes são **backups passivos**.

Quando você desenha/colore/remove/seleciona:

```
 Cliente (requisitante)        Coordenador (PRIMÁRIO)              Demais (BACKUPS)
        │                              │                                  │
        │── ACTION_REQUEST ───────────▶│                                  │
        │                       [ action_lock ]                           │
        │                    valida (exclusão mútua)                      │
        │                              │                                  │
        │   (válida) aplica em coord_board                                │
        │◀── ACTION_APPLY ─────────────│── ACTION_APPLY ─────────────────▶│
        │   aplica na réplica          │       aplica na réplica          │
        │                              │                                  │
        │   (conflito) ────────────────┤                                  │
        │◀── ERROR ────────────────────┘  (só ao requisitante; nada propaga)
```

Código (`_coord_handle_action_request`): tudo dentro de `with self.action_lock:`
1. `validate` → se falhar, manda `ERROR` só ao requisitante e para.
2. `apply` no `coord_board` (a "verdade").
3. monta `ACTION_APPLY` e envia ao requisitante **e** faz broadcast aos demais.

**Por que isso é correto?**
- **Ordenação total:** como só o primário muta e propaga, e tudo passa por um
  único `action_lock`, todos os backups aplicam as ações **na mesma ordem** →
  convergem para o mesmo estado.
- **Exclusão mútua:** validada *antes* de aplicar. Dois pedidos conflitantes
  simultâneos são serializados pelo lock; o segundo já enxerga o efeito do
  primeiro e é rejeitado.
- **Sem votação, sem abort:** ação válida sempre é aplicada por todos; inválida,
  por ninguém.

---

## 10. `app.py` — a interface (Tkinter)

Não tem lógica de rede; só desenha e chama o `Node`.

- **Tela de menu** (`_build_menu_screen`): botões **CRIAR NOVO QUADRO** e
  **INGRESSAR EM QUADRO EXISTENTE**. Se eu estiver hospedando um quadro em segundo
  plano, mostra um aviso.
- **Ingressar** (`_on_join_clicked` / `_pick_board_dialog`): pede a lista ao NS e
  mostra um diálogo para escolher o quadro.
- **Tela do quadro** (`_build_board_screen`): registra os **callbacks** do nó para
  a thread da GUI (via `root.after(0, ...)`, pois Tkinter não é *thread-safe*):
  - `on_board_update` → `_redraw`
  - `on_members_update` → `_refresh_members`
  - `on_status` → barra de status
  - `on_board_killed` → volta ao menu (quadro encerrado)
- **Ferramentas** (toolbar): Linha, Quadrado, Selecionar, Colorir (cor 1/2),
  Remover, **Desselecionar**.
- **`_on_canvas_click`**: para Linha/Quadrado junta 2 pontos e manda a ação; para
  Selecionar faz `hit_test` e manda `SELECT`.
- **`_on_color` / `_on_remove` / `_on_deselect`**: agem sobre o objeto atualmente
  selecionado por mim (`_current_selected_object`).
- **`_redraw`**: desenha cada objeto; quem está selecionado ganha um contorno
  tracejado **verde** (eu) ou **laranja** (outro cliente).

---

## 11. Eleição do Valentão (Bully), passo a passo

**Disparo.** Quando o coordenador cai, a conexão TCP de cada cliente devolve EOF
(`recv_msg → None`). `_client_reader_loop` chama `_on_coordinator_failure`, que
remove o coordenador morto da lista e chama `_start_election`.

**Regra do Bully.** Cada nó tem um `client_id` numérico. Quem inicia a eleição
contata **todos os nós com id MAIOR** que o seu (`ELECTION`):
- Se **ninguém maior responde**, eu sou o maior vivo → **viro coordenador**
  (`_become_coordinator`).
- Se **alguém maior responde** (`ELECTION_OK`), eu recuo e **espero** o anúncio do
  vencedor (`COORDINATOR_WIN`) por `ELECTION_TIMEOUT`.

**Quem vence (`_become_coordinator`):**
1. `is_coordinator = True`, vira o próprio endereço o do coordenador.
2. **Recupera o estado** copiando sua própria réplica (`coord_board =
   from_dict(self.board.to_dict())`) — atende "novo coordenador recupera o
   quadro". **Recupera a lista de integrantes** de `self.members`.
3. **Libera seleções presas** de quem não está mais no quadro (ex.: o coordenador
   que caiu) — senão objetos ficariam travados.
4. Sobe o heartbeat, faz `UPDATE_BOARD` no **Serviço de Nomes** com seu endereço.
5. Anuncia `COORDINATOR_WIN` (com ACK) aos demais e se reconecta a si mesmo.

**Quem perde:** ao receber `COORDINATOR_WIN` (ou descobrir o novo endereço via
`ELECTION_OK`/consulta ao NS), chama **`_adopt_new_coordinator`** e **reconecta**
ao novo coordenador (recebe novo `STATE_SYNC`).

**Robustez embutida (bons pontos para citar):**
- `_adopt_new_coordinator` é **idempotente** → evita *split-brain* por reconexão
  dupla.
- Se o anúncio `COORDINATOR_WIN` se perder, há **duas redes de segurança**: o
  `ELECTION_OK` pode já trazer o endereço do coordenador, e, no timeout, o nó
  **consulta o NS** antes de se auto-proclamar.
- Eleição em **cascata**: quem recebe `ELECTION` também inicia a sua, garantindo
  que o maior id sempre prevaleça.

---

## 12. Concorrência: threads e locks (resumo)

Por nó, rodam em paralelo:
- 1 thread **listener** (aceita conexões) + 1 thread por conexão recebida.
- Como **coordenador**: 1 thread de **heartbeat** + 1 **reader loop** por cliente.
- Como **cliente**: 1 **reader loop** lendo do coordenador.
- Eleição e reconexão rodam em threads próprias.

Locks evitam corrida:
- `action_lock` → ordem total das ações.
- `members_lock` → tabelas de membros/conexões.
- `sock_lock` → troca atômica do socket de cliente.
- `election_lock` / `_reconnect_lock` → uma eleição/reconexão por vez.

Detalhe fino citável: **`_safe_close`** dá `shutdown()` antes de `close()` para
acordar imediatamente uma thread bloqueada em `recv()` (em Linux, `close()`
sozinho não faz isso).

---

## 13. Regras de ciclo de vida (das anotações de aula)

| Situação | Comportamento | Código |
|---|---|---|
| Coordenador **sai** (botão Sair), com outros presentes | Continua hospedando em 2º plano; **sem eleição** | `leave_board` mantém `is_coordinator` |
| Máquina do coordenador **cai** | Clientes detectam (EOF) e **elegem** novo | `_on_coordinator_failure` |
| Coordenador **sozinho** sai/cai | Quadro **encerrado** e removido do NS | `_kill_hosted_board` |
| Reentrar no **próprio** quadro hospedado | Não derruba o papel de coordenador | `join_board` (`rejoining_own`) |
| Cliente que selecionou objeto **sai** | Seleção é **liberada** | `_coord_release_selections_of` |
| Múltiplos quadros simultâneos | Suportado (NS indexa por nome) | `name_service.boards` |

---

## 14. Cenários de teste obrigatórios — como demonstrar

1. **Entrada Dinâmica:** suba o NS; abra 1 cliente e **CRIAR**; abra mais 2 e
   **INGRESSAR**. Todos enxergam os mesmos desenhos e a lista de participantes.
2. **Concorrência / Exclusão mútua:** dois clientes clicam em **Selecionar** no
   mesmo objeto quase ao mesmo tempo → só um trava; o outro recebe *"objeto já
   selecionado por outro cliente"*.
3. **Morte do Coordenador:** mate o **processo** do coordenador (Ctrl+C no
   terminal). Os demais detectam, elegem um novo, que atualiza o NS, e o quadro
   continua funcionando.

**Teste automatizado (sem GUI):** `python3 test_headless.py` → **20/20 PASS**.
Ele cria 3 nós no mesmo processo e exercita: criação+ingresso+sync, propagação de
ações, exclusão mútua, eleição Bully, recuperação de estado/membros e a regra do
coordenador sozinho.

---

## 15. FAQ de banca (perguntas prováveis + respostas curtas)

**P: Onde está a descoberta de serviço?** No `name_service.py`. O cliente nunca
tem o IP do coordenador hardcoded: ele pergunta ao NS (`LIST_BOARDS`). Só o NS tem
endereço fixo (e via variável de ambiente, não no código).

**P: Como garante que todos veem o mesmo desenho?** Replicação primário-backup: só
o coordenador muta o estado e propaga `ACTION_APPLY` na mesma ordem (serializada
por `action_lock`); todos aplicam igual.

**P: Como funciona a exclusão mútua?** Para colorir/remover é preciso primeiro
**selecionar**. Se outro cliente já selecionou aquele objeto, o coordenador
responde `ERROR` e nada muda. A serialização por `action_lock` ordena pedidos
concorrentes.

**P: Por que não usou Zookeeper/Etcd?** O enunciado proíbe middleware pronto; a
eleição (Bully) e o "consenso" sobre quem é o coordenador foram implementados na
mão (`_run_election` / `_become_coordinator`).

**P: E se a mensagem de vitória da eleição se perder?** Há redunderância: o
`ELECTION_OK` pode carregar o endereço do novo coordenador, e, no timeout, o nó
consulta o NS. `_adopt_new_coordinator` é idempotente (sem split-brain).

**P: O computador do coordenador também desenha?** Sim — ele é "só mais um
cliente". Por isso há `coord_board` (canônico) e `board` (réplica) separados, e a
ação do próprio coordenador volta pelo *loopback* para não ser aplicada duas vezes.

**P: Como detecta falhas?** Dois caminhos: o coordenador usa **heartbeat** para
detectar clientes mortos; os clientes detectam a queda do coordenador pelo **EOF**
do socket TCP.

**P: Por que TCP e não UDP?** Precisamos de entrega confiável e ordenada do estado
e das ações; TCP já fornece isso. O *framing* de 4 bytes resolve as fronteiras de
mensagem.

---

## 16. Glossário rápido

- **Nó:** um processo cliente (`node.py` + `app.py`).
- **Coordenador / Primário:** o nó que detém o estado oficial e ordena as ações.
- **Backup / Réplica:** a cópia local do quadro em cada cliente.
- **Serviço de Nomes (NS):** diretório `nome → (IP, porta)` do coordenador.
- **Heartbeat:** mensagem periódica para detectar quem morreu.
- **Eleição (Bully):** algoritmo que escolhe o nó de **maior id** como novo
  coordenador.
- **Exclusão mútua:** garantia de que dois clientes não operam o mesmo objeto ao
  mesmo tempo (via seleção + `action_lock`).
- **Framing:** prefixo de tamanho que delimita cada mensagem sobre o fluxo TCP.
```
