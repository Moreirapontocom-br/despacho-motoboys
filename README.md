# Despacho de Motoboys

Sistema de despacho automático de entregas para restaurantes pequenos.
O restaurante cadastra os pedidos num painel, o sistema agrupa os que ficam perto
e escolhe o motoboy que vai conseguir entregar mais cedo. Cada motoboy vê a própria
rota no celular, com mapa, navegação pelo Google Maps e botão de "Entregue".

![Testes](https://github.com/Moreirapontocom-br/despacho-motoboys/actions/workflows/testes.yml/badge.svg)

## O que ele faz

**Vários restaurantes no mesmo sistema** (até 6; dá para aumentar com `MAX_RESTAURANTES`)
- Cada restaurante tem os **próprios pedidos, motoboys, regras, equipe e endereço**. Um
  nunca vê os dados do outro
- O **endereço do restaurante** (de onde os motoboys saem e para onde voltam) é informado
  pelo dono ao criar a conta, conferindo o pino no mapa. Depois, **só o dono** (ou o
  administrador) muda, em 👤 Conta → "Meu restaurante"
- O **administrador** (quem tem a `API_KEY`) vê todos: escolhe o restaurante no topo do
  painel e, em 👤 Conta → "Restaurantes", vê o movimento de hoje de cada um (fila, em rota,
  entregues, motoboys de turno e dono)
- O nome do motoboy é o link dele (`/m/NOME`), então não pode repetir entre restaurantes

**Login** (`/painel`)
- Cada pessoa entra com **e-mail e senha** e fica conectada por até 30 dias (até tocar em "Sair")
- **Dono**: tudo. **Funcionário**: pedidos, despacho, turno dos motoboys e relatórios (não
  mexe em regras, cadastro de motoboys nem equipe). O dono cuida da equipe na aba 👤 Conta
- **Administrador do sistema**: quem tem a `API_KEY` entra pelo link "Entrar com a chave de
  administrador", com acesso total, e pode definir senha nova para qualquer pessoa (é assim
  que se recupera o acesso se o dono esquecer a senha). **A chave fica só com o administrador**
- **Ativar um restaurante**: o administrador entra, vai em 👤 Conta → "Convidar dono de um
  restaurante novo" e manda o link (tem botão de WhatsApp). O dono abre, cria e-mail e senha
  e informa o nome e o endereço do restaurante. Só o administrador convida ou cadastra donos
- **Convidar funcionários**: o dono gera um link em 👤 Conta → "Convidar funcionário"
- Todo link de convite **vale uma vez só e vence em 7 dias**; no banco fica só o hash dele.
  Enquanto não existe nenhuma conta, o painel avisa que o sistema ainda não foi ativado

**Painel do restaurante** (`/painel`)
- **Pedido rápido**: cole o endereço numa linha só (do WhatsApp, por exemplo) e aperte
  Enter. O painel separa rua, número, bairro, CEP e complemento sozinho, já mostra o
  ponto no mapa e outro Enter confirma. Os campos separados continuam em "Preencher
  campo por campo"
- Número do pedido no dia (#1, #2...), fácil de falar no balcão e no telefone
- **Painel em 3 abas**: 🏠 Operação (pedidos, despacho e mapa: o dia a dia do
  funcionário), 🛵 Motoboys (cadastro, turno e acesso) e 📊 Relatórios (estatísticas e histórico)
- **O botão "🧠 Despachar" nunca despacha às cegas**: sempre mostra antes a sugestão do
  sistema (quem leva o quê, km, minutos, economia e por que cada motoboy foi escolhido).
  Quando não compensa juntar pedidos, avisa "Nenhum agrupamento vantajoso encontrado"
- **Dois modos**: **Assistido** (o sistema sugere, o funcionário confirma) e **Automático**
  (o sistema despacha sozinho a cada 20 s, com a mesma lógica)
- **Economia em destaque**: faixa verde com os km economizados hoje, a % de deslocamento a
  menos e o valor em reais (informando quanto custa cada km nas regras)
- **Regras em linguagem simples** ("Como quero que o despacho funcione"), cada uma com uma
  frase 💡 que explica o efeito do número escolhido: agrupar até N pedidos, juntar pedidos a até
  X km um do outro, esperar outro pedido por até N min, nunca deixar um pedido esperar mais de
  N min, limite de distância do automático e custo do km
- **Aguardando agrupamento**: pedido sozinho na região aparece em amarelo esperando;
  pedidos que podem ir juntos mostram "Pode ir junto com #12"
- Edição e cancelamento de pedidos
- **Mapa da operação**, a maior coluna do painel: pedidos aguardando (laranja),
  atrasados (vermelho piscando), pedidos em rota na cor de cada motoboy e motoboys com
  GPS. Embaixo, um cartão por motoboy (disponível, saindo, em rota ou voltando, com
  pedidos, km e quando fica livre); clicar nele mostra a rota e as paradas no mapa
- **Pedidos por situação**: 🟢 no prazo, 🟡 atenção (passou de 70% do limite) e
  🔴 atrasado, com faixa vermelha e contador no título da aba
- **Estatísticas** (hoje, 7 ou 30 dias): entregas, viagens, pedidos agrupados, km rodados
  e economizados, tempo médio, % no prazo, comparação com o período anterior e uma
  tabela por motoboy
- Botão de WhatsApp para avisar o cliente que o pedido saiu
- Resumo do dia (pendentes, em rota, entregues, tempo médio) e histórico em CSV, com os
  pedidos entregues e cancelados, o status e os horários de criação, despacho, saída do
  restaurante, entrega e cancelamento
- A sugestão de despacho diz quais pedidos vão juntos e a distância entre eles
  (ex.: "Leva #102 e #105 juntos (~1,3 km um do outro)")
- Cadastro de motoboys só com nome, WhatsApp e tipo (próprio ou terceirizado): o código
  é gerado sozinho e o painel mostra o link, um QR Code e um botão para mandar o link
  no WhatsApp dele. Controle de turno
- Funciona em computador (até 3 colunas lado a lado), tablet e celular

**Página do motoboy** (`/m/NOME`)
- Link curto: o restaurante manda `/m/NOME#codigo=CODIGO` (ou mostra o QR Code) uma vez.
  Depois disso o link `/m/NOME` sozinho já entra, e dá para **adicionar à tela inicial**
  do celular como um aplicativo. Links antigos (`/motoboy?id=NOME`) continuam valendo
- **Rota planejada**: Restaurante (retirar #1, #2, #3) → cada entrega com nome do cliente,
  endereço, tempo estimado e "Navegar até aqui", mais o mapa. Botão grande **INICIAR ROTA**
- **Uma entrega por vez**: "Entrega 1 de 3", endereço grande, complemento em destaque,
  cliente e número do pedido, **ABRIR NO MAPS**, ligar para o cliente e **ENTREGUEI**
  (toque duplo, para não marcar sem querer). Depois passa sozinho para a próxima
- **Disponível / fora de turno**: ao terminar, pergunta "Você está disponível para outra
  rota?". O próprio motoboy liga ou encerra o turno, e o painel vê na hora
- **Entrega nova**: apita, vibra e mostra um aviso. Pedido cancelado também avisa
- **Localização**: ligada sozinha enquanto ele está em rota (envio a cada 30 s). O painel
  mostra "📍 há 20 s" ao lado do motoboy, e o mapa dele mostra "Você"
- Rodapé com as entregas que ele fez hoje

## Como o despacho escolhe o motoboy

Todo motoboy precisa passar no restaurante para pegar a comida. Por isso o sistema
não escolhe quem está mais perto do cliente, e sim **quem consegue terminar a entrega
mais cedo**: o tempo até ele voltar ao restaurante mais o tempo da nova viagem.

- Pedidos próximos (até 2 km entre si) são agrupados na mesma viagem.
- Se o motoboy ainda não saiu do restaurante, os pedidos novos entram na mesma
  viagem e a ordem das paradas é recalculada.
- Se ele já saiu, os novos ficam para depois que ele voltar.
- A ordem das paradas usa o tempo real pelas ruas (LocationIQ ou OSRM). Se o
  serviço estiver fora do ar, usa a distância em linha reta.

## Configuração (variáveis de ambiente)

Configure no painel do Render, em **Environment**. **Nunca coloque chaves no código.**

### Obrigatórias

| Variável | O que é |
|---|---|
| `API_KEY` | Senha do painel do restaurante. Use 32+ caracteres aleatórios. Gere com: `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `DATABASE_URL` | Endereço do banco Postgres (o Render cria ao adicionar um banco). Sem ela, o sistema usa um arquivo SQLite local, só para testes. |

### Restaurantes

| Variável | Padrão | O que é |
|---|---|---|
| `MAX_RESTAURANTES` | `6` | Quantos restaurantes o sistema aceita |
| `TIMEZONE_OFFSET_HORAS` | `-3` | Fuso horário em relação ao UTC (vale para todos os restaurantes) |

O nome, o endereço e a cidade de cada restaurante ficam no banco (o dono informa no
painel). As variáveis abaixo só servem para **instalações antigas, de um restaurante só**:
na atualização, os pedidos, motoboys, regras e contas que já existiam passam a ser do
"restaurante 1", criado com estes dados. Depois disso, o dono ajusta no painel.

| Variável | Padrão | O que é |
|---|---|---|
| `RESTAURANTE_LAT` / `RESTAURANTE_LNG` | Itabira (MG) | Localização do restaurante 1 |
| `RESTAURANTE_ENDERECO` | — | Endereço em texto do restaurante 1 |
| `RESTAURANTE_NOME` | — | Nome do restaurante 1 |
| `RESTAURANTE_CIDADE` | `Itabira, MG` | Cidade do restaurante 1 |

### Mapas e rotas

| Variável | O que é |
|---|---|
| `LOCATIONIQ_KEY` | **Recomendada para uso comercial.** Sem ela, o sistema usa os serviços públicos gratuitos (Nominatim, OSRM e OpenStreetMap), que são só para testes. |
| `LOCATIONIQ_MAPA_KEY` | Segunda chave, só para as imagens do mapa (ela fica visível na página). Trave-a no painel da LocationIQ para funcionar só no endereço do seu site. |
| `OSRM_URL` | Servidor de rotas alternativo. Use `desligado` para usar só linha reta. |

### Ajustes do despacho (opcionais)

| Variável | Padrão | O que é |
|---|---|---|
| `PEDIDO_ATRASO_MIN` | `15` | Minutos na fila até o pedido aparecer como atrasado |
| `ROTA_ATRASO_MIN` | `40` | Minutos em rota até o pedido aparecer como atrasado |
| `PARADA_MIN` | `3` | Minutos gastos em cada entrega |
| `SAIDA_MIN` | `5` | Sem GPS, minutos após o despacho para considerar que o motoboy saiu |
| `MAX_PARADAS_VIAGEM` | `5` | Máximo de entregas numa mesma saída |
| `GPS_VALIDADE_MIN` | `10` | Por quantos minutos a posição do GPS vale |
| `PRAZO_ENTREGA_MIN` | `45` | Prazo total (pedido criado → entregue) usado no "% no prazo" das estatísticas |
| `FATOR_RUAS` | `1.3` | Os km das estatísticas são a linha reta vezes este número |
| `AUTOMATICO_INTERVALO_S` | `20` | No modo automático, de quantos em quantos segundos a fila é conferida |

O modo de despacho e as regras (máximo de entregas por viagem etc.) ficam no painel,
no botão **Regras**, e são guardados no banco. `MAX_PARADAS_VIAGEM` só vale como valor
inicial, até alguém salvar as regras.

## Estrutura do projeto

| Arquivo | O que tem |
|---|---|
| `api_despacho3.py` | Ponto de entrada (o Render inicia por aqui). Só junta as partes. |
| `config.py` | Todas as variáveis de ambiente, num lugar só |
| `horarios.py` | Horários e fuso. Regra: o banco guarda tudo em UTC |
| `banco.py` | Conexão, criação das tabelas e atualização de bancos antigos |
| `restaurantes.py` | Os restaurantes do sistema, endereço de cada um e o limite |
| `ajustes.py` | Modo de despacho e regras escolhidos no painel (um conjunto por restaurante) |
| `seguranca.py` | Chave do painel, códigos dos motoboys, limite de tentativas |
| `contas.py` | Login com e-mail e senha, sessões, dono x funcionário |
| `api_contas.py` | Endereços de login e da equipe |
| `mapas.py` | Busca de endereço, distâncias e rotas pelas ruas |
| `despacho.py` | O algoritmo: agrupar pedidos, escolher motoboy, ordenar paradas |
| `api_painel.py` | Endereços usados pelo painel do restaurante |
| `api_motoboy.py` | Endereços usados pela página do motoboy |
| `paginas.py` | Carrega as páginas HTML com as configurações |
| `painel.html` | Página do restaurante |
| `motoboy.html` | Página do motoboy |
| `test_despacho.py` | Testes automáticos |

## Publicar no Render

1. Crie um **Web Service** apontando para este repositório.
2. **Build command:** `pip install -r requirements.txt`
3. **Start command:** `uvicorn api_despacho3:app --host 0.0.0.0 --port $PORT`
4. Crie um banco **Postgres** no Render e copie o endereço para `DATABASE_URL`.
5. Configure `API_KEY` (e `LOCATIONIQ_KEY` para uso comercial).
6. Em **Settings > Auto-Deploy**, escolha **After CI Checks Pass**: o site só é
   atualizado quando os testes passam.

> O sistema roda em **um único worker**. As travas que impedem dois despachos ao
> mesmo tempo ficam na memória do servidor; com várias cópias rodando juntas, elas
> deixariam de funcionar.

## Rodar no computador

```bash
pip install -r requirements.txt
export API_KEY="uma-chave-qualquer-com-mais-de-24-caracteres"   # no Windows: set API_KEY=...
uvicorn api_despacho3:app --reload
```

Abra `http://127.0.0.1:8000/painel` e entre com a `API_KEY`.

## Testes

```bash
pip install -r requirements.txt httpx pytest
pytest -v
```

Os testes usam um banco SQLite temporário e desligam os serviços externos: os dados
reais nunca são tocados. Eles também rodam sozinhos no GitHub a cada envio de código
(aba **Actions**).

## Segurança

- O painel exige login (sessão de e-mail e senha, no cabeçalho `Authorization: Bearer`) ou a
  `API_KEY` no cabeçalho `X-API-Key` (administrador e integrações que mandam pedidos).
- Quem entra com e-mail e senha só lê e grava dados do próprio restaurante. Com a `API_KEY`,
  o restaurante vai no cabeçalho `X-Restaurante` (o número dele, visto em 👤 Conta →
  "Restaurantes"); se só existir um restaurante, o cabeçalho pode ficar de fora.
- Senhas guardadas só como hash (PBKDF2). Das sessões, o banco guarda só o hash do token.
  Trocar ou redefinir uma senha desconecta a pessoa de todos os aparelhos.
- Senha errada várias vezes bloqueia por 15 minutos (por IP e por e-mail).
- Cada motoboy tem um código próprio e só vê a própria rota. No banco fica só o
  hash do código (PBKDF2), nunca o código em si.
- Tentativas erradas bloqueiam por 15 minutos (8 por IP; 30 por nome de motoboy).
  Um motoboy que já entrou continua entrando mesmo que alguém tente trancar o nome dele.
- No link do motoboy, o código vai depois do `#`: essa parte não é enviada ao
  servidor nem aparece nos logs.
- A biblioteca do mapa é carregada com verificação de integridade (SRI).
